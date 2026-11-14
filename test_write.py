from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

import mcpauth
import server
from main import build_app


class FakeResp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self._body


def make_svc(post_body, history_before, history_after=None, status=200):
    client = MagicMock()
    client.token = "t"
    client._chat_url = "https://chat.example"
    client._auth = "a"
    client.get_profile = AsyncMock(return_value={"id": "ME"})
    client.clientsession.post = MagicMock(return_value=FakeResp(status, post_body))
    svc = server.SpondService(client)
    svc._api_get = AsyncMock(side_effect=[history_before, history_after or []])
    return svc, client


@pytest.fixture(autouse=True)
def clear_sent():
    server._sent.clear()


async def test_msg_num_from_response():
    svc, client = make_svc({"msgNum": 7}, [{"msgNum": 6}])
    assert await server.chat_reply(svc, "C1", "hei", "d1") == {"chat_id": "C1", "msg_num": 7}
    args = client.clientsession.post.call_args
    assert args.args[0] == "https://chat.example/messages"
    assert args.kwargs["json"] == {"chatId": "C1", "text": "hei", "type": "TEXT"}
    assert args.kwargs["headers"] == {"auth": "a"}


async def test_msg_num_read_back():
    before = [{"msgNum": 5, "user": "ME", "text": "hei"}]
    after = before + [
        {"msgNum": 6, "user": "OTHER", "text": "hei"},
        {"msgNum": 7, "user": "ME", "text": "hei"},
    ]
    svc, _ = make_svc({}, before, after)
    assert (await server.chat_reply(svc, "C1", "hei", "d1"))["msg_num"] == 7


async def test_idempotent_per_draft():
    svc, client = make_svc({"msgNum": 7}, [])
    await server.chat_reply(svc, "C1", "hei", "d1")
    assert (await server.chat_reply(svc, "C1", "hei", "d1"))["msg_num"] == 7
    assert client.clientsession.post.call_count == 1


async def test_error_status_not_recorded():
    svc, _ = make_svc({}, [], status=500)
    with pytest.raises(RuntimeError):
        await server.chat_reply(svc, "C1", "hei", "d1")
    assert "d1" not in server._sent


async def test_requires_fields():
    svc, client = make_svc({}, [])
    with pytest.raises(ValueError):
        await server.chat_reply(svc, "C1", "", "d1")
    client.clientsession.post.assert_not_called()


def _client(tmp_path, groups):
    for g, tok in groups.items():
        (tmp_path / g).mkdir()
        (tmp_path / g / "varden").write_text(tok + "\n")
    return TestClient(build_app(mcpauth.load(str(tmp_path))), raise_server_exceptions=False)


def test_write_route_requires_write_group(tmp_path):
    c = _client(tmp_path, {"read": "read-token-xyz-123", "write": "write-token-xyz-123"})
    assert c.post("/write/messages/").status_code == 401
    assert c.post("/write/messages/", headers={"Authorization": "Bearer read-token-xyz-123"}).status_code == 401
    assert c.post("/write/messages/", headers={"Authorization": "Bearer write-token-xyz-123"}).status_code != 401
    assert c.post("/messages/", headers={"Authorization": "Bearer write-token-xyz-123"}).status_code == 401


def test_no_write_route_without_group(tmp_path):
    c = _client(tmp_path, {"read": "read-token-xyz-123"})
    r = c.post("/write/messages/", headers={"Authorization": "Bearer read-token-xyz-123"})
    assert r.status_code == 404
