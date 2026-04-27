"""Phase 3 — extended TOS ticket parser strategies.

Covers STRADDLE, STRANGLE, BUTTERFLY, CONDOR, and IRON CONDOR — every
same-expiry multi-leg strategy from spec §Phase 3. Each test exercises
the full spec-build path: parse the ticket → assert legs + order_type
+ price + body NET_DEBIT/NET_CREDIT inference.

CALENDAR / DIAGONAL split-expiry and COVERED / CUSTOM are handled in
follow-up commits.
"""
from __future__ import annotations

from datetime import date

import pytest

from schwab_cli.order_ticket import (
    parse_ticket,
    TicketParseError,
)


# ---- STRADDLE ----------------------------------------------------------


def test_straddle_buy_creates_call_plus_put_at_same_strike():
    t = parse_ticket(
        "BUY +1 STRADDLE AMZN 1 MAY 26 200 @5.50 LMT"
    )
    assert t.strategy == "STRADDLE"
    assert t.order_type == "NET_DEBIT"
    assert t.price == 5.50
    assert len(t.legs) == 2
    sides = {(l.option_type, l.strike) for l in t.legs}
    assert sides == {("CALL", 200.0), ("PUT", 200.0)}
    # BUY → both legs are BTO.
    assert all(l.instruction == "BUY_TO_OPEN" for l in t.legs)


def test_straddle_sell_creates_two_short_legs():
    t = parse_ticket(
        "SELL -1 STRADDLE AMZN 1 MAY 26 200 @4.10 LMT"
    )
    assert t.order_type == "NET_CREDIT"
    assert all(l.instruction == "SELL_TO_OPEN" for l in t.legs)


# ---- STRANGLE ----------------------------------------------------------


def test_strangle_lower_strike_is_put_higher_is_call():
    t = parse_ticket(
        "BUY +1 STRANGLE AMZN 1 MAY 26 195/205 @3.20 LMT"
    )
    assert t.strategy == "STRANGLE"
    assert t.order_type == "NET_DEBIT"
    by_type = {l.option_type: l for l in t.legs}
    assert by_type["PUT"].strike == 195.0
    assert by_type["CALL"].strike == 205.0
    assert all(l.instruction == "BUY_TO_OPEN" for l in t.legs)


def test_strangle_rejects_equal_strikes():
    with pytest.raises(TicketParseError, match="distinct strikes"):
        parse_ticket("BUY +1 STRANGLE AMZN 1 MAY 26 200/200 @3 LMT")


# ---- BUTTERFLY ---------------------------------------------------------


def test_butterfly_call_buy_emits_1_2_1_ratio():
    t = parse_ticket(
        "BUY +1 BUTTERFLY AMZN 1 MAY 26 195/200/205 CALL @0.85 LMT"
    )
    assert t.strategy == "BUTTERFLY"
    assert t.order_type == "NET_DEBIT"
    assert len(t.legs) == 3
    # Wing legs are quantity=1 BTO, body leg is quantity=2 STO.
    legs_by_strike = {l.strike: l for l in t.legs}
    assert legs_by_strike[195.0].instruction == "BUY_TO_OPEN"
    assert legs_by_strike[195.0].quantity == 1
    assert legs_by_strike[200.0].instruction == "SELL_TO_OPEN"
    assert legs_by_strike[200.0].quantity == 2
    assert legs_by_strike[205.0].instruction == "BUY_TO_OPEN"
    assert legs_by_strike[205.0].quantity == 1


def test_butterfly_rejects_non_equidistant_strikes():
    with pytest.raises(TicketParseError, match="equidistant"):
        parse_ticket(
            "BUY +1 BUTTERFLY AMZN 1 MAY 26 195/200/210 CALL @0.85 LMT"
        )


def test_butterfly_sell_inverts_instructions():
    t = parse_ticket(
        "SELL -1 BUTTERFLY AMZN 1 MAY 26 195/200/205 PUT @0.50 LMT"
    )
    assert t.order_type == "NET_CREDIT"
    legs_by_strike = {l.strike: l for l in t.legs}
    assert legs_by_strike[195.0].instruction == "SELL_TO_OPEN"
    assert legs_by_strike[200.0].instruction == "BUY_TO_OPEN"
    assert legs_by_strike[200.0].quantity == 2
    assert legs_by_strike[205.0].instruction == "SELL_TO_OPEN"


# ---- CONDOR ------------------------------------------------------------


def test_condor_call_buy_long_outer_short_inner():
    t = parse_ticket(
        "BUY +1 CONDOR AMZN 1 MAY 26 190/195/205/210 CALL @0.50 LMT"
    )
    assert t.strategy == "CONDOR"
    assert t.order_type == "NET_DEBIT"
    legs_by_strike = {l.strike: l for l in t.legs}
    # Long outer.
    assert legs_by_strike[190.0].instruction == "BUY_TO_OPEN"
    assert legs_by_strike[210.0].instruction == "BUY_TO_OPEN"
    # Short inner.
    assert legs_by_strike[195.0].instruction == "SELL_TO_OPEN"
    assert legs_by_strike[205.0].instruction == "SELL_TO_OPEN"


# ---- IRON CONDOR -------------------------------------------------------


def test_iron_condor_buy_emits_credit_with_mixed_p_c_legs():
    """TOS convention: 'BUY +1 IRON CONDOR' establishes the short-vol
    position which collects net credit on the inner short legs.
    Schwab body must say NET_CREDIT for that case."""
    t = parse_ticket(
        "BUY +1 IRON CONDOR AMZN 1 MAY 26 190/195/205/210 @1.20 LMT"
    )
    assert t.strategy == "IRON_CONDOR"
    assert t.order_type == "NET_CREDIT"          # see docstring
    assert len(t.legs) == 4
    by_strike = {l.strike: l for l in t.legs}
    # Lower pair are PUTs.
    assert by_strike[190.0].option_type == "PUT"
    assert by_strike[195.0].option_type == "PUT"
    # Upper pair are CALLs.
    assert by_strike[205.0].option_type == "CALL"
    assert by_strike[210.0].option_type == "CALL"
    # Long outer / short inner regardless of P or C.
    assert by_strike[190.0].instruction == "BUY_TO_OPEN"
    assert by_strike[210.0].instruction == "BUY_TO_OPEN"
    assert by_strike[195.0].instruction == "SELL_TO_OPEN"
    assert by_strike[205.0].instruction == "SELL_TO_OPEN"


def test_iron_condor_sell_inverts_to_net_debit():
    t = parse_ticket(
        "SELL -1 IRON CONDOR AMZN 1 MAY 26 190/195/205/210 @0.80 LMT"
    )
    assert t.order_type == "NET_DEBIT"
    by_strike = {l.strike: l for l in t.legs}
    assert by_strike[195.0].instruction == "BUY_TO_OPEN"
    assert by_strike[205.0].instruction == "BUY_TO_OPEN"


def test_iron_condor_requires_four_strikes():
    with pytest.raises(TicketParseError, match="IRON_CONDOR requires 4"):
        parse_ticket(
            "BUY +1 IRON CONDOR AMZN 1 MAY 26 190/195/210 @1.20 LMT"
        )


# ---- error paths -------------------------------------------------------


def test_back_ratio_still_unimplemented():
    """Strategies the parser doesn't yet handle return phase2-kind
    errors so the CLI can suggest --leg as the workaround."""
    with pytest.raises(TicketParseError) as exc:
        parse_ticket(
            "BUY +1 BACK_RATIO AMZN 1 MAY 26 250/260 CALL @2 LMT"
        )
    assert exc.value.kind == "phase2"


def test_unknown_strategy_token_still_parses_as_underlying():
    """A token that isn't a known strategy keyword falls through to
    being treated as the underlying — error surfaces later when the
    expiry parse fails. Existing behaviour preserved."""
    with pytest.raises(TicketParseError):
        parse_ticket("BUY +1 GIBBERISH NVDA 1 MAY 26 250 CALL @2 LMT")
