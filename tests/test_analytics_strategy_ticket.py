"""Tests for the Schwab order-ticket renderer.

Verifies the ticket string matches Schwab's observed copy-paste format
for named shapes and the CUSTOM fallback. Live paste-test in the
Schwab order entry UI is a separate pre-ship gate (see
``docs/plan/strategy.md``).
"""

from __future__ import annotations

from datetime import date

import pytest

from schwab_cli.analytics.strategy import PricedLeg
from schwab_cli.analytics.strategy_classify import Classification
from schwab_cli.analytics.strategy_ticket import render_ticket

EXP = date(2026, 5, 1)      # Friday. Not the third Friday of May (15th). Weeklys.
EXP_MONTHLY = date(2026, 5, 15)  # Third Friday of May 2026.
EXP2 = date(2026, 7, 1)     # Wednesday, multi-expiry test.


def PL(qty: int, side: str, strike: float, premium: float, exp: date = EXP) -> PricedLeg:
    return PricedLeg(qty=qty, side=side, expiry=exp, strike=strike, premium=premium)  # type: ignore[arg-type]


def CLS(
    strategy: str,
    ticket_name: str,
    *,
    supported: bool = True,
    reason: str | None = None,
    naked: bool = False,
) -> Classification:
    return Classification(
        strategy=strategy,
        ticket_name=ticket_name,
        supported=supported,
        reason=reason,
        naked=naked,
    )


# ---- 1-leg -------------------------------------------------------------


def test_ticket_long_call_single_leg():
    legs = [PL(1, "C", 255, 2.35)]
    c = CLS("Long Call", "")
    ticket = render_ticket(legs, c, symbol="AMZN")
    # +1 long call; premium 2.35 debit.
    assert ticket == "BUY +1 AMZN 100 (Weeklys) 1 MAY 26 255 CALL @2.35 LMT"


def test_ticket_short_put_monthly_no_weeklys_tag():
    legs = [PL(-1, "P", 240, 1.75, exp=EXP_MONTHLY)]
    c = CLS("Short Put", "")
    ticket = render_ticket(legs, c, symbol="AMZN")
    assert ticket == "SELL -1 AMZN 100 15 MAY 26 240 PUT @1.75 LMT"


def test_ticket_strike_integer_shows_no_decimals():
    legs = [PL(1, "C", 255, 2.35)]
    c = CLS("Long Call", "")
    ticket = render_ticket(legs, c, symbol="AMZN")
    assert "255 CALL" in ticket
    assert "255.0" not in ticket


def test_ticket_strike_half_dollar_keeps_decimal():
    legs = [PL(1, "C", 192.5, 1.15)]
    c = CLS("Long Call", "")
    ticket = render_ticket(legs, c, symbol="NVDA")
    assert "192.5 CALL" in ticket


# ---- VERTICAL ----------------------------------------------------------


def test_ticket_bull_call_spread_is_buy_debit():
    # +C255 @ 3, -C260 @ 1 → net debit 2.
    legs = [PL(1, "C", 255, 3.00), PL(-1, "C", 260, 1.00)]
    c = CLS("Bull Call Spread", "VERTICAL")
    t = render_ticket(legs, c, symbol="AMZN")
    assert t == "BUY +1 VERTICAL AMZN 100 (Weeklys) 1 MAY 26 260/255 CALL @2.00 LMT"


def test_ticket_bear_call_spread_is_sell_credit():
    # -C255 @ 3, +C260 @ 1 → net credit 2.
    legs = [PL(-1, "C", 255, 3.00), PL(1, "C", 260, 1.00)]
    c = CLS("Bear Call Spread", "VERTICAL")
    t = render_ticket(legs, c, symbol="AMZN")
    assert t == "SELL -1 VERTICAL AMZN 100 (Weeklys) 1 MAY 26 260/255 CALL @2.00 LMT"


def test_ticket_bull_put_spread_is_sell_credit():
    # +P235 @ 1, -P240 @ 3 → net credit 2.
    legs = [PL(1, "P", 235, 1.00), PL(-1, "P", 240, 3.00)]
    c = CLS("Bull Put Spread", "VERTICAL")
    t = render_ticket(legs, c, symbol="AMZN")
    assert t == "SELL -1 VERTICAL AMZN 100 (Weeklys) 1 MAY 26 240/235 PUT @2.00 LMT"


# ---- STRADDLE / STRANGLE ----------------------------------------------


def test_ticket_long_straddle_same_strike_single_strike_token():
    legs = [PL(1, "C", 255, 3.00), PL(1, "P", 255, 2.00)]
    c = CLS("Long Straddle", "STRADDLE")
    t = render_ticket(legs, c, symbol="AMZN")
    assert t == "BUY +1 STRADDLE AMZN 100 (Weeklys) 1 MAY 26 255 CALL/PUT @5.00 LMT"


def test_ticket_long_strangle_two_strikes_call_first():
    # Long strangle: +C 265 + P 245. Call strike first (per Schwab convention).
    legs = [PL(1, "C", 265, 1.50), PL(1, "P", 245, 1.25)]
    c = CLS("Long Strangle", "STRANGLE")
    t = render_ticket(legs, c, symbol="AMZN")
    assert t == "BUY +1 STRANGLE AMZN 100 (Weeklys) 1 MAY 26 265/245 CALL/PUT @2.75 LMT"


# ---- BUTTERFLY ---------------------------------------------------------


def test_ticket_long_call_butterfly_high_to_low_strikes():
    # +C250 @ 6, -2 C255 @ 3, +C260 @ 1.50 → debit 1.50.
    legs = [
        PL(1, "C", 250, 6.00),
        PL(-2, "C", 255, 3.00),
        PL(1, "C", 260, 1.50),
    ]
    c = CLS("Long Call Butterfly", "BUTTERFLY")
    t = render_ticket(legs, c, symbol="AMZN")
    assert t == "BUY +1 BUTTERFLY AMZN 100 (Weeklys) 1 MAY 26 260/255/250 CALL @1.50 LMT"


# ---- IRON CONDOR / IRON BUTTERFLY -------------------------------------


def test_ticket_iron_condor_descending_strikes_call_put_sides():
    # +P192.5, -P197.5, -C207.5, +C210; net credit.
    legs = [
        PL(1, "P", 192.5, 0.80),
        PL(-1, "P", 197.5, 1.40),
        PL(-1, "C", 207.5, 1.60),
        PL(1, "C", 210, 0.90),
    ]
    c = CLS("Iron Condor", "IRON CONDOR")
    t = render_ticket(legs, c, symbol="NVDA")
    assert t == (
        "SELL -1 IRON CONDOR NVDA 100 (Weeklys) 1 MAY 26 "
        "210/207.5/197.5/192.5 CALL/PUT @1.30 LMT"
    )


def test_ticket_iron_butterfly_three_strikes():
    # +P250, -P255, -C255, +C260; net credit (body strike shared).
    legs = [
        PL(1, "P", 250, 1.00),
        PL(-1, "P", 255, 3.50),
        PL(-1, "C", 255, 3.50),
        PL(1, "C", 260, 1.00),
    ]
    c = CLS("Iron Butterfly", "IRON BUTTERFLY")
    t = render_ticket(legs, c, symbol="AMZN")
    assert t == (
        "SELL -1 IRON BUTTERFLY AMZN 100 (Weeklys) 1 MAY 26 "
        "260/255/250 CALL/PUT @5.00 LMT"
    )


# ---- CUSTOM fallback --------------------------------------------------


def test_ticket_custom_with_ratios_and_per_leg_fields():
    # 2:1 call ratio backspread, unnamed → CUSTOM.
    # -1 C255 @ 3.00, +2 C260 @ 1.20 → net cost = 2*1.20 - 3.00 = -0.60 (credit).
    legs = [PL(-1, "C", 255, 3.00), PL(2, "C", 260, 1.20)]
    c = CLS("Custom 2-leg", "CUSTOM")
    t = render_ticket(legs, c, symbol="AMZN")
    # Descending strikes (260 @ qty 2 / 255 @ qty 1), matching side list.
    assert t == (
        "SELL -1 2/1 CUSTOM AMZN 100 (Weeklys) 1 MAY 26/1 MAY 26 "
        "260/255 CALL/CALL @0.60 LMT"
    )


def test_ticket_custom_multi_expiry_calendar_renders_slashed_dates():
    # Calendar: -C 300 near, +C 300 far. Multi-expiry, CUSTOM fallback is fine.
    legs = [
        PL(-1, "C", 300, 5.00, exp=EXP),
        PL(1, "C", 300, 8.00, exp=EXP2),
    ]
    c = CLS("Calendar Spread", "CALENDAR", supported=False, reason="multi-expiry")
    t = render_ticket(legs, c, symbol="AMZN")
    # The net is a debit: paid 8 received 5 → debit 3.
    assert "BUY +1 CALENDAR AMZN 100" in t
    assert "1 MAY 26/1 JUL 26" in t
    assert "300 CALL" in t
    assert "@3.00 LMT" in t


# ---- Weeklys / monthly detection --------------------------------------


def test_ticket_monthly_expiry_omits_weeklys_tag():
    # May 15, 2026 is the third Friday → standard monthly.
    legs = [PL(1, "C", 255, 2.00, exp=EXP_MONTHLY)]
    c = CLS("Long Call", "")
    t = render_ticket(legs, c, symbol="AMZN")
    assert "(Weeklys)" not in t
    assert "15 MAY 26" in t


def test_ticket_weekly_expiry_has_weeklys_tag():
    legs = [PL(1, "C", 255, 2.00, exp=EXP)]  # May 1 = Friday but not third Friday.
    c = CLS("Long Call", "")
    t = render_ticket(legs, c, symbol="AMZN")
    assert "(Weeklys)" in t


# ---- defensive ---------------------------------------------------------


def test_ticket_rejects_empty_legs():
    with pytest.raises(ValueError, match="at least one leg"):
        render_ticket([], CLS("", ""), symbol="X")
