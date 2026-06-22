"""P4b order-mutation endpoint tests.

The placement gateway (authed client + headless pipeline run) is
faked at its seam — the pipeline's own protective rules are covered by
the existing order_pipeline / order_policy suites. These tests lock the
REST layer: dynamic ``order:<profile>`` scope gating, the hard
profile-must-load rule, request validation, and HTTP mapping of every
pipeline outcome. Spec normalization (_spec_from_flags → _build_body)
runs REAL so the request schema is exercised end to end.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from schwab_cli.order_pipeline.runner import PipelineExit
from schwab_cli.server.orders_write import _PlacementGateway
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


def _client(*scopes, has_providers=True) -> TestClient:
    app = WebAuthMiddleware(
        build_rest_app(),
        verifier=_GrantVerifier(scopes) if has_providers else None,
        has_providers=has_providers,
        allow=("127.0.0.1",),
        peer_of=lambda scope: "127.0.0.1",
    )
    return TestClient(app)


_AUTH = {"Authorization": "Bearer x.y.z"}
_ORDER = {
    "account": "1234",
    "profile": "default",
    "order": {
        "symbol": "SPY", "side": "BUY", "quantity": 1,
        "order_type": "LIMIT", "price": 1.00,
    },
}


def _approved(rule="allow"):
    return type("D", (), {"approved": True, "rule_name": rule,
                          "reason": "ok"})()


class _Ctx:
    def __init__(self, **kw) -> None:
        self.placed_order_id = kw.get("order_id", "777")
        self.account = type("A", (), {"account_number": "123456789"})()
        self.policy_decision = kw.get(
            "decision", _approved(),
        ) if "decision" in kw or kw.get("approved", True) else None
        self.profile_name = kw.get("profile_name", "default")
        self.preview_summary = kw.get("preview_summary")
        self.profile_load_error = kw.get("profile_load_error")


@pytest.fixture
def profile_ok(monkeypatch):
    fake = lambda name, **kw: object()  # noqa: E731
    monkeypatch.setattr("schwab_cli.order_policy.load_profile", fake)
    monkeypatch.setattr("schwab_cli.order_policy.loader.load_profile", fake)


def _gateway_returns(monkeypatch, ctx):
    def run(self, **kw):
        self.last_ctx = ctx
        return ctx

    monkeypatch.setattr(_PlacementGateway, "run", run)


def _gateway_raises(monkeypatch, exc, ctx):
    def run(self, **kw):
        self.last_ctx = ctx
        raise exc

    monkeypatch.setattr(_PlacementGateway, "run", run)


# ---------------------------------------------------------------------------
# Scope gating
# ---------------------------------------------------------------------------


def test_place_requires_order_profile_scope(profile_ok):
    resp = _client("orders", "marketdata").post(
        "/api/v1/orders", json=_ORDER, headers=_AUTH,
    )
    assert resp.status_code == 403
    assert "order:default" in resp.json()["error"]


def test_place_profile_in_body_drives_required_scope(profile_ok, monkeypatch):
    _gateway_returns(monkeypatch, _Ctx(profile_name="conservative"))
    body = {**_ORDER, "profile": "conservative"}
    assert _client("order:default").post(
        "/api/v1/orders", json=body, headers=_AUTH,
    ).status_code == 403
    assert _client("order:conservative").post(
        "/api/v1/orders", json=body, headers=_AUTH,
    ).status_code == 201


def test_place_order_wildcard_scope_accepted(profile_ok, monkeypatch):
    _gateway_returns(monkeypatch, _Ctx())
    resp = _client("order:*").post(
        "/api/v1/orders", json=_ORDER, headers=_AUTH,
    )
    assert resp.status_code == 201


def test_legacy_mode_loopback_allows_place(profile_ok, monkeypatch):
    _gateway_returns(monkeypatch, _Ctx())
    resp = _client(has_providers=False).post("/api/v1/orders", json=_ORDER)
    assert resp.status_code == 201


# ---------------------------------------------------------------------------
# Validation + the hard profile rule
# ---------------------------------------------------------------------------


def test_place_missing_account_is_400(profile_ok):
    resp = _client("order:default").post(
        "/api/v1/orders", json={"order": {"symbol": "SPY"}}, headers=_AUTH,
    )
    assert resp.status_code == 400


def test_place_missing_symbol_is_400(profile_ok):
    resp = _client("order:default").post(
        "/api/v1/orders", json={"account": "1234", "order": {}}, headers=_AUTH,
    )
    assert resp.status_code == 400


def test_place_non_json_body_is_400(profile_ok):
    resp = _client("order:default").post(
        "/api/v1/orders", content=b"not json", headers=_AUTH,
    )
    assert resp.status_code == 400


def test_unloadable_profile_is_403_never_unpoliced(monkeypatch):
    """The CLI tolerates a missing profile (preview convenience); REST
    must NEVER place an unpoliced order."""
    def boom(name, **kw):
        raise FileNotFoundError(name)

    monkeypatch.setattr("schwab_cli.order_policy.load_profile", boom)
    monkeypatch.setattr("schwab_cli.order_policy.loader.load_profile", boom)
    resp = _client("order:default").post(
        "/api/v1/orders", json=_ORDER, headers=_AUTH,
    )
    assert resp.status_code == 403
    assert "unavailable" in resp.json()["error"]


# ---------------------------------------------------------------------------
# Pipeline outcome mapping
# ---------------------------------------------------------------------------


def test_place_happy_path_returns_201(profile_ok, monkeypatch):
    _gateway_returns(
        monkeypatch,
        _Ctx(order_id="42", decision=_approved("allow-small-orders")),
    )
    resp = _client("order:default").post(
        "/api/v1/orders", json=_ORDER, headers=_AUTH,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["order_id"] == "42"
    assert body["account"] == "6789"  # last-4 only, never the full number
    assert body["policy_rule"] == "allow-small-orders"


def test_policy_reject_maps_to_403_with_reason(profile_ok, monkeypatch):
    from schwab_cli.commands.order import EXIT_POLICY_REJECTED

    decision = type("D", (), {
        "rule_name": "max-order-value", "reason": "order value above cap",
    })()
    _gateway_raises(
        monkeypatch, PipelineExit(EXIT_POLICY_REJECTED), _Ctx(decision=decision),
    )
    resp = _client("order:default").post(
        "/api/v1/orders", json=_ORDER, headers=_AUTH,
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"] == "rejected by policy"
    assert body["rule"] == "max-order-value"
    assert body["reason"] == "order value above cap"


def test_preview_reject_maps_to_422(profile_ok, monkeypatch):
    from schwab_cli.commands.order import EXIT_REJECTED

    summary = type("S", (), {"rejects": ["insufficient buying power"]})()
    _gateway_raises(
        monkeypatch, PipelineExit(EXIT_REJECTED), _Ctx(preview_summary=summary),
    )
    resp = _client("order:default").post(
        "/api/v1/orders", json=_ORDER, headers=_AUTH,
    )
    assert resp.status_code == 422
    assert resp.json()["rejects"] == ["insufficient buying power"]


def test_other_pipeline_exit_maps_to_502(profile_ok, monkeypatch):
    _gateway_raises(monkeypatch, PipelineExit(1), _Ctx())
    resp = _client("order:default").post(
        "/api/v1/orders", json=_ORDER, headers=_AUTH,
    )
    assert resp.status_code == 502


def test_session_expired_maps_to_502(profile_ok, monkeypatch):
    from schwab_cli.service.auth import SessionExpired

    _gateway_raises(monkeypatch, SessionExpired("Session expired."), _Ctx())
    resp = _client("order:default").post(
        "/api/v1/orders", json=_ORDER, headers=_AUTH,
    )
    assert resp.status_code == 502


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


def test_cancel_requires_order_domain_scope():
    resp = _client("orders").delete(  # read scope is NOT a write grant
        "/api/v1/accounts/1234/orders/777", headers=_AUTH,
    )
    assert resp.status_code == 403


def test_cancel_happy_path(monkeypatch):
    from schwab_cli.server.orders_write import _CancelGateway

    calls = []
    monkeypatch.setattr(
        _CancelGateway, "cancel",
        lambda self, account, order_id: calls.append((account, order_id)),
    )
    resp = _client("order:default").delete(
        "/api/v1/accounts/1234/orders/777", headers=_AUTH,
    )
    assert resp.status_code == 200
    assert resp.json() == {"cancelled": "777"}
    assert calls == [("1234", "777")]


def test_cancel_api_error_maps_to_502(monkeypatch):
    from schwab_cli.server.orders_write import _CancelGateway
    from schwab_cli.service.auth import ApiError

    def boom(self, account, order_id):
        raise ApiError("404 order not found")

    monkeypatch.setattr(_CancelGateway, "cancel", boom)
    resp = _client("order:*").delete(
        "/api/v1/accounts/1234/orders/777", headers=_AUTH,
    )
    assert resp.status_code == 502


# ---------------------------------------------------------------------------
# Explicit-profile contract + hardening cases
# ---------------------------------------------------------------------------


def test_place_without_profile_is_400(profile_ok):
    """No fallback to 'default' — the writer must consciously name the
    policy profile it runs under."""
    body = {k: v for k, v in _ORDER.items() if k != "profile"}
    resp = _client("order:*").post("/api/v1/orders", json=body, headers=_AUTH)
    assert resp.status_code == 400
    assert "profile is required" in resp.json()["error"]


def test_place_bad_account_format_is_400(profile_ok):
    body = {**_ORDER, "account": "12ab"}
    resp = _client("order:default").post(
        "/api/v1/orders", json=body, headers=_AUTH,
    )
    assert resp.status_code == 400


def test_toctou_profile_loss_maps_to_403(profile_ok, monkeypatch):
    """Profile vanished between pre-check and LoadProfileRule: the
    pipeline halts with EXIT_USAGE + profile_load_error — same 403
    contract as the pre-check, never a generic 400."""
    from schwab_cli.commands.order import EXIT_USAGE

    ctx = _Ctx(profile_load_error="profile file disappeared")
    _gateway_raises(monkeypatch, PipelineExit(EXIT_USAGE), ctx)
    resp = _client("order:default").post(
        "/api/v1/orders", json=_ORDER, headers=_AUTH,
    )
    assert resp.status_code == 403
    assert resp.json()["error"] == "profile unavailable"


def test_success_without_approved_decision_is_refused(profile_ok, monkeypatch):
    """Defense in depth: a pipeline 'success' with no approved policy
    decision must never surface as a 201."""
    ctx = _Ctx()
    ctx.policy_decision = None
    _gateway_returns(monkeypatch, ctx)
    resp = _client("order:default").post(
        "/api/v1/orders", json=_ORDER, headers=_AUTH,
    )
    assert resp.status_code == 500
    assert "invariant" in resp.json()["error"]


def test_idempotency_key_replays_without_second_placement(profile_ok, monkeypatch):
    calls = []

    def run(self, **kw):
        calls.append(1)
        ctx = _Ctx(order_id="42")
        self.last_ctx = ctx
        return ctx

    monkeypatch.setattr(_PlacementGateway, "run", run)
    body = {**_ORDER, "idempotency_key": "abc-123"}
    c = _client("order:default")
    first = c.post("/api/v1/orders", json=body, headers=_AUTH)
    second = c.post("/api/v1/orders", json=body, headers=_AUTH)
    assert first.status_code == 201 and second.status_code == 201
    assert second.json()["order_id"] == "42"
    assert second.json()["replayed"] is True
    assert len(calls) == 1  # the pipeline ran exactly once


def test_validation_detail_reaches_the_caller(profile_ok):
    """CLI validators echo the reason to stderr before raising — the
    REST layer captures it so the 400 explains WHAT was wrong."""
    body = {
        "account": "1234", "profile": "default",
        "order": {"symbol": "SPY", "order_type": "LIMIT"},  # no price
    }
    resp = _client("order:default").post(
        "/api/v1/orders", json=body, headers=_AUTH,
    )
    assert resp.status_code == 400
    assert resp.json()["error"] != "invalid order parameters"  # has detail


def test_upstream_error_body_is_generic(profile_ok, monkeypatch):
    """ApiError detail can carry account/order data — the 502 body must
    stay generic (detail goes to the audit log)."""
    from schwab_cli.service.auth import ApiError

    _gateway_raises(
        monkeypatch,
        ApiError("500 {accountHash: SECRET, order: {...}}"),
        _Ctx(),
    )
    resp = _client("order:default").post(
        "/api/v1/orders", json=_ORDER, headers=_AUTH,
    )
    assert resp.status_code == 502
    assert resp.json() == {"error": "upstream API error"}
    assert "SECRET" not in resp.text


def test_cancel_bad_order_id_is_400():
    resp = _client("order:*").delete(
        "/api/v1/accounts/1234/orders/not-an-id", headers=_AUTH,
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Dollar-denominated (notional) orders
# ---------------------------------------------------------------------------


def test_place_dollar_order_accepted(profile_ok, monkeypatch):
    _gateway_returns(monkeypatch, _Ctx())
    body = {
        "account": "1234", "profile": "default",
        "order": {"symbol": "QQQ", "side": "BUY", "dollar": 500.00,
                  "order_type": "MARKET"},
    }
    resp = _client("order:default").post(
        "/api/v1/orders", json=body, headers=_AUTH,
    )
    assert resp.status_code == 201


def test_place_dollar_and_quantity_mutually_exclusive(profile_ok):
    body = {
        "account": "1234", "profile": "default",
        "order": {"symbol": "QQQ", "dollar": 500.0, "quantity": 1},
    }
    resp = _client("order:default").post(
        "/api/v1/orders", json=body, headers=_AUTH,
    )
    assert resp.status_code == 400
    assert "mutually exclusive" in resp.json()["error"]


def test_place_dollar_negative_rejected(profile_ok):
    body = {
        "account": "1234", "profile": "default",
        "order": {"symbol": "QQQ", "dollar": -5},
    }
    resp = _client("order:default").post(
        "/api/v1/orders", json=body, headers=_AUTH,
    )
    assert resp.status_code == 400
    assert "positive" in resp.json()["error"]
