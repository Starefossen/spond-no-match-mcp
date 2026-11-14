"""Spond MCP server entry point — Starlette app with health + SSE endpoints."""

import asyncio
import json
import logging
import os
import time
import urllib.request
from contextlib import asynccontextmanager

import aiohttp
import uvicorn
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import TextContent, Tool
from spond import AuthenticationError
from spond.spond import Spond
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Mount, Route

import mcpauth
from server import TOOLS, WRITE_TOOLS, SpondService, chat_reply, handle_tool_call

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("spond-mcp")


class Unauthorized(Exception):
    pass


async def _raise_on_401(resp: aiohttp.ClientResponse) -> None:
    if resp.status == 401:
        raise Unauthorized


def create_service() -> SpondService:
    client = Spond(username=os.environ["SPOND_USERNAME"], password=os.environ["SPOND_PASSWORD"])
    # The lib swallows non-200 bodies as JSON; make 401 visible so we can re-login.
    asyncio.ensure_future(client.clientsession.close())
    client.clientsession = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(), raise_for_status=_raise_on_401)
    return SpondService(client=client, kids_config=json.loads(os.environ.get("KIDS_CONFIG", "[]")))


def ntfy_alert(msg: str) -> None:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return
    try:
        req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=msg.encode(), headers={"Title": "Spond"})
        urllib.request.urlopen(req, timeout=10).close()
    except Exception as e:
        logger.warning("ntfy: %s", type(e).__name__)


class LoginFailed(Exception):
    pass


class SpondAuth:
    """One shared client; re-login on 401; 15 min backoff after a failed login to avoid account lockout."""

    BACKOFF = 900.0

    def __init__(self, factory=create_service, alert=ntfy_alert):
        self._factory = factory
        self._alert = alert
        self._lock = asyncio.Lock()
        self.service: SpondService | None = None
        self.failed_at: float | None = None

    def _backoff_error(self) -> LoginFailed:
        mins = max(1, round((self.BACKOFF - (time.monotonic() - self.failed_at)) / 60))
        return LoginFailed(f"Innlogging mot Spond feilet (prøver igjen om {mins} min)")

    async def _drop(self) -> None:
        svc, self.service = self.service, None
        if svc is not None:
            await svc.close()

    async def ensure(self) -> SpondService:
        async with self._lock:
            if self.failed_at is not None and time.monotonic() - self.failed_at < self.BACKOFF:
                raise self._backoff_error()
            if self.service is None or self.service._client.clientsession.closed:
                await self._drop()
                self.service = self._factory()
            client = self.service._client
            if not client.token:
                try:
                    await client.login()
                except (AuthenticationError, Unauthorized):
                    logger.error("Spond login rejected; backing off %d min", self.BACKOFF // 60)
                    self.failed_at = time.monotonic()
                    await self._drop()
                    msg = "Innlogging mot Spond feilet. Sjekk passord/2FA (apps/spond/README.md)."
                    await asyncio.to_thread(self._alert, msg)
                    raise self._backoff_error() from None
                except Exception as e:
                    logger.error("Spond login error: %s", type(e).__name__)
                    await self._drop()
                    raise LoginFailed("Kunne ikke nå Spond, prøv igjen senere") from None
            self.failed_at = None
            return self.service

    async def call(self, name: str, arguments: dict | None) -> str:
        svc = await self.ensure()
        try:
            return await handle_tool_call(svc, name, arguments)
        except Unauthorized:
            logger.info("Spond token rejected; logging in again")
            async with self._lock:
                if self.service is svc:
                    await self._drop()
            return await handle_tool_call(await self.ensure(), name, arguments)


auth = SpondAuth()


# MCP server setup
mcp_server = Server("spond")
sse = SseServerTransport("/messages/")


@mcp_server.list_tools()
async def list_tools() -> list[Tool]:
    return [Tool(**t) for t in TOOLS]


@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict | None) -> list[TextContent]:
    try:
        result = await auth.call(name, arguments)
    except LoginFailed as e:
        result = str(e)
    except Unauthorized:
        result = "Spond avviste innloggingen, prøv igjen senere"
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
        await mcp_server.run(read_stream, write_stream, mcp_server.create_initialization_options())
    return Response()


async def health(request: Request):
    return PlainTextResponse("ok")


@asynccontextmanager
async def lifespan(app):
    yield
    await auth._drop()


write_server = Server("spond-write")
write_sse = SseServerTransport("/write/messages/")


@write_server.list_tools()
async def list_write_tools() -> list[Tool]:
    return [Tool(**t) for t in WRITE_TOOLS]


@write_server.call_tool()
async def call_write_tool(name: str, arguments: dict | None) -> list[TextContent]:
    args = arguments or {}
    try:
        if name != "chat_reply":
            raise ValueError(f"Ukjent verktøy: {name}")
        svc = await auth.ensure()
        sent = await chat_reply(svc, args.get("chat_id", ""), args.get("text", ""), args.get("draft_id", ""))
        result = json.dumps(sent)
    except LoginFailed as e:
        result = str(e)
    except Exception as e:
        logger.exception("Write tool %s failed", name)
        result = f"Feil: {e}"
    return [TextContent(type="text", text=result)]


async def handle_write_sse(request: Request):
    async with write_sse.connect_sse(request.scope, request.receive, request._send) as (r, w):
        await write_server.run(r, w, write_server.create_initialization_options())
    return Response()


def build_app(callers: mcpauth.Auth) -> Starlette:
    write = []
    if callers.has("write"):
        write = [
            Mount(
                "/write",
                app=callers.require(
                    "write",
                    Starlette(
                        routes=[
                            Route("/sse", handle_write_sse),
                            Mount("/messages/", app=write_sse.handle_post_message),
                        ]
                    ),
                ),
            )
        ]
    return Starlette(
        routes=[
            Route("/health", health),
            *write,
            Mount(
                "/",
                app=callers.require(
                    "read",
                    Starlette(
                        routes=[
                            Route("/sse", handle_sse),
                            Mount("/messages/", app=sse.handle_post_message),
                        ]
                    ),
                ),
            ),
        ],
        lifespan=lifespan,
    )


def require_credentials() -> None:
    if not os.environ.get("SPOND_USERNAME") or not os.environ.get("SPOND_PASSWORD"):
        raise SystemExit("SPOND_USERNAME/SPOND_PASSWORD not set; refusing to start")


if __name__ == "__main__":
    try:
        callers = mcpauth.load(os.environ.get("MCP_CALLERS_DIR"))
    except (OSError, ValueError) as e:
        raise SystemExit(f"{e}; refusing to start without auth") from None
    if not callers.has("read"):
        raise SystemExit("mcpauth: no read group; refusing to start")
    require_credentials()
    port = int(os.environ.get("PORT", "8080"))
    logger.info("spond MCP server starting on :%d (auth enabled)", port)
    uvicorn.run(build_app(callers), host="0.0.0.0", port=port, log_level="info", http="h11")
