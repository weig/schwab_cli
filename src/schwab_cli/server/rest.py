"""REST PoC — proves the REST -> service path end-to-end.

A single unauthenticated ``GET /quote/{symbol}`` (plus a ``/health``
probe) that goes straight through the SERVICE layer
(:func:`schwab_cli.service.quotes.get_quote_payload`) and returns the
raw Schwab payload as JSON. There is deliberately NO auth or symbol
allowlisting here — that is a later, separate step. This exists only to
demonstrate that a Starlette route can reach the service layer cleanly.
"""

from __future__ import annotations

from schwab_cli.service.quotes import QuoteService
from schwab_cli.service.auth import (
    ApiError,
    NotAuthenticated,
    NotConfigured,
    SessionExpired,
)


async def _health(request):
    return _json_response({"ok": True})


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
    """Return the REST PoC route objects.

    Exposed separately so the ``--enable-mcp --enable-rest`` path can
    mount these same routes onto the MCP server's Starlette app (shared
    port) without rebuilding a second Starlette application.
    """
    from starlette.routing import Route

    return [
        Route("/health", _health, methods=["GET"]),
        Route("/quote/{symbol}", _quote, methods=["GET"]),
    ]


def build_rest_app():
    """Starlette app for the REST PoC. UNAUTHENTICATED — a proof of the
    REST -> service path; auth/allowlisting is a deliberate later step."""
    from starlette.applications import Starlette

    return Starlette(routes=rest_routes())
