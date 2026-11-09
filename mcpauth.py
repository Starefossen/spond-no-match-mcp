"""Per-caller bearer auth for MCP servers. mirror of github.com/starefossen/mcpauth (keep in sync).

Tokens live in <MCP_CALLERS_DIR>/<group>/<caller>. load() fails closed; Require
is a pure ASGI wrapper (BaseHTTPMiddleware breaks SSE). Mount /health outside it.
Tokens load once at start: rotate by rolling a new revision / restarting.
Under the Python mcp SDK, caller() reflects the SSE opener, not each message.

A group dir may also hold jwks.json: JWT (Kanidm ES256 at+jwt) verification for
that group, mirroring mcpauth v0.3.0 jwt.go. Static tokens are tried first.
"""

import asyncio
import base64
import contextvars
import hashlib
import hmac
import json
import logging
import math
import os
import re
import threading
import time

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

MIN_TOKEN_LEN = 16
JWKS_FILE = "jwks.json"
CLOCK_SKEW = 60
REFRESH_INTERVAL = 3600
KID_REFETCH_GAP = 60
MAX_JWT_LEN = 8 << 10
MAX_JWKS_BYTES = 64 << 10
RETRY_DELAYS = [1, 5, 30]
_B64URL = re.compile(r"[A-Za-z0-9_-]*")
_CONFIG_KEYS = {"issuer", "audience", "jwks_url", "required_scope", "callers"}

http_client = httpx.Client(timeout=10, follow_redirects=False)

logger = logging.getLogger("mcpauth")
_caller: contextvars.ContextVar[str] = contextvars.ContextVar("mcpauth_caller", default="")


def caller() -> str:
    return _caller.get()


def _finite_number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


class _StaticVerifier:
    def __init__(self, tokens: dict[str, bytes]):
        self._entries = [(name, hashlib.sha256(tok).digest()) for name, tok in tokens.items()]

    def verify(self, bearer: bytes) -> str | None:
        h = hashlib.sha256(bearer).digest()
        found = None
        for name, th in self._entries:
            if hmac.compare_digest(h, th) and found is None:
                found = name
        return found


def _b64d(seg: str) -> bytes:
    if not _B64URL.fullmatch(seg):
        raise ValueError("not raw base64url")
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


def _decode_json(seg: str):
    try:
        return json.loads(_b64d(seg))
    except ValueError:
        return None


def fetch_jwks(url: str) -> dict[str, ec.EllipticCurvePublicKey]:
    with http_client.stream("GET", url) as resp:
        if resp.status_code != 200:
            raise ValueError(f"status {resp.status_code}")
        body = b""
        for chunk in resp.iter_bytes():
            body += chunk
            if len(body) > MAX_JWKS_BYTES:
                raise ValueError("jwks too large")
    keys = {}
    for k in json.loads(body).get("keys") or []:
        if not isinstance(k, dict):
            continue
        g = lambda f: k.get(f) if isinstance(k.get(f), str) else ""  # noqa: E731
        if (
            g("kty") != "EC"
            or g("crv") != "P-256"
            or not g("kid")
            or g("use") not in ("", "sig")
            or g("alg") not in ("", "ES256")
        ):
            continue
        try:
            x, y = _b64d(g("x")), _b64d(g("y"))
            if len(x) != 32 or len(y) != 32:
                continue
            keys[g("kid")] = ec.EllipticCurvePublicNumbers(
                int.from_bytes(x, "big"), int.from_bytes(y, "big"), ec.SECP256R1()
            ).public_key()
        except ValueError:
            continue
    if not keys:
        raise ValueError("no usable ES256 keys")
    return keys


class _JWTVerifier:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._lock = threading.Lock()
        self.keys: dict[str, ec.EllipticCurvePublicKey] = {}
        self.last_fetch = 0.0

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="mcpauth-jwks").start()

    def _run(self):
        i = 0
        while not self.refresh():
            time.sleep(RETRY_DELAYS[min(i, len(RETRY_DELAYS) - 1)])
            i += 1
        while True:
            time.sleep(REFRESH_INTERVAL)
            self.refresh()

    def refresh(self) -> bool:
        with self._lock:
            self.last_fetch = time.monotonic()
        return self._fetch()

    def _fetch(self) -> bool:
        try:
            keys = fetch_jwks(self.cfg["jwks_url"])
        except Exception as e:
            logger.warning("mcpauth: jwks fetch %s: %s", self.cfg["jwks_url"], e)
            return False
        with self._lock:
            self.keys = keys
        return True

    async def key(self, kid: str):
        with self._lock:
            k = self.keys.get(kid)
            if k is not None or time.monotonic() - self.last_fetch < KID_REFETCH_GAP:
                return k
            self.last_fetch = time.monotonic()
        await asyncio.to_thread(self._fetch)
        with self._lock:
            return self.keys.get(kid)

    async def verify(self, bearer: bytes) -> str | None:
        if len(bearer) > MAX_JWT_LEN:
            return None
        try:
            parts = bearer.decode("ascii").split(".")
        except UnicodeDecodeError:
            return None
        if len(parts) != 3:
            return None
        hdr = _decode_json(parts[0])
        if (
            not isinstance(hdr, dict)
            or hdr.get("alg") != "ES256"
            or not isinstance(hdr.get("kid"), str)
            or not hdr["kid"]
            or not isinstance(hdr.get("typ"), str)
            or hdr["typ"].lower() != "at+jwt"
            or "crit" in hdr
        ):
            return None
        try:
            sig = _b64d(parts[2])
        except ValueError:
            return None
        if len(sig) != 64:
            return None
        pub = await self.key(hdr["kid"])
        if pub is None:
            return None
        der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
        try:
            pub.verify(der, (parts[0] + "." + parts[1]).encode(), ec.ECDSA(hashes.SHA256()))
        except InvalidSignature:
            return None
        c = _decode_json(parts[1])
        if not isinstance(c, dict) or c.get("iss") != self.cfg["issuer"]:
            return None
        aud = c.get("aud")
        aud_ok = aud == self.cfg["audience"] or (
            isinstance(aud, list) and all(isinstance(a, str) for a in aud) and self.cfg["audience"] in aud
        )
        if not aud_ok:
            return None
        exp, nbf = c.get("exp"), c.get("nbf")
        if not _finite_number(exp) or (nbf is not None and not _finite_number(nbf)):
            return None
        t = time.time()
        if t > exp + CLOCK_SKEW or (nbf is not None and t < nbf - CLOCK_SKEW):
            return None
        scope = c.get("scope")
        scopes = scope.split() if isinstance(scope, str) else []
        if any(w not in scopes for w in self.cfg["required_scope"]):
            return None
        sub = c.get("sub")
        return self.cfg["callers"].get(sub) if isinstance(sub, str) else None


def _load_jwt_config(p: str) -> _JWTVerifier:
    with open(p, "rb") as f:
        try:
            c = json.load(f)
        except ValueError as e:
            raise ValueError(f"{JWKS_FILE}: {e}") from None
    if not isinstance(c, dict) or set(c) - _CONFIG_KEYS:
        raise ValueError(f"{JWKS_FILE}: unknown fields")
    if (
        not isinstance(c.get("issuer"), str)
        or not c["issuer"]
        or not isinstance(c.get("audience"), str)
        or not c["audience"]
        or not isinstance(c.get("jwks_url"), str)
        or not c["jwks_url"].startswith("https://")
    ):
        raise ValueError(f"{JWKS_FILE}: issuer, audience and https jwks_url are required")
    c.setdefault("required_scope", [])
    c.setdefault("callers", {})
    if (
        not isinstance(c["required_scope"], list)
        or not all(isinstance(s, str) for s in c["required_scope"])
        or not isinstance(c["callers"], dict)
        or not all(isinstance(v, str) for v in c["callers"].values())
    ):
        raise ValueError(f"{JWKS_FILE}: bad required_scope or callers")
    return _JWTVerifier(c)


class _Group:
    def __init__(self, static: _StaticVerifier | None, jwt: _JWTVerifier | None):
        self.static = static
        self.jwt = jwt

    async def verify(self, bearer: bytes) -> str | None:
        if self.static and (name := self.static.verify(bearer)):
            return name
        if self.jwt:
            return await self.jwt.verify(bearer)
        return None


def _load_group(gdir: str) -> _Group:
    tokens = {}
    for name in sorted(os.listdir(gdir)):
        p = os.path.join(gdir, name)
        if name.startswith(".") or name == JWKS_FILE or not os.path.isfile(p):
            continue
        with open(p, "rb") as f:
            tok = f.read().strip()
        if not tok:
            continue
        if len(tok) < MIN_TOKEN_LEN:
            raise ValueError(f"caller {name}: token shorter than {MIN_TOKEN_LEN} bytes")
        for other, t in tokens.items():
            if hmac.compare_digest(hashlib.sha256(t).digest(), hashlib.sha256(tok).digest()):
                raise ValueError(f"callers {other} and {name} share a token")
        tokens[name] = tok
    static = _StaticVerifier(tokens) if tokens else None
    jp = os.path.join(gdir, JWKS_FILE)
    if not os.path.exists(jp):
        if not tokens:
            raise ValueError("no non-empty token files")
        return _Group(static, None)
    j = _load_jwt_config(jp)
    for sub, name in j.cfg["callers"].items():
        if not sub.strip():
            raise ValueError(f"{JWKS_FILE}: caller {name!r} has an empty sub")
        if not name or name in tokens:
            raise ValueError(f"{JWKS_FILE}: caller name {name!r} empty or used by a static token")
    if not j.cfg["callers"]:
        logger.warning("mcpauth: %s has no callers; no JWT will be accepted", jp)
    j.start()
    return _Group(static, j)


class Auth:
    def __init__(self, groups: dict[str, _Group]):
        self._groups = groups

    def has(self, group: str) -> bool:
        return group in self._groups

    def require(self, group: str, app):
        return _Require(self._groups.get(group), app)


def load(directory: str | None) -> Auth:
    if not directory:
        raise ValueError("mcpauth: callers dir not set")
    groups = {}
    for name in sorted(os.listdir(directory)):
        gdir = os.path.join(directory, name)
        if name.startswith(".") or not os.path.isdir(gdir):
            continue
        try:
            groups[name] = _load_group(gdir)
        except ValueError as e:
            raise ValueError(f"mcpauth: group {name}: {e}") from None
    if not groups:
        raise ValueError(f"mcpauth: no groups in {directory}")
    return Auth(groups)


class _Require:
    def __init__(self, verifier: _Group | None, app):
        self.verifier = verifier
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        auth = b""
        for k, v in scope.get("headers", []):
            if k.lower() == b"authorization":
                auth = v
                break
        if self.verifier and auth.startswith(b"Bearer "):
            name = await self.verifier.verify(auth[7:])
            if name:
                logger.info("caller=%s path=%s", name, path)
                token = _caller.set(name)
                try:
                    return await self.app(scope, receive, send)
                finally:
                    _caller.reset(token)
        client = scope.get("client")
        logger.warning("auth=denied path=%s remote=%s", path, client[0] if client else "unknown")
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1008})
        await send(
            {"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"text/plain; charset=utf-8")]}
        )
        await send({"type": "http.response.body", "body": b"unauthorized"})
