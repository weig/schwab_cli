"""Tests for Black-Scholes + IV solver.

The solver has been validated live against Schwab's chain endpoint on
near-ATM, short-dated NVDA options to within a few bps. These tests
pin the sign + magnitude of the math and key edge cases (zero/negative
T, below-intrinsic price, converged round-trip).
"""

import math

import pytest

from schwab_cli.analytics.bs import bs_price, implied_vol


# ---- BS price ----------------------------------------------------------


def test_bs_price_atm_call_has_positive_time_value():
    # S=K, r=0, T>0, sigma>0 → price > 0 (all extrinsic).
    p = bs_price(100.0, 100.0, 0.25, 0.0, 0.30, is_call=True)
    assert p > 0


def test_bs_price_zero_time_equals_intrinsic():
    assert bs_price(110.0, 100.0, 0.0, 0.05, 0.30, is_call=True) == pytest.approx(10.0)
    assert bs_price(90.0, 100.0, 0.0, 0.05, 0.30, is_call=True) == pytest.approx(0.0)
    assert bs_price(90.0, 100.0, 0.0, 0.05, 0.30, is_call=False) == pytest.approx(10.0)


def test_bs_price_zero_sigma_equals_intrinsic():
    assert bs_price(110.0, 100.0, 1.0, 0.0, 0.0, is_call=True) == pytest.approx(10.0)


def test_bs_price_call_put_parity():
    # c - p = S - K * exp(-rT)  (no dividends)
    S, K, T, r, sigma = 100.0, 100.0, 0.5, 0.05, 0.30
    c = bs_price(S, K, T, r, sigma, is_call=True)
    p = bs_price(S, K, T, r, sigma, is_call=False)
    assert c - p == pytest.approx(S - K * math.exp(-r * T), rel=1e-10)


# ---- IV solver ---------------------------------------------------------


def test_implied_vol_round_trips_atm_call():
    """price(σ=0.30) solved → σ ≈ 0.30."""
    S, K, T, r, sigma = 100.0, 100.0, 0.25, 0.04, 0.30
    price = bs_price(S, K, T, r, sigma, is_call=True)
    solved = implied_vol(price, S, K, T, r, is_call=True)
    assert solved == pytest.approx(sigma, rel=1e-6)


def test_implied_vol_round_trips_deep_itm_put():
    S, K, T, r, sigma = 80.0, 100.0, 1.0, 0.04, 0.45
    price = bs_price(S, K, T, r, sigma, is_call=False)
    solved = implied_vol(price, S, K, T, r, is_call=False)
    assert solved == pytest.approx(sigma, rel=1e-4)


def test_implied_vol_matches_live_nvda_validation_within_1pct():
    # Values pulled from our earlier live validation against Schwab API:
    # NVDA 260501 C 202.5 @ S=$202.50, K=$202.50, T=9/365, r=4.5%, price $4.75
    # Schwab reported IV ≈ 0.3658. Our solver got 0.3657.
    iv = implied_vol(4.75, 202.50, 202.50, 9 / 365, 0.045, is_call=True)
    assert iv is not None
    assert iv == pytest.approx(0.3658, abs=0.001)


def test_implied_vol_none_on_zero_time():
    assert implied_vol(5.0, 100.0, 100.0, 0.0, 0.04, is_call=True) is None
    assert implied_vol(5.0, 100.0, 100.0, -0.1, 0.04, is_call=True) is None


def test_implied_vol_none_below_intrinsic():
    # Call priced below (S-K) can't exist under BS.
    assert implied_vol(1.0, 200.0, 100.0, 0.5, 0.04, is_call=True) is None


def test_implied_vol_handles_very_deep_otm_gracefully():
    # Deep OTM with tiny premium — should either solve to a very low IV
    # or return None without raising.
    iv = implied_vol(0.001, 100.0, 200.0, 0.1, 0.04, is_call=True)
    assert iv is None or iv >= 0
