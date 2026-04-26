"""Variance-linear ATM IV interpolation.

We interpolate in v(t) = iv²·t (variance × time) — the industry-
standard form for term-structure interpolation. Linear-in-IV is
subtly biased near the front; linear-in-variance is unbiased under
flat-skew assumptions.
"""
from __future__ import annotations

import math

import pytest

from schwab_cli.analytics.vol import interp_iv_in_variance


def test_returns_none_when_below_bracket():
    curve = [(30, 0.30), (60, 0.32)]
    assert interp_iv_in_variance(curve, 14) is None


def test_returns_none_when_above_bracket():
    curve = [(30, 0.30), (60, 0.32)]
    assert interp_iv_in_variance(curve, 90) is None


def test_returns_exact_when_target_matches_endpoint():
    curve = [(30, 0.30), (60, 0.32)]
    assert interp_iv_in_variance(curve, 30) == pytest.approx(0.30)
    assert interp_iv_in_variance(curve, 60) == pytest.approx(0.32)


def test_variance_linear_midpoint():
    """At target=45 between (30, 0.30) and (60, 0.40):
       v0 = 0.30² · 30 = 2.70
       v1 = 0.40² · 60 = 9.60
       v_mid = (2.70 + 9.60) / 2 = 6.15
       iv_mid = sqrt(6.15 / 45) = sqrt(0.13666…) ≈ 0.3697
    Strictly different from linear-in-IV (which would give 0.35).
    """
    curve = [(30, 0.30), (60, 0.40)]
    out = interp_iv_in_variance(curve, 45)
    assert out == pytest.approx(math.sqrt(6.15 / 45), rel=1e-6)
    assert out != pytest.approx(0.35, rel=1e-3)  # not linear in IV


def test_handles_three_point_curve():
    curve = [(7, 0.30), (30, 0.32), (60, 0.34)]
    # Target between the second pair — must use (30, 60), not (7, 30).
    out = interp_iv_in_variance(curve, 45)
    v0 = 0.32**2 * 30
    v1 = 0.34**2 * 60
    v_t = v0 + (v1 - v0) * (45 - 30) / (60 - 30)
    assert out == pytest.approx(math.sqrt(v_t / 45), rel=1e-6)


def test_empty_curve_returns_none():
    assert interp_iv_in_variance([], 30) is None


def test_single_point_curve_returns_none_unless_exact():
    curve = [(30, 0.30)]
    assert interp_iv_in_variance(curve, 30) == pytest.approx(0.30)
    assert interp_iv_in_variance(curve, 45) is None


from schwab_cli.analytics.vol import pick_atm_curve


def _expiry(dte, total_vol, strikes):
    """Helper: build an expiry dict with (strike, vol, call_iv, put_iv) tuples."""
    contracts = []
    for strike, vol, civ, piv in strikes:
        contracts.append({"strike": strike, "side": "C",
                          "volume": vol // 2, "iv": civ})
        contracts.append({"strike": strike, "side": "P",
                          "volume": vol // 2, "iv": piv})
    return {"expiry": "2026-01-01", "dte": dte, "contracts": contracts}


def test_pick_atm_curve_skips_low_volume():
    expiries = [
        _expiry(7, total_vol=10, strikes=[(100, 5, 0.35, 0.36)]),  # < 100
        _expiry(30, total_vol=200, strikes=[(100, 200, 0.30, 0.32)]),
    ]
    out = pick_atm_curve(expiries, spot=100.0)
    assert len(out) == 1
    assert out[0] == (30, pytest.approx(0.31))  # midpoint


def test_pick_atm_curve_picks_closest_strike():
    expiries = [_expiry(30, total_vol=500, strikes=[
        (90, 200, 0.40, 0.42),
        (100, 200, 0.30, 0.32),
        (110, 100, 0.50, 0.52),
    ])]
    out = pick_atm_curve(expiries, spot=101.0)
    assert out == [(30, pytest.approx(0.31))]   # 100 strike, midpoint


def test_pick_atm_curve_uses_single_side_when_only_one_iv():
    expiries = [_expiry(30, total_vol=500, strikes=[(100, 500, None, 0.33)])]
    out = pick_atm_curve(expiries, spot=100.0)
    assert out == [(30, pytest.approx(0.33))]


def test_pick_atm_curve_sorted_by_dte():
    e1 = _expiry(60, 500, [(100, 500, 0.30, 0.30)])
    e2 = _expiry(7,  500, [(100, 500, 0.40, 0.40)])
    e3 = _expiry(30, 500, [(100, 500, 0.35, 0.35)])
    out = pick_atm_curve([e1, e2, e3], spot=100.0)
    assert [d for d, _ in out] == [7, 30, 60]


from schwab_cli.analytics.vol import closest_dte_expiry


def test_closest_dte_picks_nearest():
    expiries = [
        {"expiry": "a", "dte": 7, "contracts": []},
        {"expiry": "b", "dte": 32, "contracts": []},
        {"expiry": "c", "dte": 60, "contracts": []},
    ]
    assert closest_dte_expiry(expiries, target_dte=30)["expiry"] == "b"
    assert closest_dte_expiry(expiries, target_dte=90)["expiry"] == "c"
    assert closest_dte_expiry(expiries, target_dte=1)["expiry"] == "a"


def test_closest_dte_breaks_tie_by_dte_ascending():
    expiries = [
        {"expiry": "x", "dte": 25, "contracts": []},
        {"expiry": "y", "dte": 35, "contracts": []},
    ]
    # Both 5 days off — pick lower DTE (more liquid front side).
    assert closest_dte_expiry(expiries, target_dte=30)["expiry"] == "x"


def test_closest_dte_empty_returns_none():
    assert closest_dte_expiry([], target_dte=30) is None


from schwab_cli.analytics.vol import pick_25d_wing


def test_pick_25d_wing_uses_provided_delta():
    expiry = {
        "expiry": "x", "dte": 30,
        "contracts": [
            {"strike": 90, "side": "P", "delta": -0.10, "iv": 0.40},
            {"strike": 95, "side": "P", "delta": -0.25, "iv": 0.35},  # winner
            {"strike": 98, "side": "P", "delta": -0.40, "iv": 0.32},
        ],
    }
    out = pick_25d_wing(expiry, side="P", target_delta=-0.25, spot=100.0)
    assert out["strike"] == 95
    assert out["iv"] == pytest.approx(0.35)


def test_pick_25d_wing_picks_closest_delta():
    expiry = {
        "expiry": "x", "dte": 30,
        "contracts": [
            {"strike": 110, "side": "C", "delta": +0.20, "iv": 0.31},
            {"strike": 108, "side": "C", "delta": +0.27, "iv": 0.30},  # closest to 0.25
            {"strike": 105, "side": "C", "delta": +0.40, "iv": 0.29},
        ],
    }
    out = pick_25d_wing(expiry, side="C", target_delta=+0.25, spot=100.0)
    assert out["strike"] == 108


def test_pick_25d_wing_returns_none_when_no_side_match():
    expiry = {"expiry": "x", "dte": 30,
              "contracts": [{"strike": 95, "side": "P", "delta": None, "iv": 0.35}]}
    # No deltas at all and no spot/atm_iv to BS-derive.
    out = pick_25d_wing(expiry, side="P", target_delta=-0.25,
                        spot=None, atm_iv=None)
    assert out is None


def test_pick_25d_wing_filters_too_far():
    """If no contract has |delta - target| < 0.10 the wing is too thin.
    Return None rather than picking a useless 0.05Δ contract."""
    expiry = {
        "expiry": "x", "dte": 30,
        "contracts": [
            {"strike": 80, "side": "P", "delta": -0.05, "iv": 0.50},
        ],
    }
    out = pick_25d_wing(expiry, side="P", target_delta=-0.25, spot=100.0)
    assert out is None
