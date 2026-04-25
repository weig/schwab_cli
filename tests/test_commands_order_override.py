"""Phase 2e/2f end-to-end tests — override flow.

After Phase 2f-1 the per-profile override gating, override-tier enum,
and override_max_per_day cap are all gone. Every profile accepts an
override via the single CLI ceremony.

Coverage:
- flag validation (mutex with --yes / --dry-run, both required, length)
- happy path: typed OVERRIDE → yea → place
- abort when typed OVERRIDE not entered
- audit row carries reason + tier="cli"

Tier-specific tests (telegram_notify_then_cli, telegram_inbound) and
the override_max_per_day cap test were dropped along with the schema
fields.
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


def _patches(*, place_calls=None, telegram_send_log=None):
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
    ]


def _enter(patches): return [p.__enter__() for p in patches]
def _exit(patches):
    for p in reversed(patches):
        p.__exit__(None, None, None)


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


# ---- happy path ----------------------------------------------------------


def test_override_happy_path_typed_override_then_yea(
    monkeypatch, tmp_path,
):
    profiles = _prep(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "deny",                       # would normally reject
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
            input="OVERRIDE\nyes\n",
        )
    finally:
        _exit(patches)
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert len(place_calls) == 1


def test_override_aborts_when_user_does_not_type_override(
    monkeypatch, tmp_path,
):
    profiles = _prep(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "deny",
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


def test_override_notify_on_override_fires_telegram(monkeypatch, tmp_path):
    profiles = _prep(monkeypatch, tmp_path)
    _enable_telegram(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "deny",
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
            input="OVERRIDE\nyes\n",
        )
    finally:
        _exit(patches)
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert len(place_calls) == 1
    assert any("OVERRIDE INVOKED" in m["text"] for m in tg_log)


# ---- audit row ----------------------------------------------------------


def test_override_audit_row_contains_reason(monkeypatch, tmp_path):
    profiles = _prep(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "deny",
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
            input="OVERRIDE\nyes\n",
        )
    finally:
        _exit(patches)
    log = list((tmp_path / "audit").glob("*.order.log"))[0].read_text().splitlines()
    rows = [json.loads(l) for l in log]
    invoked = next(r for r in rows if r["stage"] == "override_invoked")
    assert invoked["override_reason"] == _REASON
    # tier is gone in 2f-2 — the row must NOT carry it anymore.
    assert "override_tier" not in invoked
    # And the placed row carries an order_id (placement actually happened).
    placed = next(r for r in rows if r["stage"] == "placed")
    assert placed["order_id"] == "987654"


def test_override_does_not_increment_override_counter(monkeypatch, tmp_path):
    """Phase 2f-1 dropped record_override on the place path. Counter
    file may not even exist after a successful override place."""
    profiles = _prep(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "deny",
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
            input="OVERRIDE\nyes\n",
        )
    finally:
        _exit(patches)
    state = json.loads((tmp_path / "order_counters.json").read_text())
    # daily_order_count_total still bumps on place.
    assert state["counters"]["daily_order_count_total"]["12345678"] == 1
    # override counter is NOT bumped.
    assert state["counters"].get("override_count_per_day", {}) == {}
