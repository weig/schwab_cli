"""MCP server wiring for Schwab.

Exposes a minimal set of tools over MCP (stdio transport). Wraps
the existing REST API for synchronous queries; streaming tools are
scaffolded here and wired through :class:`SubscriptionManager` but
the actual Schwab-WebSocket → progress-notification bridge is
declared as a TODO for a follow-up commit.

Tools currently live:

* ``get_quote(symbols)`` — REST one-shot quote.
* ``get_chain(symbol, expiry, strike_count)`` — REST chain.
* ``server_status()`` — counts and subscription summary.

The server instance is designed to be transport-agnostic; SSE mode
can be added by wiring the same :class:`SchwabMcpServer` instance
through the mcp SDK's ``SseServerTransport``.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import date, datetime
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from schwab_cli.api.chains import get_chain
from schwab_cli.api.client import ApiError, SchwabClient, SessionExpired
from schwab_cli.api.quotes import get_quotes
from schwab_cli.mcp_server.logbook import LogBook
from schwab_cli.mcp_server.streamer_bridge import StreamerBridge
from schwab_cli.mcp_server.subscription import SubscriptionManager
from schwab_cli.output.chains import shape_envelope


class SchwabMcpServer:
    """MCP server object. One instance per daemon process."""

    def __init__(
        self,
        client: SchwabClient,
        logbook: LogBook,
        *,
        server_name: str = "schwab",
        admin_token: str | None = None,
    ) -> None:
        self._client = client
        self._logbook = logbook
        self._manager = SubscriptionManager()
        self._bridge = StreamerBridge(client, logbook, self._manager)
        self._server = Server(server_name)
        self._started_at = time.time()
        self._admin_token = admin_token
        self._shutdown_event: asyncio.Event | None = None
        self._transport = "idle"
        self._stream_counter = 0
        self._register_tools()

    def _register_tools(self) -> None:
        server = self._server

        @server.list_tools()
        async def list_tools() -> list[Tool]:
            return [
                Tool(
                    name="get_quote",
                    description=(
                        "Fetch a real-time quote snapshot for one or more "
                        "symbols via Schwab's REST API. Returns price, "
                        "volume, bid/ask, and day-range fields as JSON."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "symbols": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Ticker symbols (upper-case).",
                            },
                        },
                        "required": ["symbols"],
                    },
                ),
                Tool(
                    name="get_chain",
                    description=(
                        "Fetch an option chain for one underlying at a "
                        "given expiry. Returns the flattened envelope "
                        "(underlying spot, contracts with greeks, IV, "
                        "bid/ask) as JSON."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "symbol": {"type": "string"},
                            "expiry": {
                                "type": "string",
                                "description": "ISO date YYYY-MM-DD.",
                            },
                            "strike_count": {
                                "type": "integer",
                                "default": 20,
                                "description": "Strikes to keep near ATM.",
                            },
                        },
                        "required": ["symbol", "expiry"],
                    },
                ),
                Tool(
                    name="stream_quote",
                    description=(
                        "Subscribe to real-time Schwab level-1 equity "
                        "quotes. Long-running — emits MCP progress "
                        "notifications (one per update) until the "
                        "client cancels the call. The message field of "
                        "each progress notification is a JSON object "
                        "with bid/ask/last/volume/etc. keys. Returns a "
                        "summary when cancelled."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "symbols": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Ticker symbols (upper-case).",
                            },
                        },
                        "required": ["symbols"],
                    },
                ),
                Tool(
                    name="server_status",
                    description=(
                        "Snapshot of this MCP server's state: active "
                        "sessions, refcounted Schwab subscriptions, and "
                        "configuration."
                    ),
                    inputSchema={"type": "object", "properties": {}},
                ),
            ]

        @server.call_tool()
        async def call_tool(
            name: str, arguments: dict[str, Any]
        ) -> list[TextContent]:
            self._logbook.info(
                "tool.call", tool=name, args=_redact(arguments)
            )
            try:
                if name == "get_quote":
                    return await self._tool_get_quote(arguments)
                if name == "get_chain":
                    return await self._tool_get_chain(arguments)
                if name == "stream_quote":
                    return await self._tool_stream_quote(arguments)
                if name == "server_status":
                    return await self._tool_server_status()
                return [TextContent(
                    type="text",
                    text=f"unknown tool: {name}",
                )]
            except (ApiError, SessionExpired) as e:
                self._logbook.error("tool.error", tool=name, error=str(e))
                return [TextContent(
                    type="text",
                    text=f"schwab error: {e}",
                )]
            except Exception as e:
                self._logbook.error(
                    "tool.error", tool=name, error=f"{type(e).__name__}: {e}"
                )
                return [TextContent(
                    type="text",
                    text=f"internal error: {type(e).__name__}: {e}",
                )]

    # ---- tool handlers -------------------------------------------------

    async def _tool_get_quote(self, args: dict[str, Any]) -> list[TextContent]:
        symbols = args.get("symbols") or []
        if not isinstance(symbols, list) or not all(isinstance(s, str) for s in symbols):
            return [TextContent(
                type="text", text="symbols must be a list of strings",
            )]
        if not symbols:
            return [TextContent(type="text", text="symbols list is empty")]
        data = get_quotes(self._client, [s.upper() for s in symbols])
        return [TextContent(type="text", text=json.dumps(data, default=str))]

    async def _tool_get_chain(self, args: dict[str, Any]) -> list[TextContent]:
        symbol = args.get("symbol")
        expiry_str = args.get("expiry")
        strike_count = int(args.get("strike_count") or 20)
        if not symbol or not expiry_str:
            return [TextContent(
                type="text", text="symbol and expiry are required",
            )]
        try:
            expiry = date.fromisoformat(expiry_str)
        except ValueError:
            return [TextContent(
                type="text", text=f"invalid expiry {expiry_str!r} (need YYYY-MM-DD)",
            )]
        raw = get_chain(
            self._client,
            symbol.upper(),
            contract_type="ALL",
            strike_count=strike_count,
            from_date=expiry,
            to_date=expiry,
        )
        envelope = shape_envelope(raw)
        return [TextContent(type="text", text=json.dumps(envelope, default=str))]

    async def _tool_stream_quote(
        self, args: dict[str, Any]
    ) -> list[TextContent]:
        """Long-running tool: open a SUBS subscription for the given
        symbols and pump each incoming update out as an MCP progress
        notification. Completes (normally or via ``CancelledError``)
        when the client cancels, with a summary of updates received.
        """
        symbols = args.get("symbols") or []
        if not isinstance(symbols, list) or not all(isinstance(s, str) for s in symbols):
            return [TextContent(
                type="text", text="symbols must be a list of strings",
            )]
        if not symbols:
            return [TextContent(type="text", text="symbols list is empty")]

        ctx = self._server.request_context
        progress_token: str | int | None = None
        if ctx is not None and getattr(ctx, "meta", None) is not None:
            progress_token = getattr(ctx.meta, "progressToken", None)
        mcp_session = ctx.session if ctx is not None else None

        # Session key: the MCP SDK exposes one session per connection
        # but doesn't hand us a stable id — id() of the session object
        # is stable for the life of the connection, which is what we
        # want. Progress-token string is stable per tool call.
        session_id = f"mcp_{id(mcp_session)}" if mcp_session else "mcp_unknown"
        self._stream_counter += 1
        token_key = (
            str(progress_token)
            if progress_token is not None
            else f"local_{self._stream_counter}"
        )

        queue = await self._bridge.add_subscription(
            session_id,
            token_key,
            "LEVELONE_EQUITIES",
            [s.upper() for s in symbols],
        )

        count = 0
        try:
            while True:
                try:
                    update = await asyncio.wait_for(queue.get(), timeout=60.0)
                except asyncio.TimeoutError:
                    # Quiet period (common off-hours). Send a keep-alive
                    # only if the client gave us a progress token — an
                    # agent that didn't is fine receiving nothing until
                    # real data arrives.
                    if progress_token is not None and mcp_session is not None:
                        await mcp_session.send_progress_notification(
                            progress_token=progress_token,
                            progress=float(count),
                            message="keepalive",
                        )
                    continue
                count += 1
                if progress_token is not None and mcp_session is not None:
                    await mcp_session.send_progress_notification(
                        progress_token=progress_token,
                        progress=float(count),
                        message=json.dumps(update, default=str),
                    )
        except asyncio.CancelledError:
            # Agent cancelled the tool — clean up and propagate so the
            # SDK emits the correct response.
            await self._bridge.remove_subscription(session_id, token_key)
            raise
        except Exception as e:
            self._logbook.error(
                "stream_quote.error", error=f"{type(e).__name__}: {e}",
            )
            await self._bridge.remove_subscription(session_id, token_key)
            return [TextContent(
                type="text",
                text=f"stream_quote error: {type(e).__name__}: {e}",
            )]

    async def _tool_server_status(self) -> list[TextContent]:
        session = self._client.session
        payload = {
            "server_name": self._server.name,
            "access_token_expires_at": _iso_from_epoch(session.expires_at),
            "refresh_token_expires_at": _iso_from_epoch(
                session.refresh_token_expires_at
            ),
            "subscription_summary": self._manager.snapshot(),
        }
        return [TextContent(type="text", text=json.dumps(payload, default=str))]

    # ---- lifecycle -----------------------------------------------------

    async def run_stdio(self) -> None:
        """Drive the server over stdio until the client disconnects."""
        self._transport = "stdio"
        self._logbook.info("server.start", transport="stdio")
        # Stdio has exactly one session for the life of the process;
        # mark it explicitly so the log ladder matches SSE's shape.
        self._logbook.info("session.connect", session="stdio_0", transport="stdio")
        try:
            async with stdio_server() as (read, write):
                await self._server.run(
                    read,
                    write,
                    self._server.create_initialization_options(),
                )
        finally:
            # Clean up any subscriptions held by the stdio session.
            await self._bridge.drop_session("stdio_0")
            self._logbook.info(
                "session.disconnect", session="stdio_0", transport="stdio",
            )
            self._logbook.info("server.stop", transport="stdio")

    async def run_sse(self, host: str, port: int) -> None:
        """Drive the server over SSE + HTTP admin endpoints on
        ``host:port`` until a shutdown is signalled.

        Uses Starlette + uvicorn (transitive deps of the ``mcp`` SDK).
        A small set of ``/admin/*`` routes are mounted alongside the
        SSE MCP transport for status / shutdown control.
        """
        # Deferred imports keep the stdio path free of Starlette cost
        # and avoid pulling in uvicorn when tests only exercise tool
        # handlers.
        import uvicorn
        from mcp.server.sse import SseServerTransport
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse, Response
        from starlette.routing import Mount, Route

        self._transport = "sse"
        self._shutdown_event = asyncio.Event()
        sse = SseServerTransport("/messages/")

        async def handle_sse(request):
            client_ip = (
                request.client.host if request.client else "unknown"
            )
            # Use id() of the request as a stable session marker for logs.
            sess_id = f"sse_{id(request)}"
            self._logbook.info(
                "session.connect", session=sess_id, transport="sse",
                client=client_ip,
            )
            try:
                async with sse.connect_sse(
                    request.scope, request.receive, request._send,
                ) as streams:
                    await self._server.run(
                        streams[0], streams[1],
                        self._server.create_initialization_options(),
                    )
            except Exception as e:
                # Swallow post-SSE errors so the response to the client
                # stays clean. Log for debugging.
                self._logbook.warning(
                    "session.sse_error", session=sess_id,
                    error=f"{type(e).__name__}: {e}",
                )
            finally:
                # Ensure any subscriptions this session held are cleared,
                # regardless of how the stream ended.
                try:
                    await self._bridge.drop_session(sess_id)
                except Exception as e:
                    self._logbook.warning(
                        "session.drop_error", session=sess_id,
                        error=f"{type(e).__name__}: {e}",
                    )
                self._logbook.info(
                    "session.disconnect", session=sess_id, transport="sse",
                )
            # SSE transport already sent the response; Starlette's Route
            # layer still wants a Response object — return an empty one.
            return Response()

        async def admin_status(request):
            if not self._admin_auth_ok(request):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return JSONResponse(self._status_payload())

        async def admin_shutdown(request):
            if not self._admin_auth_ok(request):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            self._logbook.info("daemon.shutdown_requested", via="admin_api")
            assert self._shutdown_event is not None
            self._shutdown_event.set()
            return JSONResponse({"ok": True})

        async def admin_flush(request):
            if not self._admin_auth_ok(request):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            # Clear the subscription state. Actual UNSUBS to Schwab
            # will be wired when the streamer bridge lands.
            before = self._manager.snapshot()
            self._manager = SubscriptionManager()
            self._logbook.info(
                "daemon.flush",
                previous_sessions=before.get("session_count"),
                previous_subs=before.get("subscription_count"),
            )
            return JSONResponse({"ok": True, "flushed": before})

        # `/messages/` must be Mount, not Route: `sse.handle_post_message`
        # is an ASGI app (scope/receive/send → None) rather than a
        # Starlette endpoint that returns a Response. Wrapping it in
        # Route yields a NoneType-not-callable crash when Starlette
        # tries to treat the None return as a Response.
        routes = [
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
            Route("/admin/status", endpoint=admin_status, methods=["GET"]),
            Route("/admin/shutdown", endpoint=admin_shutdown, methods=["POST"]),
            Route("/admin/flush", endpoint=admin_flush, methods=["POST"]),
        ]
        app = Starlette(routes=routes)

        self._logbook.info("server.start", transport="sse", bind=f"{host}:{port}")
        cfg = uvicorn.Config(
            app, host=host, port=port, log_level="warning", loop="asyncio",
        )
        uvi = uvicorn.Server(cfg)

        async def _watch_shutdown():
            assert self._shutdown_event is not None
            await self._shutdown_event.wait()
            uvi.should_exit = True

        watcher = asyncio.create_task(_watch_shutdown())
        try:
            await uvi.serve()
        finally:
            watcher.cancel()
            self._logbook.info("server.stop", transport="sse")

    def _admin_auth_ok(self, request) -> bool:
        if self._admin_token is None:
            return True
        auth = request.headers.get("authorization", "")
        return auth == f"Bearer {self._admin_token}"

    def _status_payload(self) -> dict[str, Any]:
        session = self._client.session
        return {
            "pid": os.getpid(),
            "uptime_sec": int(time.time() - self._started_at),
            "transport": self._transport,
            "server_name": self._server.name,
            "auth": {
                "access_expires_at": _iso_from_epoch(session.expires_at),
                "refresh_expires_at": _iso_from_epoch(
                    session.refresh_token_expires_at
                ),
            },
            "streamer": {
                "state": self._bridge.streamer_state(),
                "reconnects": self._bridge.reconnect_count(),
            },
            "subscription_summary": self._manager.snapshot(),
        }

    # ---- introspection -------------------------------------------------

    @property
    def subscription_manager(self) -> SubscriptionManager:
        return self._manager

    @property
    def logbook(self) -> LogBook:
        return self._logbook


# ---- helpers ----------------------------------------------------------


def _redact(args: dict[str, Any]) -> dict[str, Any]:
    """Redact secrets from tool args before logging. Current tool
    set has no secrets, but this is the hook for when they appear."""
    return {
        k: ("<redacted>" if k.lower() in {"token", "password"} else v)
        for k, v in args.items()
    }


def _iso_from_epoch(ts: int | None) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.utcfromtimestamp(int(ts)).isoformat() + "Z"
    except (ValueError, OSError):
        return None
