"""Tests for the v7 screener storage layer."""
from __future__ import annotations

import pytest

from schwab_cli.storage import screener as sc
from schwab_cli.storage.vol_history import connect


@pytest.fixture
def conn(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    with connect() as c:
        yield c


def test_v7_tables_created(conn):
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {
        "contract_snapshots", "events", "index_membership",
        "daily_ranking", "paper_ledger",
    } <= tables
    ver = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    assert ver == 7


def _snap(**kw) -> sc.ContractSnapshot:
    base = dict(
        snapshot_date="2026-07-06", symbol="QQQ", captured_at_ms=1_700_000_000_000,
        target_expiry="2026-08-07", dte=32, put_strike=500.0,
        put_delta_actual=-0.24, put_bid=4.10, put_ask=4.30, put_mid=4.20,
        put_oi=1200, put_volume=300, spread_pct=0.047, underlying_last=540.0,
    )
    base.update(kw)
    return sc.ContractSnapshot(**base)


def test_contract_snapshot_roundtrip_and_idempotent(conn):
    sc.record_contract_snapshot(conn, _snap())
    sc.record_contract_snapshot(conn, _snap(put_bid=4.15))  # same-day re-run
    rows = sc.read_contract_snapshots(conn, snapshot_date="2026-07-06")
    assert len(rows) == 1  # upsert, not duplicate
    assert rows[0]["put_bid"] == 4.15  # refreshed to newer quote
    assert rows[0]["snapshot_quality"] == "ok"


def test_forward_rv_survives_resnapshot(conn):
    sc.record_contract_snapshot(conn, _snap())
    sc.set_forward_rv(conn, snapshot_date="2026-07-06", symbol="QQQ", rv=0.19)
    # A later same-day re-snapshot must NOT wipe the backfilled rv.
    sc.record_contract_snapshot(conn, _snap(put_bid=9.99))
    row = sc.read_contract_snapshots(conn, snapshot_date="2026-07-06")[0]
    assert row["rv_fwd_21d"] == 0.19
    assert row["put_bid"] == 9.99


def test_read_snapshots_needing_rv(conn):
    sc.record_contract_snapshot(conn, _snap(snapshot_date="2026-06-01", symbol="A"))
    sc.record_contract_snapshot(conn, _snap(snapshot_date="2026-07-06", symbol="B"))
    sc.set_forward_rv(conn, snapshot_date="2026-06-01", symbol="A", rv=0.2)
    due = sc.read_snapshots_needing_rv(conn, on_or_before="2026-07-01")
    # A already has rv; B is after the cutoff → neither qualifies.
    assert [(r["snapshot_date"], r["symbol"]) for r in due] == []
    sc.record_contract_snapshot(conn, _snap(snapshot_date="2026-06-02", symbol="C"))
    due = sc.read_snapshots_needing_rv(conn, on_or_before="2026-07-01")
    assert [(r["snapshot_date"], r["symbol"]) for r in due] == [("2026-06-02", "C")]


def test_events_upsert_and_next(conn):
    sc.upsert_event(conn, symbol="AAPL", event_type="earnings",
                    event_date="2026-07-30", confirmed=False, updated_at_ms=1)
    sc.upsert_event(conn, symbol="AAPL", event_type="earnings",
                    event_date="2026-10-29", confirmed=True, updated_at_ms=2)
    assert sc.next_event_date(conn, symbol="AAPL", event_type="earnings",
                              on_or_after="2026-07-06") == "2026-07-30"
    assert sc.next_event_date(conn, symbol="AAPL", event_type="earnings",
                              on_or_after="2026-08-01") == "2026-10-29"
    assert sc.next_event_date(conn, symbol="AAPL", event_type="earnings",
                              on_or_after="2026-11-01") is None


def test_membership_point_in_time(conn):
    sc.record_membership(conn, as_of_date="2026-07-06", index_name="NDX",
                         symbols=["QQQ", "AAPL"], captured_at_ms=1)
    # Re-run same week must not overwrite / duplicate.
    sc.record_membership(conn, as_of_date="2026-07-06", index_name="NDX",
                         symbols=["QQQ", "AAPL", "MSFT"], captured_at_ms=2)
    assert sc.read_membership(conn, as_of_date="2026-07-06", index_name="NDX") == \
        ["AAPL", "MSFT", "QQQ"]
    assert sc.latest_membership_date(conn, on_or_before="2026-07-10") == "2026-07-06"


def test_ranking_write_is_idempotent(conn):
    rows = [
        {"rank": 1, "symbol": "A", "executable_vrp": 0.05, "put_bid": 1.0},
        {"rank": 2, "symbol": "B", "executable_vrp": 0.03, "put_bid": 2.0},
    ]
    sc.write_ranking(conn, ranking_date="2026-07-06", rows=rows)
    sc.write_ranking(conn, ranking_date="2026-07-06", rows=rows)  # re-run
    got = sc.read_ranking(conn, ranking_date="2026-07-06")
    assert [r["symbol"] for r in got] == ["A", "B"]
    assert sc.read_ranking(conn, ranking_date="2026-07-06", limit=1)[0]["symbol"] == "A"
    assert sc.latest_ranking_date(conn) == "2026-07-06"


def test_paper_ledger_open_settle(conn):
    sc.open_position(conn, open_date="2026-07-06", symbol="A", cohort="top",
                     strike=100.0, dte=30, premium_bid=1.50, expiry="2026-08-05")
    # Duplicate open is a no-op.
    sc.open_position(conn, open_date="2026-07-06", symbol="A", cohort="top",
                     strike=100.0, dte=30, premium_bid=9.9, expiry="2026-08-05")
    due = sc.read_unsettled_due(conn, on_or_after_expiry="2026-08-05")
    assert len(due) == 1 and due[0]["premium_bid"] == 1.50
    sc.settle_position(conn, open_date="2026-07-06", symbol="A",
                       settle_price=98.0, pnl=-0.50, settled_at=123)
    assert sc.read_unsettled_due(conn, on_or_after_expiry="2026-09-01") == []
    settled = sc.read_ledger(conn, settled_only=True)
    assert len(settled) == 1 and settled[0]["pnl"] == -0.50
    # Double-settle guard: a second settle must NOT overwrite the recorded PnL.
    sc.settle_position(conn, open_date="2026-07-06", symbol="A",
                       settle_price=200.0, pnl=999.0, settled_at=456)
    row = sc.read_ledger(conn, settled_only=True)[0]
    assert row["pnl"] == -0.50 and row["settled_at"] == 123
