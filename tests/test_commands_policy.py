"""End-to-end tests for the ``policy`` subcommand group + the policy
gate hooked into ``order place``.

All Schwab calls mocked. Profile dirs use tmp_path so the real config
isn't touched.
"""

from __future__ import annotations

import json
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


def _prep(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_PROFILE", raising=False)
    monkeypatch.setattr(audit, "DEFAULT_AUDIT_DIR", tmp_path / "audit")
    monkeypatch.setattr(
        audit, "DEFAULT_HMAC_KEY_PATH", tmp_path / "audit_hmac.key",
    )
    profiles = tmp_path / "profiles" / "order"
    monkeypatch.setenv("SCHWAB_CLI_POLICY_DIR", str(profiles))
    save_config(Config(
        client_id="cid", client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
    ))
    save_session(Session(
        access_token="atok", refresh_token="rtok",
        expires_at=9_000_000_000, refresh_token_expires_at=9_000_000_000,
    ))
    return profiles


def _write_profile(profiles_dir: Path, name: str, body: dict) -> None:
    profiles_dir.mkdir(parents=True, exist_ok=True)
    (profiles_dir / f"{name}.json").write_text(json.dumps(body))


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


def _patches(*, place_calls=None):
    pc = place_calls

    def _record_place(client, account_hash, body):
        if pc is not None:
            pc.append((account_hash, body))
        return ("987654", httpx.Response(
            201, headers={"Location": f"/accounts/{account_hash}/orders/987654"},
        ))

    return [
        patch("schwab_cli.commands.order.SchwabClient.resolve_account",
              return_value=_ACCT),
        patch("schwab_cli.commands.order.SchwabClient.account_ids",
              return_value=[_ACCT]),
        patch("schwab_cli.commands.order.preview_order",
              return_value=_PREVIEW_PAYLOAD),
        patch("schwab_cli.commands.order.place_order",
              side_effect=_record_place),
    ]


def _enter(patches): return [p.__enter__() for p in patches]
def _exit(patches):
    for p in reversed(patches):
        p.__exit__(None, None, None)


# ---- `policy show / lint / test` -----------------------------------------


def test_policy_show_missing_default_returns_helpful_error(monkeypatch, tmp_path):
    """Phase 2f dropped bundled reserved profiles. Without a user
    file, `default` resolves to nothing and the loader points at
    `profile new`."""
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["policy", "show", "--profile", "default"])
    assert result.exit_code == 2
    assert "profile new" in result.stderr


def test_policy_show_user_profile_wins(monkeypatch, tmp_path):
    profiles = _prep(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "description": "user override",
        "default_action": "deny",
        "policies": [],
    })
    result = runner.invoke(app, ["policy", "show", "--profile", "default"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert "user override" in data["description"]
    assert data["default_action"] == "deny"


def test_policy_lint_all_with_no_profiles_says_no_profiles_found(
    monkeypatch, tmp_path,
):
    """No reserved profiles ship anymore; an empty dir → 'no profiles
    found' rather than a wall of bundled-default validations."""
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["policy", "lint", "--all"])
    assert result.exit_code == 0
    assert "no profiles found" in result.stderr or "no profiles found" in result.stdout


def test_policy_lint_reports_bad_profile(monkeypatch, tmp_path):
    profiles = _prep(monkeypatch, tmp_path)
    _write_profile(profiles, "broken", {"unknown_field": True})
    result = runner.invoke(app, ["policy", "lint", "--profile", "broken"])
    assert result.exit_code == 2
    assert "broken" in result.stdout or "broken" in result.stderr


def test_policy_test_evaluates_order_body(monkeypatch, tmp_path):
    profiles = _prep(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "deny",
        "policies": [{
            "name": "allow_ko_buy",
            "match": {"underlying": ["KO"], "instruction": ["BUY"]},
            "conditions": [{"quantity": {"lte": 100}}],
            "effect": "allow",
        }],
    })
    body = {
        "session": "NORMAL", "duration": "DAY",
        "orderType": "LIMIT", "price": "50.00", "quantity": 10,
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [{
            "instruction": "BUY", "quantity": 10,
            "instrument": {"assetType": "EQUITY", "symbol": "KO"},
        }],
    }
    body_path = tmp_path / "order.json"
    body_path.write_text(json.dumps(body))
    result = runner.invoke(app, [
        "policy", "test", str(body_path), "--profile", "default",
    ])
    assert result.exit_code == 0
    out = json.loads(result.stdout)
    assert out["decision"] == "approve"
    assert out["rule"] == "allow_ko_buy"
    assert out["phase"] == "C"


def test_policy_test_returns_4_when_rejected(monkeypatch, tmp_path):
    profiles = _prep(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "deny",
        "policies": [],
    })
    body = {
        "orderType": "LIMIT", "price": "50.00", "quantity": 10,
        "orderLegCollection": [{
            "instruction": "BUY", "quantity": 10,
            "instrument": {"assetType": "EQUITY", "symbol": "KO"},
        }],
    }
    body_path = tmp_path / "order.json"
    body_path.write_text(json.dumps(body))
    result = runner.invoke(app, ["policy", "test", str(body_path)])
    assert result.exit_code == 4


# ---- policy gate hooked into `order place` -------------------------------


def test_order_place_blocked_by_policy_exit_4(monkeypatch, tmp_path):
    profiles = _prep(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "deny",
        "policies": [],
    })
    place_calls: list = []
    patches = _patches(place_calls=place_calls)
    _enter(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
            "--yes",
        ])
    finally:
        _exit(patches)
    assert result.exit_code == 4, (result.stdout, result.stderr)
    assert place_calls == [], "policy reject must NOT call placeOrder"
    assert "REJECTED by policy" in result.stderr


def test_order_preview_warns_but_exits_zero_on_policy_reject(monkeypatch, tmp_path):
    profiles = _prep(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "deny",
        "policies": [],
    })
    place_calls: list = []
    patches = _patches(place_calls=place_calls)
    _enter(patches)
    try:
        result = runner.invoke(app, [
            "order", "preview", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
        ])
    finally:
        _exit(patches)
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert place_calls == [], "preview never places"
    assert "WARNING" in result.stderr
    assert "REJECTED" not in result.stderr.split("WARNING", 1)[0]


def test_order_place_passes_through_when_policy_approves(monkeypatch, tmp_path):
    profiles = _prep(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "allow",
        "policies": [],
    })
    place_calls: list = []
    patches = _patches(place_calls=place_calls)
    _enter(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
            "--yes",
        ])
    finally:
        _exit(patches)
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert len(place_calls) == 1


def test_profile_flag_takes_precedence_over_env(monkeypatch, tmp_path):
    profiles = _prep(monkeypatch, tmp_path)
    _write_profile(profiles, "lockdown", {
        "default_action": "deny", "policies": [],
    })
    _write_profile(profiles, "permissive", {
        "default_action": "allow", "policies": [],
    })
    monkeypatch.setenv("SCHWAB_CLI_PROFILE", "lockdown")
    place_calls: list = []
    patches = _patches(place_calls=place_calls)
    _enter(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
            "--yes",
            "--profile", "permissive",     # flag overrides env
        ])
    finally:
        _exit(patches)
    assert result.exit_code == 0
    assert len(place_calls) == 1


def test_audit_row_includes_policy_evaluations(monkeypatch, tmp_path):
    audit_dir = tmp_path / "audit"
    profiles = _prep(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "deny",
        "policies": [{
            "name": "allow_aapl",
            "match": {"underlying": ["AAPL"]},
            "conditions": [{"quantity": {"lte": 100}}],
            "effect": "allow",
        }],
    })
    place_calls: list = []
    patches = _patches(place_calls=place_calls)
    _enter(patches)
    try:
        runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
            "--yes",
        ])
    finally:
        _exit(patches)
    log = list(audit_dir.glob("*.order.log"))[0].read_text().splitlines()
    rows = [json.loads(line) for line in log]
    pol_row = next(r for r in rows if r["stage"] == "policy_evaluated")
    assert pol_row["profile_name"] == "default"
    assert pol_row["decision"] == "approve"
    assert any(
        ev["policy"] == "allow_aapl" and ev["matched"] is True
        for ev in pol_row["policy_evaluations"]
    )
    # Every row carries an HMAC audit_id.
    assert all("audit_id" in r and len(r["audit_id"]) == 64 for r in rows)


def test_audit_id_uses_hmac_when_key_present(monkeypatch, tmp_path):
    """Two separate machines (different HMAC keys) produce different
    audit_ids for the same logical row."""
    profiles = _prep(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "allow", "policies": [],
    })
    place_calls: list = []
    patches = _patches(place_calls=place_calls)
    _enter(patches)
    try:
        runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
            "--yes",
        ])
    finally:
        _exit(patches)
    # Re-generate with a different key and re-run; ids should differ
    # for the same wall-clock-ish stage.
    monkeypatch.setattr(
        audit, "DEFAULT_HMAC_KEY_PATH",
        tmp_path / "different_hmac.key",
    )
    place_calls2: list = []
    patches2 = _patches(place_calls=place_calls2)
    _enter(patches2)
    try:
        runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
            "--yes",
        ])
    finally:
        _exit(patches2)
    log = list((tmp_path / "audit").glob("*.order.log"))[0].read_text().splitlines()
    rows = [json.loads(line) for line in log]
    # Find both `placed` rows (one from each run) — they will have
    # the same fingerprint shape but different HMAC keys.
    placed_rows = [r for r in rows if r["stage"] == "placed"]
    assert len(placed_rows) >= 2
    ids = {r["audit_id"] for r in placed_rows}
    assert len(ids) == len(placed_rows), \
        "different HMAC keys must produce different audit_ids"
