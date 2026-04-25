"""Tests for the order audit log.

Two layers:

1. ``schwab_cli.audit`` unit tests — file path + writes are pure I/O.
2. End-to-end tests that drive ``schwab_cli order ...`` and assert
   the audit log captures the full lifecycle (invoke → preview →
   confirm/abort → place/reject/cancel). The audit dir is redirected
   to ``tmp_path`` per test so we never touch ``~/.config``.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
from typer.testing import CliRunner

from schwab_cli import audit
from schwab_cli.api.client import AccountIds
from schwab_cli.cli import app
from schwab_cli.config import Config, save as save_config
from schwab_cli.session import Session, save as save_session


runner = CliRunner()


# ---- audit module unit tests ---------------------------------------------


def test_today_path_uses_iso_date(tmp_path: Path):
    p = audit.today_path(base_dir=tmp_path, today=date(2026, 4, 25))
    assert p == tmp_path / "2026-04-25.order.log"


def test_write_event_appends_jsonl(tmp_path: Path):
    audit.write_event(
        {"subcommand": "place", "stage": "invoked"},
        base_dir=tmp_path,
        today=date(2026, 4, 25),
        now=datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc),
    )
    audit.write_event(
        {"subcommand": "place", "stage": "placed", "order_id": "999"},
        base_dir=tmp_path,
        today=date(2026, 4, 25),
        now=datetime(2026, 4, 25, 12, 0, 1, tzinfo=timezone.utc),
    )
    log = (tmp_path / "2026-04-25.order.log").read_text()
    rows = [json.loads(line) for line in log.splitlines()]
    assert len(rows) == 2
    assert rows[0]["stage"] == "invoked"
    assert rows[0]["ts"] == "2026-04-25T12:00:00+00:00"
    assert rows[1]["stage"] == "placed"
    assert rows[1]["order_id"] == "999"


def test_write_event_creates_directory(tmp_path: Path):
    nested = tmp_path / "deep" / "audit"
    audit.write_event(
        {"subcommand": "place", "stage": "invoked"},
        base_dir=nested, today=date(2026, 4, 25),
    )
    assert (nested / "2026-04-25.order.log").exists()


def test_write_event_swallows_oserror(tmp_path: Path, capsys):
    # Make the parent path a regular file so mkdir + open will fail.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    audit.write_event(
        {"stage": "invoked"},
        base_dir=blocker / "audit", today=date(2026, 4, 25),
    )
    captured = capsys.readouterr()
    assert "audit log write failed" in captured.err


def test_sanitise_body_drops_account_number_and_underscores():
    body = {
        "orderType": "LIMIT", "quantity": 1,
        "accountNumber": "12345678",   # admin echo we tag for our own log only
        "_internal": "x",
    }
    cleaned = audit.sanitise_body(body)
    assert "accountNumber" not in cleaned
    assert "_internal" not in cleaned
    assert cleaned["orderType"] == "LIMIT"


# ---- end-to-end audit captures via the CLI -------------------------------


_ACCT = AccountIds(account_number="12345678", hash_value="HASH")
_PREVIEW_PAYLOAD = {
    "commission": 1.30,
    "fees": 0.05,
    "orderValueImpact": {
        "buyingPowerEffect": -86.35,
        "buyingPowerAfter": 14213.65,
    },
    "orderValidationResult": {"warnings": [], "rejects": []},
}


def _prep(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_PROFILE", raising=False)
    monkeypatch.setattr(
        audit, "DEFAULT_AUDIT_DIR", tmp_path / "audit",
    )
    # Phase 2f: place runs through the policy gate, which needs an
    # explicit default.json on disk — no bundled fallback.
    profiles_dir = tmp_path / "profiles" / "order"
    monkeypatch.setenv("SCHWAB_CLI_POLICY_DIR", str(profiles_dir))
    profiles_dir.mkdir(parents=True, exist_ok=True)
    (profiles_dir / "default.json").write_text(json.dumps({
        "default_action": "allow", "policies": [],
    }))
    save_config(Config(
        client_id="cid", client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
    ))
    save_session(Session(
        access_token="atok", refresh_token="rtok",
        expires_at=9_000_000_000, refresh_token_expires_at=9_000_000_000,
    ))
    return tmp_path / "audit"


def _audit_rows(audit_dir: Path) -> list[dict]:
    """Read every row across every audit file in ``audit_dir``."""
    rows: list[dict] = []
    for path in sorted(audit_dir.glob("*.order.log")):
        for line in path.read_text().splitlines():
            rows.append(json.loads(line))
    return rows


def _stages(rows: list[dict]) -> list[str]:
    return [r["stage"] for r in rows]


def _patches(*, place_calls=None, preview_payload=None,
             list_payload=None, get_payload=None,
             cancel_response=None, place_side_effect=None):
    pp = preview_payload if preview_payload is not None else _PREVIEW_PAYLOAD
    lp = list_payload if list_payload is not None else []
    gp = get_payload if get_payload is not None else {
        "orderId": 777, "status": "WORKING",
        "orderType": "LIMIT", "duration": "DAY", "session": "NORMAL",
        "orderLegCollection": [],
    }
    cr = cancel_response if cancel_response is not None else httpx.Response(200)

    def _record_place(client, account_hash, body):
        if place_side_effect:
            return place_side_effect(client, account_hash, body)
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


def _enter(patches): return [p.__enter__() for p in patches]
def _exit(patches):
    for p in reversed(patches):
        p.__exit__(None, None, None)


# ---- preview / dry-run ---------------------------------------------------


def test_audit_preview_logs_invoke_preview_dry_run(monkeypatch, tmp_path):
    audit_dir = _prep(monkeypatch, tmp_path)
    patches = _patches()
    _enter(patches)
    try:
        result = runner.invoke(app, [
            "order", "preview", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
        ])
    finally:
        _exit(patches)
    assert result.exit_code == 0
    rows = _audit_rows(audit_dir)
    stages = _stages(rows)
    assert stages == [
        "invoked", "body_built", "preview_ok",
        "policy_evaluated", "dry_run_done",
    ]
    invoked = rows[0]
    _prev = rows[2]
    pol = rows[3]
    assert invoked["subcommand"] == "preview"
    assert invoked["account"] == "5678"
    assert invoked["flags"]["price"] == 150
    assert _prev["commission"] == 1.30
    assert _prev["bp_effect"] == -86.35
    assert pol["profile_name"] == "default"
    assert pol["decision"] == "approve"


# ---- placement: yes path -------------------------------------------------


def test_audit_place_with_yes_logs_full_lifecycle(monkeypatch, tmp_path):
    audit_dir = _prep(monkeypatch, tmp_path)
    patches = _patches()
    _enter(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
            "--yes",
        ])
    finally:
        _exit(patches)
    assert result.exit_code == 0
    rows = _audit_rows(audit_dir)
    stages = _stages(rows)
    assert stages == [
        "invoked", "body_built", "preview_ok",
        "policy_evaluated", "confirmed", "placed",
    ]
    placed = rows[-1]
    assert placed["order_id"] == "987654"
    assert placed["account"] == "12345678"
    confirmed = rows[4]
    assert confirmed["via"] == "--yes"


# ---- placement: aborted prompt -------------------------------------------


def test_audit_logs_abort_when_user_does_not_say_yea(monkeypatch, tmp_path):
    audit_dir = _prep(monkeypatch, tmp_path)
    patches = _patches()
    _enter(patches)
    try:
        result = runner.invoke(
            app,
            ["order", "place", "AAPL", "--account", "5678",
             "--type", "LIMIT", "--price", "150", "--side", "BUY"],
            input="no\n",
        )
    finally:
        _exit(patches)
    assert result.exit_code == 0
    rows = _audit_rows(audit_dir)
    stages = _stages(rows)
    assert "aborted" in stages
    assert "placed" not in stages
    assert "rejected" not in stages


# ---- placement: Schwab rejection ------------------------------------------


def test_audit_logs_rejection_on_schwab_4xx(monkeypatch, tmp_path):
    audit_dir = _prep(monkeypatch, tmp_path)
    from schwab_cli.api.client import ApiError

    def _fail(client, account_hash, body):
        raise ApiError("400 insufficient buying power")

    patches = _patches(place_side_effect=_fail)
    _enter(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
            "--yes",
        ])
    finally:
        _exit(patches)
    assert result.exit_code == 3, result.stderr  # EXIT_REJECTED
    rows = _audit_rows(audit_dir)
    stages = _stages(rows)
    assert stages == [
        "invoked", "body_built", "preview_ok",
        "policy_evaluated", "confirmed", "rejected",
    ]
    rejected = rows[-1]
    assert "insufficient" in rejected["error"].lower()


# ---- list / get / cancel -------------------------------------------------


def test_audit_list_logs_invoked_and_fetched(monkeypatch, tmp_path):
    audit_dir = _prep(monkeypatch, tmp_path)
    patches = _patches(list_payload=[
        {"orderId": 1, "status": "WORKING", "orderType": "LIMIT",
         "enteredTime": "2026-04-25T12:00:00.000Z", "orderLegCollection": []},
    ])
    _enter(patches)
    try:
        result = runner.invoke(app, [
            "order", "list", "--account", "5678", "--json",
        ])
    finally:
        _exit(patches)
    assert result.exit_code == 0
    rows = _audit_rows(audit_dir)
    stages = _stages(rows)
    assert stages == ["invoked", "fetched"]
    assert rows[0]["status"] == "ACTIVE"
    assert rows[1]["result_count"] == 1


def test_audit_get_logs_invoked_and_fetched(monkeypatch, tmp_path):
    audit_dir = _prep(monkeypatch, tmp_path)
    patches = _patches()
    _enter(patches)
    try:
        result = runner.invoke(app, [
            "order", "get", "777", "--account", "5678",
        ])
    finally:
        _exit(patches)
    assert result.exit_code == 0
    rows = _audit_rows(audit_dir)
    stages = _stages(rows)
    assert stages == ["invoked", "fetched"]
    assert rows[1]["status"] == "WORKING"
    assert rows[1]["order_id"] == "777"


def test_audit_cancel_with_yes_logs_lifecycle(monkeypatch, tmp_path):
    audit_dir = _prep(monkeypatch, tmp_path)
    patches = _patches()
    _enter(patches)
    try:
        result = runner.invoke(app, [
            "order", "cancel", "777", "--account", "5678", "--yes",
        ])
    finally:
        _exit(patches)
    assert result.exit_code == 0
    rows = _audit_rows(audit_dir)
    stages = _stages(rows)
    assert stages == [
        "invoked", "found", "confirmed", "cancelled",
    ]


def test_audit_cancel_aborted(monkeypatch, tmp_path):
    audit_dir = _prep(monkeypatch, tmp_path)
    patches = _patches()
    _enter(patches)
    try:
        result = runner.invoke(
            app,
            ["order", "cancel", "777", "--account", "5678"],
            input="no\n",
        )
    finally:
        _exit(patches)
    assert result.exit_code == 0
    rows = _audit_rows(audit_dir)
    stages = _stages(rows)
    assert "aborted" in stages
    assert "cancelled" not in stages
