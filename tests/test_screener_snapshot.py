"""Tests for Stage A snapshot builder + quality guards."""
from __future__ import annotations

from schwab_cli.screener.config import ScreenerConfig
from schwab_cli.screener.locate import TargetPut
from schwab_cli.screener.snapshot import (
    VolContext,
    assess_quality,
    build_snapshot,
    is_survivor,
)

CFG = ScreenerConfig()


def _tp(delta=-0.25, bid=4.0, ask=4.2, oi=1000, vol=300, spread=0.048):
    return TargetPut(expiry="2026-08-21", dte=31, strike=500.0, delta=delta,
                     bid=bid, ask=ask, mid=(bid + ask) / 2,
                     open_interest=oi, volume=vol, spread_pct=spread)


def test_quality_market_closed():
    assert assess_quality(_tp(), None, market_open=False) == (
        "stale_quote", "market_closed")


def test_quality_bid_gt_ask():
    assert assess_quality(_tp(bid=5.0, ask=4.0), None, market_open=True) == (
        "bad_data", "bid_gt_ask")


def test_quality_delta_out_of_band():
    assert assess_quality(_tp(delta=-0.05), None, market_open=True) == (
        "bad_data", "delta_out_of_band")


def test_quality_no_contract_carries_locate_reason():
    assert assess_quality(None, "no_expiry_in_window", market_open=True) == (
        "bad_data", "no_expiry_in_window")


def test_quality_ok():
    assert assess_quality(_tp(), None, market_open=True) == ("ok", None)


def _build(**kw):
    base = dict(
        snapshot_date="2026-07-06", symbol="QQQ", captured_at_ms=1,
        tp=_tp(), locate_reason=None,
        vol_ctx=VolContext(atm_iv_30d=0.22, hv_30d=0.18, ivr=0.4),
        underlying_last=540.0, next_earnings_date="2026-09-15",
        market_open=True, cfg=CFG,
    )
    base.update(kw)
    return build_snapshot(**base)


def test_build_survivor():
    snap = _build()
    assert snap.snapshot_quality == "ok"
    assert snap.filter_reason is None
    assert is_survivor(snap)
    assert snap.days_to_earnings == 71  # 2026-07-06 -> 2026-09-15
    assert snap.put_bid == 4.0 and snap.hv_30d == 0.18


def test_build_applies_hard_filter_when_ok():
    # ok quality but earnings 5 days out → filter_reason set, not a survivor.
    snap = _build(next_earnings_date="2026-07-11")
    assert snap.snapshot_quality == "ok"
    assert snap.filter_reason == "earnings_window"
    assert not is_survivor(snap)


def test_build_bad_quality_skips_filters():
    snap = _build(tp=_tp(bid=9.0, ask=4.0))
    assert snap.snapshot_quality == "bad_data"
    assert snap.filter_reason == "bid_gt_ask"
    assert not is_survivor(snap)


def test_build_no_contract():
    snap = _build(tp=None, locate_reason="no_puts")
    assert snap.snapshot_quality == "bad_data"
    assert snap.put_strike is None
    assert snap.filter_reason == "no_puts"


def test_build_earnings_unknown_fail_closed():
    snap = _build(next_earnings_date=None)
    assert snap.snapshot_quality == "ok"
    assert snap.filter_reason == "earnings_unknown"
    assert not is_survivor(snap)
