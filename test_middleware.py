"""Bearer middleware: fail-closed, /health open, SSE streaming works."""

import asyncio

import httpx
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse, StreamingResponse
from starlette.routing import Mount, Route

import mcpauth


def make_app(tmp):
    (tmp / "read").mkdir(exist_ok=True)
    (tmp / "read" / "varden").write_text("s3cret-0123456789")
    (tmp / "other").mkdir(exist_ok=True)
    (tmp / "other" / "x").write_text("otherkey-0123456789")

    async def stream(request):
        async def gen():
            yield b"data: 1\n\n"
            await asyncio.sleep(0)
            yield b"data: 2\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    async def ok(request):
        return PlainTextResponse("ok")

    guarded = mcpauth.load(str(tmp)).require("read", Starlette(routes=[Route("/sse", stream)]))
    return Starlette(routes=[Route("/health", ok), Mount("/", app=guarded)])


async def get(tmp, path, headers=None):
    transport = httpx.ASGITransport(app=make_app(tmp))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.get(path, headers=headers)


async def test_streaming_with_valid_token(tmp_path):
    r = await get(tmp_path, "/sse", {"Authorization": "Bearer s3cret-0123456789"})
    assert r.status_code == 200
    assert r.text == "data: 1\n\ndata: 2\n\n"


async def test_rejects_missing_wrong_and_other_group(tmp_path):
    assert (await get(tmp_path, "/sse")).status_code == 401
    assert (await get(tmp_path, "/sse", {"Authorization": "Bearer nope"})).status_code == 401
    assert (await get(tmp_path, "/sse", {"Authorization": "s3cret-0123456789"})).status_code == 401
    assert (await get(tmp_path, "/sse", {"Authorization": "Bearer otherkey-0123456789"})).status_code == 401


async def test_empty_bearer_rejected(tmp_path):
    assert (await get(tmp_path, "/sse", {"Authorization": "Bearer "})).status_code == 401


async def test_health_open(tmp_path):
    assert (await get(tmp_path, "/health")).status_code == 200
