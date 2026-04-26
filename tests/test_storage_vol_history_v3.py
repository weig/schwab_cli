"""v2 → v3 schema migration tests.

The migration is additive only; v2 rows persist with NULL in new
columns. We never DROP / RENAME — same idempotency contract as v1→v2.
"""
from __future__ import annotations

import sqlite3

from schwab_cli.storage import vol_history


_NEW_COLUMNS = {
    "atm_iv_30d", "atm_iv_60d", "atm_iv_90d",
    "iv_25d_put_30d", "iv_25d_call_30d",
    "iv_25d_put_60d", "iv_25d_call_60d",
    "iv_25d_put_90d", "iv_25d_call_90d",
    "hv_30d", "raw_chain_summary", "archive_date",
}


def test_v3_adds_new_columns(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    with vol_history.connect() as conn:
        # table_xinfo includes generated/virtual columns (like archive_date);
        # table_info omits them. Use table_xinfo so the full column set is visible.
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_xinfo(vol_snapshots)"
        ).fetchall()}
    missing = _NEW_COLUMNS - cols
    assert not missing, f"missing v3 columns: {missing}"


def test_v3_schema_version_bumped(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    with vol_history.connect() as conn:
        v = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    assert v == 3


import pytest


def test_subscriptions_table_exists(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    with vol_history.connect() as conn:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(subscriptions)"
        ).fetchall()}
    assert cols == {
        "symbol", "group_name", "source", "source_key",
        "subscribed_at", "unsubscribed_at",
    }


def test_subscriptions_pk_is_composite(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    with vol_history.connect() as conn:
        # Same composite PK ⇒ 2nd insert must IGNORE.
        conn.execute(
            "INSERT INTO subscriptions VALUES (?,?,?,?,?,?)",
            ("NVDA", "volatility", "equity", "", 1000, None),
        )
        conn.execute(
            "INSERT OR IGNORE INTO subscriptions VALUES (?,?,?,?,?,?)",
            ("NVDA", "volatility", "equity", "", 2000, None),
        )
        n = conn.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE symbol='NVDA'"
        ).fetchone()[0]
    assert n == 1


def test_index_subscriptions_table_exists(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    with vol_history.connect() as conn:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(index_subscriptions)"
        ).fetchall()}
    assert cols == {
        "index_name", "group_name", "subscribed_at", "unsubscribed_at",
    }


def test_ticker_state_table_exists(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    with vol_history.connect() as conn:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(ticker_state)"
        ).fetchall()}
    assert cols == {
        "symbol", "group_name", "tier", "tier_since",
        "consecutive_days_below", "last_evaluated_at",
    }


def test_archive_date_index_exists(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    with vol_history.connect() as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()}
    assert "idx_vol_archive_date" in names
