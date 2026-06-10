"""REST surface.

Two route sets:

* :func:`rest_routes` — the original loopback PoC (``/health``,
  ``/quote/{symbol}``), unauthenticated; the webauth middleware's tier-2
  rule confines them to loopback peers.
* :func:`api_routes` — the public resource-server surface under
  ``/api/v1/``. The webauth middleware authenticates the caller and
  attaches a Principal; each route declares its required scope via
  :func:`schwab_cli.webauth.middleware.scope_denial`:

  ========================  =================================
  scope                     endpoints
  ========================  =================================
  ``marketdata``            quote, chain, history, vol, skew,
                            greeks, dividends, fundamentals
  ``accounts``              accounts list / detail
  ``positions``             positions
  ``transactions``          transactions
  ``orders``                order reads
  ``dataset``               local vol-history dataset reads
  ========================  =================================

All handlers are thin wrappers over the SERVICE layer, run in a worker
thread (the services do blocking HTTP), and map service errors onto
HTTP statuses: bad params → 400, not configured/authenticated → 503,
upstream Schwab failures → 502.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import json
from datetime import date
from typing import Any, Callable

from schwab_cli.api import orders as api_orders
from schwab_cli.history_spec import parse_interval, parse_range
from schwab_cli.service.accounts import AccountsService
from schwab_cli.service.auth import (
    ApiError,
    NotAuthenticated,
    NotConfigured,
    SessionExpired,
)
from schwab_cli.service import ServiceError
from schwab_cli.service.base import BaseService
from schwab_cli.service.chains import ChainsService
from schwab_cli.service.dividends import DividendsService
from schwab_cli.service.fundamentals import FundamentalsService
from schwab_cli.service.greeks import GreeksService
from schwab_cli.service.history import HistoryService
from schwab_cli.service.quotes import QuoteService
from schwab_cli.service.skew import SkewService
from schwab_cli.service.transactions import TransactionsService
from schwab_cli.service.vol import VolService
from schwab_cli.webauth.middleware import scope_denial


class _BadParam(Exception):
    """Invalid request parameter — mapped to HTTP 400."""


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------


async def _health(request):
    return _json_response({"ok": True})


def _endpoint(scope_name: str, fn: Callable[[Any], Any]):
    """Build an async handler: scope check → worker thread → error map.

    ``fn(request)`` is synchronous, extracts/validates params (raising
    :class:`_BadParam`), calls the service layer, and returns a
    JSON-able payload.
    """

    @functools.wraps(fn)
    async def handler(request):
        denial = scope_denial(request, scope_name)
        if denial is not None:
            return denial
        try:
            # Services do blocking HTTP — never run them on the loop.
            payload = await asyncio.to_thread(fn, request)
        except _BadParam as e:
            return _json_response({"error": str(e)}, status_code=400)
        except (NotConfigured, NotAuthenticated) as e:
            return _json_response({"error": _err(e)}, status_code=503)
        except (ApiError, SessionExpired) as e:
            return _json_response({"error": _err(e)}, status_code=502)
        except ServiceError as e:
            # Any other service-layer failure (NoVolData, storage errors,
            # ...) is an upstream/data problem, not a server crash — keep
            # the 500-with-traceback path for genuine bugs only.
            return _json_response({"error": _err(e)}, status_code=502)
        return _json_response(payload)

    return handler


def _err(e: Exception) -> str:
    """Format an exception for the error body. Some service exceptions are
    raised bare (no message), so fall back to the class name rather than
    emitting a misleading ``"NotConfigured: "`` with a trailing colon."""
    detail = str(e)
    return f"{type(e).__name__}: {detail}" if detail else type(e).__name__


def _json_response(data, *, status_code: int = 200):
    # Deferred import keeps Starlette out of the import path until a
    # handler actually runs / the app is built.
    from starlette.responses import Response

    return Response(
        json.dumps(_jsonable(data), default=str),
        status_code=status_code,
        media_type="application/json",
    )


def _jsonable(obj: Any) -> Any:
    # Deliberate: nested non-JSON leaves (datetime, Decimal) in raw
    # Schwab payloads serialize via json.dumps(default=str) — same
    # contract the MCP tool surface uses.
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    return obj


def _expiry_param(request) -> date:
    raw = request.query_params.get("expiry")
    if not raw:
        raise _BadParam("expiry query parameter is required (YYYY-MM-DD)")
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise _BadParam(f"invalid expiry {raw!r} (need YYYY-MM-DD)")


def _int_param(
    request, key: str, default: int | None, *, max_value: int | None = None,
) -> int | None:
    raw = request.query_params.get(key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise _BadParam(f"{key} must be an integer")
    if value < 0 or (max_value is not None and value > max_value):
        raise _BadParam(f"{key} must be between 0 and {max_value}")
    return value


def _range_param(request, default: str) -> tuple:
    raw = request.query_params.get("range") or default
    try:
        start, end = parse_range(raw)
    except ValueError as e:
        raise _BadParam(f"invalid range {raw!r}: {e}")
    return raw, start, end


_MAX_SYMBOLS = 50


def _symbols_param(request) -> list[str]:
    raw = request.query_params.get("symbols", "")
    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()]
    if not symbols:
        raise _BadParam("symbols query parameter is required (comma-separated)")
    if len(symbols) > _MAX_SYMBOLS:
        # Each symbol fans out to upstream Schwab calls — cap the
        # amplification a single request can demand.
        raise _BadParam(f"too many symbols (max {_MAX_SYMBOLS})")
    return symbols


class _OrdersGateway(BaseService):  # TODO(P4): promote to OrdersService
    """Order READS through the service-layer client plumbing.

    There is no OrdersService yet; this thin gateway reuses
    :class:`BaseService`'s authed-client contextmanager so REST order
    reads share the exact auth/error semantics of every other service.
    Mutations are deliberately absent — P4 adds them behind
    ``order:<profile>``.
    """

    def get_order(self, account: str, order_id: str):
        with self._authed_client() as client:
            acct = client.resolve_account(account)
            return api_orders.get_order(client, acct.hash_value, order_id)

    def list_orders(
        self, account: str | None, *, start, end, status, max_results,
    ):
        with self._authed_client() as client:
            if account:
                acct = client.resolve_account(account)
                return api_orders.list_orders_for_account(
                    client, acct.hash_value,
                    start=start, end=end, status=status,
                    max_results=max_results,
                )
            return api_orders.list_orders_all_accounts(
                client,
                start=start, end=end, status=status, max_results=max_results,
            )


# ---------------------------------------------------------------------------
# marketdata
# ---------------------------------------------------------------------------


def _do_quote(request):
    symbol = request.path_params["symbol"].upper()
    return QuoteService().get_quote_payload([symbol])


def _do_chain(request):
    return ChainsService().get_chain_envelope(
        request.path_params["symbol"].upper(),
        expiry=_expiry_param(request),
        strike_count=_int_param(request, "strikes", 20, max_value=200),
    )


def _do_history(request):
    interval_str = request.query_params.get("interval") or "1day"
    range_str = request.query_params.get("range") or "-1y..now"
    try:
        interval = parse_interval(interval_str)
    except ValueError as e:
        raise _BadParam(f"invalid interval {interval_str!r}: {e}")
    try:
        start, end = parse_range(range_str)
    except ValueError as e:
        raise _BadParam(f"invalid range {range_str!r}: {e}")
    result = HistoryService().get_history(
        request.path_params["symbol"].upper(),
        frequency_type=interval.frequency_type,
        frequency=interval.frequency,
        label=interval.label,
        start=start,
        end=end,
        range_str=range_str,
    )
    return result.envelope


def _do_vol(request):
    symbol = request.path_params["symbol"].upper()
    # no_record=True: an ad-hoc REST read must not write a snapshot into
    # the IV history (owned by the scheduled vol job).
    result = VolService().get_vol(
        symbol,
        hv_window=_int_param(request, "hv_window", 30, max_value=504),
        hv_lookback=_int_param(request, "hv_lookback", 252, max_value=2520),
        ivp_lookback=_int_param(request, "ivp_lookback", 252, max_value=2520),
        no_record=True,
    )
    # NoVolData / storage failures raise ServiceError (mapped to 502 by
    # _endpoint); get_vol never returns None on this path.
    return result.envelope


def _do_skew(request):
    result = SkewService().get_skew_l1(
        request.path_params["symbol"].upper(),
        _expiry_param(request),
        strikes=_int_param(request, "strikes", 20, max_value=200),
    )
    return _jsonable(result.metrics)


def _do_greeks(request):
    side = (request.query_params.get("side") or "").upper()
    if side not in ("C", "P"):
        raise _BadParam("side must be 'C' or 'P'")
    raw_strike = request.query_params.get("strike")
    if raw_strike is None:
        raise _BadParam("strike query parameter is required")
    try:
        strike = float(raw_strike)
    except ValueError:
        raise _BadParam("strike must be a number")
    result = GreeksService().get_greeks(
        request.path_params["symbol"].upper(),
        strike=strike,
        expiry=_expiry_param(request),
        side=side,
    )
    return result.envelope


def _do_dividends(request):
    return DividendsService().get_dividends(_symbols_param(request)).payload


def _do_fundamentals(request):
    return FundamentalsService().get_fundamentals(_symbols_param(request)).payload


# ---------------------------------------------------------------------------
# accounts / positions / transactions / orders (reads)
# ---------------------------------------------------------------------------


def _do_accounts(request):
    return [dict(a) for a in AccountsService().list_accounts().accounts]


def _do_account(request):
    result = AccountsService().get_account(request.path_params["account"])
    return dict(result.account)


def _do_positions(request):
    result = AccountsService().get_positions(request.path_params["account"])
    return [dict(p) for p in result.positions]


def _do_transactions(request):
    _raw, start, end = _range_param(request, "-30d..now")
    result = TransactionsService().get_transactions(
        request.path_params["account"],
        start=start,
        end=end,
        type_filter=request.query_params.get("type") or "",
    )
    return [dict(r) for r in result.rows]


def _do_list_orders(request):
    _raw, start, end = _range_param(request, "-7d..now")
    return _OrdersGateway().list_orders(
        request.query_params.get("account"),
        start=start,
        end=end,
        status=request.query_params.get("status") or None,
        max_results=_int_param(request, "max_results", None, max_value=500),
    )


def _do_get_order(request):
    return _OrdersGateway().get_order(
        request.path_params["account"],
        request.path_params["order_id"],
    )


# ---------------------------------------------------------------------------
# dataset (local vol-history reads)
# ---------------------------------------------------------------------------


def _dataset_dispatch(name: str, arguments: dict) -> Any:
    """Reuse the MCP dataset dispatcher (read-only, local SQLite).

    The dispatcher's SQL-injection guard (PRAGMA-based field allowlist
    for dataset.history) applies to this path too. Dispatcher failures
    (unknown tool, non-JSON output) surface as ServiceError → 502, never
    an unhandled 500.
    """
    from schwab_cli.mcp_server.app import dispatch_dataset_tool

    try:
        return json.loads(dispatch_dataset_tool(name, arguments=arguments))
    except (ValueError, KeyError) as e:  # incl. JSONDecodeError
        raise ServiceError(f"dataset dispatch failed: {type(e).__name__}")


def _do_dataset_status(request):
    return _dataset_dispatch("dataset.status", {
        "group": request.query_params.get("group", "volatility"),
        "tier": request.query_params.get("tier"),
        "source": request.query_params.get("source"),
    })


def _do_dataset_history(request):
    args: dict = {
        "symbol": request.path_params["symbol"].upper(),
        "lookback_days": _int_param(request, "lookback_days", 252, max_value=730),
    }
    fields = request.query_params.get("fields")
    if fields:
        args["fields"] = [f.strip() for f in fields.split(",") if f.strip()]
    return _dataset_dispatch("dataset.history", args)


def _do_dataset_iv_rank(request):
    return _dataset_dispatch("dataset.iv_rank", {
        "symbol": request.path_params["symbol"].upper(),
        "lookback_days": _int_param(request, "lookback_days", 252, max_value=730),
    })


# ---------------------------------------------------------------------------
# Legacy loopback PoC routes
# ---------------------------------------------------------------------------


async def _quote(request):
    symbol = request.path_params["symbol"]
    try:
        # Worker thread: same blocking-IO discipline as the /api handlers.
        payload = await asyncio.to_thread(
            QuoteService().get_quote_payload, [symbol.upper()],
        )
    except (NotConfigured, NotAuthenticated) as e:
        return _json_response({"error": _err(e)}, status_code=503)
    except (ApiError, SessionExpired, ServiceError) as e:
        return _json_response({"error": _err(e)}, status_code=502)
    return _json_response(payload)


def rest_routes():
    """Return the loopback REST PoC route objects.

    Exposed separately so the ``--enable-mcp --enable-rest`` path can
    mount these same routes onto the MCP server's Starlette app (shared
    port) without rebuilding a second Starlette application.
    """
    from starlette.routing import Route

    return [
        Route("/health", _health, methods=["GET"]),
        Route("/quote/{symbol}", _quote, methods=["GET"]),
    ]


def api_routes():
    """Return the public ``/api/v1`` resource-server routes.

    The webauth middleware fronts these: peer allowlist + JWT; each
    handler then enforces its own required scope. ``/api/v1/health`` is
    the JWT-exempt proxy liveness probe.
    """
    from starlette.routing import Route

    md = "marketdata"
    return [
        Route("/api/v1/health", _health, methods=["GET"]),
        # marketdata
        Route("/api/v1/quote/{symbol}", _endpoint(md, _do_quote), methods=["GET"]),
        Route("/api/v1/chain/{symbol}", _endpoint(md, _do_chain), methods=["GET"]),
        Route("/api/v1/history/{symbol}", _endpoint(md, _do_history), methods=["GET"]),
        Route("/api/v1/vol/{symbol}", _endpoint(md, _do_vol), methods=["GET"]),
        Route("/api/v1/skew/{symbol}", _endpoint(md, _do_skew), methods=["GET"]),
        Route("/api/v1/greeks/{symbol}", _endpoint(md, _do_greeks), methods=["GET"]),
        Route("/api/v1/dividends", _endpoint(md, _do_dividends), methods=["GET"]),
        Route("/api/v1/fundamentals", _endpoint(md, _do_fundamentals), methods=["GET"]),
        # account data (financial PII)
        Route("/api/v1/accounts", _endpoint("accounts", _do_accounts), methods=["GET"]),
        Route("/api/v1/accounts/{account}", _endpoint("accounts", _do_account), methods=["GET"]),
        Route(
            "/api/v1/accounts/{account}/positions",
            _endpoint("positions", _do_positions), methods=["GET"],
        ),
        Route(
            "/api/v1/accounts/{account}/transactions",
            _endpoint("transactions", _do_transactions), methods=["GET"],
        ),
        # order reads (mutations are P4, behind order:<profile>)
        Route("/api/v1/orders", _endpoint("orders", _do_list_orders), methods=["GET"]),
        Route(
            "/api/v1/accounts/{account}/orders/{order_id}",
            _endpoint("orders", _do_get_order), methods=["GET"],
        ),
        # local dataset reads
        Route(
            "/api/v1/dataset/status",
            _endpoint("dataset", _do_dataset_status), methods=["GET"],
        ),
        Route(
            "/api/v1/dataset/history/{symbol}",
            _endpoint("dataset", _do_dataset_history), methods=["GET"],
        ),
        Route(
            "/api/v1/dataset/iv-rank/{symbol}",
            _endpoint("dataset", _do_dataset_iv_rank), methods=["GET"],
        ),
    ]


def build_rest_app():
    """Starlette app for the standalone REST mode: loopback PoC routes
    plus the /api/v1 surface (auth is applied by the caller wrapping
    the app in WebAuthMiddleware)."""
    from starlette.applications import Starlette

    return Starlette(routes=rest_routes() + api_routes())
