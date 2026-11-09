import asyncio
import logging
import os

import pytest

import mcpauth

VARDEN = "varden-token-0123456789"
LEGACY = "legacy-token-0123456789"


def write(p, s):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(s)


@pytest.fixture
def callers(tmp_path):
    write(tmp_path / "read/varden", VARDEN + "\n")
    write(tmp_path / "read/legacy", LEGACY)
    return str(tmp_path)


def call(auth, group, authz=None, query=b""):
    seen = {}

    async def app(scope, receive, send):
        seen["caller"] = mcpauth.caller()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    headers = [(b"authorization", authz.encode())] if authz is not None else []
    scope = {"type": "http", "path": "/sse", "query_string": query, "headers": headers, "client": ("10.0.0.7", 5555)}
    out = []

    async def send(m):
        out.append(m)

    asyncio.run(auth.require(group, app)(scope, None, send))
    return out[0]["status"], out[1]["body"], seen.get("caller")


@pytest.mark.parametrize("case", ["unset", "missing", "empty", "blank", "short"])
def test_load_fails_closed(tmp_path, case):
    d = tmp_path
    if case == "blank":
        write(d / "read/x", "  \n")
    if case == "short":
        write(d / "read/x", "123456789012345\n")
    arg = {"unset": None, "missing": str(d / "nope")}.get(case, str(d))
    with pytest.raises((ValueError, OSError)):
        mcpauth.load(arg)


def test_sixteen_bytes_ok(tmp_path):
    write(tmp_path / "read/x", " 1234567890123456 \n")
    a = mcpauth.load(str(tmp_path))
    assert a.has("read") and not a.has("write")


def test_k8s_projection(tmp_path):
    write(tmp_path / "..2025_01_01_00_00_00.123/read/varden", VARDEN)
    os.symlink("..2025_01_01_00_00_00.123", tmp_path / "..data")
    os.symlink("..data/read", tmp_path / "read")
    a = mcpauth.load(str(tmp_path))
    assert list(a._groups) == ["read"]
    assert call(a, "read", "Bearer " + VARDEN)[0] == 200


def test_require(callers, caplog):
    a = mcpauth.load(callers)
    caplog.set_level(logging.INFO, logger="mcpauth")
    assert call(a, "read", "Bearer " + VARDEN) == (200, b"ok", "varden")
    assert call(a, "read", "Bearer " + LEGACY)[2] == "legacy"
    assert "caller=varden path=/sse" in caplog.text
    assert mcpauth.caller() == ""


@pytest.mark.parametrize(
    "authz,query",
    [
        (None, b""),
        (None, ("token=" + VARDEN).encode()),
        (VARDEN, b""),
        ("Basic " + VARDEN, b""),
        ("bearer " + VARDEN, b""),
        ("Bearer wrong-token-0123456789", b""),
        ("Bearer " + VARDEN + "x", b""),
    ],
)
def test_denied(callers, caplog, authz, query):
    a = mcpauth.load(callers)
    caplog.set_level(logging.INFO, logger="mcpauth")
    assert call(a, "read", authz, query) == (401, b"unauthorized", None)
    assert "auth=denied path=/sse remote=10.0.0.7" in caplog.text
    assert VARDEN not in caplog.text and LEGACY not in caplog.text


def test_unknown_group_denied(callers):
    assert call(mcpauth.load(callers), "write", "Bearer " + VARDEN)[0] == 401


def test_compare_all_no_early_exit(callers, monkeypatch):
    v = mcpauth.load(callers)._groups["read"].static
    n = 0
    real = mcpauth.hmac.compare_digest

    def counting(a, b):
        nonlocal n
        n += 1
        return real(a, b)

    monkeypatch.setattr(mcpauth.hmac, "compare_digest", counting)
    for tok in (LEGACY, VARDEN, "nope-nope-nope-nope"):
        n = 0
        v.verify(tok.encode())
        assert n == 2


def test_duplicate_token_rejected(tmp_path):
    write(tmp_path / "g/a", VARDEN)
    write(tmp_path / "g/b", VARDEN + "\n")
    with pytest.raises(ValueError, match="a and b") as e:
        mcpauth.load(str(tmp_path))
    assert VARDEN not in str(e.value)
