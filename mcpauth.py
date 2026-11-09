"""Per-caller bearer auth for MCP servers. mirror of libs/mcpauth (keep in sync).

Tokens live in <MCP_CALLERS_DIR>/<group>/<caller>. load() fails closed; Require
is a pure ASGI wrapper (BaseHTTPMiddleware breaks SSE). Mount /health outside it.
Tokens load once at start: rotate by rolling a new revision / restarting.
Under the Python mcp SDK, caller() reflects the SSE opener, not each message.
"""

import contextvars
import hashlib
import hmac
import logging
import os

MIN_TOKEN_LEN = 16

logger = logging.getLogger("mcpauth")
_caller: contextvars.ContextVar[str] = contextvars.ContextVar("mcpauth_caller", default="")


def caller() -> str:
    return _caller.get()


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


def _load_group(gdir: str) -> _StaticVerifier:
    tokens = {}
    for name in sorted(os.listdir(gdir)):
        p = os.path.join(gdir, name)
        if name.startswith(".") or not os.path.isfile(p):
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
    if not tokens:
        raise ValueError("no non-empty token files")
    return _StaticVerifier(tokens)


class Auth:
    def __init__(self, groups: dict[str, _StaticVerifier]):
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
    def __init__(self, verifier: _StaticVerifier | None, app):
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
            name = self.verifier.verify(auth[7:])
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
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
        await send({"type": "http.response.body", "body": b"unauthorized"})
