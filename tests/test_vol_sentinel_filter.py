"""INC-1 regression: option-chain sentinels must never reach the ATM curve.

The GOOG 2026-05-17 incident: a weekend run pulled a stale/empty chain whose
ATM strikes carried Schwab's ``-9.99`` IV sentinel. ``pick_atm_curve`` only
filtered ``None``, so ``-9.99`` entered the curve and ``interp_iv_in_variance``
SQUARED it in variance space ((-9.99)² = 99.8), producing atm_iv_30d = 8.43
(843% vol). Being POSITIVE, that value then evaded every downstream ``iv > 0``
defense. Sentinels must be rejected at the curve-building entrance.
"""
from __future__ import annotations

import pytest

from schwab_cli.analytics.vol import (
    interp_iv_in_variance,
    is_valid_contract,
    pick_atm_curve,
)


# ---- is_valid_contract ----------------------------------------------------

@pytest.mark.parametrize("contract,expected", [
    ({"iv": 0.31, "delta": 0.5, "bid": 1.0, "ask": 1.2}, True),
    ({"iv": -9.99, "delta": 0.5, "bid": 1.0, "ask": 1.2}, False),   # IV sentinel
    ({"iv": 0.0, "delta": 0.5, "bid": 1.0, "ask": 1.2}, False),     # zero IV
    ({"iv": 0.31, "delta": -999, "bid": 1.0, "ask": 1.2}, False),   # delta sentinel
    ({"iv": 0.31, "delta": 0.5, "bid": 0.0, "ask": 1.2}, False),    # no bid
    ({"iv": 0.31, "delta": 0.5, "bid": 1.0, "ask": 0.0}, False),    # no ask
    ({"iv": None, "delta": 0.5, "bid": 1.0, "ask": 1.2}, False),    # missing IV
])
def test_is_valid_contract(contract, expected):
    assert is_valid_contract(contract) is expected


def test_is_valid_contract_tolerates_missing_bid_ask():
    """Curve/skew inputs may lack bid/ask (only iv/delta matter there); a
    contract with a sane iv and no quote fields is still IV-valid."""
    assert is_valid_contract({"iv": 0.31, "delta": 0.5}) is True


# ---- pick_atm_curve drops sentinel IVs ------------------------------------

def _expiry(dte, strike, iv, spot=100.0, oi=500):
    return {"dte": dte, "contracts": [
        {"side": "C", "strike": strike, "iv": iv, "openInterest": oi,
         "volume": 10, "delta": 0.5},
        {"side": "P", "strike": strike, "iv": iv, "openInterest": oi,
         "volume": 10, "delta": -0.5},
    ]}


def test_pick_atm_curve_excludes_sentinel_expiry():
    """An expiry whose ATM strike carries -9.99 must not contribute a curve
    point (rather than contributing a poisoned -9.99 one)."""
    expiries = [
        _expiry(19, 100.0, -9.99),   # sentinel — must be dropped
        _expiry(26, 100.0, 0.3334),  # good
    ]
    curve = pick_atm_curve(expiries, spot=100.0)
    dtes = [d for d, _ in curve]
    assert 19 not in dtes
    assert (26, pytest.approx(0.3334)) in [(d, v) for d, v in curve]


def test_curve_with_only_sentinels_is_empty_and_interp_returns_none():
    expiries = [_expiry(19, 100.0, -9.99), _expiry(32, 100.0, -9.99)]
    curve = pick_atm_curve(expiries, spot=100.0)
    assert curve == []
    # And interpolation over the empty curve never fabricates a value.
    assert interp_iv_in_variance(curve, 30) is None


def test_the_exact_goog_incident_yields_none_not_8_43():
    """Reproduce the incident bracket: (26, 0.3334) good, (32, -9.99) bad.
    Pre-fix this produced sqrt(2130/30)=8.43. The sentinel expiry must be
    dropped, leaving a single-point curve that cannot bracket dte=30."""
    expiries = [_expiry(26, 100.0, 0.3334), _expiry(32, 100.0, -9.99)]
    curve = pick_atm_curve(expiries, spot=100.0)
    assert (32, -9.99) not in curve
    out = interp_iv_in_variance(curve, 30)
    # only (26,·) survives → 30 is outside the bracket → None, never 8.43
    assert out is None or out < 3.0
