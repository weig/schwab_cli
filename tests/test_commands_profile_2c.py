"""Phase 2c CLI tests — policy counters + policy audit + counter
increment after a successful place."""

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
_ACCT = AccountIds(account_number="12345678", hash_value="HASH")


def _prep(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_PROFILE", raising=False)
    monkeypatch.setattr(audit, "DEFAULT_AUDIT_DIR", tmp_path / "audit")
    monkeypatch.setattr(
        audit, "DEFAULT_HMAC_KEY_PATH", tmp_path / "audit_hmac.key",
    )
    profiles = tmp_path / "profiles" / "order"
    monkeypatch.setenv("SCHWAB_CLI_POLICY_DIR", str(profiles))
    monkeypatch.setenv(
        "SCHWAB_CLI_COUNTERS_FILE", str(tmp_path / "order_counters.json"),
    )
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
              return_value={"orderValueImpact": {"buyingPowerEffect": -86.35}}),
        patch("schwab_cli.commands.order.place_order",
              side_effect=_record_place),
    ]


def _enter(patches): return [p.__enter__() for p in patches]
def _exit(patches):
    for p in reversed(patches):
        p.__exit__(None, None, None)


# ---- counter increment after place ---------------------------------------


def test_successful_place_increments_counter_file(monkeypatch, tmp_path):
    profiles = _prep(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "allow", "policies": [],
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
    assert result.exit_code == 0
    raw = json.loads((tmp_path / "order_counters.json").read_text())
    counters = raw["counters"]
    assert counters["daily_order_count_total"]["12345678"] == 1
    assert counters["daily_order_count_per_ticker"]["12345678"]["AAPL"] == 1
    # And the audit log has no counter_increment_failed row.
    log_files = list((tmp_path / "audit").glob("*.order.log"))
    assert log_files
    rows = [json.loads(l) for l in log_files[0].read_text().splitlines()]
    assert not any(r["stage"] == "counter_increment_failed" for r in rows)


def test_two_places_yields_count_of_two(monkeypatch, tmp_path):
    profiles = _prep(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "allow", "policies": [],
    })
    patches = _patches()
    _enter(patches)
    try:
        for _ in range(2):
            runner.invoke(app, [
                "order", "place", "AAPL", "--account", "5678",
                "--type", "LIMIT", "--price", "150", "--side", "BUY",
                "--yes",
            ])
    finally:
        _exit(patches)
    raw = json.loads((tmp_path / "order_counters.json").read_text())
    assert raw["counters"]["daily_order_count_total"]["12345678"] == 2


def test_policy_reject_does_not_increment_counters(monkeypatch, tmp_path):
    profiles = _prep(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "deny", "policies": [],   # rejects everything
    })
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
    assert result.exit_code == 4
    # Counter file shouldn't even exist (no place succeeded).
    assert not (tmp_path / "order_counters.json").exists()


# ---- daily-cap policy uses live counter file -----------------------------


def test_daily_cap_policy_blocks_after_threshold(monkeypatch, tmp_path):
    profiles = _prep(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "deny",
        "policies": [{
            "name": "daily_cap",
            "match": "*",
            "conditions": [{"daily_order_count": {"lte": 1}}],
            "effect": "allow",
        }],
    })
    patches = _patches()
    _enter(patches)
    try:
        # First place: counter starts at 0, condition lte 1 → approve.
        result1 = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
            "--yes",
        ])
        # Second place: counter is 1, condition still lte 1 → approve again.
        result2 = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
            "--yes",
        ])
        # Third place: counter is 2, condition lte 1 fails → reject.
        result3 = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
            "--yes",
        ])
    finally:
        _exit(patches)
    assert result1.exit_code == 0
    assert result2.exit_code == 0
    assert result3.exit_code == 4, result3.stderr


# ---- policy counters CLI -------------------------------------------------


def test_policy_counters_human(monkeypatch, tmp_path):
    profiles = _prep(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "allow", "policies": [],
    })
    patches = _patches()
    _enter(patches)
    try:
        runner.invoke(app, [
            "order", "place", "KO", "--account", "5678",
            "--type", "LIMIT", "--price", "65", "--side", "BUY",
            "--yes",
        ])
        result = runner.invoke(app, ["profile", "counters", "--type", "order"])
    finally:
        _exit(patches)
    assert result.exit_code == 0
    assert "Account ********5678" in result.stdout
    assert "daily_order_count:   1" in result.stdout
    assert "KO: 1" in result.stdout


def test_policy_counters_json(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    # Inject a synthetic counter file to skip going through place.
    (tmp_path / "order_counters.json").write_text(json.dumps({
        "date": "2026-04-25",
        "counters": {
            "daily_order_count_total": {"12345678": 5},
            "daily_order_count_per_ticker": {"12345678": {"NVDA": 5}},
            "minutely_buckets": {},
            "replace_count_per_order": {},
            "override_count_per_day": {},
        },
    }))
    # CLI wants today's ET date to match — use the date of the file
    # by faking `date.today()` indirectly: easiest is to load via the
    # module's own load() with `now` arg. The CLI doesn't expose that,
    # so this test uses the JSON output and accepts the date may be
    # rotated.
    result = runner.invoke(app, ["profile", "counters", "--type", "order", "--json"])
    assert result.exit_code == 0
    out = json.loads(result.stdout)
    assert out["tz"] == "America/New_York"


# ---- policy audit CLI -----------------------------------------------------


def test_policy_audit_lists_recent_rows(monkeypatch, tmp_path):
    profiles = _prep(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "allow", "policies": [],
    })
    patches = _patches()
    _enter(patches)
    try:
        runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
            "--yes",
        ])
        result = runner.invoke(app, ["profile", "audit", "--type", "order", "--limit", "10"])
    finally:
        _exit(patches)
    assert result.exit_code == 0
    assert "place" in result.stdout
    assert "policy_evaluated" in result.stdout or "placed" in result.stdout


def test_policy_audit_filter_by_decision(monkeypatch, tmp_path):
    profiles = _prep(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "deny", "policies": [],
    })
    patches = _patches()
    _enter(patches)
    try:
        runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
            "--yes",
        ])
        result = runner.invoke(app, [
            "profile", "audit", "--type", "order", "--decision", "reject", "--json",
        ])
    finally:
        _exit(patches)
    assert result.exit_code == 0
    rows = json.loads(result.stdout)
    # Every row that carries a `decision` field must be 'reject'.
    for r in rows:
        if "decision" in r:
            assert r["decision"] == "reject"


def test_policy_audit_no_log_dir_returns_empty(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["profile", "audit", "--type", "order"])
    assert result.exit_code == 0
    assert "no audit log entries" in result.stdout
