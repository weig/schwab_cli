"""Watchlist subscription CRUD."""
from __future__ import annotations

import pytest

from schwab_cli.dataset.store import (
    has_other_active_source,
    list_watched_symbols,
    subscribe_equity,
    subscribe_watch,
    unsubscribe_watch,
)
from schwab_cli.storage import vol_history


@pytest.fixture
def conn(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    with vol_history.connect() as c:
        yield c


def test_subscribe_watch_inserts_row(conn):
    subscribe_watch(conn, symbol="NVDA", group_name="volatility",
                    captured_at_ms=1000)
    assert list_watched_symbols(conn) == ["NVDA"]


def test_subscribe_watch_idempotent(conn):
    subscribe_watch(conn, symbol="NVDA", group_name="ohlcv",
                    captured_at_ms=1000)
    subscribe_watch(conn, symbol="NVDA", group_name="ohlcv",
                    captured_at_ms=2000)
    assert list_watched_symbols(conn) == ["NVDA"]


def test_unsubscribe_watch_removes_from_list(conn):
    subscribe_watch(conn, symbol="NVDA", group_name="volatility")
    subscribe_watch(conn, symbol="NVDA", group_name="ohlcv")
    unsubscribe_watch(conn, symbol="NVDA", group_name="volatility")
    # Still on the list because ohlcv subscription is active.
    assert list_watched_symbols(conn) == ["NVDA"]
    unsubscribe_watch(conn, symbol="NVDA", group_name="ohlcv")
    assert list_watched_symbols(conn) == []


def test_list_watched_returns_distinct_symbols(conn):
    for s in ("AAPL", "NVDA", "MSFT"):
        subscribe_watch(conn, symbol=s, group_name="volatility")
        subscribe_watch(conn, symbol=s, group_name="ohlcv")
    assert list_watched_symbols(conn) == ["AAPL", "MSFT", "NVDA"]


def test_has_other_active_source_detects_non_watch_coverage(conn):
    # Both 'watch' and 'equity' rows exist — has_other should be True
    # for exclude='watch' because the equity row covers it.
    subscribe_watch(conn, symbol="NVDA", group_name="volatility")
    subscribe_equity(conn, symbol="NVDA", group_name="volatility")
    assert has_other_active_source(
        conn, symbol="NVDA", group_name="volatility",
        exclude_source="watch",
    ) is True


def test_has_other_active_source_false_when_only_watch(conn):
    subscribe_watch(conn, symbol="NVDA", group_name="volatility")
    assert has_other_active_source(
        conn, symbol="NVDA", group_name="volatility",
        exclude_source="watch",
    ) is False
