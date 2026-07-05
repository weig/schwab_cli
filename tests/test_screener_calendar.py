"""Tests for earnings refresh and point-in-time membership."""
from __future__ import annotations

import pytest

from schwab_cli.screener import earnings, membership
from schwab_cli.screener.earnings import _parse_next_report_date
from schwab_cli.storage import screener as store
from schwab_cli.storage.vol_history import connect


@pytest.fixture
def conn(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    with connect() as c:
        yield c


def test_refresh_earnings_upserts_and_counts(conn):
    feed = {"AAPL": ("2026-07-30", True), "MSFT": ("2026-07-28", False),
            "NODATE": (None, False)}
    summary = earnings.refresh_earnings(
        conn, ["AAPL", "MSFT", "NODATE"], lambda s: feed[s], now_ms=10
    )
    assert summary == {"updated": 2, "missing": 1, "total": 3}
    assert store.next_event_date(
        conn, symbol="AAPL", event_type="earnings", on_or_after="2026-07-01"
    ) == "2026-07-30"


def test_refresh_earnings_swallows_fetcher_errors(conn):
    def _boom(symbol):
        raise RuntimeError("feed down")
    summary = earnings.refresh_earnings(conn, ["X"], _boom, now_ms=1)
    assert summary == {"updated": 0, "missing": 1, "total": 1}


def test_parse_next_report_date_formats():
    assert _parse_next_report_date({"data": {"nextReportDate": "07/30/2026"}}) == "2026-07-30"
    assert _parse_next_report_date({"data": {"reportDate": "Jul 30, 2026"}}) == "2026-07-30"
    assert _parse_next_report_date({"data": {"earningsDate": "2026-07-30"}}) == "2026-07-30"
    assert _parse_next_report_date({"data": {}}) is None
    assert _parse_next_report_date({}) is None


def test_membership_snapshot_from_supplied(conn):
    summary = membership.record_membership_snapshot(
        conn, as_of_date="2026-07-06", now_ms=1,
        members_by_index={"NDX": ["AAPL", "MSFT"], "SPX": ["AAPL"]},
    )
    assert summary["indices"] == 2 and summary["symbols"] == 3
    assert membership.__name__  # smoke
    assert store.read_membership(conn, as_of_date="2026-07-06", index_name="NDX") == \
        ["AAPL", "MSFT"]


def test_membership_snapshot_from_subscriptions(conn):
    now = 1_700_000_000_000
    conn.executemany(
        "INSERT INTO subscriptions (symbol, group_name, source, source_key, "
        "subscribed_at) VALUES (?, 'volatility', 'indices', ?, ?)",
        [("AAPL", "NDX", now), ("MSFT", "NDX", now), ("XOM", "SPX", now)],
    )
    conn.commit()
    got = membership.current_members_by_index(conn)
    assert got == {"NDX": ["AAPL", "MSFT"], "SPX": ["XOM"]}
    membership.record_membership_snapshot(conn, as_of_date="2026-07-06", now_ms=now)
    assert store.read_membership(conn, as_of_date="2026-07-06") == ["AAPL", "MSFT", "XOM"]
