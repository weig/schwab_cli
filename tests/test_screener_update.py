"""Integration test for the daily screener reader (injected fakes).

The screener reads a stored put band per symbol (no fetch); these tests feed
band rows directly through the injected ``put_band`` dep.
"""
from __future__ import annotations

import pytest

from schwab_cli.screener.config import ScreenerConfig
from schwab_cli.screener.snapshot import VolContext
from schwab_cli.screener.update import ScreenerDeps, run_screener_update
from schwab_cli.storage import screener as store
from schwab_cli.storage.vol_history import connect

CFG = ScreenerConfig(require_earnings_date=False, cohort_size=1)


@pytest.fixture
def conn(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    with connect() as c:
        yield c


def _band(bid: float, *, expiry="2026-08-21", dte=31, spot=540.0, oi=2000) -> list[dict]:
    """A one-contract stored put band (dict rows, as read_put_band returns)."""
    return [{
        "expiry": expiry, "dte": dte, "strike": 500.0, "delta": -0.25,
        "bid": bid, "ask": bid + 0.2, "open_interest": oi, "volume": 400,
        "underlying_last": spot,
    }]


def _deps(bands: dict, *, vol=None, earnings=None, fwd=None, settle=None,
          market_open=True) -> ScreenerDeps:
    vol = vol or {}
    earnings = earnings or {}
    fwd = fwd or {}
    settle = settle or {}
    return ScreenerDeps(
        universe=lambda: sorted(bands),
        put_band=lambda s: bands[s],
        vol_context=lambda s: vol.get(s, VolContext(atm_iv_30d=0.25, hv_30d=0.15)),
        earnings_date=lambda s: earnings.get(s),
        forward_closes=lambda s, d: fwd.get(s, []),
        settle_price=lambda s, e: settle.get(s),
        market_open=market_open,
    )


def test_full_pass_ranks_and_opens_positions(conn):
    bands = {"FAT": _band(9.0), "THIN": _band(3.0)}
    summary = run_screener_update(
        conn, _deps(bands), CFG, snapshot_date="2026-07-06", now_ms=1_700_000_000_000
    )
    assert summary["universe"] == 2 and summary["survivors"] == 2
    ranking = store.read_ranking(conn, ranking_date="2026-07-06")
    assert [r["symbol"] for r in ranking] == ["FAT", "THIN"]
    cohorts = {r["symbol"]: r["cohort"] for r in store.read_ledger(conn)}
    assert cohorts == {"FAT": "top", "THIN": "bottom"}


def test_filtered_names_excluded_from_ranking(conn):
    bands = {"GOOD": _band(4.0), "ILLIQUID": _band(4.0, oi=10)}
    summary = run_screener_update(conn, _deps(bands), CFG, snapshot_date="2026-07-06", now_ms=1)
    assert summary["survivors"] == 1
    assert summary["filtered"].get("oi_too_low") == 1
    assert [r["symbol"] for r in store.read_ranking(conn, ranking_date="2026-07-06")] == ["GOOD"]


def test_locates_target_from_multi_contract_band(conn):
    # Band has several puts; locator must pick delta closest to -0.25.
    band = [
        {"expiry": "2026-08-21", "dte": 31, "strike": 520.0, "delta": -0.40,
         "bid": 8.0, "ask": 8.2, "open_interest": 2000, "volume": 400,
         "underlying_last": 540.0},
        {"expiry": "2026-08-21", "dte": 31, "strike": 500.0, "delta": -0.26,
         "bid": 4.0, "ask": 4.2, "open_interest": 2000, "volume": 400,
         "underlying_last": 540.0},
    ]
    summary = run_screener_update(conn, _deps({"X": band}), CFG,
                                  snapshot_date="2026-07-06", now_ms=1)
    assert summary["survivors"] == 1
    row = store.read_ranking(conn, ranking_date="2026-07-06")[0]
    assert row["put_strike"] == 500.0


def test_market_closed_yields_no_survivors(conn):
    summary = run_screener_update(conn, _deps({"A": _band(4.0)}, market_open=False),
                                  CFG, snapshot_date="2026-07-06", now_ms=1)
    assert summary["survivors"] == 0
    assert summary["filtered"].get("stale_quote") == 1


def test_empty_band_symbol_is_bad_data(conn):
    summary = run_screener_update(conn, _deps({"A": []}), CFG,
                                  snapshot_date="2026-07-06", now_ms=1)
    assert summary["survivors"] == 0
    rows = store.read_contract_snapshots(conn, snapshot_date="2026-07-06")
    assert rows[0]["snapshot_quality"] == "bad_data"


def test_idempotent_rerun(conn):
    bands = {"FAT": _band(9.0), "THIN": _band(3.0)}
    a = run_screener_update(conn, _deps(bands), CFG, snapshot_date="2026-07-06", now_ms=1)
    b = run_screener_update(conn, _deps(bands), CFG, snapshot_date="2026-07-06", now_ms=2)
    assert a["positions_opened"] == b["positions_opened"]
    assert len(store.read_ledger(conn)) == 2
    assert len(store.read_contract_snapshots(conn, snapshot_date="2026-07-06")) == 2


def test_settles_matured_positions(conn):
    store.open_position(conn, open_date="2026-06-01", symbol="A", cohort="top",
                        strike=100.0, dte=30, premium_bid=1.5, expiry="2026-07-01")
    deps = _deps({"A": _band(4.0)}, settle={"A": 98.0})
    run_screener_update(conn, deps, CFG, snapshot_date="2026-07-06", now_ms=999)
    settled = store.read_ledger(conn, settled_only=True)
    assert len(settled) == 1 and settled[0]["pnl"] == 1.5 - 2.0


def test_backfills_forward_rv(conn):
    store.record_contract_snapshot(conn, store.ContractSnapshot(
        snapshot_date="2026-05-01", symbol="A", captured_at_ms=1,
        put_strike=100.0, dte=30))
    closes = [100.0 * (1.001 ** i) for i in range(22)]
    deps = _deps({"A": _band(4.0)}, fwd={"A": closes})
    summary = run_screener_update(conn, deps, CFG, snapshot_date="2026-07-06", now_ms=1)
    assert summary["rv_backfilled"] == 1
    row = store.read_contract_snapshots(conn, snapshot_date="2026-05-01")[0]
    assert row["rv_fwd_21d"] is not None and row["rv_fwd_21d"] > 0


def test_symbol_error_does_not_abort_run(conn):
    def _boom(_s):
        raise RuntimeError("band read failed")
    deps = ScreenerDeps(
        universe=lambda: ["BAD", "GOOD"],
        put_band=lambda s: _boom(s) if s == "BAD" else _band(4.0),
        vol_context=lambda s: VolContext(atm_iv_30d=0.25, hv_30d=0.15),
        earnings_date=lambda s: None, forward_closes=lambda s, d: [],
        settle_price=lambda s, e: None, market_open=True,
    )
    summary = run_screener_update(conn, deps, CFG, snapshot_date="2026-07-06", now_ms=1)
    assert summary["universe"] == 2 and summary["survivors"] == 1
    rows = {r["symbol"]: r["snapshot_quality"]
            for r in store.read_contract_snapshots(conn, snapshot_date="2026-07-06")}
    assert rows["BAD"] == "error" and rows["GOOD"] == "ok"
