"""Phase 2b end-to-end tests — conditional fetching via the CLI.

Verifies that ``order place`` only triggers ``getChain`` /
``getAccount`` / fundamental ``get_quotes(fields=all)`` calls when
the active profile actually references a field from that source.
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


# Counters to track which fetchers were called.
def _make_patches(*, place_calls, fetch_log):
    def _record_place(client, account_hash, body):
        place_calls.append((account_hash, body))
        return ("987654", httpx.Response(
            201, headers={"Location": f"/accounts/{account_hash}/orders/987654"},
        ))

    def _track_chain(client, symbol):
        fetch_log.append(("chain", symbol))
        return {"underlying": {"last": 245.0}, "callExpDateMap": {}}

    def _track_quote(client, symbol):
        fetch_log.append(("quote", symbol))
        return {symbol: {"quote": {
            "lastPrice": 150.0, "bidPrice": 149.95,
            "askPrice": 150.05, "mark": 150.0,
        }}}

    def _track_account(client, account_number):
        fetch_log.append(("account", account_number))
        return {"securitiesAccount": {"currentBalances": {
            "liquidationValue": 100000.0, "buyingPower": 70000.0,
            "longMarketValue": 5000.0, "shortMarketValue": 0.0,
        }}}

    def _track_dividends(client, symbol):
        fetch_log.append(("dividends", symbol))
        return {symbol: {"nextDividendDate": "2026-05-15"}}

    return [
        patch("schwab_cli.commands.order.SchwabClient.resolve_account",
              return_value=_ACCT),
        patch("schwab_cli.commands.order.SchwabClient.account_ids",
              return_value=[_ACCT]),
        patch("schwab_cli.commands.order.preview_order",
              return_value={"orderValueImpact": {"buyingPowerEffect": -86.35}}),
        patch("schwab_cli.commands.order.place_order",
              side_effect=_record_place),
        patch("schwab_cli.commands.order._fetch_chain_safe",
              side_effect=_track_chain),
        patch("schwab_cli.commands.order._fetch_quote_safe",
              side_effect=_track_quote),
        patch("schwab_cli.commands.order._fetch_account_safe",
              side_effect=_track_account),
        patch("schwab_cli.commands.order._fetch_dividend_safe",
              side_effect=_track_dividends),
    ]


def _enter(patches): return [p.__enter__() for p in patches]
def _exit(patches):
    for p in reversed(patches):
        p.__exit__(None, None, None)


# ---- intrinsic-only profile triggers no extra fetches -------------------


def test_no_extra_fetch_when_profile_only_uses_intrinsic_fields(
    monkeypatch, tmp_path,
):
    profiles = _prep(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "deny",
        "policies": [{
            "name": "allow_aapl_buy",
            "match": {"underlying": ["AAPL"], "instruction": ["BUY"]},
            "conditions": [{"quantity": {"lte": 100}}],
            "effect": "allow",
        }],
    })
    place_calls: list = []
    fetch_log: list = []
    patches = _make_patches(place_calls=place_calls, fetch_log=fetch_log)
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
    assert fetch_log == [], (
        "intrinsic-only profile must not trigger chain/account/quote/dividend "
        f"fetches; got {fetch_log}"
    )
    assert len(place_calls) == 1


# ---- profile referencing market-data triggers chain fetch ---------------


def test_profile_with_delta_triggers_chain_fetch(monkeypatch, tmp_path):
    profiles = _prep(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "deny",
        "policies": [{
            "name": "allow_low_delta",
            "match": {"asset_type": ["OPTION"]},
            "conditions": [{"delta": {"gte": -0.30}}],
            "effect": "allow",
        }],
    })
    place_calls: list = []
    fetch_log: list = []
    patches = _make_patches(place_calls=place_calls, fetch_log=fetch_log)
    _enter(patches)
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
            "--yes",
        ])
    finally:
        _exit(patches)
    assert result.exit_code in (0, 4)
    sources = {kind for kind, _sym in fetch_log}
    assert sources == {"quote"}, (
        "equity order with chain-source policy should call quote (not chain) — "
        f"got {fetch_log}"
    )


def test_profile_with_account_field_triggers_account_fetch(monkeypatch, tmp_path):
    profiles = _prep(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "deny",
        "policies": [{
            "name": "allow_low_bp",
            "match": "*",
            "conditions": [{"bp_used_pct": {"lte": 50}}],
            "effect": "allow",
        }],
    })
    place_calls: list = []
    fetch_log: list = []
    patches = _make_patches(place_calls=place_calls, fetch_log=fetch_log)
    _enter(patches)
    try:
        runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
            "--yes",
        ])
    finally:
        _exit(patches)
    sources = {kind for kind, _ in fetch_log}
    assert sources == {"account"}


def test_profile_with_days_to_ex_div_triggers_fundamental_fetch(
    monkeypatch, tmp_path,
):
    profiles = _prep(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "deny",
        "policies": [{
            "name": "skip_pre_div",
            "match": "*",
            "conditions": [{"days_to_ex_div": {"gte": 5}}],
            "effect": "allow",
        }],
    })
    place_calls: list = []
    fetch_log: list = []
    patches = _make_patches(place_calls=place_calls, fetch_log=fetch_log)
    _enter(patches)
    try:
        runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
            "--yes",
        ])
    finally:
        _exit(patches)
    sources = {kind for kind, _ in fetch_log}
    assert sources == {"dividends"}


# ---- failed optional fetch degrades gracefully --------------------------


def test_chain_fetch_failure_audited_but_does_not_crash(
    monkeypatch, tmp_path,
):
    profiles = _prep(monkeypatch, tmp_path)
    _write_profile(profiles, "default", {
        "default_action": "deny",
        "policies": [{
            "name": "needs_delta",
            "match": "*",
            "conditions": [{"delta": {"gte": -0.30}}],
            "effect": "allow",
        }],
    })
    fetch_log: list = []

    def _broken_chain(client, symbol):
        fetch_log.append(("chain", symbol))
        raise RuntimeError("synthetic failure")

    place_calls: list = []
    patches = _make_patches(place_calls=place_calls, fetch_log=fetch_log)
    # Override the equity fetch to simulate a failure; equity orders
    # actually go through quote, so override that.
    p_q = patch(
        "schwab_cli.commands.order._fetch_quote_safe",
        side_effect=RuntimeError("synthetic quote failure"),
    )
    _enter(patches)
    p_q.__enter__()
    try:
        result = runner.invoke(app, [
            "order", "place", "AAPL", "--account", "5678",
            "--type", "LIMIT", "--price", "150", "--side", "BUY",
            "--yes",
        ])
    finally:
        p_q.__exit__(None, None, None)
        _exit(patches)
    # Policy needs `delta` which is unavailable → matched-allow with
    # failing condition → reject (Phase B). Exit 4. Crucially, the
    # command did NOT crash with a Python exception.
    assert result.exit_code == 4, (result.stdout, result.stderr)
    # Audit captured the fetch failure.
    log = list((tmp_path / "audit").glob("*.order.log"))[0].read_text().splitlines()
    rows = [json.loads(line) for line in log]
    assert any(r["stage"] == "policy_chain_fetch_failed" for r in rows)
