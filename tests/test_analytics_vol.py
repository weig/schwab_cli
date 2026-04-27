"""Unit tests for volatility analytics.

Pure math, no network. The chain flattener and API orchestration live in
`commands/vol.py`; these tests pin down only the stateless calculations.
"""

import math

import pytest

from schwab_cli.analytics.vol import (
    aggregate_pc,
    log_returns,
    percentile_rank,
    pick_atm_contract,
    realized_vol,
    rolling_realized_vol,
)


# ---- log_returns ---------------------------------------------------------


def test_log_returns_empty_is_empty():
    assert log_returns([]) == []


def test_log_returns_single_close_is_empty():
    assert log_returns([100.0]) == []


def test_log_returns_two_closes_returns_one_value():
    out = log_returns([100.0, 110.0])
    assert len(out) == 1
    assert out[0] == pytest.approx(math.log(1.10), rel=1e-12)


def test_log_returns_matches_math_log_exactly():
    closes = [100.0, 101.0, 99.0, 105.0]
    expected = [
        math.log(101.0 / 100.0),
        math.log(99.0 / 101.0),
        math.log(105.0 / 99.0),
    ]
    assert log_returns(closes) == pytest.approx(expected, rel=1e-12)


# ---- realized_vol --------------------------------------------------------


def test_realized_vol_none_when_insufficient_data():
    # window=30 requires 31 closes; give only 5.
    assert realized_vol([100, 101, 102, 103, 104], window=30) is None


def test_realized_vol_of_constant_prices_is_zero():
    closes = [100.0] * 35
    assert realized_vol(closes, window=30) == pytest.approx(0.0, abs=1e-12)


def test_realized_vol_known_sinusoidal_series():
    # Synthetic series where every return is ±1%. stdev of such a sequence
    # over 30 obs is 0.01 (sample stdev with mean 0 is ~0.01 depending on
    # sign pattern); annualised = 0.01 × sqrt(252) ≈ 0.1587.
    rets = [0.01 if i % 2 == 0 else -0.01 for i in range(30)]
    closes = [100.0]
    for r in rets:
        closes.append(closes[-1] * math.exp(r))
    hv = realized_vol(closes, window=30)
    # stdev of alternating ±0.01 = 0.01 * sqrt(30/29) ≈ 0.01017
    assert hv is not None
    assert hv == pytest.approx(0.01 * math.sqrt(30 / 29) * math.sqrt(252), rel=1e-6)


def test_realized_vol_uses_only_last_window_closes():
    # First 20 closes are wildly volatile; last 30+1 are flat.
    # With window=30, we should see ~0 vol even though the full series is volatile.
    volatile = [100.0, 200.0, 50.0, 400.0, 100.0] * 4  # 20 values
    flat = [100.0] * 35
    closes = volatile + flat
    assert realized_vol(closes, window=30) == pytest.approx(0.0, abs=1e-12)


# ---- rolling_realized_vol ------------------------------------------------


def test_rolling_vol_returns_empty_when_insufficient():
    # window=30 needs 31 closes to produce even 1 rolling value.
    assert rolling_realized_vol([100.0] * 10, window=30) == []


def test_rolling_vol_length_is_n_minus_window():
    # 40 closes, window=30 → 40 - 30 = 10 rolling values.
    closes = [100.0 + i for i in range(40)]
    out = rolling_realized_vol(closes, window=30)
    assert len(out) == 10


def test_rolling_vol_last_value_matches_realized_vol():
    # The final entry in the rolling series equals realized_vol on the same data.
    closes = [100.0 + 0.5 * i + (0.3 if i % 3 == 0 else 0.0) for i in range(60)]
    rolling = rolling_realized_vol(closes, window=30)
    single = realized_vol(closes, window=30)
    assert rolling[-1] == pytest.approx(single, rel=1e-12)


# ---- percentile_rank -----------------------------------------------------


def test_percentile_rank_empty_series_returns_zero():
    assert percentile_rank([], 0.5) == 0.0


def test_percentile_rank_value_below_all():
    assert percentile_rank([1, 2, 3, 4, 5], 0) == 0.0


def test_percentile_rank_value_above_all():
    assert percentile_rank([1, 2, 3, 4, 5], 10) == 100.0


def test_percentile_rank_midrank_on_ties():
    # Series [1, 2, 2, 2, 5], value=2: below=1, equal=3 → rank=1+1.5=2.5 → 50%.
    assert percentile_rank([1, 2, 2, 2, 5], 2) == 50.0


def test_percentile_rank_matches_simple_case():
    # [1..10], value=5 → below=4, equal=1 → rank=4.5 → 45%.
    assert percentile_rank(list(range(1, 11)), 5) == 45.0


# ---- aggregate_pc --------------------------------------------------------


def test_aggregate_pc_empty_returns_zero_totals_and_none_ratios():
    out = aggregate_pc([])
    assert out["call_volume"] == 0
    assert out["put_volume"] == 0
    assert out["call_oi"] == 0
    assert out["put_oi"] == 0
    assert out["volume_ratio"] is None
    assert out["oi_ratio"] is None


def test_aggregate_pc_handles_missing_fields_as_zero():
    contracts = [
        {"side": "C", "volume": 100, "openInterest": 50},
        {"side": "C"},  # no volume / oi fields
        {"side": "P", "volume": None, "openInterest": 10},
    ]
    out = aggregate_pc(contracts)
    assert out["call_volume"] == 100
    assert out["call_oi"] == 50
    assert out["put_volume"] == 0
    assert out["put_oi"] == 10


def test_aggregate_pc_computes_ratios():
    contracts = [
        {"side": "C", "volume": 1000, "openInterest": 500},
        {"side": "P", "volume": 720, "openInterest": 470},
    ]
    out = aggregate_pc(contracts)
    assert out["volume_ratio"] == pytest.approx(0.72)
    assert out["oi_ratio"] == pytest.approx(0.94)


def test_aggregate_pc_ignores_unknown_sides():
    contracts = [
        {"side": "C", "volume": 100, "openInterest": 50},
        {"side": "X", "volume": 999, "openInterest": 999},
        {"side": "P", "volume": 50, "openInterest": 25},
    ]
    out = aggregate_pc(contracts)
    assert out["call_volume"] == 100
    assert out["put_volume"] == 50
    # Unknown side doesn't leak into either bucket.


# ---- pick_atm_contract ---------------------------------------------------


def _exp(expiry: str, dte: int, strikes: dict[float, dict]) -> dict:
    """Test helper: build an expiry block.

    `strikes` maps strike → {"c_iv": …, "p_iv": …, "c_vol": …, "p_vol": …}.
    Returns the expiry-shaped dict the analytics layer expects.
    """
    contracts = []
    for strike, data in strikes.items():
        contracts.append({
            "side": "C", "strike": strike,
            "iv": data.get("c_iv"),
            "volume": data.get("c_vol", 0),
        })
        contracts.append({
            "side": "P", "strike": strike,
            "iv": data.get("p_iv"),
            "volume": data.get("p_vol", 0),
        })
    return {"expiry": expiry, "dte": dte, "contracts": contracts}


def test_pick_atm_picks_strike_closest_to_spot():
    expiries = [
        _exp("2026-05-01", 9, {
            200.0: {"c_iv": 0.35, "p_iv": 0.37, "c_vol": 100, "p_vol": 100},
            202.5: {"c_iv": 0.36, "p_iv": 0.37, "c_vol": 500, "p_vol": 500},
            205.0: {"c_iv": 0.38, "p_iv": 0.40, "c_vol": 100, "p_vol": 100},
        }),
    ]
    out = pick_atm_contract(expiries, spot=202.50)
    assert out is not None
    assert out["strike"] == 202.5
    # IV is midpoint of call and put IV at the chosen strike.
    assert out["iv"] == pytest.approx((0.36 + 0.37) / 2)
    assert out["expiry"] == "2026-05-01"
    assert out["dte"] == 9


def test_pick_atm_skips_low_volume_expiries():
    expiries = [
        # Weekly with tiny volume — should be skipped.
        _exp("2026-04-25", 2, {
            200.0: {"c_iv": 0.35, "p_iv": 0.37, "c_vol": 1, "p_vol": 1},
        }),
        # Monthly with real volume — should be chosen.
        _exp("2026-05-15", 22, {
            200.0: {"c_iv": 0.30, "p_iv": 0.32, "c_vol": 500, "p_vol": 500},
        }),
    ]
    out = pick_atm_contract(expiries, spot=200.0, min_liquidity=100)
    assert out is not None
    assert out["expiry"] == "2026-05-15"


def test_pick_atm_returns_none_when_no_suitable_expiry():
    expiries = [
        _exp("2026-04-25", 2, {
            200.0: {"c_iv": 0.35, "p_iv": 0.37, "c_vol": 1, "p_vol": 1},
        }),
    ]
    out = pick_atm_contract(expiries, spot=200.0, min_liquidity=100)
    assert out is None


def test_pick_atm_uses_open_interest_when_volume_is_zero():
    """Weekend / pre-market chains have volume=0 across the board but
    open interest carries over. The liquidity gate must still pass on
    OI alone — otherwise the cron rejects every symbol on Sunday."""
    # Build an expiry with zero volume but real OI.
    contracts = []
    for strike in (195.0, 200.0, 205.0):
        contracts.append({
            "side": "C", "strike": strike,
            "iv": 0.30, "volume": 0, "openInterest": 500,
        })
        contracts.append({
            "side": "P", "strike": strike,
            "iv": 0.32, "volume": 0, "openInterest": 500,
        })
    expiries = [{
        "expiry": "2026-05-15", "dte": 22, "contracts": contracts,
    }]
    out = pick_atm_contract(expiries, spot=200.0, min_liquidity=100)
    assert out is not None
    assert out["strike"] == 200.0
    assert out["iv"] == pytest.approx(0.31)


def test_pick_atm_handles_missing_iv_gracefully():
    # The ATM strike has no IV; algorithm skips this expiry rather than
    # returning {iv: None}.
    expiries = [
        _exp("2026-05-01", 9, {
            202.5: {"c_iv": None, "p_iv": None, "c_vol": 500, "p_vol": 500},
        }),
        _exp("2026-05-15", 22, {
            200.0: {"c_iv": 0.30, "p_iv": 0.32, "c_vol": 500, "p_vol": 500},
        }),
    ]
    out = pick_atm_contract(expiries, spot=200.0, min_liquidity=100)
    assert out is not None
    assert out["expiry"] == "2026-05-15"
