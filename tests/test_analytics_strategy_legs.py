"""Tests for the option-leg parser used by ``strategy``.

Grammar: ``±N@YYYYMMDD{C|P}STRIKE``
    -1@20260501P270.5    -> sell 1 put, expiry 2026-05-01, strike 270.5
    +2@20260501C255      -> buy 2 calls, expiry 2026-05-01, strike 255
    1@20260701P300       -> buy 1 put (unsigned = buy)
"""

from __future__ import annotations

from datetime import date

import pytest

from schwab_cli.analytics.strategy_legs import Leg, LegParseError, parse_leg


# ---- happy path --------------------------------------------------------


def test_parse_leg_buy_explicit_sign():
    leg = parse_leg("+1@20260501C255")
    assert leg == Leg(qty=1, side="C", expiry=date(2026, 5, 1), strike=255.0)


def test_parse_leg_sell_explicit_sign():
    leg = parse_leg("-1@20260501P240")
    assert leg == Leg(qty=-1, side="P", expiry=date(2026, 5, 1), strike=240.0)


def test_parse_leg_unsigned_quantity_is_buy():
    leg = parse_leg("1@20260501C255")
    assert leg.qty == 1


def test_parse_leg_ratio_quantity():
    leg = parse_leg("-2@20260501C265")
    assert leg.qty == -2


def test_parse_leg_fractional_strike():
    leg = parse_leg("+1@20260501P192.5")
    assert leg.strike == 192.5


def test_parse_leg_fractional_strike_multiple_decimals():
    leg = parse_leg("+1@20260501C1234.50")
    assert leg.strike == 1234.5


def test_parse_leg_leading_zero_stripped_in_qty():
    leg = parse_leg("-01@20260501C255")
    assert leg.qty == -1


# ---- grammar errors ----------------------------------------------------


def test_parse_leg_missing_at_separator():
    with pytest.raises(LegParseError, match="expected.*@"):
        parse_leg("+120260501C255")


def test_parse_leg_missing_side():
    with pytest.raises(LegParseError, match="side"):
        parse_leg("+1@20260501X255")


def test_parse_leg_missing_strike():
    with pytest.raises(LegParseError, match="strike"):
        parse_leg("+1@20260501C")


def test_parse_leg_missing_qty():
    with pytest.raises(LegParseError, match="quantity"):
        parse_leg("@20260501C255")


def test_parse_leg_zero_qty_rejected():
    with pytest.raises(LegParseError, match="zero"):
        parse_leg("0@20260501C255")


def test_parse_leg_non_integer_qty_rejected():
    with pytest.raises(LegParseError, match="quantity"):
        parse_leg("1.5@20260501C255")


def test_parse_leg_empty_input():
    with pytest.raises(LegParseError):
        parse_leg("")


def test_parse_leg_whitespace_only():
    with pytest.raises(LegParseError):
        parse_leg("   ")


# ---- date errors -------------------------------------------------------


def test_parse_leg_invalid_calendar_date():
    with pytest.raises(LegParseError, match="date"):
        parse_leg("+1@20260231C255")  # Feb 31 doesn't exist


def test_parse_leg_date_too_short():
    with pytest.raises(LegParseError, match="date"):
        parse_leg("+1@2026501C255")


def test_parse_leg_date_non_numeric():
    with pytest.raises(LegParseError, match="date"):
        parse_leg("+1@2026MAY01C255")


# ---- strike errors -----------------------------------------------------


def test_parse_leg_negative_strike_rejected():
    with pytest.raises(LegParseError, match="strike"):
        parse_leg("+1@20260501C-255")


def test_parse_leg_zero_strike_rejected():
    with pytest.raises(LegParseError, match="strike"):
        parse_leg("+1@20260501C0")


def test_parse_leg_non_numeric_strike():
    with pytest.raises(LegParseError, match="strike"):
        parse_leg("+1@20260501Cabc")


# ---- casing / whitespace tolerance ------------------------------------


def test_parse_leg_accepts_lowercase_side():
    leg = parse_leg("+1@20260501c255")
    assert leg.side == "C"


def test_parse_leg_strips_surrounding_whitespace():
    leg = parse_leg("  +1@20260501C255  ")
    assert leg.qty == 1
    assert leg.strike == 255.0


# ---- Leg dataclass -----------------------------------------------------


def test_leg_is_frozen():
    leg = Leg(qty=1, side="C", expiry=date(2026, 5, 1), strike=255.0)
    with pytest.raises(Exception):  # FrozenInstanceError subclass of AttributeError
        leg.qty = 2  # type: ignore[misc]


def test_leg_equality():
    a = Leg(qty=1, side="C", expiry=date(2026, 5, 1), strike=255.0)
    b = Leg(qty=1, side="C", expiry=date(2026, 5, 1), strike=255.0)
    assert a == b


def test_leg_is_short_and_is_long_helpers():
    short = Leg(qty=-1, side="P", expiry=date(2026, 5, 1), strike=240.0)
    long_ = Leg(qty=1, side="C", expiry=date(2026, 5, 1), strike=260.0)
    assert short.is_short and not short.is_long
    assert long_.is_long and not long_.is_short


# ---- open/close suffix --------------------------------------------------


def test_parse_leg_default_effect_is_open():
    leg = parse_leg("+1@20260501C255")
    assert leg.effect == "o"
    assert leg.instruction == "BUY_TO_OPEN"


def test_parse_leg_open_suffix_explicit():
    leg = parse_leg("+1@20260501C255o")
    assert leg.effect == "o"
    assert leg.instruction == "BUY_TO_OPEN"


def test_parse_leg_close_suffix_long_call():
    leg = parse_leg("+1@20260117C250c")
    assert leg.effect == "c"
    assert leg.instruction == "BUY_TO_CLOSE"


def test_parse_leg_close_suffix_short_put():
    leg = parse_leg("-1@20260501P270.5c")
    assert leg.effect == "c"
    assert leg.instruction == "SELL_TO_CLOSE"
    assert leg.strike == 270.5


def test_parse_leg_close_suffix_uppercase():
    leg = parse_leg("-2@20260501P240C")
    assert leg.effect == "c"
    assert leg.instruction == "SELL_TO_CLOSE"


def test_parse_leg_invalid_effect_letter_rejected():
    with pytest.raises(LegParseError, match="suffix"):
        parse_leg("+1@20260501C255x")


def test_parse_leg_instruction_all_four():
    long_open = parse_leg("+1@20260501C250o")
    long_close = parse_leg("+1@20260501C250c")
    short_open = parse_leg("-1@20260501C250o")
    short_close = parse_leg("-1@20260501C250c")
    assert long_open.instruction == "BUY_TO_OPEN"
    assert long_close.instruction == "BUY_TO_CLOSE"
    assert short_open.instruction == "SELL_TO_OPEN"
    assert short_close.instruction == "SELL_TO_CLOSE"
