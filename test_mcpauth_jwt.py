import base64
import hashlib
import hmac
import json
import logging
import time

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

import mcpauth
from test_mcpauth import LEGACY, call, write

ISS = "https://id.example.org/oauth2/openid/mcp-read"
AUD = "mcp-read"
URL = "https://jwks.example.org/keys"
HDR = {"alg": "ES256", "kid": "k1", "typ": "at+jwt"}
BASE = {
    "issuer": ISS,
    "audience": AUD,
    "jwks_url": URL,
    "required_scope": ["groups"],
    "callers": {"0b0e5c4e-uuid": "varden"},
}


def b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64json(v):
    return b64(json.dumps(v).encode())


class JWKS:
    def __init__(self):
        self.keys, self.down, self.calls, self.redirect = {}, False, 0, False

    def add(self, kid):
        self.keys[kid] = ec.generate_private_key(ec.SECP256R1())
        return self.keys[kid]

    def handler(self, req):
        self.calls += 1
        if self.redirect:
            return httpx.Response(302, headers={"location": URL})
        if self.down:
            return httpx.Response(503)
        ks = []
        for kid, k in self.keys.items():
            n = k.public_key().public_numbers()
            ks.append(
                {
                    "kty": "EC",
                    "crv": "P-256",
                    "kid": kid,
                    "use": "sig",
                    "alg": "ES256",
                    "x": b64(n.x.to_bytes(32, "big")),
                    "y": b64(n.y.to_bytes(32, "big")),
                }
            )
        return httpx.Response(200, json={"keys": ks})


def sign(k, hdr, cl):
    inp = b64json(hdr) + "." + b64json(cl)
    r, s = decode_dss_signature(k.sign(inp.encode(), ec.ECDSA(hashes.SHA256())))
    return inp + "." + b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


def claims(**kw):
    n = int(time.time())
    c = {
        "iss": ISS,
        "sub": "0b0e5c4e-uuid",
        "aud": AUD,
        "iat": n,
        "nbf": n,
        "exp": n + 900,
        "jti": "j",
        "client_id": AUD,
        "scope": "openid groups",
    }
    c.update(kw)
    return {k: v for k, v in c.items() if v is not None}


@pytest.fixture
def jwks(monkeypatch):
    s = JWKS()
    monkeypatch.setattr(mcpauth, "http_client", httpx.Client(transport=httpx.MockTransport(s.handler)))
    monkeypatch.setattr(mcpauth, "RETRY_DELAYS", [0.01])
    return s


def wait_loaded(a):
    for _ in range(200):
        if a._groups["read"].jwt.keys:
            return
        time.sleep(0.01)
    raise AssertionError("jwks never loaded")


def setup(tmp_path, jwks, cfg=BASE, static=False):
    k = jwks.add("k1")
    if static:
        write(tmp_path / "read/legacy", LEGACY)
    write(tmp_path / "read" / mcpauth.JWKS_FILE, json.dumps(cfg))
    a = mcpauth.load(str(tmp_path))
    wait_loaded(a)
    return a, k


def test_accepts(tmp_path, jwks, caplog):
    a, k = setup(tmp_path, jwks, static=True)
    caplog.set_level(logging.INFO, logger="mcpauth")
    tok = sign(k, HDR, claims())
    assert call(a, "read", "Bearer " + tok) == (200, b"ok", "varden")
    assert "caller=varden path='/sse' remote=" in caplog.text and tok not in caplog.text
    assert call(a, "read", "Bearer " + LEGACY)[2] == "legacy"
    cl = claims(sub="unmapped-uuid", aud=["other", AUD], scope="groups openid extra")
    assert call(a, "read", "Bearer " + sign(k, {**HDR, "typ": "AT+JWT"}, cl))[0] == 401
    assert call(a, "read", "Bearer " + sign(k, {**HDR, "typ": "AT+JWT"}, claims(aud=["other", AUD])))[0] == 200


def test_log_path_cannot_forge_lines(tmp_path, jwks, caplog):
    a, k = setup(tmp_path, jwks, static=True)
    caplog.set_level(logging.INFO, logger="mcpauth")
    authz = ("Bearer " + sign(k, HDR, claims())).encode()
    scope = {"type": "http", "path": "/x\ncaller=evil-jwt", "headers": [(b"authorization", authz)]}

    async def app(scope, receive, send):
        pass

    import asyncio

    asyncio.run(a.require("read", app)(scope, None, None))
    assert "\n" not in caplog.records[-1].getMessage() and "path='/x\\ncaller=evil-jwt'" in caplog.text


def reject_cases(k):
    n = int(time.time())
    other = ec.generate_private_key(ec.SECP256R1())
    good = sign(k, HDR, claims())
    hs = b64json({"alg": "HS256", "kid": "k1"}) + "." + b64json(claims())
    noexp = claims()
    del noexp["exp"]
    return {
        "expired": sign(k, HDR, claims(exp=n - 61)),
        "no exp": sign(k, HDR, noexp),
        "future nbf": sign(k, HDR, claims(nbf=n + 120)),
        "wrong aud": sign(k, HDR, claims(aud="mcp-write")),
        "wrong iss": sign(k, HDR, claims(iss="https://evil")),
        "missing scope": sign(k, HDR, claims(scope="openid")),
        "no scope": sign(k, HDR, {c: v for c, v in claims().items() if c != "scope"}),
        "id token typ": sign(k, {**HDR, "typ": "JWT"}, claims(groups=["mcp-read"])),
        "no typ": sign(k, {"alg": "ES256", "kid": "k1"}, claims()),
        "wrong key": sign(other, HDR, claims()),
        "unknown kid": sign(k, {**HDR, "kid": "nope"}, claims()),
        "no kid": sign(k, {"alg": "ES256", "typ": "at+jwt"}, claims()),
        "alg none": b64json({"alg": "none", "kid": "k1"}) + "." + b64json(claims()) + ".",
        "alg HS256": hs + "." + b64(hmac.new(b"x", hs.encode(), hashlib.sha256).digest()),
        "crit": sign(k, {**HDR, "crit": ["x"]}, claims()),
        "tampered claim": good[:-90] + ("B" if good[-90] == "A" else "A") + good[-89:],
        "two parts": good.rsplit(".", 1)[0],
        "padded sig": good + "==",
        "string exp": sign(k, HDR, claims(exp=str(n + 900))),
        "oversize": sign(k, HDR, claims(pad="x" * 9000)),
    }


def test_rejects(tmp_path, jwks):
    a, k = setup(tmp_path, jwks)
    for name, tok in reject_cases(k).items():
        assert call(a, "read", "Bearer " + tok)[0] == 401, name
    assert call(a, "read", "Bearer " + sign(k, HDR, claims(exp=int(time.time()) - 30)))[0] == 200


def test_unknown_kid_refetch_rate_limited(tmp_path, jwks):
    a, _ = setup(tmp_path, jwks)
    a._groups["read"].jwt.last_fetch = 0.0
    k2 = jwks.add("k2")
    before = jwks.calls
    assert call(a, "read", "Bearer " + sign(k2, {**HDR, "kid": "k2"}, claims()))[0] == 200
    for _ in range(20):
        call(a, "read", "Bearer " + sign(k2, {**HDR, "kid": "random"}, claims()))
    assert jwks.calls - before == 1


def test_no_redirects(jwks):
    jwks.add("k1")
    jwks.redirect = True
    with pytest.raises(ValueError, match="302"):
        mcpauth.fetch_jwks(URL)


def test_outage_keeps_keys_and_static(tmp_path, jwks):
    a, k = setup(tmp_path, jwks, static=True)
    jwks.down = True
    assert not a._groups["read"].jwt.refresh()
    assert call(a, "read", "Bearer " + sign(k, HDR, claims()))[0] == 200
    assert call(a, "read", "Bearer " + LEGACY)[0] == 200


def test_down_at_start(tmp_path, jwks):
    k = jwks.add("k1")
    jwks.down = True
    write(tmp_path / "read/legacy", LEGACY)
    write(tmp_path / "read" / mcpauth.JWKS_FILE, json.dumps(BASE))
    a = mcpauth.load(str(tmp_path))
    assert call(a, "read", "Bearer " + LEGACY)[0] == 200
    tok = sign(k, HDR, claims())
    assert call(a, "read", "Bearer " + tok)[0] == 401
    jwks.down = False
    wait_loaded(a)
    assert call(a, "read", "Bearer " + tok)[0] == 200


@pytest.mark.parametrize(
    "cfg",
    [
        '{"issuer":"i","audience":"a","jwks_url":"http://x"}',
        '{"issuer":"i","jwks_url":"https://x"}',
        '{"issuer":"i","audience":"a","jwks_url":"https://x","alg":"none"}',
        "{",
        '{"issuer":"i","audience":"a","jwks_url":"https://x","callers":{"u":"legacy"}}',
        '{"issuer":"i","audience":"a","jwks_url":"https://x","callers":{"":"varden"}}',
        '{"issuer":"i","audience":"a","jwks_url":"https://x","callers":{" ":"varden"}}',
        '{"issuer":"i","audience":"a","jwks_url":"https://x","callers":{"u":""}}',
    ],
    ids=[
        "http url",
        "no audience",
        "unknown field",
        "bad json",
        "name collision",
        "empty sub",
        "blank sub",
        "empty name",
    ],
)
def test_config_validation(tmp_path, cfg):
    write(tmp_path / "read" / mcpauth.JWKS_FILE, cfg)
    write(tmp_path / "read/legacy", LEGACY)
    with pytest.raises(ValueError):
        mcpauth.load(str(tmp_path))


def test_no_callers_accepts_nothing(tmp_path, jwks, caplog):
    caplog.set_level(logging.WARNING, logger="mcpauth")
    a, k = setup(tmp_path, jwks, cfg={c: v for c, v in BASE.items() if c != "callers"})
    assert "no callers" in caplog.text
    assert call(a, "read", "Bearer " + sign(k, HDR, claims()))[0] == 401


def test_finite_number_rejects_bool_nan_inf():
    from mcpauth import _finite_number

    assert _finite_number(1) and _finite_number(1.5)
    assert not _finite_number(True)
    assert not _finite_number(float("nan"))
    assert not _finite_number(float("inf"))
    assert not _finite_number("1")


def test_cold_start_fetches_when_no_keys(tmp_path, jwks):
    a, k = setup(tmp_path, jwks)
    v = a._groups["read"].jwt
    # First background fetch has started (last_fetch is recent) but not finished.
    v.keys = {}
    v.last_fetch = time.monotonic()
    assert call(a, "read", "Bearer " + sign(k, HDR, claims()))[0] == 200
