"""Spond MCP server entry point — Starlette app with health + SSE endpoints."""

import json
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import TextContent, Tool
from spond.spond import Spond
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route

from server import TOOLS, SpondService, handle_tool_call

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("spond-mcp")

service: SpondService | None = None


def create_service() -> SpondService:
    username = os.environ.get("SPOND_USERNAME", "")
    password = os.environ.get("SPOND_PASSWORD", "")
    kids_json = os.environ.get("KIDS_CONFIG", "[]")

    if not username or not password:
        logger.warning("SPOND_USERNAME/SPOND_PASSWORD not set — API calls will fail")

    kids_config = json.loads(kids_json)
    client = Spond(username=username, password=password)
    return SpondService(client=client, kids_config=kids_config)


# MCP server setup
mcp_server = Server("spond")
sse = SseServerTransport("/messages/")


@mcp_server.list_tools()
async def list_tools() -> list[Tool]:
    return [Tool(**t) for t in TOOLS]


@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict | None) -> list[TextContent]:
    global service
    if service is None:
        service = create_service()
    try:
        result = await handle_tool_call(service, name, arguments)
    except Exception as e:
        logger.exception("Tool %s failed", name)
        result = f"Feil: {e}"
    return [TextContent(type="text", text=result)]


# HTTP handlers
async def handle_sse(request: Request):
    async with sse.connect_sse(request.scope, request.receive, request._send) as (
        read_stream,
        write_stream,
    ):
        await mcp_server.run(
            read_stream, write_stream, mcp_server.create_initialization_options()
        )


async def health(request: Request):
    return PlainTextResponse("ok")


@asynccontextmanager
async def lifespan(app):
    yield
    if service is not None:
        await service.close()


app = Starlette(
    routes=[
        Route("/health", health),
        Route("/sse", handle_sse),
        Mount("/messages/", app=sse.handle_post_message),
    ],
    lifespan=lifespan,
)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    logger.info("spond MCP server starting on :%d", port)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info", http="h11")
