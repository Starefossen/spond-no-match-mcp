"""Tool error shape, secret scrub, audit log and write tier."""

import json
import logging
import re
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

import main
import mcpauth

SECRET_PATTERNS = [
    re.compile(r"Bearer\s+\S+", re.I),
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),
    re.compile(r"[A-Za-z0-9+/_-]{40,}={0,2}"),
    re.compile(r"password\s*[=:]", re.I),
    re.compile(r"[?&](token|access_token|key)=", re.I),
]
LEAK = (
    "boom https://api.spond.com/x?token=abc password=hunter2 "
    "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig " + "a" * 40
)


def assert_clean(text: str):
    for p in SECRET_PATTERNS:
        assert not p.search(text), f"{p.pattern} in {text!r}"


def test_patterns_catch_leak():
    with pytest.raises(AssertionError):
        assert_clean(LEAK)


@pytest.mark.parametrize(
    "exc",
    [
        Exception(LEAK),
        main.Unauthorized(),
        RuntimeError(LEAK),
        main.LoginFailed("Kunne ikke nå Spond, prøv igjen senere"),
    ],
)
async def test_tool_errors_are_is_error_without_raw_text(monkeypatch, exc):
    monkeypatch.setattr(main.auth, "call", AsyncMock(side_effect=exc))
    res = await main.run_tool("get_upcoming_events", {})
    assert res.isError
    assert_clean(res.content[0].text)
    assert "boom" not in res.content[0].text


async def test_tool_success_not_error_and_clean(monkeypatch, caplog):
    out = "Trening tirsdag 18:00, Ulriken. Svar: ja"
    monkeypatch.setattr(main.auth, "call", AsyncMock(return_value=out))
    with caplog.at_level(logging.INFO, logger="spond-mcp.audit"):
        res = await main.run_tool("respond_to_event", {"secret_arg": "ARGVALUE"})
    assert not res.isError and res.content[0].text == out
    assert_clean(res.content[0].text)
    line = next(r.getMessage() for r in caplog.records if r.name == "spond-mcp.audit")
    assert "tool=respond_to_event tier=write decision=allowed ok=True latency_ms=" in line
    assert "ARGVALUE" not in line and out not in line


async def test_audit_unknown_tool_name_not_echoed(monkeypatch, caplog):
    monkeypatch.setattr(main.auth, "call", AsyncMock(return_value="x"))
    with caplog.at_level(logging.INFO, logger="spond-mcp.audit"):
        await main.run_tool("evil\nname", {})
    assert "tool=unknown tier=read" in caplog.records[-1].getMessage()


def test_needs_write():
    def call(n):
        return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": n}}).encode()

    assert main.needs_write(call("respond_to_event"))
    assert not main.needs_write(call("get_upcoming_events"))
    assert main.needs_write(b"[" + call("get_posts") + b"," + call("respond_to_event") + b"]")
    assert main.needs_write(b"{not json")
    assert main.needs_write(b'{"method":"tools/call","params":"x"}')
    assert not main.needs_write(b'{"method":"tools/list"}')
    assert not main.needs_write(b"")


@pytest.fixture
def write_client(tmp_path, monkeypatch):
    for g, n, t in (("read", "varden", "read-secret-token-xyz"), ("write", "varden", "write-secret-token-xyz")):
        (tmp_path / g).mkdir(exist_ok=True)
        (tmp_path / g / n).write_text(t)
    monkeypatch.setenv("WRITE_REQUIRES_WRITE_GROUP", "true")
    return TestClient(main.build_app(mcpauth.load(str(tmp_path))), raise_server_exceptions=False)


def test_write_tier(write_client):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "respond_to_event"}}
    read = {"Authorization": "Bearer read-secret-token-xyz"}
    assert write_client.post("/messages/", json=body, headers=read).status_code == 401
    assert (
        write_client.post(
            "/messages/", json=body, headers={"Authorization": "Bearer write-secret-token-xyz"}
        ).status_code
        != 401
    )
    body["params"]["name"] = "get_posts"
    assert write_client.post("/messages/", json=body, headers=read).status_code != 401
    assert write_client.get("/sse", headers={"Authorization": "Bearer write-secret-token-xyz"}).status_code == 401
