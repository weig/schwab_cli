"""Integration test for the daily screener orchestration (injected fakes)."""
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


def _chain(bid: float, *, expiry="2026-08-21", dte=31, spot=540.0) -> dict:
    row = {"strikePrice": 500.0, "delta": -0.25, "bid": bid, "ask": bid + 0.2,
           "openInterest": 2000, "totalVolume": 400}
    return {"underlying": {"last": spot},
            "putExpDateMap": {f"{expiry}:{dte}": {"500.0": [row]}}}


def _deps(chains: dict, *, vol=None, earnings=None, fwd=None, settle=None,
          market_open=True) -> ScreenerDeps:
    vol = vol or {}
    earnings = earnings or {}
    fwd = fwd or {}
    settle = settle or {}
    return ScreenerDeps(
        universe=lambda: sorted(chains),
        fetch_chain=lambda s: chains[s],
        vol_context=lambda s: vol.get(s, VolContext(atm_iv_30d=0.25, hv_30d=0.15)),
        earnings_date=lambda s: earnings.get(s),
        forward_closes=lambda s, d: fwd.get(s, []),
        settle_price=lambda s, e: settle.get(s),
        market_open=market_open,
    )


def test_full_pass_ranks_and_opens_positions(conn):
    # FAT has a fatter bid → higher VRP → rank 1 (top); THIN → bottom.
    chains = {"FAT": _chain(9.0), "THIN": _chain(3.0)}
    deps = _deps(chains)
    summary = run_screener_update(
        conn, deps, CFG, snapshot_date="2026-07-06", now_ms=1_700_000_000_000
    )
    assert summary["universe"] == 2
    assert summary["survivors"] == 2
    ranking = store.read_ranking(conn, ranking_date="2026-07-06")
    assert [r["symbol"] for r in ranking] == ["FAT", "THIN"]
    # cohort_size=1 → top=FAT, bottom=THIN
    ledger = store.read_ledger(conn)
    cohorts = {r["symbol"]: r["cohort"] for r in ledger}
    assert cohorts == {"FAT": "top", "THIN": "bottom"}


def test_filtered_names_excluded_from_ranking(conn):
    chains = {"GOOD": _chain(4.0), "ILLIQUID": _chain(4.0)}
    # ILLIQUID fails the OI filter.
    deps = _deps(chains, vol={
        "GOOD": VolContext(atm_iv_30d=0.25, hv_30d=0.15),
        "ILLIQUID": VolContext(atm_iv_30d=0.25, hv_30d=0.15),
    })
    # Override ILLIQUID's chain to thin OI.
    chains["ILLIQUID"]["putExpDateMap"]["2026-08-21:31"]["500.0"][0]["openInterest"] = 10
    summary = run_screener_update(
        conn, deps, CFG, snapshot_date="2026-07-06", now_ms=1
    )
    assert summary["survivors"] == 1
    assert summary["filtered"].get("oi_too_low") == 1
    assert [r["symbol"] for r in store.read_ranking(conn, ranking_date="2026-07-06")] == ["GOOD"]


def test_market_closed_yields_no_survivors(conn):
    deps = _deps({"A": _chain(4.0)}, market_open=False)
    summary = run_screener_update(conn, deps, CFG, snapshot_date="2026-07-06", now_ms=1)
    assert summary["survivors"] == 0
    assert summary["filtered"].get("stale_quote") == 1


def test_idempotent_rerun(conn):
    chains = {"FAT": _chain(9.0), "THIN": _chain(3.0)}
    deps = _deps(chains)
    a = run_screener_update(conn, deps, CFG, snapshot_date="2026-07-06", now_ms=1)
    b = run_screener_update(conn, deps, CFG, snapshot_date="2026-07-06", now_ms=2)
    assert a["positions_opened"] == b["positions_opened"]
    # No duplicate ledger rows, single snapshot per symbol.
    assert len(store.read_ledger(conn)) == 2
    assert len(store.read_contract_snapshots(conn, snapshot_date="2026-07-06")) == 2


def test_settles_matured_positions(conn):
    store.open_position(conn, open_date="2026-06-01", symbol="A", cohort="top",
                        strike=100.0, dte=30, premium_bid=1.5, expiry="2026-07-01")
    deps = _deps({"A": _chain(4.0)}, settle={"A": 98.0})
    run_screener_update(conn, deps, CFG, snapshot_date="2026-07-06", now_ms=999)
    settled = store.read_ledger(conn, settled_only=True)
    assert len(settled) == 1
    assert settled[0]["settle_price"] == 98.0
    assert settled[0]["pnl"] == 1.5 - 2.0  # premium minus intrinsic


def test_backfills_forward_rv(conn):
    # An old snapshot lacking rv, plus 22 forward closes → rv filled.
    store.record_contract_snapshot(conn, store.ContractSnapshot(
        snapshot_date="2026-05-01", symbol="A", captured_at_ms=1,
        put_strike=100.0, dte=30))
    closes = [100.0 * (1.001 ** i) for i in range(22)]
    deps = _deps({"A": _chain(4.0)}, fwd={"A": closes})
    summary = run_screener_update(conn, deps, CFG, snapshot_date="2026-07-06", now_ms=1)
    assert summary["rv_backfilled"] == 1
    row = [r for r in store.read_contract_snapshots(conn, snapshot_date="2026-05-01")][0]
    assert row["rv_fwd_21d"] is not None and row["rv_fwd_21d"] > 0


def test_symbol_error_does_not_abort_run(conn):
    def _boom(_s):
        raise RuntimeError("chain down")
    deps = ScreenerDeps(
        universe=lambda: ["BAD", "GOOD"],
        fetch_chain=lambda s: _boom(s) if s == "BAD" else _chain(4.0),
        vol_context=lambda s: VolContext(atm_iv_30d=0.25, hv_30d=0.15),
        earnings_date=lambda s: None, forward_closes=lambda s, d: [],
        settle_price=lambda s, e: None, market_open=True,
    )
    summary = run_screener_update(conn, deps, CFG, snapshot_date="2026-07-06", now_ms=1)
    assert summary["universe"] == 2
    assert summary["survivors"] == 1  # GOOD survived, BAD captured as error
    rows = {r["symbol"]: r["snapshot_quality"]
            for r in store.read_contract_snapshots(conn, snapshot_date="2026-07-06")}
    assert rows["BAD"] == "error" and rows["GOOD"] == "ok"
