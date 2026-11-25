"""Spond MCP server entry point — Starlette app with health + SSE endpoints."""

import asyncio
import json
import logging
import os
import re
import time
import urllib.request
from contextlib import asynccontextmanager

import aiohttp
import uvicorn
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import CallToolResult, TextContent, Tool
from spond import AuthenticationError
from spond.spond import Spond
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route

import mcpauth
from server import TOOLS, SpondService, ToolError, handle_tool_call, kid_events, kid_trainings

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

    async def run(self, fn):
        svc = await self.ensure()
        try:
            return await fn(svc)
        except Unauthorized:
            logger.info("Spond token rejected; logging in again")
            async with self._lock:
                if self.service is svc:
                    await self._drop()
            return await fn(await self.ensure())

    async def call(self, name: str, arguments: dict | None) -> str:
        return await self.run(lambda svc: handle_tool_call(svc, name, arguments))


auth = SpondAuth()


# MCP server setup
mcp_server = Server("spond")
sse = SseServerTransport("/messages/")


@mcp_server.list_tools()
async def list_tools() -> list[Tool]:
    return [Tool(**t) for t in TOOLS]


WRITE_TOOLS = frozenset({"respond_to_event"})
audit = logging.getLogger("spond-mcp.audit")


_REDACT = [
    (re.compile(r"\b([A-Za-z][A-Za-z0-9+.\-]*://)[^/\s@\"']+@"), r"\1"),
    (re.compile(r"\b([A-Za-z][A-Za-z0-9+.\-]*://[^\s?#\"']*)[?#][^\s\"']*"), r"\1"),
    (re.compile(r"(^|[\s\"'(=,;])[^\s/@:\"'(=,;]+:[^\s/@\"']+@"), r"\1"),
    (re.compile(r"(?i)\b(bearer|basic)\s+(?![a-z]+\b)[A-Za-z0-9._~+/=\-]+"), r"\1 [redacted]"),
    (
        re.compile(
            r"(?i)(\\?[\"']?)\b([a-z0-9_\-]*(?:password|passwd|passord|pwd)[a-z0-9_\-]*|authorization|private_?key)"
            r"(\\?[\"']?)(\s*[=:]\s*)[^\n&,;}]+"
        ),
        r"\1\2\3\4[redacted]",
    ),
    (
        re.compile(
            r"(?i)(\\?[\"']?)\b([a-z0-9_\-]*(?:token|secret|session|signature)[a-z0-9_\-]*|sid|sig|api_?key|key)"
            r"(\\?[\"']?)(\s*[=:]\s*)(?:\\\"[^\"\\]*\\\"|\"[^\"]*\"|'[^']*'|[^\s&\"',;}\\]+)"
        ),
        r"\1\2\3\4[redacted]",
    ),
]
_TOKEN_RUN = re.compile(r"[A-Za-z0-9_\-.=+]{32,}")
_MAX_REDACTED = 300


def _token_like(t: str) -> bool:
    kinds = sum(bool(re.search(p, t)) for p in ("[A-Z]", "[a-z]", "[0-9]"))
    return kinds >= 2 and re.search(r"[A-Za-z0-9+=]{16,}", t) is not None


def redact(s: str) -> str:
    """Port of libs/mcpwrite Redact: strip URL queries, credentials and token-like runs; cap length."""
    for pat, rep in _REDACT:
        s = pat.sub(rep, s)
    s = _TOKEN_RUN.sub(lambda m: "[redacted]" if _token_like(m.group()) else m.group(), s)
    if len(s) > _MAX_REDACTED:
        s = s[:_MAX_REDACTED] + f"… [kuttet: {len(s) - _MAX_REDACTED} tegn til]"
    return s


def tier(name: str) -> str:
    return "write" if name in WRITE_TOOLS else "read"


async def run_tool(name: str, arguments: dict | None) -> CallToolResult:
    """Tool failures are isError with a short Norwegian message; raw exception text stays in the server log."""
    start = time.monotonic()
    error = None
    try:
        result = await auth.call(name, arguments)
    except ToolError as e:
        error = redact(str(e))
    except LoginFailed as e:
        error = str(e)
    except Unauthorized:
        error = "Spond avviste innloggingen. Prøv igjen senere."
    except Exception:
        logger.exception("Tool %s failed", name)
        error = "Spond-kallet feilet. Prøv igjen senere, eller spør på en annen måte."
    audit.info(
        "audit caller=%s tool=%s tier=%s decision=allowed ok=%s latency_ms=%d",
        mcpauth.caller() or "-",
        name if any(t["name"] == name for t in TOOLS) else "unknown",
        tier(name),
        error is None,
        (time.monotonic() - start) * 1000,
    )
    return CallToolResult(content=[TextContent(type="text", text=error or result)], isError=error is not None)


@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict | None) -> CallToolResult:
    return await run_tool(name, arguments)


def needs_write(body: bytes) -> bool:
    """True if the JSON-RPC body calls a write tool; unparseable bodies need write (fail closed)."""
    body = body.strip()
    if not body:
        return False
    try:
        msgs = json.loads(body)
    except ValueError:
        return True
    for m in msgs if isinstance(msgs, list) else [msgs]:
        if not isinstance(m, dict):
            return True
        params = m.get("params")
        if m.get("method") == "tools/call" and (not isinstance(params, dict) or params.get("name") in WRITE_TOOLS):
            return True
    return False


MAX_BODY = 4 << 20


class WriteGuard:
    """Mirror of libs/mcpwrite.Guard: with WRITE_REQUIRES_WRITE_GROUP, write tool calls need group "write"."""

    def __init__(self, callers: mcpauth.Auth, app):
        self.read = callers.require("read", app)
        self.write = callers.require("write", app)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST":
            return await self.read(scope, receive, send)
        chunks, size = [], 0
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return
            chunks.append(msg.get("body", b""))
            size += len(chunks[-1])
            if size > MAX_BODY:
                await send({"type": "http.response.start", "status": 413, "headers": []})
                return await send({"type": "http.response.body", "body": b"request too large"})
            if not msg.get("more_body"):
                break
        body = b"".join(chunks)
        sent = False

        async def replay():
            nonlocal sent
            if sent:
                return await receive()
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        if not needs_write(body):
            return await self.read(scope, replay, send)

        async def audit_denied(msg):
            if msg["type"] == "http.response.start" and msg["status"] == 401:
                audit.info("audit caller=- tool=- tier=write decision=denied ok=False latency_ms=0")
            await send(msg)

        return await self.write(scope, replay, audit_denied)


def write_required() -> bool:
    return os.environ.get("WRITE_REQUIRES_WRITE_GROUP", "").strip().lower() in ("1", "t", "true", "yes")


# HTTP handlers
async def handle_sse(request: Request):
    async with sse.connect_sse(request.scope, request.receive, request._send) as (
        read_stream,
        write_stream,
    ):
        await mcp_server.run(read_stream, write_stream, mcp_server.create_initialization_options())
    return Response()


def minside_days(raw: str | None) -> int:
    """The days query parameter, 1 to 7; anything else is 1."""
    try:
        return min(max(int(raw or 1), 1), 7)
    except ValueError:
        return 1


async def minside_today(request: Request):
    """GET /api/minside/today?kid=<name>[&days=N]: the kid's Spond events for
    the next day, or N days capped at 7 (RFC-011). mcpauth group "minside";
    Min side resolves the kid from the Kanidm login and spond trusts the
    caller for that."""
    kid = request.query_params.get("kid", "")
    days = minside_days(request.query_params.get("days"))
    try:
        events = await auth.run(lambda svc: kid_events(svc, kid, days))
    except Exception as e:
        logger.warning("minside today: %s", type(e).__name__)
        return PlainTextResponse("Spond unavailable", status_code=503)
    if events is None:
        return PlainTextResponse("Unknown kid", status_code=404)
    return JSONResponse({"events": events}, headers={"Cache-Control": "no-store"})


async def exercise_days(request: Request):
    """GET /api/exercise/days?kid=<name>[&days=N]: minutes of the kid's ended
    Spond trainings per day, N days back (1-14, default 2). mcpauth group
    "exercise" (fleet's own key, RFC-026 §12); no names or places."""
    kid = request.query_params.get("kid", "")
    try:
        days = min(max(int(request.query_params.get("days") or 2), 1), 14)
    except ValueError:
        days = 2
    try:
        out = await auth.run(lambda svc: kid_trainings(svc, kid, days))
    except Exception as e:
        logger.warning("exercise days: %s", type(e).__name__)
        return PlainTextResponse("Spond unavailable", status_code=503)
    if out is None:
        return PlainTextResponse("Unknown kid", status_code=404)
    return JSONResponse({"days": out}, headers={"Cache-Control": "no-store"})


async def health(request: Request):
    return PlainTextResponse("ok")


@asynccontextmanager
async def lifespan(app):
    yield
    await auth._drop()


def build_app(callers: mcpauth.Auth) -> Starlette:
    return Starlette(
        routes=[
            Route("/health", health),
            Mount("/api/minside", app=callers.require("minside", Starlette(routes=[Route("/today", minside_today)]))),
            Mount("/api/exercise", app=callers.require("exercise", Starlette(routes=[Route("/days", exercise_days)]))),
            Mount(
                "/",
                app=(WriteGuard if write_required() else lambda c, a: c.require("read", a))(
                    callers,
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
    if write_required() and not callers.has("write"):
        raise SystemExit("mcpauth: WRITE_REQUIRES_WRITE_GROUP set but no write group; refusing to start")
    require_credentials()
    port = int(os.environ.get("PORT", "8080"))
    logger.info("spond MCP server starting on :%d (auth enabled)", port)
    uvicorn.run(build_app(callers), host="0.0.0.0", port=port, log_level="info", http="h11")
