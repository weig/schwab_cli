"""Tests for the strategy classifier.

Input: a list of :class:`Leg`. Output: a :class:`Classification`
carrying the human-readable strategy name, Schwab-ticket keyword,
support flag for Phase-1 analytics, and flags for naked / unlimited
exposure.
"""

from __future__ import annotations

from datetime import date

import pytest

from schwab_cli.analytics.strategy_classify import Classification, classify
from schwab_cli.analytics.strategy_legs import Leg


EXP = date(2026, 5, 1)
EXP2 = date(2026, 7, 1)


def L(qty: int, side: str, strike: float, exp: date = EXP) -> Leg:  # noqa: E743
    return Leg(qty=qty, side=side, expiry=exp, strike=strike)  # type: ignore[arg-type]


# ---- 1-leg ------------------------------------------------------------


def test_long_call():
    c = classify([L(1, "C", 255)])
    assert c.strategy == "Long Call"
    assert c.ticket_name == ""
    assert c.supported is True
    assert c.naked is False


def test_short_call_is_naked():
    c = classify([L(-1, "C", 255)])
    assert c.strategy == "Short Call"
    assert c.naked is True
    assert c.ticket_name == ""


def test_long_put():
    c = classify([L(1, "P", 240)])
    assert c.strategy == "Long Put"
    assert c.ticket_name == ""


def test_short_put_is_naked():
    c = classify([L(-1, "P", 240)])
    assert c.strategy == "Short Put"
    assert c.naked is True


# ---- 2-leg verticals --------------------------------------------------


def test_bull_call_spread():
    c = classify([L(1, "C", 255), L(-1, "C", 260)])
    assert c.strategy == "Bull Call Spread"
    assert c.ticket_name == "VERTICAL"
    assert c.naked is False
    assert c.supported is True


def test_bear_call_spread():
    c = classify([L(-1, "C", 255), L(1, "C", 260)])
    assert c.strategy == "Bear Call Spread"
    assert c.ticket_name == "VERTICAL"
    assert c.naked is False


def test_bull_put_spread():
    # Long low-strike put, short high-strike put (credit, bullish bias).
    c = classify([L(1, "P", 235), L(-1, "P", 240)])
    assert c.strategy == "Bull Put Spread"
    assert c.ticket_name == "VERTICAL"


def test_bear_put_spread():
    # Short low-strike put, long high-strike put (debit, bearish bias).
    c = classify([L(-1, "P", 235), L(1, "P", 240)])
    assert c.strategy == "Bear Put Spread"
    assert c.ticket_name == "VERTICAL"


# ---- 2-leg straddle / strangle ----------------------------------------


def test_long_straddle():
    c = classify([L(1, "C", 255), L(1, "P", 255)])
    assert c.strategy == "Long Straddle"
    assert c.ticket_name == "STRADDLE"


def test_short_straddle_is_naked():
    c = classify([L(-1, "C", 255), L(-1, "P", 255)])
    assert c.strategy == "Short Straddle"
    assert c.ticket_name == "STRADDLE"
    assert c.naked is True  # naked short call leg


def test_long_strangle():
    c = classify([L(1, "C", 265), L(1, "P", 245)])
    assert c.strategy == "Long Strangle"
    assert c.ticket_name == "STRANGLE"


def test_short_strangle_is_naked():
    c = classify([L(-1, "C", 265), L(-1, "P", 245)])
    assert c.strategy == "Short Strangle"
    assert c.ticket_name == "STRANGLE"
    assert c.naked is True


# ---- 3-leg butterflies -------------------------------------------------


def test_long_call_butterfly():
    c = classify([L(1, "C", 250), L(-2, "C", 255), L(1, "C", 260)])
    assert c.strategy == "Long Call Butterfly"
    assert c.ticket_name == "BUTTERFLY"
    assert c.naked is False


def test_short_call_butterfly():
    c = classify([L(-1, "C", 250), L(2, "C", 255), L(-1, "C", 260)])
    assert c.strategy == "Short Call Butterfly"
    assert c.ticket_name == "BUTTERFLY"


def test_long_put_butterfly():
    c = classify([L(1, "P", 250), L(-2, "P", 255), L(1, "P", 260)])
    assert c.strategy == "Long Put Butterfly"
    assert c.ticket_name == "BUTTERFLY"


def test_broken_wing_fly_is_custom():
    # Wings NOT equidistant (K2 - K1 = 5, K3 - K2 = 10).
    c = classify([L(1, "C", 250), L(-2, "C", 255), L(1, "C", 265)])
    assert "Broken" in c.strategy or "Custom" in c.strategy
    assert c.ticket_name == "CUSTOM"


# ---- 4-leg iron condor / butterfly ------------------------------------


def test_iron_condor_credit():
    # +P low, -P high-put, -C low-call, +C high.
    legs = [L(1, "P", 192.5), L(-1, "P", 197.5), L(-1, "C", 207.5), L(1, "C", 210)]
    c = classify(legs)
    assert c.strategy.startswith("Iron Condor")
    assert c.ticket_name == "IRON CONDOR"
    assert c.naked is False  # both shorts covered


def test_reverse_iron_condor():
    # Flip all signs.
    legs = [L(-1, "P", 192.5), L(1, "P", 197.5), L(1, "C", 207.5), L(-1, "C", 210)]
    c = classify(legs)
    assert c.strategy.startswith("Reverse Iron Condor") or c.strategy.startswith(
        "Iron Condor"
    )
    assert c.ticket_name == "IRON CONDOR"


def test_iron_butterfly():
    # Short legs share a strike (the body).
    legs = [L(1, "P", 250), L(-1, "P", 255), L(-1, "C", 255), L(1, "C", 260)]
    c = classify(legs)
    assert c.strategy.startswith("Iron Butterfly")
    assert c.ticket_name == "IRON BUTTERFLY"
    assert c.naked is False


# ---- irregular shapes fall back to CUSTOM -----------------------------


def test_ratio_spread_is_custom():
    # 2:1 call ratio backspread — not a named Schwab strategy.
    legs = [L(-1, "C", 255), L(2, "C", 260)]
    c = classify(legs)
    assert c.ticket_name == "CUSTOM"
    # Still supported (single expiry) — math still works.
    assert c.supported is True


def test_three_leg_random_is_custom():
    legs = [L(1, "C", 250), L(-1, "P", 245), L(1, "C", 260)]
    c = classify(legs)
    assert c.ticket_name == "CUSTOM"
    assert c.strategy.startswith("Custom")


# ---- multi-expiry detection -------------------------------------------


def test_multi_expiry_calendar_unsupported():
    legs = [L(-1, "C", 300, EXP), L(1, "C", 300, EXP2)]
    c = classify(legs)
    assert c.supported is False
    assert c.reason == "multi-expiry"
    # But ticket name is still something useful.
    assert c.ticket_name in ("CALENDAR", "CUSTOM")


def test_multi_expiry_diagonal_unsupported():
    legs = [L(-1, "C", 300, EXP), L(1, "C", 310, EXP2)]
    c = classify(legs)
    assert c.supported is False
    assert c.reason == "multi-expiry"
    assert c.ticket_name in ("DIAGONAL", "CUSTOM")


# ---- empty / degenerate -----------------------------------------------


def test_empty_legs_raises():
    with pytest.raises(ValueError, match="at least one leg"):
        classify([])


# ---- return type ------------------------------------------------------


def test_classification_is_frozen():
    c = classify([L(1, "C", 255)])
    assert isinstance(c, Classification)
    with pytest.raises(Exception):
        c.strategy = "X"  # type: ignore[misc]
