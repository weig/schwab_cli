"""End-to-end tests for ``schwab_cli order ...``.

**SAFETY**: every Schwab API call is mocked. The tests intentionally
patch :func:`schwab_cli.commands.order.place_order` (etc.) so a stray
exit can't reach the real Schwab server. CI must NEVER run these
tests against a live network.

Coverage:
    - ``order preview`` (and ``order place --dry-run``) build the body
      and render the panel without calling placeOrder.
    - ``order place --yes`` calls placeOrder exactly once and prints
      the returned id.
    - ``order place`` aborts cleanly when stdin doesn't supply "yes".
    - ``--parse`` mutex check.
    - ``--leg`` parsing and option-leg expansion.
    - ``order list`` defaults: ACTIVE → ALL → 60-day window;
      synthetic categories filter client-side.
    - ``order get`` and ``order cancel`` happy paths.
"""

from __future__ import annotations

import json
import re
from unittest.mock import patch

import httpx
from typer.testing import CliRunner

from schwab_cli.api.client import AccountIds
from schwab_cli.cli import app
from schwab_cli.config import Config, save as save_config
from schwab_cli.session import Session, save as save_session


runner = CliRunner()


def _prep(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_PROFILE", raising=False)
    # Redirect the audit log to tmp_path so tests don't write into
    # the real ~/.config/schwab_cli/audit/ directory.
    from schwab_cli import audit as audit_mod
    monkeypatch.setattr(
        audit_mod, "DEFAULT_AUDIT_DIR", tmp_path / "audit",
    )
    # Phase 2f: reserved profiles are gone. Tests that exercise the
    # policy gate (or any path that runs through it) need an explicit
    # default.json on disk. Use a permissive baseline so existing
    # tests still see "approve" by default.
    profiles_dir = tmp_path / "profiles" / "order"
    monkeypatch.setenv("SCHWAB_CLI_POLICY_DIR", str(profiles_dir))
    profiles_dir.mkdir(parents=True, exist_ok=True)
    import json as _json
    (profiles_dir / "default.json").write_text(_json.dumps({
        "default_action": "allow",
        "policies": [],
    }))
    save_config(Config(
        client_id="cid", client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
    ))
    save_session(Session(
        access_token="atok", refresh_token="rtok",
        expires_at=9_000_000_000, refresh_token_expires_at=9_000_000_000,
    ))


_ACCT = AccountIds(account_number="12345678", hash_value="HASH")
_PREVIEW_PAYLOAD = {
    "commissionAndFee": {
        "commission": {"commissionLegs": [
            {"commissionValues": [{"type": "COMMISSION", "value": 1.30}]},
        ]},
        "fee": {"feeLegs": [
            {"feeValues": [{"type": "SEC_FEE", "value": 0.05}]},
        ]},
    },
    "orderStrategy": {
        "orderBalance": {
            "orderValue": 86.35,
            "projectedBuyingPower": 14213.65,
            "projectedAvailableFund": 7106.83,
        },
        "orderLegs": [{"instruction": "BUY"}],
    },
    "orderValidationResult": {"warnings": [], "rejects": []},
}


def _patches(*, place_calls=None, preview_payload=None, list_payload=None,
             get_payload=None, cancel_response=None):
    """Build the standard set of patches: account resolution + every
    Schwab API touchpoint stubbed. Returns a list of context-managers
    the caller installs in an ExitStack-equivalent ``with`` chain."""
    pp = preview_payload if preview_payload is not None else _PREVIEW_PAYLOAD
    lp = list_payload if list_payload is not None else []
    gp = get_payload if get_payload is not None else {
        "orderId": 777, "status": "WORKING",
        "orderType": "LIMIT", "duration": "DAY", "session": "NORMAL",
        "orderLegCollection": [],
    }
    cr = cancel_response if cancel_response is not None else httpx.Response(200)

    def _record_place(client, account_hash, body):
        if place_calls is not None:
            place_calls.append((account_hash, body))
        return ("987654", httpx.Response(
            201, headers={"Location": f"/accounts/{account_hash}/orders/987654"},
        ))

    return [
        patch("schwab_cli.commands.order.SchwabClient.resolve_account",
              return_value=_ACCT),
        patch("schwab_cli.commands.order.SchwabClient.account_ids",
              return_value=[_ACCT]),
        patch("schwab_cli.commands.order.preview_order", return_value=pp),
        patch("schwab_cli.commands.order.place_order",
              side_effect=_record_place),
        patch("schwab_cli.commands.order.list_orders_for_account",
              return_value=lp),
        patch("schwab_cli.commands.order.list_orders_all_accounts",
              return_value=lp),
        patch("schwab_cli.commands.order.get_order", return_value=gp),
        patch("schwab_cli.commands.order.cancel_order",
              return_value=cr),
    ]


def _enter_all(patches):
    return [p.__enter__() for p in patches]


def _exit_all(patches):
    for p in reversed(patches):
        p.__exit__(None, None, None)


# ---- preview / dry-run ---------------------------------------------------


def test_preview_renders_panel_without_placing(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    place_calls: list = []
    patches = _patches(place_calls=place_calls)
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "preview", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--quantity", "10",
            "--side", "BUY",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert place_calls == [], "preview must NOT call placeOrder"
    # Panel goes to stderr.
    assert "Confirm Order" in result.stderr
    assert "********5678" in result.stderr


def test_place_dry_run_equivalent_to_preview(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    place_calls: list = []
    patches = _patches(place_calls=place_calls)
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
            "--dry-run",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code == 0
    assert place_calls == []
    assert "dry-run" in result.stderr.lower()


def test_dry_run_with_json_emits_body(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    place_calls: list = []
    patches = _patches(place_calls=place_calls)
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
            "--dry-run", "--json",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["order"]["orderType"] == "LIMIT"
    assert payload["order"]["price"] == "150.00"


# ---- placement w/ confirmation -------------------------------------------


def test_place_with_yes_skips_prompt_and_calls_placeOrder(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    place_calls: list = []
    patches = _patches(place_calls=place_calls)
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
            "--yes",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert len(place_calls) == 1
    account_hash, body = place_calls[0]
    assert account_hash == "HASH"
    assert body["orderType"] == "LIMIT"
    assert body["orderLegCollection"][0]["instrument"]["symbol"] == "AAPL"
    assert "Schwab: placed order 987654" in result.stderr


def test_place_aborts_when_user_does_not_type_yea(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    place_calls: list = []
    patches = _patches(place_calls=place_calls)
    _enter_all(patches)
    try:
        result = runner.invoke(
            app,
            ["order", "place", "AAPL", "--account", "5678",
             "--type", "LIMIT", "--price", "150", "--side", "BUY"],
            input="no\n",
        )
    finally:
        _exit_all(patches)
    assert result.exit_code == 0
    assert place_calls == [], "must NOT place when prompt rejected"
    assert "aborted" in result.stderr


def test_place_proceeds_when_user_types_yes(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    place_calls: list = []
    patches = _patches(place_calls=place_calls)
    _enter_all(patches)
    try:
        result = runner.invoke(
            app,
            ["order", "place", "AAPL", "--account", "5678",
             "--type", "LIMIT", "--price", "150", "--side", "BUY"],
            input="YES\n",
        )
    finally:
        _exit_all(patches)
    assert result.exit_code == 0
    assert len(place_calls) == 1


# ---- --parse mutex / multi-leg ------------------------------------------


def test_parse_mutex_with_price(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    patches = _patches()
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "--account", "5678",
            "--parse", "BUY +1 NVDA @100 LMT",
            "--price", "100",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code == 2
    assert "--parse may not be combined" in result.stderr


def test_parse_vertical_builds_two_legs(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    place_calls: list = []
    patches = _patches(place_calls=place_calls)
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "place",
            "--account", "5678",
            "--parse",
            "BUY +1 VERTICAL AMZN 100 (Weeklys) 1 MAY 26 262.5/267.5 CALL @2.35 LMT",
            "--yes",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert len(place_calls) == 1
    _, body = place_calls[0]
    assert body["orderType"] == "NET_DEBIT"
    assert body["complexOrderStrategyType"] == "VERTICAL"
    assert body["price"] == "2.35"
    legs = body["orderLegCollection"]
    assert len(legs) == 2
    assert legs[0]["instruction"] == "BUY_TO_OPEN"
    assert legs[0]["instrument"]["symbol"] == "AMZN  260501C00262500"
    assert legs[1]["instruction"] == "SELL_TO_OPEN"
    assert legs[1]["instrument"]["symbol"] == "AMZN  260501C00267500"


def test_leg_flag_builds_option_legs(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    place_calls: list = []
    patches = _patches(place_calls=place_calls)
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "AMZN",
            "--account", "5678",
            "--leg", "+1@20260501C262.5",
            "--leg", "-1@20260501C267.5",
            "--type", "LIMIT", "--price", "2.35",
            "--complex", "VERTICAL",
            "--yes",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code == 0, (result.stdout, result.stderr)
    _, body = place_calls[0]
    assert body["complexOrderStrategyType"] == "VERTICAL"
    assert body["orderType"] == "NET_DEBIT"  # auto-rewritten for multi-leg LIMIT
    assert body["orderLegCollection"][0]["instrument"]["symbol"] == "AMZN  260501C00262500"


# ---- validation guards ----------------------------------------------------


def test_account_required_for_place(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    patches = _patches()
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL",
            "--type", "LIMIT", "--price", "100", "--side", "BUY",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code == 2
    assert "--account is required" in result.stderr


def test_limit_requires_price(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    patches = _patches()
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--side", "BUY",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code == 2
    assert "requires --price" in result.stderr


def test_extended_session_only_with_limit_day(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    patches = _patches()
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "MARKET", "--side", "BUY",
            "--session", "PM",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code == 2
    assert "PM requires" in result.stderr or "AM/PM" in result.stderr


# ---- preview unavailable fallback -----------------------------------------


def test_preview_unavailable_renders_panel_with_unavailable_fields(
    monkeypatch, tmp_path,
):
    _prep(monkeypatch, tmp_path)
    place_calls: list = []
    from schwab_cli.api.client import ApiError
    patches = _patches(place_calls=place_calls)
    # Override preview_order to raise 404.
    p = patch(
        "schwab_cli.commands.order.preview_order",
        side_effect=ApiError("404 not enabled"),
    )
    _enter_all(patches)
    p.__enter__()
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
            "--dry-run",
        ])
    finally:
        p.__exit__(None, None, None)
        _exit_all(patches)
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert "preview unavailable" in result.stderr.lower() or \
           "unavailable" in result.stderr


# ---- list -----------------------------------------------------------------


def test_list_active_default_filters_client_side(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    payload = [
        {"orderId": 1, "status": "WORKING", "orderType": "LIMIT",
         "enteredTime": "2026-04-25T13:00:00.000Z", "orderLegCollection": []},
        {"orderId": 2, "status": "FILLED", "orderType": "LIMIT",
         "enteredTime": "2026-04-25T12:00:00.000Z", "orderLegCollection": []},
        {"orderId": 3, "status": "REJECTED", "orderType": "LIMIT",
         "enteredTime": "2026-04-25T11:00:00.000Z", "orderLegCollection": []},
    ]
    patches = _patches(list_payload=payload)
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "list", "--account", "5678", "--json",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code == 0, (result.stdout, result.stderr)
    out = json.loads(result.stdout)
    # ACTIVE category should only keep status=WORKING here.
    assert [o["orderId"] for o in out] == [1]


def test_list_filled_uses_default_7d_range(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    captured: dict = {}

    def _capture(client, account_hash, *, start, end, status=None, max_results=None):
        captured["start"] = start
        captured["end"] = end
        captured["status"] = status
        return [{"orderId": 99, "status": "FILLED",
                 "enteredTime": "2026-04-22T12:00:00.000Z",
                 "orderType": "LIMIT", "orderLegCollection": []}]

    patches = _patches()
    p = patch(
        "schwab_cli.commands.order.list_orders_for_account",
        side_effect=_capture,
    )
    _enter_all(patches)
    p.__enter__()
    try:
        result = runner.invoke(app, [
            "order", "list", "--account", "5678", "--status", "FILLED",
            "--json",
        ])
    finally:
        p.__exit__(None, None, None)
        _exit_all(patches)
    assert result.exit_code == 0
    delta = (captured["end"] - captured["start"]).days
    assert 6 <= delta <= 8, f"FILLED default range should be ~7d, got {delta}d"
    # Synthetic FILLED → no server-side status filter (we filter client-side).
    assert captured["status"] is None


def test_list_no_account_warns_and_uses_cross_account(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    payload = [{"orderId": 1, "status": "WORKING",
                "enteredTime": "2026-04-25T12:00:00.000Z",
                "orderType": "LIMIT", "orderLegCollection": []}]
    patches = _patches(list_payload=payload)
    _enter_all(patches)
    try:
        result = runner.invoke(app, ["order", "list", "--json"])
    finally:
        _exit_all(patches)
    assert result.exit_code == 0
    assert "no --account" in result.stderr.lower()


def test_list_range_over_60_days_rejected(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    patches = _patches()
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "list", "--account", "5678",
            "--status", "FILLED", "--range", "-3mo..now",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code == 2
    assert "60-day" in result.stderr


# ---- get / cancel ---------------------------------------------------------


def test_get_renders_human(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    patches = _patches()
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "get", "777", "--account", "5678",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code == 0
    assert "Order 777" in result.stdout


def test_get_json(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    patches = _patches()
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "get", "777", "--account", "5678", "--json",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["orderId"] == 777


def test_cancel_with_yes_skips_prompt(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    patches = _patches()
    cancel_calls: list = []
    p = patch(
        "schwab_cli.commands.order.cancel_order",
        side_effect=lambda c, h, oid: cancel_calls.append((h, oid))
        or httpx.Response(200),
    )
    _enter_all(patches)
    p.__enter__()
    try:
        result = runner.invoke(app, [
            "order", "cancel", "777", "--account", "5678", "--yes",
        ])
    finally:
        p.__exit__(None, None, None)
        _exit_all(patches)
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert len(cancel_calls) == 1
    assert cancel_calls[0] == ("HASH", "777")
    assert "cancel requested" in result.stderr


def test_cancel_aborts_without_yea(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    patches = _patches()
    cancel_calls: list = []
    p = patch(
        "schwab_cli.commands.order.cancel_order",
        side_effect=lambda c, h, oid: cancel_calls.append((h, oid))
        or httpx.Response(200),
    )
    _enter_all(patches)
    p.__enter__()
    try:
        result = runner.invoke(
            app,
            ["order", "cancel", "777", "--account", "5678"],
            input="nope\n",
        )
    finally:
        p.__exit__(None, None, None)
        _exit_all(patches)
    assert result.exit_code == 0
    assert cancel_calls == [], "must NOT cancel when prompt rejected"
    assert "aborted" in result.stderr


# ---- verify-and-rollback safety net --------------------------------------


def test_safe_place_rolls_back_when_network_error_finds_match(
    monkeypatch, tmp_path,
):
    """place_order raises a non-4xx error → list shows our order on
    Schwab → cancel called → original error re-raised → exit 1."""
    _prep(monkeypatch, tmp_path)
    from schwab_cli.api.client import ApiError

    def _network_fail(client, account_hash, body):
        raise ApiError("network: TimeoutException")

    list_payload = [{
        "orderId": 555, "status": "WORKING",
        "orderType": "LIMIT", "complexOrderStrategyType": "NONE",
        "price": 150.00,
        "enteredTime": "2026-04-25T12:00:00.000Z",
        "orderLegCollection": [{
            "instruction": "BUY",
            "quantity": 1,
            "instrument": {"assetType": "EQUITY", "symbol": "AAPL"},
        }],
    }]
    cancel_calls: list = []
    patches = _patches(list_payload=list_payload)
    p_place = patch("schwab_cli.commands.order.place_order", side_effect=_network_fail)
    p_cancel = patch(
        "schwab_cli.commands.order.cancel_order",
        side_effect=lambda c, h, oid: cancel_calls.append((h, oid)) or httpx.Response(200),
    )
    _enter_all(patches)
    p_place.__enter__()
    p_cancel.__enter__()
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--quantity", "1",
            "--side", "BUY", "--yes",
        ])
    finally:
        p_cancel.__exit__(None, None, None)
        p_place.__exit__(None, None, None)
        _exit_all(patches)
    assert result.exit_code == 1, (result.stdout, result.stderr)
    assert cancel_calls == [("HASH", "555")]
    assert "found 1 matching order" in result.stderr
    assert "cancelled: 555" in result.stderr


def test_safe_place_no_rollback_when_no_match(monkeypatch, tmp_path):
    """place_order raises → list shows nothing matching → no cancel →
    user told the order likely never reached Schwab."""
    _prep(monkeypatch, tmp_path)
    from schwab_cli.api.client import ApiError

    def _network_fail(client, account_hash, body):
        raise ApiError("network: ConnectError")

    cancel_calls: list = []
    patches = _patches(list_payload=[])  # no matches
    p_place = patch("schwab_cli.commands.order.place_order", side_effect=_network_fail)
    p_cancel = patch(
        "schwab_cli.commands.order.cancel_order",
        side_effect=lambda c, h, oid: cancel_calls.append((h, oid)) or httpx.Response(200),
    )
    _enter_all(patches)
    p_place.__enter__()
    p_cancel.__enter__()
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
            "--yes",
        ])
    finally:
        p_cancel.__exit__(None, None, None)
        p_place.__exit__(None, None, None)
        _exit_all(patches)
    assert result.exit_code == 1
    assert cancel_calls == []
    assert "no matching recent orders" in result.stderr


def test_safe_place_rollback_verify_failure_is_loudly_reported(
    monkeypatch, tmp_path,
):
    """place_order raises → list also raises → strongest warning,
    no cancel."""
    _prep(monkeypatch, tmp_path)
    from schwab_cli.api.client import ApiError

    def _place_fail(client, account_hash, body):
        raise ApiError("network: ConnectError")

    cancel_calls: list = []
    patches = _patches()  # default list patch returns []
    # Override list_orders_for_account to raise.
    p_list = patch(
        "schwab_cli.commands.order.list_orders_for_account",
        side_effect=ApiError("503 service unavailable"),
    )
    p_place = patch("schwab_cli.commands.order.place_order", side_effect=_place_fail)
    p_cancel = patch(
        "schwab_cli.commands.order.cancel_order",
        side_effect=lambda c, h, oid: cancel_calls.append((h, oid)) or httpx.Response(200),
    )
    _enter_all(patches)
    p_list.__enter__()
    p_place.__enter__()
    p_cancel.__enter__()
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
            "--yes",
        ])
    finally:
        p_cancel.__exit__(None, None, None)
        p_place.__exit__(None, None, None)
        p_list.__exit__(None, None, None)
        _exit_all(patches)
    assert result.exit_code == 1
    assert cancel_calls == [], "no cancellations when verify fails"
    assert "could not verify with Schwab" in result.stderr
    assert "Check your Schwab account immediately" in result.stderr


def test_safe_place_skips_rollback_for_definitive_4xx(monkeypatch, tmp_path):
    """4xx is Schwab's definitive 'no' — the order isn't placed, so
    no rollback should fire and no list/cancel calls happen."""
    _prep(monkeypatch, tmp_path)
    from schwab_cli.api.client import ApiError

    def _reject(client, account_hash, body):
        raise ApiError("400 insufficient buying power")

    list_calls: list = []
    cancel_calls: list = []
    patches = _patches()
    p_list = patch(
        "schwab_cli.commands.order.list_orders_for_account",
        side_effect=lambda *a, **kw: list_calls.append(1) or [],
    )
    p_place = patch("schwab_cli.commands.order.place_order", side_effect=_reject)
    p_cancel = patch(
        "schwab_cli.commands.order.cancel_order",
        side_effect=lambda c, h, oid: cancel_calls.append((h, oid)) or httpx.Response(200),
    )
    _enter_all(patches)
    p_list.__enter__()
    p_place.__enter__()
    p_cancel.__enter__()
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
            "--yes",
        ])
    finally:
        p_cancel.__exit__(None, None, None)
        p_place.__exit__(None, None, None)
        p_list.__exit__(None, None, None)
        _exit_all(patches)
    assert result.exit_code == 3, "definitive 4xx → EXIT_REJECTED"
    assert list_calls == [], "no rollback verify on 4xx"
    assert cancel_calls == [], "no cancel on 4xx"


def test_safe_place_rollback_audits_full_lifecycle(monkeypatch, tmp_path):
    """The audit log captures place_uncertainty → rollback_cancel_attempt →
    rollback_cancelled when the safety net fires."""
    audit_dir = tmp_path / "audit"
    _prep(monkeypatch, tmp_path)
    from schwab_cli.api.client import ApiError
    from schwab_cli import audit as audit_mod
    monkeypatch.setattr(audit_mod, "DEFAULT_AUDIT_DIR", audit_dir)

    def _network_fail(client, account_hash, body):
        raise ApiError("network: TimeoutException")

    list_payload = [{
        "orderId": 999, "status": "WORKING",
        "orderType": "LIMIT", "complexOrderStrategyType": "NONE",
        "price": 150.00,
        "enteredTime": "2026-04-25T12:00:00.000Z",
        "orderLegCollection": [{
            "instruction": "BUY", "quantity": 1,
            "instrument": {"assetType": "EQUITY", "symbol": "AAPL"},
        }],
    }]
    patches = _patches(list_payload=list_payload)
    p_place = patch("schwab_cli.commands.order.place_order", side_effect=_network_fail)
    p_cancel = patch(
        "schwab_cli.commands.order.cancel_order",
        return_value=httpx.Response(200),
    )
    _enter_all(patches)
    p_place.__enter__()
    p_cancel.__enter__()
    try:
        runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
            "--yes",
        ])
    finally:
        p_cancel.__exit__(None, None, None)
        p_place.__exit__(None, None, None)
        _exit_all(patches)

    # Read the JSONL audit log.
    log_files = list(audit_dir.glob("*.order.log"))
    assert len(log_files) == 1
    rows = [json.loads(line) for line in log_files[0].read_text().splitlines()]
    stages = [r["stage"] for r in rows]
    # Confirm the rollback stages are recorded.
    assert "place_uncertainty" in stages
    assert "rollback_cancel_attempt" in stages
    assert "rollback_cancelled" in stages
    # And final place_failed (re-raised network error → place_failed audit).
    assert "place_failed" in stages


# ---- _orders_match unit tests ---------------------------------------------


def test_orders_match_identical_equity_body():
    from schwab_cli.commands.order import _orders_match
    body = {
        "orderType": "LIMIT", "complexOrderStrategyType": "NONE",
        "price": "150.00",
        "orderLegCollection": [{
            "instruction": "BUY", "quantity": 1,
            "instrument": {"assetType": "EQUITY", "symbol": "AAPL"},
        }],
    }
    schwab = {
        "orderType": "LIMIT", "complexOrderStrategyType": "NONE",
        "price": 150.0,
        "orderLegCollection": [{
            "instruction": "BUY", "quantity": 1,
            "instrument": {"assetType": "EQUITY", "symbol": "AAPL"},
        }],
    }
    assert _orders_match(body, schwab) is True


def test_orders_match_different_price_rejects():
    from schwab_cli.commands.order import _orders_match
    body = {"orderType": "LIMIT", "price": "150.00",
            "orderLegCollection": [{"instruction": "BUY", "quantity": 1,
                                    "instrument": {"symbol": "AAPL"}}]}
    schwab = {"orderType": "LIMIT", "price": 151.0,
              "orderLegCollection": [{"instruction": "BUY", "quantity": 1,
                                      "instrument": {"symbol": "AAPL"}}]}
    assert _orders_match(body, schwab) is False


def test_orders_match_different_symbol_rejects():
    from schwab_cli.commands.order import _orders_match
    body = {"orderType": "LIMIT", "price": "150.00",
            "orderLegCollection": [{"instruction": "BUY", "quantity": 1,
                                    "instrument": {"symbol": "AAPL"}}]}
    schwab = {"orderType": "LIMIT", "price": 150.0,
              "orderLegCollection": [{"instruction": "BUY", "quantity": 1,
                                      "instrument": {"symbol": "MSFT"}}]}
    assert _orders_match(body, schwab) is False


def test_orders_match_market_no_price_required():
    from schwab_cli.commands.order import _orders_match
    body = {
        "orderType": "MARKET", "complexOrderStrategyType": "NONE",
        "orderLegCollection": [{"instruction": "BUY", "quantity": 1,
                                "instrument": {"symbol": "AAPL"}}],
    }
    schwab = {
        "orderType": "MARKET", "complexOrderStrategyType": "NONE",
        "orderLegCollection": [{"instruction": "BUY", "quantity": 1,
                                "instrument": {"symbol": "AAPL"}}],
    }
    assert _orders_match(body, schwab) is True


def test_orders_match_multi_leg_order_strict():
    from schwab_cli.commands.order import _orders_match
    body = {
        "orderType": "NET_DEBIT", "complexOrderStrategyType": "VERTICAL",
        "price": "2.35",
        "orderLegCollection": [
            {"instruction": "BUY_TO_OPEN", "quantity": 1,
             "instrument": {"symbol": "AMZN  260501C00262500"}},
            {"instruction": "SELL_TO_OPEN", "quantity": 1,
             "instrument": {"symbol": "AMZN  260501C00267500"}},
        ],
    }
    schwab_match = {
        "orderType": "NET_DEBIT", "complexOrderStrategyType": "VERTICAL",
        "price": 2.35,
        "orderLegCollection": [
            {"instruction": "BUY_TO_OPEN", "quantity": 1,
             "instrument": {"symbol": "AMZN  260501C00262500"}},
            {"instruction": "SELL_TO_OPEN", "quantity": 1,
             "instrument": {"symbol": "AMZN  260501C00267500"}},
        ],
    }
    assert _orders_match(body, schwab_match) is True
    # Same legs, different leg order → does NOT match (we compare positionally
    # to keep the matcher conservative; if Schwab returns legs in a different
    # order than we sent, we err toward "no match" — better to miss a rollback
    # than cancel an unrelated order).
    schwab_swapped = dict(schwab_match)
    schwab_swapped["orderLegCollection"] = list(reversed(schwab_match["orderLegCollection"]))
    assert _orders_match(body, schwab_swapped) is False


# ---- preview without a profile (regression) ----------------------------


def test_preview_renders_panel_when_no_profile_resolves(monkeypatch, tmp_path):
    """`order preview` should always show the panel, even when no
    profile resolves. Real `place` still hard-errors in that state."""
    # Set up everything except the default.json — strip it so the
    # loader returns "no profile resolved".
    _prep(monkeypatch, tmp_path)
    profiles_dir = tmp_path / "profiles" / "order"
    (profiles_dir / "default.json").unlink()  # remove what _prep wrote

    place_calls: list = []
    patches = _patches(place_calls=place_calls)
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "preview", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert place_calls == [], "preview must NOT call placeOrder"
    # Panel rendered to stderr.
    assert "Confirm Order" in result.stderr
    # Warning that policy gate was skipped.
    assert "no policy profile resolved" in result.stderr or \
           "no profile loaded" in result.stderr
    # Should still surface BP impact (Schwab preview ran).
    assert "Buying Power (Stock)" in result.stderr


def test_real_place_still_errors_when_no_profile_resolves(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    profiles_dir = tmp_path / "profiles" / "order"
    (profiles_dir / "default.json").unlink()

    place_calls: list = []
    patches = _patches(place_calls=place_calls)
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
            "--yes",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code == 2
    assert place_calls == []
    assert "policy load failed" in result.stderr


# ---- underlying-quote gating policy --------------------------------------
#
# preview        → fetch (standard)
# place (no -y)  → fetch (live)
# place (-y)     → skip


_QUOTE_PAYLOAD = {
    "AAPL": {"quote": {
        "lastPrice": 200.10,
        "bidPrice": 200.05, "askPrice": 200.15,
        "bidSize": 1500, "askSize": 800,
        "totalVolume": 12_345_678,
        "netChange": 1.25,
    }},
}


def test_preview_fetches_quote_and_renders_underlying_section(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    patches = _patches(place_calls=[])
    quote_calls: list = []
    def _stub_quotes(client, symbols, **kwargs):
        quote_calls.append(symbols)
        return _QUOTE_PAYLOAD
    patches.append(patch("schwab_cli.api.quotes.get_quotes",
                         side_effect=_stub_quotes))
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "preview", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert quote_calls == [["AAPL"]]
    # Underlying section + standard ("Quote") label, not the live one.
    assert "Underlying" in result.stderr
    assert "AAPL — Quote" in result.stderr
    assert "AAPL — Live Quote" not in result.stderr
    assert "200.05" in result.stderr  # bid
    assert "1,500" in result.stderr   # bid size


def test_place_without_yes_fetches_live_quote(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    place_calls: list = []
    patches = _patches(place_calls=place_calls)
    quote_calls: list = []
    def _stub_quotes(client, symbols, **kwargs):
        quote_calls.append(symbols)
        return _QUOTE_PAYLOAD
    patches.append(patch("schwab_cli.api.quotes.get_quotes",
                         side_effect=_stub_quotes))
    _enter_all(patches)
    try:
        # No --yes — interactive confirm prompt aborts at "no" (typer's
        # default when stdin has nothing). We just want to see that the
        # quote was fetched and labeled live.
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
        ], input="\n")
    finally:
        _exit_all(patches)
    # Quote was fetched once for the panel.
    assert quote_calls == [["AAPL"]]
    assert "AAPL — Live Quote" in result.stderr
    # Confirmation declined → no place call.
    assert place_calls == []


def test_place_with_yes_skips_quote_fetch(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    place_calls: list = []
    patches = _patches(place_calls=place_calls)
    quote_calls: list = []
    def _stub_quotes(client, symbols, **kwargs):
        quote_calls.append(symbols)
        return _QUOTE_PAYLOAD
    patches.append(patch("schwab_cli.api.quotes.get_quotes",
                         side_effect=_stub_quotes))
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
            "--yes",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code == 0, (result.stdout, result.stderr)
    # --yes path skips the quote fetch entirely.
    assert quote_calls == []
    # And the panel must not show the Underlying section.
    assert "Underlying" not in result.stderr
    assert len(place_calls) == 1


# ---- --type value validation --------------------------------------------


def test_type_rejects_side_word_with_helpful_hint(monkeypatch, tmp_path):
    """`--type SELL` is the user mixing --type with --side. Catch it
    before we hit Schwab."""
    _prep(monkeypatch, tmp_path)
    patches = _patches()
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "AXP", "--account", "5678",
            "--type", "SELL", "--price", "312",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code == 2
    assert "not a valid order type" in result.stderr
    assert "use --side" in result.stderr


def test_place_without_yes_starts_and_stops_live_ticker(monkeypatch, tmp_path):
    """Real-place + interactive confirm should construct a LiveTicker
    around the prompt: started before the readline, stopped after.
    The ticker class itself is exercised in test_live_ticker.py — here
    we just verify the wiring."""
    _prep(monkeypatch, tmp_path)
    place_calls: list = []
    patches = _patches(place_calls=place_calls)
    quote_calls: list = []
    def _stub_quotes(client, symbols, **kwargs):
        quote_calls.append(symbols)
        return _QUOTE_PAYLOAD
    patches.append(patch("schwab_cli.api.quotes.get_quotes",
                         side_effect=_stub_quotes))

    started: list = []
    stopped: list = []

    class _StubTicker:
        def __init__(self, *, fetch, render, initial_line, config=None):
            self._initial = initial_line
            self._fetch = fetch
        def start(self) -> None:
            started.append(self._initial)
        def stop(self) -> None:
            stopped.append(True)

    patches.append(
        patch("schwab_cli.order_pipeline.live_ticker.LiveTicker",
              _StubTicker),
    )

    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
        ], input="yes\n")
    finally:
        _exit_all(patches)
    assert result.exit_code == 0, (result.stdout, result.stderr)
    # Ticker constructed and used exactly once.
    assert len(started) == 1
    assert len(stopped) == 1
    # Initial line carried the symbol from the panel-time fetch.
    assert "AAPL" in started[0]
    # Order actually placed (we typed "yes").
    assert len(place_calls) == 1


def test_place_with_yes_does_not_start_ticker(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    place_calls: list = []
    patches = _patches(place_calls=place_calls)
    started: list = []

    class _StubTicker:
        def __init__(self, **kw):
            pass
        def start(self) -> None:
            started.append(True)
        def stop(self) -> None:
            pass

    patches.append(
        patch("schwab_cli.order_pipeline.live_ticker.LiveTicker",
              _StubTicker),
    )
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
            "--yes",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert started == []
    assert len(place_calls) == 1


def test_quantity_with_leg_is_rejected(monkeypatch, tmp_path):
    """``--quantity`` is ambiguous when paired with ``--leg`` (each leg
    already carries its own signed N). Reject before doing anything."""
    _prep(monkeypatch, tmp_path)
    patches = _patches()
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "preview", "AXP", "--account", "5678",
            "--quantity", "2", "--leg", "+1@260501C335",
            "--price", "0.40",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code == 2
    assert "--quantity may not be combined with --leg" in result.stderr


def test_yymmdd_leg_form_works_through_cli(monkeypatch, tmp_path):
    """End-to-end: a 6-digit YYMMDD leg parses cleanly through the CLI
    and reaches the panel."""
    _prep(monkeypatch, tmp_path)
    patches = _patches()
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "preview", "AXP", "--account", "5678",
            "--leg", "+1@260501C335", "--price", "0.40",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code == 0, (result.stdout, result.stderr)
    # OSI-style symbol on the leg row (date in the symbol is YYMMDD).
    assert "260501C00335000" in result.stderr


def test_type_rejects_garbage_value(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    patches = _patches()
    _enter_all(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "AXP", "--account", "5678",
            "--type", "BOGUS", "--price", "312", "--side", "BUY",
        ])
    finally:
        _exit_all(patches)
    assert result.exit_code == 2
    assert "not a valid order type" in result.stderr
