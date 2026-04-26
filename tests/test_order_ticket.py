"""Tests for the Schwab/TOS-style ticket parser and OSI helper.

All tests are pure-function; no network, no mocks needed. The parser
never touches Schwab — these are just string → dataclass.
"""

from __future__ import annotations

from datetime import date

import pytest

from schwab_cli.order_ticket import (
    ParsedLeg,
    ParsedTicket,
    TicketParseError,
    parse_ticket,
    to_osi,
)


# ---- single-leg option ----------------------------------------------------


def test_single_leg_buy_call():
    t = parse_ticket("BUY +2 AAPL 100 17 JAN 26 250 CALL @1.20 LMT")
    assert t.side == "BUY"
    assert t.quantity == 2
    assert t.underlying == "AAPL"
    assert t.expiry == date(2026, 1, 17)
    assert t.option_type == "CALL"
    assert t.strikes == (250.0,)
    assert t.order_type == "LIMIT"
    assert t.duration == "DAY"
    assert t.price == 1.20
    assert t.strategy is None
    assert len(t.legs) == 1
    leg = t.legs[0]
    assert leg.instruction == "BUY_TO_OPEN"
    assert leg.quantity == 2
    assert leg.strike == 250.0


def test_single_leg_sell_put_with_gtc():
    t = parse_ticket("SELL -1 NVDA 100 17 JAN 26 200 PUT @0.85 LMT GTC")
    assert t.side == "SELL"
    assert t.duration == "GOOD_TILL_CANCEL"
    assert t.legs[0].instruction == "SELL_TO_OPEN"
    assert t.legs[0].option_type == "PUT"


def test_single_leg_market_order():
    t = parse_ticket("BUY +1 SPY 100 1 MAY 26 500 CALL MKT")
    assert t.order_type == "MARKET"
    assert t.price is None


# ---- VERTICAL spreads -----------------------------------------------------


def test_vertical_call_debit():
    """BUY VERTICAL CALL: BTO lower, STO higher (bull call debit)."""
    t = parse_ticket(
        "BUY +1 VERTICAL AMZN 100 (Weeklys) 1 MAY 26 262.5/267.5 CALL @2.35 LMT"
    )
    assert t.strategy == "VERTICAL"
    assert t.side == "BUY"
    assert t.option_type == "CALL"
    assert t.strikes == (262.5, 267.5)
    assert t.order_type == "NET_DEBIT"
    assert t.price == 2.35
    assert t.duration == "DAY"
    assert len(t.legs) == 2

    lower, higher = t.legs
    assert lower.strike == 262.5
    assert lower.instruction == "BUY_TO_OPEN"
    assert higher.strike == 267.5
    assert higher.instruction == "SELL_TO_OPEN"


def test_vertical_call_credit():
    """SELL VERTICAL CALL: STO lower, BTO higher (bear call credit)."""
    t = parse_ticket("SELL -1 VERTICAL NVDA 1 MAY 26 250/260 CALL @1.40 LMT")
    assert t.order_type == "NET_CREDIT"
    lower, higher = t.legs
    assert lower.strike == 250.0
    assert lower.instruction == "SELL_TO_OPEN"
    assert higher.strike == 260.0
    assert higher.instruction == "BUY_TO_OPEN"


def test_vertical_put_debit():
    """BUY VERTICAL PUT: STO lower, BTO higher (bear put debit)."""
    t = parse_ticket("BUY +1 VERTICAL AAPL 1 MAY 26 240/250 PUT @2.10 LMT")
    assert t.order_type == "NET_DEBIT"
    lower, higher = t.legs
    assert lower.strike == 240.0
    assert lower.instruction == "SELL_TO_OPEN"
    assert higher.strike == 250.0
    assert higher.instruction == "BUY_TO_OPEN"


def test_vertical_put_credit():
    """SELL VERTICAL PUT: BTO lower, STO higher (bull put credit)."""
    t = parse_ticket("SELL -1 VERTICAL TSLA 1 MAY 26 240/250 PUT @1.50 LMT")
    assert t.order_type == "NET_CREDIT"
    lower, higher = t.legs
    assert lower.strike == 240.0
    assert lower.instruction == "BUY_TO_OPEN"
    assert higher.strike == 250.0
    assert higher.instruction == "SELL_TO_OPEN"


def test_vertical_decorations_ignored():
    a = parse_ticket("BUY +1 VERTICAL AMZN 100 (Weeklys) 1 MAY 26 100/110 CALL @0.50 LMT")
    b = parse_ticket("BUY +1 VERTICAL AMZN 1 MAY 26 100/110 CALL @0.50 LMT")
    assert a.legs == b.legs
    assert a.price == b.price


# ---- equity orders --------------------------------------------------------


def test_equity_limit_buy():
    t = parse_ticket("BUY +100 NVDA @150.00 LMT DAY")
    assert t.is_equity
    assert t.side == "BUY"
    assert t.quantity == 100
    assert t.underlying == "NVDA"
    assert t.order_type == "LIMIT"
    assert t.price == 150.0
    assert t.duration == "DAY"
    assert t.legs == ()
    assert t.expiry is None


def test_equity_market_sell():
    t = parse_ticket("SELL -50 TSLA MKT")
    assert t.is_equity
    assert t.order_type == "MARKET"
    assert t.price is None
    assert t.quantity == 50


def test_equity_implicit_day():
    t = parse_ticket("BUY +10 NVDA @100 LMT")
    assert t.duration == "DAY"


# ---- diagnostics ----------------------------------------------------------


def test_empty_ticket_rejected():
    with pytest.raises(TicketParseError, match="empty"):
        parse_ticket("")


def test_missing_side_rejected():
    with pytest.raises(TicketParseError, match="BUY or SELL"):
        parse_ticket("HOLD +1 AAPL 1 MAY 26 250 CALL @1 LMT")


def test_missing_quantity_rejected():
    with pytest.raises(TicketParseError, match="quantity"):
        parse_ticket("BUY AAPL 1 MAY 26 250 CALL @1 LMT")


def test_zero_quantity_rejected():
    with pytest.raises(TicketParseError, match="zero"):
        parse_ticket("BUY +0 NVDA @100 LMT")


def test_lmt_without_price_rejected():
    with pytest.raises(TicketParseError, match="LMT requires"):
        parse_ticket("BUY +1 AAPL 1 MAY 26 250 CALL LMT")


def test_mkt_with_price_rejected():
    with pytest.raises(TicketParseError, match="MKT must not"):
        parse_ticket("BUY +1 AAPL 1 MAY 26 250 CALL @1 MKT")


def test_vertical_requires_two_strikes():
    with pytest.raises(TicketParseError, match="multiple strikes"):
        parse_ticket("BUY +1 VERTICAL AMZN 1 MAY 26 250 CALL @2 LMT")


def test_vertical_two_strikes_required_format():
    with pytest.raises(TicketParseError, match="VERTICAL requires"):
        parse_ticket(
            "BUY +1 VERTICAL AMZN 1 MAY 26 250/260/270 CALL @2 LMT"
        )


def test_vertical_equal_strikes_rejected():
    with pytest.raises(TicketParseError, match="must differ"):
        parse_ticket("BUY +1 VERTICAL AMZN 1 MAY 26 250/250 CALL @2 LMT")


def test_phase2_strategy_rejected_with_phase2_kind():
    with pytest.raises(TicketParseError) as exc:
        parse_ticket(
            "BUY +1 BUTTERFLY AMZN 1 MAY 26 250/260/270 CALL @2 LMT"
        )
    assert exc.value.kind == "phase2"
    assert "Phase 1" in str(exc.value) or "Phase 2" in str(exc.value)


def test_invalid_month_falls_through_to_equity_error():
    # An unknown 3-letter token after a 1-digit "day" makes the parser
    # think this is an equity ticket (no expiry recognised), so the
    # downstream "1" gets read as the order-type token and fails. The
    # user typically catches the mistake from this error and re-types.
    with pytest.raises(TicketParseError, match="LMT or MKT"):
        parse_ticket("BUY +1 NVDA 1 ABC 26 250 CALL @1 LMT")


def test_unsupported_order_type_rejected():
    with pytest.raises(TicketParseError, match="LMT or MKT"):
        parse_ticket("BUY +1 NVDA @1 STP")


def test_unexpected_trailing_rejected():
    with pytest.raises(TicketParseError, match="trailing"):
        parse_ticket("BUY +100 NVDA @150 LMT DAY EXTRA")


def test_strategy_keyword_with_single_strike_rejected():
    with pytest.raises(TicketParseError, match="multiple strikes"):
        parse_ticket("BUY +1 VERTICAL AMZN 1 MAY 26 250 CALL @2 LMT")


# ---- OSI symbol -----------------------------------------------------------


def test_osi_call_basic():
    assert to_osi("NVDA", date(2026, 1, 17), "CALL", 250) == "NVDA  260117C00250000"


def test_osi_put_fractional_strike():
    assert to_osi("AMZN", date(2026, 5, 1), "PUT", 262.5) == "AMZN  260501P00262500"


def test_osi_one_char_ticker():
    assert to_osi("F", date(2026, 5, 1), "CALL", 12) == "F     260501C00012000"


def test_osi_six_char_ticker():
    # GOOGL has 5 chars; MSFT etc. fit in 4. Six-char tickers are rare but valid.
    assert to_osi("ABCDEF", date(2026, 5, 1), "CALL", 100) == "ABCDEF260501C00100000"


def test_osi_lowercase_input_normalised():
    assert to_osi("nvda", date(2026, 1, 17), "CALL", 250) == "NVDA  260117C00250000"


def test_osi_high_strike_index():
    # SPX-style 5000 strike
    assert to_osi("SPX", date(2026, 1, 17), "CALL", 5000) == "SPX   260117C05000000"


def test_osi_seven_char_ticker_rejected():
    with pytest.raises(ValueError, match="1-6 chars"):
        to_osi("ABCDEFG", date(2026, 5, 1), "CALL", 100)


def test_osi_zero_strike_rejected():
    with pytest.raises(ValueError, match="positive"):
        to_osi("NVDA", date(2026, 5, 1), "CALL", 0)


def test_osi_strike_overflow_rejected():
    with pytest.raises(ValueError, match="too large"):
        to_osi("SPX", date(2026, 5, 1), "CALL", 100_001)


def test_osi_strike_rounds_cents():
    # 262.500001 should round to the same OSI as 262.50.
    a = to_osi("AMZN", date(2026, 5, 1), "PUT", 262.500001)
    b = to_osi("AMZN", date(2026, 5, 1), "PUT", 262.5)
    assert a == b


# ---- optional [TO OPEN] / [TO CLOSE] / [AUTO] marker --------------------


def test_to_close_marker_rewrites_instruction_to_close():
    t = parse_ticket(
        "BUY +1 AMZN 100 15 JAN 27 190 PUT @5.70 LMT [TO CLOSE]"
    )
    assert t.legs[0].instruction == "BUY_TO_CLOSE"


def test_to_open_marker_keeps_default_open():
    """[TO OPEN] is informational — the default already produces *_TO_OPEN
    so this just round-trips the instruction unchanged."""
    t = parse_ticket(
        "BUY +1 AMZN 100 15 JAN 27 190 PUT @5.70 LMT [TO OPEN]"
    )
    assert t.legs[0].instruction == "BUY_TO_OPEN"


def test_auto_marker_keeps_default_open():
    """[AUTO] is the explicit "let the pipeline decide" marker — same
    effect as omitting the bracket entirely."""
    t = parse_ticket(
        "SELL -1 AMZN 100 15 JAN 27 190 PUT @5.70 LMT [AUTO]"
    )
    assert t.legs[0].instruction == "SELL_TO_OPEN"


def test_marker_is_case_insensitive():
    t = parse_ticket(
        "BUY +1 AMZN 100 15 JAN 27 190 PUT @5.70 LMT [to close]"
    )
    assert t.legs[0].instruction == "BUY_TO_CLOSE"


def test_to_close_marker_applies_to_every_leg_of_a_vertical():
    t = parse_ticket(
        "BUY +1 VERTICAL AMZN 100 1 MAY 26 260/255 CALL @0.85 LMT [TO CLOSE]"
    )
    instructions = {leg.instruction for leg in t.legs}
    assert instructions == {"BUY_TO_CLOSE", "SELL_TO_CLOSE"}


def test_marker_with_extra_inner_whitespace_accepted():
    t = parse_ticket(
        "BUY +1 AMZN 100 15 JAN 27 190 PUT @5.70 LMT [  TO   CLOSE  ]"
    )
    assert t.legs[0].instruction == "BUY_TO_CLOSE"


def test_no_marker_is_default_open():
    """Sanity check: no bracket → unchanged behavior."""
    t = parse_ticket(
        "BUY +1 AMZN 100 15 JAN 27 190 PUT @5.70 LMT"
    )
    assert t.legs[0].instruction == "BUY_TO_OPEN"


def test_two_markers_rejected_as_ambiguous():
    with pytest.raises(TicketParseError, match="multiple position-effect"):
        parse_ticket(
            "BUY +1 AMZN 100 15 JAN 27 190 PUT @5.70 LMT [TO CLOSE] [AUTO]"
        )


def test_per_leg_marker_applies_in_body_order():
    """``[TO CLOSE/AUTO]`` on a 2-leg vertical: leg[0] closes, leg[1]
    stays at default OPEN. Body leg order matches input strike order
    (descending-input is the common case)."""
    t = parse_ticket(
        "BUY +1 VERTICAL AMZN 100 1 MAY 26 260/255 CALL @0.85 LMT "
        "[TO CLOSE/AUTO]"
    )
    assert t.legs[0].strike == 260.0
    assert t.legs[0].instruction == "BUY_TO_CLOSE"
    assert t.legs[1].strike == 255.0
    assert t.legs[1].instruction == "SELL_TO_OPEN"


def test_per_leg_marker_count_must_match_legs():
    with pytest.raises(TicketParseError, match="must match number of option legs"):
        parse_ticket(
            "BUY +1 VERTICAL AMZN 100 1 MAY 26 260/255 CALL @0.85 LMT "
            "[TO CLOSE/AUTO/TO OPEN]"
        )


def test_per_leg_marker_with_all_open_keeps_default():
    t = parse_ticket(
        "BUY +1 VERTICAL AMZN 100 1 MAY 26 260/255 CALL @0.85 LMT "
        "[TO OPEN/TO OPEN]"
    )
    assert all(l.instruction.endswith("_TO_OPEN") for l in t.legs)


def test_marker_on_equity_is_ignored():
    """Marker has no leg-level effect on equity (no open/close concept).
    Parser just accepts and drops it so a copy/pasted TOS string with a
    stray marker doesn't error out."""
    t = parse_ticket("BUY +100 NVDA @200 LMT [TO CLOSE]")
    assert t.order_type == "LIMIT"
    assert t.quantity == 100
    assert t.legs == ()  # equity tickets carry no option legs
