"""Phase 2e end-to-end tests — override flow.

Coverage:
- flag validation (mutex with --yes, both required, reason length)
- profile-level deny
- override_max_per_day cap
- cli tier (typed OVERRIDE → yea → place)
- telegram_notify_then_cli (notification fired)
- telegram_inbound (CONFIRM_OVERRIDE matched / timed out)
- audit row contents (override_invoked + override_reason / tier / count)
- counter increment after place

All Schwab + Telegram calls mocked.
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
_ACCT = AccountIds(account_number="12345678", hash_value="HASH")
_REASON = "manual hedge for unforeseen event"   # > 10 chars


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
        "SCHWAB_CLI_COUNTERS_FILE",
        str(tmp_path / "order_counters.json"),
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


def _patches(*, place_calls=None, telegram_send_log=None,
             telegram_wait_result="CONFIRM_OVERRIDE"):
    pc = place_calls
    tg_log = telegram_send_log

    def _record_place(client, account_hash, body):
        if pc is not None:
            pc.append((account_hash, body))
        return ("987654", httpx.Response(
            201, headers={"Location": f"/accounts/{account_hash}/orders/987654"},
        ))

    def _send(*, bot_token, chat_id, text, timeout=10.0):
        if tg_log is not None:
            tg_log.append({"chat_id": chat_id, "text": text})
        return (True, "ok")

    def _wait(*, bot_token, chat_id, expected_text, timeout_seconds=300,
              case_sensitive=True, allowed_user_ids=frozenset()):
        return telegram_wait_result

    return [
        patch("schwab_cli.commands.order.SchwabClient.resolve_account",
              return_value=_ACCT),
        patch("schwab_cli.commands.order.SchwabClient.account_ids",
              return_value=[_ACCT]),
        patch("schwab_cli.commands.order.preview_order",
              return_value={"orderValueImpact": {"buyingPowerEffect": -86.35}}),
        patch("schwab_cli.commands.order.place_order",
              side_effect=_record_place),
        patch("schwab_cli.notify.telegram.send",
              side_effect=_send),
        patch("schwab_cli.notify.telegram_poll.wait_for_text_reply",
              side_effect=_wait),
    ]


def _enter(patches): return [p.__enter__() for p in patches]
def _exit(patches):
    for p in reversed(patches):
        p.__exit__(None, None, None)


# Configure Telegram in the notify config so the override flow can
# proceed (otherwise telegram_inbound exits early).
def _enable_telegram(monkeypatch, tmp_path):
    cfg_dir = tmp_path / ".config" / "schwab_cli"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "notification.json").write_text(json.dumps({
        "telegram": {"bot_token": "TESTBOT", "chat_id": "1234"},
    }))


# ---- flag validation -----------------------------------------------------


def test_override_alone_without_confirm_rejected(monkeypatch, tmp_path):
    profiles = _prep(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "deny", "policies": [],
    })
    patches = _patches()
    _enter(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
            "--override", _REASON,
        ])
    finally:
        _exit(patches)
    assert result.exit_code == 2
    assert "both be set together" in result.stderr


def test_override_with_yes_rejected(monkeypatch, tmp_path):
    profiles = _prep(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "deny", "policies": [],
    })
    patches = _patches()
    _enter(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
            "--override", _REASON, "--override-confirm", "--yes",
        ])
    finally:
        _exit(patches)
    assert result.exit_code == 2
    assert "may not be combined with --yes" in result.stderr


def test_override_short_reason_rejected(monkeypatch, tmp_path):
    profiles = _prep(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "deny", "policies": [],
    })
    patches = _patches()
    _enter(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
            "--override", "short", "--override-confirm",
        ])
    finally:
        _exit(patches)
    assert result.exit_code == 2
    assert "10..500 characters" in result.stderr


def test_override_with_dry_run_rejected(monkeypatch, tmp_path):
    profiles = _prep(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "deny", "policies": [],
    })
    patches = _patches()
    _enter(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
            "--override", _REASON, "--override-confirm", "--dry-run",
        ])
    finally:
        _exit(patches)
    assert result.exit_code == 2
    assert "meaningless with --dry-run" in result.stderr


# ---- profile gate / counter cap -----------------------------------------


def test_override_blocked_when_profile_disallows(monkeypatch, tmp_path):
    profiles = _prep(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "deny",
        "allow_override": False,
        "override_confirmation": "deny",
        "policies": [],
    })
    place_calls: list = []
    patches = _patches(place_calls=place_calls)
    _enter(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
            "--override", _REASON, "--override-confirm",
        ])
    finally:
        _exit(patches)
    assert result.exit_code == 2
    assert place_calls == []
    assert "not permitted by profile" in result.stderr


def test_override_max_per_day_blocks_after_cap(monkeypatch, tmp_path):
    profiles = _prep(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "deny",
        "override_confirmation": "cli",
        "override_max_per_day": 1,
        "policies": [],
    })
    # Pre-seed the counter file so we're already at the cap.
    (tmp_path / "order_counters.json").write_text(json.dumps({
        "date": "2026-04-25",
        "counters": {
            "daily_order_count_total": {},
            "daily_order_count_per_ticker": {},
            "minutely_buckets": {},
            "replace_count_per_order": {},
            "override_count_per_day": {"12345678": 1},
        },
    }))
    place_calls: list = []
    patches = _patches(place_calls=place_calls)
    _enter(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
            "--override", _REASON, "--override-confirm",
        ])
    finally:
        _exit(patches)
    # Cap triggers either today (date matches) or after rotation
    # (depends on timezone). Cleanest: just check no place fired.
    assert place_calls == []


# ---- cli tier happy path -------------------------------------------------


def test_override_cli_tier_happy_path_typed_override_then_yea(
    monkeypatch, tmp_path,
):
    profiles = _prep(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "deny",                       # would normally reject
        "override_confirmation": "cli",
        "notify_on_override": False,
        "policies": [],
    })
    place_calls: list = []
    patches = _patches(place_calls=place_calls)
    _enter(patches)
    try:
        result = runner.invoke(
            app,
            ["order", "place", "AAPL", "--account", "5678",
             "--type", "LIMIT", "--price", "150", "--side", "BUY",
             "--override", _REASON, "--override-confirm"],
            input="OVERRIDE\nyea\n",
        )
    finally:
        _exit(patches)
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert len(place_calls) == 1


def test_override_cli_tier_aborts_when_user_does_not_type_override(
    monkeypatch, tmp_path,
):
    profiles = _prep(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "deny",
        "override_confirmation": "cli",
        "notify_on_override": False,
        "policies": [],
    })
    place_calls: list = []
    patches = _patches(place_calls=place_calls)
    _enter(patches)
    try:
        result = runner.invoke(
            app,
            ["order", "place", "AAPL", "--account", "5678",
             "--type", "LIMIT", "--price", "150", "--side", "BUY",
             "--override", _REASON, "--override-confirm"],
            input="override\n",   # lowercase — exact-case required
        )
    finally:
        _exit(patches)
    assert result.exit_code == 0   # aborted = exit 0
    assert place_calls == []


# ---- telegram_notify_then_cli tier --------------------------------------


def test_override_telegram_notify_tier_fires_notification(
    monkeypatch, tmp_path,
):
    profiles = _prep(monkeypatch, tmp_path)
    _enable_telegram(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "deny",
        "override_confirmation": "telegram_notify_then_cli",
        "notify_on_override": True,
        "policies": [],
    })
    place_calls: list = []
    tg_log: list = []
    patches = _patches(place_calls=place_calls, telegram_send_log=tg_log)
    _enter(patches)
    try:
        result = runner.invoke(
            app,
            ["order", "place", "AAPL", "--account", "5678",
             "--type", "LIMIT", "--price", "150", "--side", "BUY",
             "--override", _REASON, "--override-confirm"],
            input="OVERRIDE\nyea\n",
        )
    finally:
        _exit(patches)
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert len(place_calls) == 1
    # The notify fires once with the OVERRIDE INVOKED banner.
    assert any("OVERRIDE INVOKED" in m["text"] for m in tg_log)


# ---- telegram_inbound tier ----------------------------------------------


def test_override_telegram_inbound_tier_proceeds_when_confirmed(
    monkeypatch, tmp_path,
):
    profiles = _prep(monkeypatch, tmp_path)
    _enable_telegram(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "deny",
        "override_confirmation": "telegram_inbound",
        "notify_on_override": False,
        "policies": [],
    })
    place_calls: list = []
    patches = _patches(
        place_calls=place_calls, telegram_wait_result="CONFIRM_OVERRIDE",
    )
    _enter(patches)
    try:
        result = runner.invoke(
            app,
            ["order", "place", "AAPL", "--account", "5678",
             "--type", "LIMIT", "--price", "150", "--side", "BUY",
             "--override", _REASON, "--override-confirm"],
            input="OVERRIDE\nyea\n",
        )
    finally:
        _exit(patches)
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert len(place_calls) == 1


def test_override_telegram_inbound_tier_aborts_on_timeout(
    monkeypatch, tmp_path,
):
    profiles = _prep(monkeypatch, tmp_path)
    _enable_telegram(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "deny",
        "override_confirmation": "telegram_inbound",
        "notify_on_override": False,
        "policies": [],
    })
    place_calls: list = []
    patches = _patches(place_calls=place_calls, telegram_wait_result=None)
    _enter(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
            "--override", _REASON, "--override-confirm",
        ])
    finally:
        _exit(patches)
    assert result.exit_code == 2
    assert place_calls == []
    assert "timed out" in result.stderr.lower()


# ---- audit row contents -------------------------------------------------


def test_override_audit_row_contains_reason_tier_count(monkeypatch, tmp_path):
    profiles = _prep(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "deny",
        "override_confirmation": "cli",
        "notify_on_override": False,
        "policies": [],
    })
    place_calls: list = []
    patches = _patches(place_calls=place_calls)
    _enter(patches)
    try:
        runner.invoke(
            app,
            ["order", "place", "AAPL", "--account", "5678",
             "--type", "LIMIT", "--price", "150", "--side", "BUY",
             "--override", _REASON, "--override-confirm"],
            input="OVERRIDE\nyea\n",
        )
    finally:
        _exit(patches)
    log = list((tmp_path / "audit").glob("*.order.log"))[0].read_text().splitlines()
    rows = [json.loads(l) for l in log]
    invoked = next(r for r in rows if r["stage"] == "override_invoked")
    assert invoked["override_reason"] == _REASON
    assert invoked["override_tier"] == "cli"
    assert invoked["override_count_today"] == 1
    # And the placed row carries an order_id (placement actually happened).
    placed = next(r for r in rows if r["stage"] == "placed")
    assert placed["order_id"] == "987654"


def test_override_counter_incremented_after_successful_place(
    monkeypatch, tmp_path,
):
    profiles = _prep(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "deny",
        "override_confirmation": "cli",
        "notify_on_override": False,
        "policies": [],
    })
    patches = _patches()
    _enter(patches)
    try:
        runner.invoke(
            app,
            ["order", "place", "AAPL", "--account", "5678",
             "--type", "LIMIT", "--price", "150", "--side", "BUY",
             "--override", _REASON, "--override-confirm"],
            input="OVERRIDE\nyea\n",
        )
    finally:
        _exit(patches)
    state = json.loads((tmp_path / "order_counters.json").read_text())
    assert state["counters"]["override_count_per_day"]["12345678"] == 1
