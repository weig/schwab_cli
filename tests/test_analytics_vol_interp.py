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
