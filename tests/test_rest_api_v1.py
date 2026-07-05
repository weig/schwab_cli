"""P3 /api/v1 endpoint tests: scope gating per group, param validation,
and service-error mapping — all services mocked, zero network."""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from schwab_cli.server.rest import build_rest_app
from schwab_cli.webauth.middleware import WebAuthMiddleware
from schwab_cli.webauth.verify import Principal


class _GrantVerifier:
    def __init__(self, scopes) -> None:
        self._scopes = frozenset(scopes)

    def verify(self, token: str) -> Principal:
        return Principal(
            provider="auth0", subject="auth0|abc", email=None,
            scopes=frozenset(self._scopes),
        )


def _client(*scopes) -> TestClient:
    app = WebAuthMiddleware(
        build_rest_app(),
        verifier=_GrantVerifier(scopes),
        has_providers=True,
        allow=("127.0.0.1",),
        peer_of=lambda scope: "127.0.0.1",
    )
    return TestClient(app)


_AUTH = {"Authorization": "Bearer x.y.z"}


class _Result:
    """Tiny attribute bag standing in for service result objects."""

    def __init__(self, **kw) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


# ---------------------------------------------------------------------------
# Scope gating: every endpoint group rejects a token without its scope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path,required", [
    ("/api/v1/quote/SPY", "marketdata"),
    ("/api/v1/chain/SPY?expiry=2026-06-19", "marketdata"),
    ("/api/v1/history/SPY", "marketdata"),
    ("/api/v1/vol/SPY", "marketdata"),
    ("/api/v1/skew/SPY?expiry=2026-06-19", "marketdata"),
    ("/api/v1/greeks/SPY?expiry=2026-06-19&strike=600&side=C", "marketdata"),
    ("/api/v1/dividends?symbols=SPY", "marketdata"),
    ("/api/v1/fundamentals?symbols=SPY", "marketdata"),
    ("/api/v1/accounts", "accounts"),
    ("/api/v1/accounts/1234", "accounts"),
    ("/api/v1/accounts/1234/positions", "positions"),
    ("/api/v1/accounts/1234/transactions", "transactions"),
    ("/api/v1/orders", "orders"),
    ("/api/v1/accounts/1234/orders/777", "orders"),
    ("/api/v1/dataset/status", "dataset"),
    ("/api/v1/dataset/history/SPY", "dataset"),
    ("/api/v1/dataset/iv-rank/SPY", "dataset"),
])
def test_endpoint_requires_its_scope(path, required):
    resp = _client("some-other-scope").get(path, headers=_AUTH)
    assert resp.status_code == 403, path
    assert required in resp.json()["error"]


# ---------------------------------------------------------------------------
# Happy paths (services mocked)
# ---------------------------------------------------------------------------


def test_quote_happy(monkeypatch):
    monkeypatch.setattr(
        "schwab_cli.server.rest.QuoteService.get_quote_payload",
        lambda self, symbols: {"SPY": {"last": 600.0}},
    )
    resp = _client("marketdata").get("/api/v1/quote/spy", headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["SPY"]["last"] == 600.0


def test_chain_happy_passes_params(monkeypatch):
    seen = {}

    def fake(self, symbol, *, expiry, strike_count):
        seen.update(symbol=symbol, expiry=expiry, strike_count=strike_count)
        return {"chain": []}

    monkeypatch.setattr(
        "schwab_cli.server.rest.ChainsService.get_chain_envelope", fake,
    )
    resp = _client("marketdata").get(
        "/api/v1/chain/spy?expiry=2026-06-19&strikes=8", headers=_AUTH,
    )
    assert resp.status_code == 200
    assert seen["symbol"] == "SPY"
    assert str(seen["expiry"]) == "2026-06-19"
    assert seen["strike_count"] == 8


def test_vol_happy(monkeypatch):
    def fake(self, symbol, **kw):
        assert kw["no_record"] is True  # ad-hoc reads must not write history
        return _Result(envelope={"symbol": symbol, "atm_iv": 0.24})

    monkeypatch.setattr("schwab_cli.server.rest.VolService.get_vol", fake)
    resp = _client("marketdata").get("/api/v1/vol/spy", headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["atm_iv"] == 0.24


def test_accounts_happy(monkeypatch):
    monkeypatch.setattr(
        "schwab_cli.server.rest.AccountsService.list_accounts",
        lambda self: _Result(accounts=[{"account": "1234", "value": 1.0}]),
    )
    resp = _client("accounts").get("/api/v1/accounts", headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()[0]["account"] == "1234"


def test_positions_happy(monkeypatch):
    monkeypatch.setattr(
        "schwab_cli.server.rest.AccountsService.get_positions",
        lambda self, account: _Result(positions=[{"symbol": "SPY", "qty": 10}]),
    )
    resp = _client("positions").get(
        "/api/v1/accounts/1234/positions", headers=_AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()[0]["symbol"] == "SPY"


def test_list_orders_happy(monkeypatch):
    monkeypatch.setattr(
        "schwab_cli.server.rest._OrdersGateway.list_orders",
        lambda self, account, **kw: [{"orderId": 777}],
    )
    resp = _client("orders").get("/api/v1/orders", headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()[0]["orderId"] == 777


def test_get_order_happy(monkeypatch):
    monkeypatch.setattr(
        "schwab_cli.server.rest._OrdersGateway.get_order",
        lambda self, account, order_id: {"orderId": order_id, "account": account},
    )
    resp = _client("orders").get(
        "/api/v1/accounts/1234/orders/777", headers=_AUTH,
    )
    assert resp.status_code == 200
    assert resp.json() == {"orderId": "777", "account": "1234"}


def test_dataset_history_happy(monkeypatch):
    seen = {}

    def fake(name, *, arguments):
        seen["name"] = name
        seen["arguments"] = arguments
        return '{"rows": []}'

    monkeypatch.setattr(
        "schwab_cli.mcp_server.app.dispatch_dataset_tool", fake,
    )
    resp = _client("dataset").get(
        "/api/v1/dataset/history/spy?lookback_days=30&fields=atm_iv_30d",
        headers=_AUTH,
    )
    assert resp.status_code == 200
    assert seen["name"] == "dataset_history"
    assert seen["arguments"]["symbol"] == "SPY"
    assert seen["arguments"]["lookback_days"] == 30
    assert seen["arguments"]["fields"] == ["atm_iv_30d"]


# ---------------------------------------------------------------------------
# Param validation → 400
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    "/api/v1/chain/SPY",                                   # missing expiry
    "/api/v1/chain/SPY?expiry=junk",                       # bad expiry
    "/api/v1/chain/SPY?expiry=2026-06-19&strikes=lots",    # bad int
    "/api/v1/history/SPY?interval=hourly-ish",             # bad interval
    "/api/v1/history/SPY?range=whenever",                  # bad range
    "/api/v1/greeks/SPY?expiry=2026-06-19&strike=600",     # missing side
    "/api/v1/greeks/SPY?expiry=2026-06-19&side=C",         # missing strike
    "/api/v1/dividends",                                   # missing symbols
    "/api/v1/accounts/1/transactions?range=whenever",      # bad range
])
def test_bad_params_are_400(path):
    resp = _client("marketdata", "accounts", "transactions").get(
        path, headers=_AUTH,
    )
    assert resp.status_code == 400, path


# ---------------------------------------------------------------------------
# Service-error mapping
# ---------------------------------------------------------------------------


def test_not_authenticated_maps_to_503(monkeypatch):
    from schwab_cli.service.auth import NotAuthenticated

    def boom(self, symbols):
        raise NotAuthenticated

    monkeypatch.setattr(
        "schwab_cli.server.rest.QuoteService.get_quote_payload", boom,
    )
    resp = _client("marketdata").get("/api/v1/quote/SPY", headers=_AUTH)
    assert resp.status_code == 503


def test_api_error_maps_to_502(monkeypatch):
    from schwab_cli.service.auth import ApiError

    def boom(self, symbols):
        raise ApiError("upstream 500")

    monkeypatch.setattr(
        "schwab_cli.server.rest.QuoteService.get_quote_payload", boom,
    )
    resp = _client("marketdata").get("/api/v1/quote/SPY", headers=_AUTH)
    assert resp.status_code == 502


def test_session_expired_maps_to_502(monkeypatch):
    from schwab_cli.service.auth import SessionExpired

    def boom(self, symbols):
        raise SessionExpired("Session expired.")

    monkeypatch.setattr(
        "schwab_cli.server.rest.QuoteService.get_quote_payload", boom,
    )
    resp = _client("marketdata").get("/api/v1/quote/SPY", headers=_AUTH)
    assert resp.status_code == 502


def test_other_service_errors_map_to_502_not_500(monkeypatch):
    """NoVolData / storage errors are ServiceError subclasses — they must
    surface as 502, never escape as an unhandled 500."""
    from schwab_cli.service import ServiceError

    class NoVolData(ServiceError):
        pass

    def boom(self, symbol, **kw):
        raise NoVolData("no spot price in chain")

    monkeypatch.setattr("schwab_cli.server.rest.VolService.get_vol", boom)
    resp = _client("marketdata").get("/api/v1/vol/SPY", headers=_AUTH)
    assert resp.status_code == 502
    assert "NoVolData" in resp.json()["error"]


def test_dataset_dispatch_failure_maps_to_502(monkeypatch):
    monkeypatch.setattr(
        "schwab_cli.mcp_server.app.dispatch_dataset_tool",
        lambda name, *, arguments: "not json at all",
    )
    resp = _client("dataset").get("/api/v1/dataset/status", headers=_AUTH)
    assert resp.status_code == 502


# ---------------------------------------------------------------------------
# Remaining happy paths + amplification caps
# ---------------------------------------------------------------------------


def test_skew_happy(monkeypatch):
    monkeypatch.setattr(
        "schwab_cli.server.rest.SkewService.get_skew_l1",
        lambda self, symbol, expiry, *, strikes: _Result(
            metrics={"rr25": -0.02},
        ),
    )
    resp = _client("marketdata").get(
        "/api/v1/skew/spy?expiry=2026-06-19", headers=_AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["rr25"] == -0.02


def test_greeks_happy(monkeypatch):
    seen = {}

    def fake(self, underlying, *, strike, expiry, side):
        seen.update(underlying=underlying, strike=strike, side=side)
        return _Result(envelope={"delta": 0.5})

    monkeypatch.setattr("schwab_cli.server.rest.GreeksService.get_greeks", fake)
    resp = _client("marketdata").get(
        "/api/v1/greeks/spy?expiry=2026-06-19&strike=600&side=c",
        headers=_AUTH,
    )
    assert resp.status_code == 200
    assert seen == {"underlying": "SPY", "strike": 600.0, "side": "C"}


def test_account_detail_happy(monkeypatch):
    monkeypatch.setattr(
        "schwab_cli.server.rest.AccountsService.get_account",
        lambda self, account: _Result(account={"account": account}),
    )
    resp = _client("accounts").get("/api/v1/accounts/1234", headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["account"] == "1234"


def test_transactions_happy(monkeypatch):
    monkeypatch.setattr(
        "schwab_cli.server.rest.TransactionsService.get_transactions",
        lambda self, account, *, start, end, type_filter: _Result(
            rows=[{"type": "TRADE", "amount": 1.0}],
        ),
    )
    resp = _client("transactions").get(
        "/api/v1/accounts/1234/transactions", headers=_AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()[0]["type"] == "TRADE"


def test_dataset_status_and_iv_rank_happy(monkeypatch):
    monkeypatch.setattr(
        "schwab_cli.mcp_server.app.dispatch_dataset_tool",
        lambda name, *, arguments: '{"name": "%s"}' % name,
    )
    c = _client("dataset")
    assert c.get("/api/v1/dataset/status", headers=_AUTH).json() == {
        "name": "dataset_status",
    }
    assert c.get("/api/v1/dataset/iv-rank/SPY", headers=_AUTH).json() == {
        "name": "dataset_iv_rank",
    }


def test_symbols_amplification_cap():
    blob = ",".join(f"S{i}" for i in range(60))
    resp = _client("marketdata").get(
        f"/api/v1/dividends?symbols={blob}", headers=_AUTH,
    )
    assert resp.status_code == 400
    assert "max 50" in resp.json()["error"]


def test_int_param_upper_bound():
    resp = _client("orders").get(
        "/api/v1/orders?max_results=99999", headers=_AUTH,
    )
    assert resp.status_code == 400
