"""Tests for login recovery, 401 re-login and login backoff."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from spond import AuthenticationError

import main
from main import LoginFailed, SpondAuth, Unauthorized


def fake_service(login=None):
    client = MagicMock()
    client.token = None
    client.clientsession.closed = False

    async def default_login():
        client.token = "t"

    client.login = AsyncMock(side_effect=login or default_login)
    svc = MagicMock()
    svc._client = client
    svc.close = AsyncMock(side_effect=lambda: setattr(client.clientsession, "closed", True))
    return svc


async def test_failed_login_recreates_client_after_backoff(monkeypatch):
    services = [fake_service(login=AuthenticationError("bad")), fake_service()]
    alerts = []
    auth = SpondAuth(factory=lambda: services.pop(0), alert=alerts.append)
    with pytest.raises(LoginFailed, match="prøver igjen om 15 min"):
        await auth.ensure()
    assert auth.service is None and len(alerts) == 1
    auth.failed_at -= SpondAuth.BACKOFF
    svc = await auth.ensure()
    assert svc._client.token == "t" and not services


async def test_backoff_makes_no_upstream_call():
    calls = []

    def factory():
        calls.append(1)
        return fake_service(login=AuthenticationError("bad"))

    alerts = []
    auth = SpondAuth(factory=factory, alert=alerts.append)
    for _ in range(3):
        with pytest.raises(LoginFailed):
            await auth.ensure()
    assert len(calls) == 1 and len(alerts) == 1


async def test_closed_session_is_recreated():
    first, second = fake_service(), fake_service()
    services = [first, second]
    auth = SpondAuth(factory=lambda: services.pop(0), alert=lambda m: None)
    assert await auth.ensure() is first
    first._client.clientsession.closed = True
    assert await auth.ensure() is second


async def test_401_relogs_and_retries_once(monkeypatch):
    services = [fake_service(), fake_service()]
    created = list(services)
    auth = SpondAuth(factory=lambda: services.pop(0), alert=lambda m: None)
    handle = AsyncMock(side_effect=[Unauthorized(), "ok"])
    monkeypatch.setattr(main, "handle_tool_call", handle)
    assert await auth.call("list_groups", None) == "ok"
    assert handle.await_args_list[1].args[0] is created[1]
    assert created[1]._client.login.await_count == 1


async def test_401_twice_gives_up(monkeypatch):
    auth = SpondAuth(factory=fake_service, alert=lambda m: None)
    monkeypatch.setattr(main, "handle_tool_call", AsyncMock(side_effect=Unauthorized()))
    with pytest.raises(Unauthorized):
        await auth.call("list_groups", None)
