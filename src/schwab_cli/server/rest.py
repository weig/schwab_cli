"""REST surface.

Two route sets:

* :func:`rest_routes` — the original loopback PoC (``/health``,
  ``/quote/{symbol}``), unauthenticated; the webauth middleware's tier-2
  rule confines them to loopback peers.
* :func:`api_routes` — the public resource-server surface under
  ``/api/v1/``. The webauth middleware authenticates the caller and
  attaches a Principal; each route declares its required scope via
  :func:`schwab_cli.webauth.middleware.scope_denial`.
"""

from __future__ import annotations

from schwab_cli.service.quotes import QuoteService
from schwab_cli.service.auth import (
    ApiError,
    NotAuthenticated,
    NotConfigured,
    SessionExpired,
)
from schwab_cli.webauth.middleware import scope_denial


async def _health(request):
    return _json_response({"ok": True})


async def _api_quote(request):
    denial = scope_denial(request, "marketdata")
    if denial is not None:
        return denial
    return await _quote(request)


async def _quote(request):
    symbol = request.path_params["symbol"]
    try:
        # `get_quote_payload` is synchronous and opens its own
        # `with SchwabClient(...)`. Calling it from an async handler is
        # fine for a PoC — it is a brief blocking REST call. A real
        # service would offload this to a thread pool.
        payload = QuoteService().get_quote_payload([symbol.upper()])
    except (NotConfigured, NotAuthenticated) as e:
        return _json_response({"error": _err(e)}, status_code=503)
    except (ApiError, SessionExpired) as e:
        return _json_response({"error": _err(e)}, status_code=502)
    return _json_response(payload)


def _err(e: Exception) -> str:
    """Format an exception for the error body. Some service exceptions are
    raised bare (no message), so fall back to the class name rather than
    emitting a misleading ``"NotConfigured: "`` with a trailing colon."""
    detail = str(e)
    return f"{type(e).__name__}: {detail}" if detail else type(e).__name__


def _json_response(data, *, status_code: int = 200):
    # Deferred import keeps Starlette out of the import path until a
    # handler actually runs / the app is built.
    from starlette.responses import JSONResponse

    return JSONResponse(data, status_code=status_code)


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

    return [
        Route("/api/v1/health", _health, methods=["GET"]),
        Route("/api/v1/quote/{symbol}", _api_quote, methods=["GET"]),
    ]


def build_rest_app():
    """Starlette app for the standalone REST mode: loopback PoC routes
    plus the /api/v1 surface (auth is applied by the caller wrapping
    the app in WebAuthMiddleware)."""
    from starlette.applications import Starlette

    return Starlette(routes=rest_routes() + api_routes())
