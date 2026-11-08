"""Bearer middleware: fail-closed, /health open, SSE streaming works."""

import asyncio

import httpx
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse, StreamingResponse
from starlette.routing import Route

import main


def make_app():
    async def stream(request):
        async def gen():
            yield b"data: 1\n\n"
            await asyncio.sleep(0)
            yield b"data: 2\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    async def ok(request):
        return PlainTextResponse("ok")

    return Starlette(
        routes=[Route("/health", ok), Route("/sse", stream)],
        middleware=[Middleware(main.BearerAuthMiddleware)],
    )


async def get(path, headers=None):
    transport = httpx.ASGITransport(app=make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.get(path, headers=headers)


async def test_streaming_with_valid_token(monkeypatch):
    monkeypatch.setattr(main, "AUTH_TOKEN", "s3cret")
    r = await get("/sse", {"Authorization": "Bearer s3cret"})
    assert r.status_code == 200
    assert r.text == "data: 1\n\ndata: 2\n\n"


async def test_rejects_missing_and_wrong_token(monkeypatch):
    monkeypatch.setattr(main, "AUTH_TOKEN", "s3cret")
    assert (await get("/sse")).status_code == 401
    assert (await get("/sse", {"Authorization": "Bearer nope"})).status_code == 401
    assert (await get("/sse", {"Authorization": "s3cret"})).status_code == 401


async def test_empty_token_fails_closed(monkeypatch):
    monkeypatch.setattr(main, "AUTH_TOKEN", "")
    assert (await get("/sse", {"Authorization": "Bearer "})).status_code == 401


async def test_health_open(monkeypatch):
    monkeypatch.setattr(main, "AUTH_TOKEN", "s3cret")
    assert (await get("/health")).status_code == 200
