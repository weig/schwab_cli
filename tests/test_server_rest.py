"""Tests for the REST PoC (`schwab_cli.server.rest`) and its wiring.

The REST PoC is an UNAUTHENTICATED proof that a Starlette route can
reach the SERVICE layer end-to-end:

    GET /health          -> {"ok": true}
    GET /quote/{symbol}  -> service.quotes.get_quote_payload([SYMBOL])

All tests mock the service layer — NO real network / auth.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Import guard
# ---------------------------------------------------------------------------
try:
    from schwab_cli.server import rest as rest_module
    _REST_AVAILABLE = True
except (ModuleNotFoundError, ImportError):
    _REST_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not _REST_AVAILABLE,
    reason="schwab_cli.server.rest not implemented yet",
)


def _client():
    from starlette.testclient import TestClient

    return TestClient(rest_module.build_rest_app())


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_returns_200_ok(self):
        resp = _client().get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}


# ---------------------------------------------------------------------------
# /quote/{symbol} — happy path
# ---------------------------------------------------------------------------

class TestQuoteHappyPath:
    def test_quote_returns_service_payload(self, monkeypatch):
        fake_payload = {"AAPL": {"quote": {"lastPrice": 123.45}}}
        monkeypatch.setattr(
            rest_module.QuoteService,
            "get_quote_payload",
            lambda self, symbols, **k: fake_payload,
        )
        resp = _client().get("/quote/AAPL")
        assert resp.status_code == 200
        assert resp.json() == fake_payload

    def test_symbol_is_upcased_before_service(self, monkeypatch):
        seen: list = []

        def _capture(self, symbols, **k):
            seen.append(symbols)
            return {}

        monkeypatch.setattr(
            rest_module.QuoteService, "get_quote_payload", _capture
        )
        resp = _client().get("/quote/aapl")
        assert resp.status_code == 200
        # The handler upcases the path param before the service call.
        assert seen == [["AAPL"]]


# ---------------------------------------------------------------------------
# /quote/{symbol} — service-layer errors mapped to HTTP status codes
# ---------------------------------------------------------------------------

class TestQuoteErrors:
    def _raise(self, monkeypatch, exc):
        def _boom(self, symbols, **k):
            raise exc

        monkeypatch.setattr(
            rest_module.QuoteService, "get_quote_payload", _boom
        )

    def test_not_authenticated_returns_503(self, monkeypatch):
        from schwab_cli.service.auth import NotAuthenticated

        self._raise(monkeypatch, NotAuthenticated("no session"))
        resp = _client().get("/quote/AAPL")
        assert resp.status_code == 503
        assert "NotAuthenticated" in resp.json()["error"]

    def test_not_configured_returns_503(self, monkeypatch):
        from schwab_cli.service.auth import NotConfigured

        self._raise(monkeypatch, NotConfigured())
        resp = _client().get("/quote/AAPL")
        assert resp.status_code == 503
        assert "NotConfigured" in resp.json()["error"]

    def test_api_error_returns_502(self, monkeypatch):
        from schwab_cli.service.auth import ApiError

        self._raise(monkeypatch, ApiError("upstream 500"))
        resp = _client().get("/quote/AAPL")
        assert resp.status_code == 502
        assert "ApiError" in resp.json()["error"]

    def test_session_expired_returns_502(self, monkeypatch):
        from schwab_cli.service.auth import SessionExpired

        self._raise(monkeypatch, SessionExpired("expired"))
        resp = _client().get("/quote/AAPL")
        assert resp.status_code == 502
        assert "SessionExpired" in resp.json()["error"]


# ---------------------------------------------------------------------------
# rest_routes() helper
# ---------------------------------------------------------------------------

class TestRestRoutes:
    def test_rest_routes_returns_two_routes(self):
        routes = rest_module.rest_routes()
        paths = {r.path for r in routes}
        assert paths == {"/health", "/quote/{symbol}"}

    def test_rest_routes_calls_service_not_api(self):
        """The /quote handler must go through the SERVICE layer.

        Guards against a regression where the route reaches into
        ``schwab_cli.api`` directly. The handler instantiates
        ``QuoteService`` and calls ``get_quote_payload`` — assert that the
        class is bound at module level to the service class.
        """
        from schwab_cli.service.quotes import QuoteService

        assert rest_module.QuoteService is QuoteService
