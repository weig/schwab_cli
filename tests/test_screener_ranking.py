"""Tests for ranking, ledger cohorts/settle, and forward RV."""
from __future__ import annotations

import math

from schwab_cli.analytics.bs import bs_price
from schwab_cli.screener.config import ScreenerConfig
from schwab_cli.screener.forward_rv import forward_rv
from schwab_cli.screener.ledger import select_cohorts, settle_pnl
from schwab_cli.screener.ranking import compute_vrp, rank_survivors
from schwab_cli.storage.screener import ContractSnapshot

CFG = ScreenerConfig()


def _snap(symbol="X", bid=4.0, strike=500.0, dte=30, hv=0.20, spot=540.0,
          ivr=0.5, spread=0.05, low_conf=False) -> ContractSnapshot:
    return ContractSnapshot(
        snapshot_date="2026-07-06", symbol=symbol, captured_at_ms=1,
        put_strike=strike, put_delta_actual=-0.25, put_bid=bid, put_ask=bid + 0.2,
        put_mid=bid + 0.1, put_oi=1000, put_volume=300, spread_pct=spread,
        underlying_last=spot, atm_iv_30d=0.22, hv_30d=hv, dte=dte,
        target_expiry="2026-08-05", ivr=ivr, ivr_low_conf=low_conf,
    )


def test_compute_vrp_matches_hand_math():
    snap = _snap(bid=4.0, strike=500.0, dte=30, hv=0.20, spot=540.0)
    got = compute_vrp(snap, CFG)
    ann = 365.0 / 30
    exp_py = 4.0 / 500.0 * ann
    fair = bs_price(S=540.0, K=500.0, T=30 / 365.0, r=CFG.rf_rate,
                    sigma=0.20, is_call=False)
    exp_fair_yield = fair / 500.0 * ann
    assert math.isclose(got["premium_yield_bid"], exp_py, rel_tol=1e-9)
    assert math.isclose(got["fair_yield"], exp_fair_yield, rel_tol=1e-9)
    assert math.isclose(
        got["executable_vrp"], exp_py - exp_fair_yield, rel_tol=1e-9
    )


def test_compute_vrp_none_on_missing_inputs():
    assert compute_vrp(_snap(hv=None) if False else ContractSnapshot(
        snapshot_date="d", symbol="X", captured_at_ms=1, put_bid=4.0,
        put_strike=500.0, dte=30, hv_30d=None, underlying_last=540.0), CFG) is None
    assert compute_vrp(ContractSnapshot(
        snapshot_date="d", symbol="X", captured_at_ms=1, put_bid=4.0,
        put_strike=500.0, dte=0, hv_30d=0.2, underlying_last=540.0), CFG) is None


def test_rank_orders_by_vrp_desc():
    # Higher bid at same fair value → higher VRP → rank 1.
    fat = _snap(symbol="FAT", bid=6.0)
    thin = _snap(symbol="THIN", bid=3.0)
    ranked = rank_survivors([thin, fat], CFG)
    assert [r["symbol"] for r in ranked] == ["FAT", "THIN"]
    assert ranked[0]["rank"] == 1 and ranked[1]["rank"] == 2


def test_rank_tiebreak_by_ivr_then_spread():
    a = _snap(symbol="A", ivr=0.9, spread=0.06)
    b = _snap(symbol="B", ivr=0.2, spread=0.02)
    ranked = rank_survivors([b, a], CFG)
    # Equal VRP → higher IVR (A) ranks first.
    assert [r["symbol"] for r in ranked] == ["A", "B"]


def test_rank_drops_unrankable():
    good = _snap(symbol="G")
    bad = ContractSnapshot(snapshot_date="d", symbol="BAD", captured_at_ms=1,
                           put_bid=4.0, put_strike=500.0, dte=30, hv_30d=None,
                           underlying_last=540.0)
    ranked = rank_survivors([good, bad], CFG)
    assert [r["symbol"] for r in ranked] == ["G"]


def test_settle_pnl():
    # OTM at expiry (S above strike): keep full premium.
    assert settle_pnl(1.50, 100.0, 105.0) == 1.50
    # ITM: premium minus intrinsic loss.
    assert math.isclose(settle_pnl(1.50, 100.0, 97.0), 1.50 - 3.0)


def test_select_cohorts_disjoint_when_scarce():
    ranked = [{"symbol": s, "rank": i + 1} for i, s in enumerate("ABC")]
    cohorts = select_cohorts(ranked, ScreenerConfig(cohort_size=10))
    labels = {(c, r["symbol"]) for c, r in cohorts}
    tops = {s for c, s in labels if c == "top"}
    bottoms = {s for c, s in labels if c == "bottom"}
    assert tops.isdisjoint(bottoms)  # never same symbol in both


def test_select_cohorts_top_and_bottom():
    ranked = [{"symbol": f"S{i}", "rank": i} for i in range(1, 25)]
    cohorts = select_cohorts(ranked, ScreenerConfig(cohort_size=10))
    tops = [r["symbol"] for c, r in cohorts if c == "top"]
    bottoms = [r["symbol"] for c, r in cohorts if c == "bottom"]
    assert tops == [f"S{i}" for i in range(1, 11)]
    assert bottoms == [f"S{i}" for i in range(15, 25)]


def test_forward_rv_needs_full_window():
    assert forward_rv([100.0] * 10) is None
    closes = [100.0 * (1.001 ** i) for i in range(22)]  # 22 closes → 21 returns
    rv = forward_rv(closes)
    assert rv is not None and rv > 0
