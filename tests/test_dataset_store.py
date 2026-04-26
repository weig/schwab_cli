"""Dataset store CRUD — exercises the new tables introduced in v3.

All tests use the real SQLite via vol_history.connect() so the
schema migration is exercised together with the queries. monkeypatched
SCHWAB_CLI_STORAGE points at tmp_path so production data stays
untouched.
"""
from __future__ import annotations

import pytest

from schwab_cli.storage import vol_history
from schwab_cli.dataset.store import (
    subscribe_equity,
    unsubscribe_equity,
    list_active_subscriptions,
)


@pytest.fixture
def conn(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    with vol_history.connect() as c:
        yield c


def test_subscribe_equity_inserts_row(conn):
    subscribe_equity(conn, symbol="NVDA", group_name="volatility",
                     captured_at_ms=1000)
    rows = list_active_subscriptions(conn, group_name="volatility")
    assert len(rows) == 1
    assert rows[0]["symbol"] == "NVDA"
    assert rows[0]["source"] == "equity"


def test_subscribe_equity_idempotent(conn):
    subscribe_equity(conn, symbol="NVDA", group_name="volatility",
                     captured_at_ms=1000)
    subscribe_equity(conn, symbol="NVDA", group_name="volatility",
                     captured_at_ms=2000)
    rows = list_active_subscriptions(conn, group_name="volatility")
    assert len(rows) == 1


def test_unsubscribe_soft_deletes(conn):
    subscribe_equity(conn, symbol="NVDA", group_name="volatility",
                     captured_at_ms=1000)
    unsubscribe_equity(conn, symbol="NVDA", group_name="volatility",
                       captured_at_ms=5000)
    active = list_active_subscriptions(conn, group_name="volatility")
    assert active == []
    all_rows = conn.execute(
        "SELECT * FROM subscriptions WHERE symbol='NVDA'"
    ).fetchall()
    assert len(all_rows) == 1
    assert all_rows[0]["unsubscribed_at"] == 5000


def test_resubscribe_clears_unsubscribed_at(conn):
    subscribe_equity(conn, symbol="NVDA", group_name="volatility",
                     captured_at_ms=1000)
    unsubscribe_equity(conn, symbol="NVDA", group_name="volatility",
                       captured_at_ms=5000)
    subscribe_equity(conn, symbol="NVDA", group_name="volatility",
                     captured_at_ms=9000)
    active = list_active_subscriptions(conn, group_name="volatility")
    assert len(active) == 1
    assert active[0]["unsubscribed_at"] is None


from schwab_cli.dataset.store import (
    subscribe_index,
    unsubscribe_index,
    list_active_index_subscriptions,
    subscribe_position,
    unsubscribe_position,
)


def test_subscribe_index_inserts_row(conn):
    subscribe_index(conn, index_name="SPX", group_name="volatility",
                    captured_at_ms=1000)
    rows = list_active_index_subscriptions(conn, group_name="volatility")
    assert len(rows) == 1
    assert rows[0]["index_name"] == "SPX"


def test_subscribe_index_rejects_unknown_index(conn):
    with pytest.raises(ValueError, match="not in supported index set"):
        subscribe_index(conn, index_name="EFA", group_name="volatility")


def test_subscribe_index_accepts_supported_set(conn):
    for name in ("SPX", "DJI", "NQ", "RUT"):
        subscribe_index(conn, index_name=name, group_name="volatility")
    rows = list_active_index_subscriptions(conn, group_name="volatility")
    assert {r["index_name"] for r in rows} == {"SPX", "DJI", "NQ", "RUT"}


def test_subscribe_position_inserts_row(conn):
    subscribe_position(conn, symbol="NVDA", group_name="volatility",
                       account_hash_last4="1234", captured_at_ms=1000)
    rows = list_active_subscriptions(conn, group_name="volatility")
    assert rows[0]["source"] == "position"
    assert rows[0]["source_key"] == "1234"


def test_unsubscribe_position_only_targets_one_account(conn):
    subscribe_position(conn, symbol="NVDA", group_name="volatility",
                       account_hash_last4="1234")
    subscribe_position(conn, symbol="NVDA", group_name="volatility",
                       account_hash_last4="5678")
    unsubscribe_position(conn, symbol="NVDA", group_name="volatility",
                         account_hash_last4="1234", captured_at_ms=9000)
    active = list_active_subscriptions(conn, group_name="volatility")
    assert len(active) == 1
    assert active[0]["source_key"] == "5678"
