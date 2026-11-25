"""Tool error shape, secret scrub, audit log and write tier."""

import json
import logging
import re
import time
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


async def test_tool_error_is_error_and_scrubbed(monkeypatch, caplog):
    monkeypatch.setattr(main.auth, "call", AsyncMock(side_effect=main.ToolError(f"Ukjent barn: x {LEAK}")))
    with caplog.at_level(logging.INFO, logger="spond-mcp.audit"):
        res = await main.run_tool("get_upcoming_events", {})
    assert res.isError
    text = res.content[0].text
    assert text.startswith("Ukjent barn: x boom https://api.spond.com/x ")
    for bad in ("abc", "hunter2", "eyJ", "a" * 40):
        assert bad not in text
    assert "ok=False" in caplog.records[-1].getMessage()


async def test_handler_failures_raise_tool_error():
    from server import handle_tool_call

    for name, args in [("get_event_details", {}), ("respond_to_event", {}), ("nope", {})]:
        with pytest.raises(main.ToolError):
            await handle_tool_call(None, name, args)


@pytest.mark.parametrize(
    "leak",
    [
        "postgres://admin:s3cr@db/x",
        "https://x/y?token=s3cr",
        "Bearer s3crABCDEFGH",
        "bearer s3crABCDEFGH",
        '{"access_token":"s3cr"}',
        '{\\"token\\":\\"s3cr\\"}',
        "password: correct horse s3cr",
        "Authorization: s3cr",
        "admin:s3cr@10.0.0.1",
        "id 9fK2xQ7mZ1aB4cD8eF0gH3jL5nP6rS2tUs3cr",
        "Basic dXNlcjpwYXNzs3cr",
        "Bearer sk-abc123s3cr",
        "Bearer ABCDEFGHs3cr",
        "Bearer abc.def.s3cr",
        "token%3Ds3cr",
        '{\\"token\\":\\"a\\\\\\"s3cr\\"}',
        '{"token":["s3cr"]}',
        '{"token":{"value":"s3cr"}}',
        "token => s3cr",
        "access_key=s3cr",
        "pass: s3cr",
        "passphrase: my s3cr",
        "Cookie: a=s3cr",
        "x-auth: s3cr",
        "credentials: s3cr",
        "blob abcdefgh/IJKLMN12/opqrst+UVWXYZ34/s3cr==",
        "1234:s3cr@db.example",
        'token="s3crX1',
        "token='s3crX1",
        '{"token":["a]s3cr"]}',
        '{"token":{"v":"a}s3cr"}}',
        "token%3A%20s3crX1",
        '{\\\\\\"token\\\\\\":\\\\\\"s3crX1\\\\\\"}',
        "Bearer: s3crX1",
        "auth=s3crX1",
        "jwt: s3crX1",
        "pw=s3crX1",
        "passcode: s3cr x",
        "pin=s3cr1234",
        "a" * 50 + "_token_" + "b" * 50 + ": s3cr",
        "x" * 45 + "password" + "y" * 45 + ": s3cr",
        "--token=s3crX1",
    ],
)
def test_redact_strips(leak):
    assert "s3cr" not in main.redact(leak)


def test_redact_keeps_text():
    for s in [
        "Ukjent barn: Oliver",
        "event 3f2b1c9e-8a7d-4e6f-9b0a-1c2d3e4f5a6b",
        "requires basic auth",
        "session: tuesday training",
        "time 10:30@home",
        "https://calendar.example.com/api/v1/families/calendar/events/upcoming-weekly-summary-for-everyone",
    ]:
        assert main.redact(s) == s
    assert main.redact("æ" * 310).endswith("… [kuttet: 10 tegn til]")


@pytest.mark.parametrize(
    "s",
    [
        "pwd-" * 1000,
        "token-" * 666,
        "a-" * 1999,
        "Bearer " * 600,
        "=" * 4000,
        'token="' + "\\\\" * 2000,
        "token=[" + '"]' * 2000,
        "token={" + '"' * 4000,
        '\\"token\\":' * 400,
        "a" * 3990 + "token",
        "token" + "a" * 3990,
        "-token" * 666,
    ],
)
def test_redact_no_backtracking(s):
    start = time.monotonic()
    main.redact(s)
    assert time.monotonic() - start < 0.2


def test_redact_long_input_fast():
    start = time.monotonic()
    out = main.redact("token" * 8000)
    assert time.monotonic() - start < 0.2
    assert out.endswith("… [kuttet: 39700 tegn til]")


async def test_tool_error_keeps_trusted_link(monkeypatch):
    link = "Lenke: https://files.spond.com/a.pdf?X-Amz-Signature=abc123"
    monkeypatch.setattr(main.auth, "call", AsyncMock(side_effect=main.ToolError("Kan ikke lese filen.", link=link)))
    res = await main.run_tool("get_attachment", {})
    assert res.content[0].text.endswith(link)


@pytest.mark.parametrize("tok", ["dXNlcjpwYXNz", "ABCDEFGHsecretval"])
def test_redact_whole_auth_token(tok):
    assert tok not in main.redact(f"Basic {tok}") and tok not in main.redact(f"Bearer {tok}")
