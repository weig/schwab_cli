"""MCP server wiring for Schwab.

Exposes a minimal set of tools over MCP via Streamable HTTP. Wraps
the existing REST API for synchronous queries; streaming tools are
scaffolded here and wired through :class:`SubscriptionManager` but
the actual Schwab-WebSocket → progress-notification bridge is
declared as a TODO for a follow-up commit.

Tools currently live:

* ``get_quote(symbols)`` — REST one-shot quote.
* ``get_chain(symbol, expiry, strike_count)`` — REST chain.
* ``server_status()`` — counts and subscription summary.

The daemon is HTTP-only: ``run_http`` wires the
:class:`SchwabMcpServer` instance through the mcp SDK's
``StreamableHTTPSessionManager`` (single ``/mcp`` endpoint). The
long-lived authenticated session this requires cannot be held over
stdio, so stdio transport is not supported.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import date, datetime
from typing import Any

import anyio
from mcp.server import Server
from mcp.types import TextContent, Tool

from schwab_cli import config as config_module
from schwab_cli.api.client import SchwabClient
from schwab_cli.mcp_server.auth_monitor import (
    DEFAULT_SUBPROCESS_TIMEOUT_SECONDS,
    AuthMonitor,
)
from schwab_cli.mcp_server.logbook import LogBook
from schwab_cli.mcp_server.streamer_bridge import StreamerBridge
from schwab_cli.mcp_server.subscription import SubscriptionManager
from schwab_cli.notify import Notifier
from schwab_cli.service.chains import ChainsService
from schwab_cli.service.quotes import QuoteService
from schwab_cli.service.auth import (
    ApiError,
    NotAuthenticated,
    NotConfigured,
    SessionExpired,
)


class SchwabMcpServer:
    """MCP server object. One instance per daemon process."""

    def __init__(
        self,
        client: SchwabClient,
        logbook: LogBook,
        *,
        server_name: str = "schwab",
        admin_token: str | None = None,
        notifier: Notifier | None = None,
        auth_monitor_enabled: bool = True,
    ) -> None:
        self._client = client
        self._logbook = logbook
        self._manager = SubscriptionManager()
        # Notifier wired into bridge so a streamer crash fires an
        # alert even if no other code paths are listening for it.
        self._notifier = notifier or Notifier.from_file(logbook=logbook)
        self._bridge = StreamerBridge(
            client, logbook, self._manager, notifier=self._notifier,
        )
        self._server = Server(server_name)
        self._started_at = time.time()
        self._admin_token = admin_token
        self._shutdown_event: asyncio.Event | None = None
        self._transport = "idle"
        self._stream_counter = 0
        # Auth monitor — proactive refresh-token rotation. Shares
        # the notifier already built above. The daemon is HTTP-only,
        # so the monitor always runs (started in run_http).
        self._auth_monitor_enabled = auth_monitor_enabled
        self._auth_monitor: AuthMonitor | None = None
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
                Tool(
                    name="dataset.status",
                    description=(
                        "Get current dataset subscription state (tier, "
                        "sources, first/last data date, days). Read-only."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "group":  {"type": "string"},
                            "tier":   {"type": "string"},
                            "source": {"type": "string"},
                            "symbol": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                    },
                ),
                Tool(
                    name="dataset.history",
                    description=(
                        "Get historical volatility snapshots for one "
                        "symbol. Returns up to lookback_days rows "
                        "(default 252, max 730)."
                    ),
                    inputSchema={
                        "type": "object",
                        "required": ["symbol"],
                        "properties": {
                            "symbol": {"type": "string"},
                            "lookback_days": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 730,
                            },
                            "fields": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                    },
                ),
                Tool(
                    name="dataset.iv_rank",
                    description=(
                        "Get IV rank/percentile for one symbol. "
                        "Read-only — uses the most recently stored "
                        "value, never triggers a backfill or chain pull."
                    ),
                    inputSchema={
                        "type": "object",
                        "required": ["symbol"],
                        "properties": {
                            "symbol":   {"type": "string"},
                            "lookback": {
                                "type": "integer",
                                "minimum": 30,
                            },
                        },
                    },
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
                if name.startswith("dataset."):
                    text = dispatch_dataset_tool(name, arguments=arguments)
                    return [TextContent(type="text", text=text)]
                return [TextContent(
                    type="text",
                    text=f"unknown tool: {name}",
                )]
            except (ApiError, SessionExpired) as e:
                self._logbook.error("tool.error", tool=name, error=str(e))
                return [TextContent(
                    type="text",
                    # Same format as the per-tool handlers so log/grep
                    # correlation is consistent across every tool.
                    text=f"schwab error: {type(e).__name__}: {e}",
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
        upcased = [s.upper() for s in symbols]
        try:
            data = QuoteService().get_quote_payload(upcased)
        except (NotConfigured, NotAuthenticated, ApiError, SessionExpired) as e:
            return [TextContent(
                type="text", text=f"schwab error: {type(e).__name__}: {e}",
            )]
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
        try:
            envelope = ChainsService().get_chain_envelope(
                symbol.upper(),
                expiry=expiry,
                strike_count=strike_count,
            )
        except (NotConfigured, NotAuthenticated, ApiError, SessionExpired) as e:
            return [TextContent(
                type="text", text=f"schwab error: {type(e).__name__}: {e}",
            )]
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
        error: Exception | None = None
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
            # Client disconnected / agent cancelled — fall through to the
            # shielded cleanup in `finally` and re-raise after.
            raise
        except Exception as e:
            error = e
            self._logbook.error(
                "stream_quote.error", error=f"{type(e).__name__}: {e}",
            )
        finally:
            # Run cleanup in a shielded scope so the subscription is
            # released even when the surrounding anyio task group has
            # been cancelled (which happens the moment the MCP session's
            # read stream closes — otherwise the next `await` here would
            # be re-cancelled immediately and the subscription leaks).
            with anyio.CancelScope(shield=True):
                await self._bridge.remove_subscription(session_id, token_key)

        if error is not None:
            return [TextContent(
                type="text",
                text=f"stream_quote error: {type(error).__name__}: {error}",
            )]
        # Normal exit only happens when the client disconnects; the
        # CancelledError path re-raises above and this return is never
        # reached in practice. Kept for type-safety.
        return [TextContent(
            type="text",
            text=f"stream_quote ended after {count} updates",
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

    async def run_http(
        self, host: str, port: int, *, extra_routes=None
    ) -> None:
        """Drive the server over Streamable HTTP + HTTP admin endpoints
        on ``host:port`` until a shutdown is signalled.

        Uses Starlette + uvicorn (transitive deps of the ``mcp`` SDK).
        The modern Streamable HTTP transport exposes a single ``/mcp``
        endpoint owned by ``StreamableHTTPSessionManager``; MCP session
        lifecycle (create/route/teardown) is the manager's concern. A
        small set of ``/admin/*`` routes are mounted alongside it for
        status / shutdown control.

        ``extra_routes`` (optional) is a list of Starlette ``Route``
        objects appended to the app's route table — used by the REST PoC
        so its ``/quote/{symbol}`` route shares this server's single
        port. Defaults to ``None`` (no extra routes) so existing callers
        are unaffected.
        """
        # Deferred imports avoid pulling in uvicorn/Starlette when tests
        # only exercise tool handlers.
        import contextlib

        import uvicorn
        from mcp.server.streamable_http_manager import (
            StreamableHTTPSessionManager,
        )
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Mount, Route

        self._transport = "http"
        self._shutdown_event = asyncio.Event()
        # The session manager owns MCP session create/route/teardown. Its
        # task group must be active for the lifetime of the server, so we
        # drive ``run()`` from the Starlette lifespan below.
        session_manager = StreamableHTTPSessionManager(
            app=self._server, json_response=False, stateless=False,
        )

        # Launch the auth monitor — proactive refresh-token rotation
        # + expiry notifications. The daemon is long-lived, so the
        # monitor always runs alongside the HTTP transport.
        if self._auth_monitor_enabled:
            self._auth_monitor = AuthMonitor(
                self._logbook,
                self._notifier,
                subprocess_timeout_seconds=_resolve_auth_subprocess_timeout(
                    self._logbook,
                ),
                on_rotation_success=self._on_rotation_success,
            )
            self._auth_monitor.start()

        async def handle_mcp(scope, receive, send):
            # Bare ASGI app: the session manager reads/writes the stream
            # directly and returns None, so this is Mount-ed (not Route-d).
            await session_manager.handle_request(scope, receive, send)

        # NOTE: Unlike the old SSE path, there is no per-request session
        # teardown hook here — ``StreamableHTTPSessionManager`` owns MCP
        # session lifecycle. The ``stream_quote`` tool + ``StreamerBridge``
        # still hold per-session subscription state keyed off the MCP
        # session id, but fine-grained bridge cleanup-on-disconnect is
        # DEFERRED: a client disconnect no longer eagerly drops its
        # subscriptions. This is acceptable for 3b — the `/admin/flush`
        # endpoint plus a process restart still clear all stream state.

        async def health(request):
            # Unauthenticated liveness probe — powers `server status`'s
            # real /health reachability check. Mirrors the REST app's
            # /health so both daemons answer the same shape.
            return JSONResponse({"ok": True})

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

        @contextlib.asynccontextmanager
        async def _lifespan(app):
            # The manager's task group must stay active for the whole
            # server lifetime — hold it open across the lifespan.
            async with session_manager.run():
                yield

        # `/mcp` must be Mount, not Route: `session_manager.handle_request`
        # is an ASGI app (scope/receive/send → None) rather than a
        # Starlette endpoint that returns a Response. Wrapping it in
        # Route yields a NoneType-not-callable crash when Starlette
        # tries to treat the None return as a Response.
        routes = [
            Mount("/mcp", app=handle_mcp),
            Route("/health", endpoint=health, methods=["GET"]),
            Route("/admin/status", endpoint=admin_status, methods=["GET"]),
            Route("/admin/shutdown", endpoint=admin_shutdown, methods=["POST"]),
            Route("/admin/flush", endpoint=admin_flush, methods=["POST"]),
        ]
        if extra_routes:
            routes.extend(extra_routes)
        app = Starlette(routes=routes, lifespan=_lifespan)

        self._logbook.info(
            "server.start", transport="http", bind=f"{host}:{port}",
        )
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
            # Stop the auth monitor cleanly on shutdown so the
            # subprocess (if mid-rotation) can wind down.
            if self._auth_monitor is not None:
                await self._auth_monitor.stop()
            self._logbook.info("server.stop", transport="http")

    async def _on_rotation_success(self) -> None:
        """Called by the auth monitor after a successful rotation.

        Reloads ``session.json`` from disk (the monitor's subprocess
        wrote the fresh tokens there) and nudges the streamer bridge
        to restart its Schwab WebSocket with the new access token.
        Subscriptions are preserved through the refcount table; the
        bridge will resubscribe the aggregate symbol set on reconnect.
        """
        # Deferred import to avoid an import cycle with cli.py.
        from schwab_cli.session import load as load_session

        new_session = load_session()
        if new_session is None:
            self._logbook.warning(
                "rotation.reload_missing_session",
            )
            return
        # Mutate the client's session in place so any in-flight REST
        # calls use the fresh token too. SchwabClient exposes it via
        # the `session` property but reassignment happens through its
        # private attr.
        self._client._session = new_session  # noqa: SLF001
        self._logbook.info("rotation.session_reloaded")

        # If the streamer is currently connected, reconnect it with
        # the new access token. The bridge preserves the active
        # subscription set via the refcount table, so subscribers'
        # queues keep flowing data after the brief reconnect gap.
        if self._bridge.streamer_state() == "connected":
            try:
                await self._bridge.reconnect_after_rotation()
            except Exception as e:
                self._logbook.error(
                    "rotation.reconnect_failed",
                    error=f"{type(e).__name__}: {e}",
                )

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


def dispatch_dataset_tool(name: str, *, arguments: dict) -> str:
    """Read-only dispatcher for dataset.* MCP tools.

    Returns a JSON string the Starlette/MCP wrapper hands back as
    TextContent. No writes — the cron is the only writer.
    """
    import json as _json
    from schwab_cli.storage import vol_history
    from schwab_cli.dataset.store import read_status_rows

    if name == "dataset.status":
        with vol_history.connect() as conn:
            rows = read_status_rows(
                conn,
                group_name=arguments.get("group", "volatility"),
                tier=arguments.get("tier"),
                source=arguments.get("source"),
                symbols=arguments.get("symbol"),
            )
        return _json.dumps(rows, indent=2, default=str)

    if name == "dataset.history":
        symbol = arguments["symbol"]
        lookback = min(int(arguments.get("lookback_days", 252)), 730)
        fields = arguments.get("fields") or ["atm_iv_30d", "hv_30d"]
        cols = "captured_at_ms, archive_date, " + ", ".join(fields)
        with vol_history.connect() as conn:
            rows = conn.execute(
                f"SELECT {cols} FROM vol_snapshots "
                f"WHERE symbol = ? "
                f"ORDER BY captured_at_ms DESC LIMIT ?",
                (symbol, lookback),
            ).fetchall()
        return _json.dumps([dict(r) for r in rows], indent=2, default=str)

    if name == "dataset.iv_rank":
        from schwab_cli.service.vol import compute_iv_rank_and_percentile
        with vol_history.connect() as conn:
            recent = conn.execute(
                "SELECT atm_iv_30d, atm_iv FROM vol_snapshots "
                "WHERE symbol = ? AND atm_iv > 0 "
                "ORDER BY captured_at_ms DESC LIMIT 1",
                (arguments["symbol"],),
            ).fetchone()
            if recent is None:
                return _json.dumps({"ivr": None, "ivp": None,
                                    "low_history": True})
            out = compute_iv_rank_and_percentile(
                conn, symbol=arguments["symbol"],
                today_iv_30d=recent["atm_iv_30d"],
                today_atm_iv=recent["atm_iv"],
                lookback=int(arguments.get("lookback", 252)),
                backfill_callable=None,
            )
        return _json.dumps(out, indent=2)

    raise ValueError(f"unknown dataset tool: {name}")


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


# Outer subprocess kill must be longer than the inner auto-login budget
# so webauto isn't SIGKILLed mid-flow. The buffer covers the post-webauto
# OAuth code-for-token exchange + session.json write.
_AUTH_SUBPROCESS_BUFFER_SECONDS = 30


def _resolve_auth_subprocess_timeout(logbook: LogBook) -> int:
    """Pick the outer ``schwab_cli auth --force`` subprocess timeout.

    Reads ``auto_login_timeout_seconds`` from config and adds a fixed
    buffer for the post-webauto token exchange. Falls back to the module
    default if the config can't be loaded — the monitor still runs, just
    with the old 2-minute envelope.
    """
    try:
        cfg = config_module.load()
    except config_module.ConfigError as e:
        logbook.warning(
            "auth_monitor.config_load_failed", error=str(e),
            fallback_seconds=DEFAULT_SUBPROCESS_TIMEOUT_SECONDS,
        )
        return DEFAULT_SUBPROCESS_TIMEOUT_SECONDS
    if cfg is None:
        return DEFAULT_SUBPROCESS_TIMEOUT_SECONDS
    return cfg.auto_login_timeout_seconds + _AUTH_SUBPROCESS_BUFFER_SECONDS
