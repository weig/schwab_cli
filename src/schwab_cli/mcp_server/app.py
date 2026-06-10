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
import math
import os
import time
from datetime import date, datetime
from typing import Any

import anyio
from mcp.server import Server
from mcp.types import TextContent, Tool

from schwab_cli.api import orders as api_orders
from schwab_cli.api.client import SchwabClient
from schwab_cli.mcp_server.logbook import LogBook
from schwab_cli.mcp_server.streamer_bridge import StreamerBridge
from schwab_cli.mcp_server.subscription import SubscriptionManager
from schwab_cli.history_spec import parse_interval, parse_range
from schwab_cli.notify import Notifier
from schwab_cli.service.accounts import AccountsService
from schwab_cli.service.chains import ChainsService
from schwab_cli.service.dividends import DividendsService
from schwab_cli.service.fundamentals import FundamentalsService
from schwab_cli.service.greeks import GreeksService
from schwab_cli.service.history import HistoryService
from schwab_cli.service.quotes import QuoteService
from schwab_cli.service.skew import SkewService
from schwab_cli.service.transactions import TransactionsService
from schwab_cli.service.vol import VolService
from schwab_cli.service.auth import (
    ApiError,
    NotAuthenticated,
    NotConfigured,
    SessionExpired,
)


_DEFAULT_IDLE_LINGER_S = 45.0


def _idle_linger_default() -> float:
    """Idle-linger seconds for the shared streamer, from
    ``SCHWAB_STREAMER_IDLE_LINGER_S`` (default 45). Keeps the socket open
    briefly after the last subscriber leaves so a quick re-subscribe
    reuses it. ``0`` restores close-immediately. Invalid values fall back
    to the default rather than crashing the daemon."""
    raw = os.environ.get("SCHWAB_STREAMER_IDLE_LINGER_S")
    if raw is None:
        return _DEFAULT_IDLE_LINGER_S
    try:
        val = float(raw)
    except ValueError:
        return _DEFAULT_IDLE_LINGER_S
    # Reject negatives AND non-finite (inf/nan): asyncio.sleep(inf) would
    # mean the socket never auto-closes.
    return val if (math.isfinite(val) and val >= 0) else _DEFAULT_IDLE_LINGER_S


class _ToolArgError(Exception):
    """Bad tool input (wrong type / out of range). Caught by ``_dispatch``
    and returned as a clean message instead of a generic internal error."""


def _as_jsonable(obj: Any) -> Any:
    """Best-effort convert a service result attribute to a JSON-serializable
    structure. Dataclass instances → ``asdict``; Mappings/lists pass through
    (``json.dumps(default=str)`` handles leaf types). Guards the loosely-typed
    ``SkewResult.metrics`` against ever emitting a stringified object."""
    import dataclasses
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    return obj


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
        idle_linger_s: float | None = None,
    ) -> None:
        self._client = client
        self._logbook = logbook
        self._manager = SubscriptionManager()
        # Notifier wired into bridge so a streamer crash fires an
        # alert even if no other code paths are listening for it.
        self._notifier = notifier or Notifier.from_file(logbook=logbook)
        self._bridge = StreamerBridge(
            client, logbook, self._manager, notifier=self._notifier,
            idle_linger_s=(
                _idle_linger_default() if idle_linger_s is None
                else idle_linger_s
            ),
        )
        self._server = Server(server_name)
        self._started_at = time.time()
        self._admin_token = admin_token
        self._shutdown_event: asyncio.Event | None = None
        self._transport = "idle"
        self._stream_counter = 0
        # Token maintenance lives in the daemon's TokenManager (attached
        # by commands/server.py); the server only exposes its /auth/*
        # surface and receives session handoffs from its threads.
        self._token_manager = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._register_tools()

    def attach_token_manager(self, token_manager) -> None:
        """Attach the daemon's TokenManager (enables the /auth/* routes)."""
        self._token_manager = token_manager

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
                    name="get_vol",
                    description=(
                        "Volatility snapshot for one underlying: ATM implied "
                        "vol, IV rank/percentile (vs stored history), and "
                        "historical vol. Read-only — pulls a fresh chain but "
                        "does NOT record a snapshot. Returns JSON."
                    ),
                    inputSchema={
                        "type": "object",
                        "required": ["symbol"],
                        "properties": {
                            "symbol": {"type": "string"},
                            "hv_window": {"type": "integer", "default": 30},
                            "hv_lookback": {"type": "integer", "default": 252},
                            "ivp_lookback": {"type": "integer", "default": 252},
                        },
                    },
                ),
                Tool(
                    name="get_skew",
                    description=(
                        "Option skew metrics for one underlying at one expiry "
                        "(put/call IV skew across strikes around ATM). Returns "
                        "JSON."
                    ),
                    inputSchema={
                        "type": "object",
                        "required": ["symbol", "expiry"],
                        "properties": {
                            "symbol": {"type": "string"},
                            "expiry": {
                                "type": "string",
                                "description": "ISO date YYYY-MM-DD.",
                            },
                            "strikes": {"type": "integer", "default": 20},
                        },
                    },
                ),
                Tool(
                    name="get_greeks",
                    description=(
                        "Greeks (delta/gamma/theta/vega/rho) + IV for one "
                        "option contract (underlying + strike + expiry + "
                        "side). Returns JSON."
                    ),
                    inputSchema={
                        "type": "object",
                        "required": ["underlying", "strike", "expiry", "side"],
                        "properties": {
                            "underlying": {"type": "string"},
                            "strike": {"type": "number"},
                            "expiry": {
                                "type": "string",
                                "description": "ISO date YYYY-MM-DD.",
                            },
                            "side": {
                                "type": "string",
                                "enum": ["C", "P"],
                                "description": "C=call, P=put.",
                            },
                        },
                    },
                ),
                Tool(
                    name="get_history",
                    description=(
                        "Historical OHLCV candles for one symbol. Returns "
                        "JSON. `interval` ∈ 1min/5min/10min/15min/30min/"
                        "1day/1wk/1mo. `range` is ytd/mtd/wtd, or "
                        "'<start>..<end>' where each endpoint is YYYYMMDD, "
                        "a relative -Nd/-Nw/-Nmo/-Ny, or now (e.g. -1y..now)."
                    ),
                    inputSchema={
                        "type": "object",
                        "required": ["symbol"],
                        "properties": {
                            "symbol": {"type": "string"},
                            "interval": {"type": "string", "default": "1day"},
                            "range": {"type": "string", "default": "-1y..now"},
                        },
                    },
                ),
                Tool(
                    name="get_dividends",
                    description=(
                        "Dividend yield / amount / ex-date fields for one or "
                        "more symbols (from the Schwab quote fundamental "
                        "block). Returns JSON."
                    ),
                    inputSchema={
                        "type": "object",
                        "required": ["symbols"],
                        "properties": {
                            "symbols": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                    },
                ),
                Tool(
                    name="get_fundamentals",
                    description=(
                        "Fundamental fields (P/E, EPS, market cap, 52-week "
                        "range, margins, etc.) for one or more symbols. "
                        "Returns JSON."
                    ),
                    inputSchema={
                        "type": "object",
                        "required": ["symbols"],
                        "properties": {
                            "symbols": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                    },
                ),
                Tool(
                    name="get_accounts",
                    description=(
                        "List all linked Schwab accounts with balances "
                        "(account numbers, type, equity, cash). Returns "
                        "JSON. Account-level financial data."
                    ),
                    inputSchema={"type": "object", "properties": {}},
                ),
                Tool(
                    name="get_account",
                    description=(
                        "Balances + detail for one account. `account` is the "
                        "account number or a unique suffix. Returns JSON."
                    ),
                    inputSchema={
                        "type": "object",
                        "required": ["account"],
                        "properties": {"account": {"type": "string"}},
                    },
                ),
                Tool(
                    name="get_positions",
                    description=(
                        "Open positions (symbol, qty, avg price, market "
                        "value, P/L). `account` optional — omit for all "
                        "accounts. Returns JSON."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {"account": {"type": "string"}},
                    },
                ),
                Tool(
                    name="get_transactions",
                    description=(
                        "Transaction history (trades, dividends, fees). "
                        "`account` optional (all accounts if omitted). "
                        "`range` is ytd/mtd/wtd or '<start>..<end>' "
                        "(YYYYMMDD / -Nd / now). `type` filters by Schwab "
                        "transaction type (comma-separated; empty=all). "
                        "Returns JSON."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "account": {"type": "string"},
                            "range": {"type": "string", "default": "-30d..now"},
                            "type": {"type": "string", "default": ""},
                        },
                    },
                ),
                Tool(
                    name="get_order",
                    description=(
                        "Fetch one order by id for an account. Read-only "
                        "(does not place/modify). Returns JSON."
                    ),
                    inputSchema={
                        "type": "object",
                        "required": ["account", "order_id"],
                        "properties": {
                            "account": {"type": "string"},
                            "order_id": {"type": "string"},
                        },
                    },
                ),
                Tool(
                    name="list_orders",
                    description=(
                        "List orders. `account` optional (all accounts if "
                        "omitted). `range` is ytd/mtd/wtd or '<start>..<end>' "
                        "(≤60-day window). `status` optional Schwab status "
                        "enum (e.g. FILLED, WORKING, CANCELED). "
                        "`max_results` optional. Read-only. Returns JSON."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "account": {"type": "string"},
                            "range": {"type": "string", "default": "-7d..now"},
                            "status": {"type": "string"},
                            "max_results": {"type": "integer"},
                        },
                    },
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
            return await self._dispatch(name, arguments)

    # ---- tool handlers -------------------------------------------------

    async def _dispatch(
        self, name: str, arguments: dict[str, Any]
    ) -> list[TextContent]:
        """Route a tool call to its handler, converting schwab/auth errors
        and unexpected exceptions into ``TextContent`` (never raises) so a
        failing tool returns a message instead of dropping the MCP call.
        Extracted from the ``call_tool`` closure to be unit-testable."""
        try:
            if name == "get_quote":
                return await self._tool_get_quote(arguments)
            if name == "get_chain":
                return await self._tool_get_chain(arguments)
            if name == "get_vol":
                return await self._tool_get_vol(arguments)
            if name == "get_skew":
                return await self._tool_get_skew(arguments)
            if name == "get_greeks":
                return await self._tool_get_greeks(arguments)
            if name == "get_history":
                return await self._tool_get_history(arguments)
            if name == "get_dividends":
                return await self._tool_get_dividends(arguments)
            if name == "get_fundamentals":
                return await self._tool_get_fundamentals(arguments)
            if name == "get_accounts":
                return await self._tool_get_accounts(arguments)
            if name == "get_account":
                return await self._tool_get_account(arguments)
            if name == "get_positions":
                return await self._tool_get_positions(arguments)
            if name == "get_transactions":
                return await self._tool_get_transactions(arguments)
            if name == "get_order":
                return await self._tool_get_order(arguments)
            if name == "list_orders":
                return await self._tool_list_orders(arguments)
            if name == "stream_quote":
                return await self._tool_stream_quote(arguments)
            if name == "server_status":
                return await self._tool_server_status()
            if name.startswith("dataset."):
                text = dispatch_dataset_tool(name, arguments=arguments)
                return [TextContent(type="text", text=text)]
            return [TextContent(type="text", text=f"unknown tool: {name}")]
        except _ToolArgError as e:
            return [TextContent(type="text", text=str(e))]
        except (
            ApiError, SessionExpired, NotConfigured, NotAuthenticated,
        ) as e:
            self._logbook.error("tool.error", tool=name, error=str(e))
            return [TextContent(
                type="text",
                text=f"schwab error: {type(e).__name__}: {e}",
            )]
        except Exception as e:  # noqa: BLE001 — surface, never drop the call
            self._logbook.error(
                "tool.error", tool=name, error=f"{type(e).__name__}: {e}"
            )
            return [TextContent(
                type="text",
                text=f"internal error: {type(e).__name__}: {e}",
            )]

    async def _tool_get_quote(self, args: dict[str, Any]) -> list[TextContent]:
        symbols = args.get("symbols") or []
        if not isinstance(symbols, list) or not all(isinstance(s, str) for s in symbols):
            return [TextContent(
                type="text", text="symbols must be a list of strings",
            )]
        if not symbols:
            return [TextContent(type="text", text="symbols list is empty")]
        upcased = [s.upper() for s in symbols]
        # Auth/API errors bubble to _dispatch (uniform "schwab error" text).
        data = QuoteService().get_quote_payload(upcased)
        return [TextContent(type="text", text=json.dumps(data, default=str))]

    async def _tool_get_chain(self, args: dict[str, Any]) -> list[TextContent]:
        symbol = args.get("symbol")
        expiry_str = args.get("expiry")
        strike_count = self._int_arg(args, "strike_count", 20)
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
        envelope = ChainsService().get_chain_envelope(
            symbol.upper(),
            expiry=expiry,
            strike_count=strike_count,
        )
        return [TextContent(type="text", text=json.dumps(envelope, default=str))]

    @staticmethod
    def _err_text(msg: str) -> list[TextContent]:
        return [TextContent(type="text", text=msg)]

    @staticmethod
    def _json_text(data: Any) -> list[TextContent]:
        return [TextContent(type="text", text=json.dumps(data, default=str))]

    @staticmethod
    def _symbol_list(args: dict[str, Any]) -> list[str] | None:
        symbols = args.get("symbols") or []
        if not isinstance(symbols, list) or not all(
            isinstance(s, str) for s in symbols
        ):
            return None
        return [s.upper() for s in symbols]

    @staticmethod
    def _int_arg(
        args: dict[str, Any], key: str, default: int | None
    ) -> int | None:
        """Parse an int arg. Missing (``None``) → default; ``0`` is kept
        (not treated as missing). Bad type → :class:`_ToolArgError` (clean
        message, not a generic "internal error")."""
        v = args.get(key)
        if v is None:
            return default
        try:
            return int(v)
        except (TypeError, ValueError):
            raise _ToolArgError(f"{key} must be an integer")

    @staticmethod
    def _float_arg(args: dict[str, Any], key: str) -> float:
        if key not in args or args[key] is None:
            raise _ToolArgError(f"{key} is required")
        try:
            return float(args[key])
        except (TypeError, ValueError):
            raise _ToolArgError(f"{key} must be a number")

    async def _tool_get_vol(self, args: dict[str, Any]) -> list[TextContent]:
        symbol = args.get("symbol")
        if not symbol or not isinstance(symbol, str):
            return self._err_text("symbol is required")
        # no_record=True: an ad-hoc MCP read must not write a snapshot into
        # the IV history (that history backs IVR/IVP and is owned by the
        # scheduled `vol` job).
        result = VolService().get_vol(
            symbol.upper(),
            hv_window=self._int_arg(args, "hv_window", 30),
            hv_lookback=self._int_arg(args, "hv_lookback", 252),
            ivp_lookback=self._int_arg(args, "ivp_lookback", 252),
            no_record=True,
        )
        if result is None:
            return self._err_text(f"no vol data for {symbol.upper()}")
        return self._json_text(result.envelope)

    async def _tool_get_skew(self, args: dict[str, Any]) -> list[TextContent]:
        symbol = args.get("symbol")
        expiry_str = args.get("expiry")
        if not symbol or not expiry_str:
            return self._err_text("symbol and expiry are required")
        try:
            expiry = date.fromisoformat(expiry_str)
        except (ValueError, TypeError):
            return self._err_text(
                f"invalid expiry {expiry_str!r} (need YYYY-MM-DD)"
            )
        result = SkewService().get_skew_l1(
            symbol.upper(), expiry, strikes=self._int_arg(args, "strikes", 20),
        )
        return self._json_text(_as_jsonable(result.metrics))

    async def _tool_get_greeks(self, args: dict[str, Any]) -> list[TextContent]:
        underlying = args.get("underlying")
        strike = args.get("strike")
        expiry_str = args.get("expiry")
        side = (args.get("side") or "").upper()
        if not underlying or strike is None or not expiry_str:
            return self._err_text(
                "underlying, strike and expiry are required"
            )
        if side not in ("C", "P"):
            return self._err_text("side must be 'C' or 'P'")
        try:
            expiry = date.fromisoformat(expiry_str)
        except (ValueError, TypeError):
            return self._err_text(
                f"invalid expiry {expiry_str!r} (need YYYY-MM-DD)"
            )
        result = GreeksService().get_greeks(
            underlying.upper(),
            strike=self._float_arg(args, "strike"),
            expiry=expiry,
            side=side,
        )
        return self._json_text(result.envelope)

    async def _tool_get_history(self, args: dict[str, Any]) -> list[TextContent]:
        symbol = args.get("symbol")
        if not symbol or not isinstance(symbol, str):
            return self._err_text("symbol is required")
        interval_str = args.get("interval") or "1day"
        range_str = args.get("range") or "-1y..now"
        try:
            interval = parse_interval(interval_str)
        except ValueError as e:
            return self._err_text(f"invalid interval {interval_str!r}: {e}")
        try:
            start, end = parse_range(range_str)
        except ValueError as e:
            return self._err_text(f"invalid range {range_str!r}: {e}")
        result = HistoryService().get_history(
            symbol.upper(),
            frequency_type=interval.frequency_type,
            frequency=interval.frequency,
            label=interval.label,
            start=start,
            end=end,
            range_str=range_str,
        )
        return self._json_text(result.envelope)

    async def _tool_get_dividends(
        self, args: dict[str, Any]
    ) -> list[TextContent]:
        symbols = self._symbol_list(args)
        if symbols is None:
            return self._err_text("symbols must be a list of strings")
        if not symbols:
            return self._err_text("symbols list is empty")
        result = DividendsService().get_dividends(symbols)
        return self._json_text(result.payload)

    async def _tool_get_fundamentals(
        self, args: dict[str, Any]
    ) -> list[TextContent]:
        symbols = self._symbol_list(args)
        if symbols is None:
            return self._err_text("symbols must be a list of strings")
        if not symbols:
            return self._err_text("symbols list is empty")
        result = FundamentalsService().get_fundamentals(symbols)
        return self._json_text(result.payload)

    # ---- Tier B: account data (read-only, financial PII) ---------------

    async def _tool_get_accounts(
        self, args: dict[str, Any]
    ) -> list[TextContent]:
        result = AccountsService().list_accounts()
        return self._json_text([dict(a) for a in result.accounts])

    async def _tool_get_account(
        self, args: dict[str, Any]
    ) -> list[TextContent]:
        account = args.get("account")
        if not account or not isinstance(account, str):
            return self._err_text("account is required")
        result = AccountsService().get_account(account)
        return self._json_text(dict(result.account))

    async def _tool_get_positions(
        self, args: dict[str, Any]
    ) -> list[TextContent]:
        account = args.get("account")  # None → all accounts
        if account is not None and not isinstance(account, str):
            return self._err_text("account must be a string")
        result = AccountsService().get_positions(account)
        return self._json_text([dict(p) for p in result.positions])

    async def _tool_get_transactions(
        self, args: dict[str, Any]
    ) -> list[TextContent]:
        account = args.get("account")  # None → all accounts
        if account is not None and not isinstance(account, str):
            return self._err_text("account must be a string")
        range_str = args.get("range") or "-30d..now"
        try:
            start, end = parse_range(range_str)
        except ValueError as e:
            return self._err_text(f"invalid range {range_str!r}: {e}")
        result = TransactionsService().get_transactions(
            account,
            start=start,
            end=end,
            type_filter=args.get("type") or "",
        )
        return self._json_text([dict(r) for r in result.rows])

    # ---- Tier C: order reads (no mutation) -----------------------------

    async def _tool_get_order(
        self, args: dict[str, Any]
    ) -> list[TextContent]:
        account = args.get("account")
        order_id = args.get("order_id")
        if not account or not order_id:
            return self._err_text("account and order_id are required")
        acct = self._client.resolve_account(str(account))
        order = api_orders.get_order(
            self._client, acct.hash_value, str(order_id),
        )
        return self._json_text(order)

    async def _tool_list_orders(
        self, args: dict[str, Any]
    ) -> list[TextContent]:
        account = args.get("account")  # None → all accounts
        if account is not None and not isinstance(account, str):
            return self._err_text("account must be a string")
        range_str = args.get("range") or "-7d..now"
        try:
            start, end = parse_range(range_str)
        except ValueError as e:
            return self._err_text(f"invalid range {range_str!r}: {e}")
        status = args.get("status") or None
        max_results = self._int_arg(args, "max_results", None)
        if account:
            acct = self._client.resolve_account(account)
            orders = api_orders.list_orders_for_account(
                self._client, acct.hash_value,
                start=start, end=end, status=status, max_results=max_results,
            )
        else:
            orders = api_orders.list_orders_all_accounts(
                self._client,
                start=start, end=end, status=status, max_results=max_results,
            )
        return self._json_text(orders)

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
        self, host: str, port: int, *, extra_routes=None, asgi_wrap=None
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
        # Captured so TokenManager threads can schedule the async session
        # handoff (streamer reconnect) onto this loop from outside it.
        self._loop = asyncio.get_running_loop()
        # The session manager owns MCP session create/route/teardown. Its
        # task group must be active for the lifetime of the server, so we
        # drive ``run()`` from the Starlette lifespan below.
        session_manager = StreamableHTTPSessionManager(
            app=self._server, json_response=False, stateless=False,
        )

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
            # Token surface for out-of-process consumers (service layer).
            # Loopback-bound and metadata-only (no token material), so
            # unauthenticated like /health.
            Route("/auth/refresh", endpoint=self._auth_refresh, methods=["POST"]),
            Route("/auth/status", endpoint=self._auth_status, methods=["GET"]),
        ]
        if extra_routes:
            routes.extend(extra_routes)
        app = Starlette(routes=routes, lifespan=_lifespan)
        # The webauth gate (peer allowlist + JWT on /api, loopback-only
        # for everything else) wraps the WHOLE app so a wide bind can
        # never expose the internal control plane.
        served_app = asgi_wrap(app) if asgi_wrap is not None else app

        self._logbook.info(
            "server.start", transport="http", bind=f"{host}:{port}",
        )
        cfg = uvicorn.Config(
            served_app, host=host, port=port, log_level="warning",
            loop="asyncio",
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
            # Tear down the shared streamer: cancels any pending idle-close
            # timer (else a lingering task is destroyed as the loop closes,
            # logging an asyncio warning) and closes the socket.
            await self._bridge.close()
            # Cleared only AFTER the bridge close so a TokenManager thread
            # handing off a session mid-shutdown still schedules onto a
            # live loop for as long as one exists; once cleared (or if the
            # loop stops first), schedule_session_replaced falls back to a
            # bare rebind — correct, since the streamer is gone anyway.
            self._loop = None
            self._logbook.info("server.stop", transport="http")

    # ---- token surface (TokenManager attach + /auth routes) -------------

    async def _auth_refresh(self, request):
        """``POST /auth/refresh`` — on-demand single-flight token exchange.

        The TokenManager collapses concurrent callers onto one Schwab
        round-trip; an ``invalid_grant`` kicks its recovery track and
        reports failure immediately rather than blocking on a browser
        flow. Responses carry expiry metadata only — never token values.
        """
        from starlette.responses import JSONResponse

        tm = self._token_manager
        if tm is None:
            return JSONResponse(
                {"ok": False, "error": "token manager not attached"},
                status_code=503,
            )
        # to_thread: force_exchange blocks on the Schwab HTTP round-trip
        # (and possibly on another in-flight exchange). Its success path
        # fires on_session_replaced → schedule_session_replaced, which
        # enqueues handle_session_replaced back onto THIS loop via
        # run_coroutine_threadsafe — safe (it runs after this await
        # resumes), as long as the bridge never shares locks with the
        # exchange path (it doesn't: bridge locks are asyncio-side only).
        fresh = await asyncio.to_thread(tm.force_exchange)
        payload = {"ok": fresh is not None, **tm.state()}
        return JSONResponse(payload, status_code=200 if fresh else 503)

    async def _auth_status(self, request):
        """``GET /auth/status`` — TokenManager state snapshot."""
        from starlette.responses import JSONResponse

        tm = self._token_manager
        if tm is None:
            return JSONResponse(
                {"error": "token manager not attached"}, status_code=503,
            )
        return JSONResponse({**tm.state(), "now": int(time.time())})

    def schedule_session_replaced(self, fresh) -> None:
        """Cross-thread session handoff (TokenManager threads call this).

        With the HTTP loop running, schedule the full async handoff
        (rebind + conditional streamer reconnect) onto it; before/after
        the loop's lifetime just rebind so in-flight REST calls still
        pick up the fresh token.
        """
        loop = self._loop
        if loop is None or not loop.is_running():
            self._client._session = fresh  # noqa: SLF001
            return
        coro = self.handle_session_replaced(fresh)
        try:
            asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            # TOCTOU on is_running(): the loop stopped between the check
            # and the call (uvicorn shutdown). The streamer is being torn
            # down with it, so a bare rebind is the complete handoff.
            coro.close()  # never scheduled — avoid a never-awaited warning
            self._client._session = fresh  # noqa: SLF001

    async def handle_session_replaced(self, fresh) -> None:
        """Adopt a replaced session: rebind the client's in-memory copy
        and reconnect the streamer ONLY on a full rotation.

        An access-only exchange (~every 15 min) keeps the refresh token,
        and Schwab websockets stay valid across it — bouncing the shared
        streamer that often would punch holes in every subscriber's
        stream. A full rotation (new refresh token) does reconnect; the
        bridge preserves subscriptions via its refcount table.
        """
        old = self._client.session
        rotated = old is None or old.refresh_token != fresh.refresh_token
        # Atomic attribute rebind so in-flight REST calls use the fresh
        # token. SchwabClient exposes `session` read-only; reassignment
        # goes through the private attr by design.
        self._client._session = fresh  # noqa: SLF001
        self._logbook.info("rotation.session_reloaded", full_rotation=rotated)
        if not rotated:
            return
        try:
            await self._bridge.reconnect_after_rotation()
        except Exception as e:  # noqa: BLE001 — keep the daemon alive
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
        if not isinstance(fields, list) or not all(
            isinstance(f, str) for f in fields
        ):
            return _json.dumps({"error": "fields must be a list of strings"})
        with vol_history.connect() as conn:
            # `fields` is interpolated into the SQL below, and this path is
            # reachable from any MCP client — validate every requested
            # column against the real schema (allowlist) to prevent SQL
            # injection.
            valid_cols = {
                r["name"]
                for r in conn.execute(
                    "PRAGMA table_info(vol_snapshots)"
                ).fetchall()
            }
            bad = [f for f in fields if f not in valid_cols]
            if bad:
                return _json.dumps({"error": f"unknown field(s): {bad}"})
            cols = "captured_at_ms, archive_date, " + ", ".join(fields)
            rows = conn.execute(
                f"SELECT {cols} FROM vol_snapshots "  # noqa: S608 — allowlisted
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
