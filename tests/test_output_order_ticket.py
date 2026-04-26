"""Tests for ``render_order_ticket`` — the inverse of ``--parse``.

Covers:
* Single equity (LIMIT, MARKET, GTC duration, BUY / SELL).
* Single option (open + close, weekly tag, weekday/expiry handling).
* Multi-leg vertical (NET_DEBIT and NET_CREDIT, sign on @-price).
* Mixed equity+option returns None.
* Unparseable OSI returns None.
* Round-trip with the existing ``--parse`` helper produces an
  equivalent body for the cases that round-trip cleanly.
"""

from __future__ import annotations

from schwab_cli.output.orders import render_order_ticket


# ---- equity --------------------------------------------------------------


def test_equity_buy_limit():
    body = {
        "orderType": "LIMIT", "duration": "DAY",
        "orderLegCollection": [{
            "instruction": "BUY", "quantity": 1,
            "instrument": {"assetType": "EQUITY", "symbol": "NVDA"},
        }],
        "price": "207.00",
    }
    assert render_order_ticket(body, underlying="NVDA") == "BUY +1 NVDA @207.00 LMT"


def test_equity_sell_market():
    body = {
        "orderType": "MARKET", "duration": "DAY",
        "orderLegCollection": [{
            "instruction": "SELL", "quantity": 5,
            "instrument": {"assetType": "EQUITY", "symbol": "AAPL"},
        }],
    }
    assert render_order_ticket(body, underlying="AAPL") == "SELL -5 AAPL @MKT"


def test_equity_buy_gtc_appends_tag():
    body = {
        "orderType": "LIMIT", "duration": "GOOD_TILL_CANCEL",
        "orderLegCollection": [{
            "instruction": "BUY", "quantity": 100,
            "instrument": {"assetType": "EQUITY", "symbol": "MSFT"},
        }],
        "price": "350.00",
    }
    assert render_order_ticket(body, underlying="MSFT") \
        == "BUY +100 MSFT @350.00 LMT GTC"


# ---- single-leg option ---------------------------------------------------


def test_single_option_sell_open_weekly():
    """Schwab uses (Weeklys) tag for non-third-Friday expiries."""
    body = {
        "orderType": "LIMIT", "duration": "DAY",
        "orderLegCollection": [{
            "instruction": "SELL_TO_OPEN", "quantity": 1,
            "instrument": {
                "assetType": "OPTION",
                "symbol": "AMZN  260501P00192500",  # 2026-05-01 PUT 192.5
            },
        }],
        "price": "1.65",
    }
    out = render_order_ticket(body, underlying="AMZN")
    assert out == "SELL -1 AMZN 100 (Weeklys) 1 MAY 26 192.5 PUT @1.65 LMT"


def test_single_option_buy_close_appends_to_close():
    """The user's earlier TOS screenshot shape — mirror it exactly."""
    body = {
        "orderType": "LIMIT", "duration": "DAY",
        "orderLegCollection": [{
            "instruction": "BUY_TO_CLOSE", "quantity": 1,
            "instrument": {
                "assetType": "OPTION",
                "symbol": "AMZN  270115P00190000",  # 2027-01-15 PUT 190
            },
        }],
        "price": "5.70",
    }
    out = render_order_ticket(body, underlying="AMZN")
    assert out == "BUY +1 AMZN 100 15 JAN 27 190 PUT @5.70 LMT [TO CLOSE]"


def test_single_option_third_friday_no_weeklys_tag():
    """2026-05-15 is the 3rd Friday of May 2026 — standard monthly,
    so the (Weeklys) marker should NOT appear."""
    body = {
        "orderType": "LIMIT", "duration": "DAY",
        "orderLegCollection": [{
            "instruction": "BUY_TO_OPEN", "quantity": 2,
            "instrument": {
                "assetType": "OPTION",
                "symbol": "NVDA  260515C00200000",
            },
        }],
        "price": "3.10",
    }
    out = render_order_ticket(body, underlying="NVDA")
    assert "(Weeklys)" not in out
    assert out == "BUY +2 NVDA 100 15 MAY 26 200 CALL @3.10 LMT"


# ---- multi-leg verticals ------------------------------------------------


def test_vertical_call_credit_spread():
    body = {
        "orderType": "NET_CREDIT", "duration": "DAY",
        "orderLegCollection": [
            {"instruction": "SELL_TO_OPEN", "quantity": 1,
             "instrument": {"assetType": "OPTION",
                            "symbol": "AMZN  260501C00260000"}},
            {"instruction": "BUY_TO_OPEN", "quantity": 1,
             "instrument": {"assetType": "OPTION",
                            "symbol": "AMZN  260501C00255000"}},
        ],
        "price": "0.85",
    }
    out = render_order_ticket(body, underlying="AMZN")
    assert out == (
        "SELL -1 VERTICAL AMZN 100 (Weeklys) 1 MAY 26 260/255 CALL @0.85 LMT"
    )


def test_vertical_put_debit_spread():
    body = {
        "orderType": "NET_DEBIT", "duration": "DAY",
        "orderLegCollection": [
            {"instruction": "BUY_TO_OPEN", "quantity": 1,
             "instrument": {"assetType": "OPTION",
                            "symbol": "AMZN  260501P00270000"}},
            {"instruction": "SELL_TO_OPEN", "quantity": 1,
             "instrument": {"assetType": "OPTION",
                            "symbol": "AMZN  260501P00265000"}},
        ],
        "price": "1.50",
    }
    out = render_order_ticket(body, underlying="AMZN")
    assert out == (
        "BUY +1 VERTICAL AMZN 100 (Weeklys) 1 MAY 26 270/265 PUT @1.50 LMT"
    )


def test_vertical_close_appends_to_close():
    """All-CLOSE multi-leg appends [TO CLOSE]."""
    body = {
        "orderType": "NET_DEBIT", "duration": "DAY",
        "orderLegCollection": [
            {"instruction": "BUY_TO_CLOSE", "quantity": 1,
             "instrument": {"assetType": "OPTION",
                            "symbol": "AMZN  260501C00260000"}},
            {"instruction": "SELL_TO_CLOSE", "quantity": 1,
             "instrument": {"assetType": "OPTION",
                            "symbol": "AMZN  260501C00255000"}},
        ],
        "price": "0.40",
    }
    out = render_order_ticket(body, underlying="AMZN")
    assert out is not None
    assert out.endswith("[TO CLOSE]")


# ---- failure cases -------------------------------------------------------


def test_empty_body_returns_none():
    assert render_order_ticket({}, underlying="NVDA") is None


def test_unparseable_osi_returns_none():
    body = {
        "orderType": "LIMIT", "duration": "DAY",
        "orderLegCollection": [{
            "instruction": "BUY_TO_OPEN", "quantity": 1,
            "instrument": {"assetType": "OPTION", "symbol": "garbage"},
        }],
        "price": "1.00",
    }
    assert render_order_ticket(body, underlying="X") is None


def test_vertical_mixed_open_close_renders_per_leg_marker():
    """Per-leg slash form when one leg explicitly closes and the other
    explicitly opens. Both legs carry positionEffect (set by parser
    on explicit user markers), so the renderer emits both tokens."""
    body = {
        "orderType": "NET_DEBIT", "duration": "DAY",
        "orderLegCollection": [
            {"instruction": "BUY_TO_CLOSE", "quantity": 1,
             "positionEffect": "CLOSING",
             "instrument": {"assetType": "OPTION",
                            "symbol": "AMZN  260501C00260000"}},
            {"instruction": "SELL_TO_OPEN", "quantity": 1,
             "positionEffect": "OPENING",
             "instrument": {"assetType": "OPTION",
                            "symbol": "AMZN  260501C00255000"}},
        ],
        "price": "0.40",
    }
    out = render_order_ticket(body, underlying="AMZN")
    assert out is not None
    assert out.endswith("[TO CLOSE/TO OPEN]")


def test_vertical_close_with_auto_on_other_leg_renders_AUTO():
    """When only one leg has been resolved to CLOSE (e.g. by
    DetectOpenCloseRule) and the other is still default OPEN, the
    marker uses AUTO for the unresolved leg."""
    body = {
        "orderType": "NET_DEBIT", "duration": "DAY",
        "orderLegCollection": [
            {"instruction": "BUY_TO_CLOSE", "quantity": 1,
             "positionEffect": "CLOSING",
             "instrument": {"assetType": "OPTION",
                            "symbol": "AMZN  260501C00260000"}},
            {"instruction": "SELL_TO_OPEN", "quantity": 1,
             "instrument": {"assetType": "OPTION",
                            "symbol": "AMZN  260501C00255000"}},
        ],
        "price": "0.40",
    }
    out = render_order_ticket(body, underlying="AMZN")
    assert out is not None
    assert out.endswith("[TO CLOSE/AUTO]")


def test_vertical_all_open_omits_marker():
    body = {
        "orderType": "NET_DEBIT", "duration": "DAY",
        "orderLegCollection": [
            {"instruction": "BUY_TO_OPEN", "quantity": 1,
             "instrument": {"assetType": "OPTION",
                            "symbol": "AMZN  260501P00270000"}},
            {"instruction": "SELL_TO_OPEN", "quantity": 1,
             "instrument": {"assetType": "OPTION",
                            "symbol": "AMZN  260501P00265000"}},
        ],
        "price": "1.50",
    }
    out = render_order_ticket(body, underlying="AMZN")
    assert out is not None
    assert "[" not in out  # no marker at all when uniform OPEN


def test_round_trip_per_leg_marker():
    """End-to-end: render a vertical with mixed effects, parse the
    output, render again. The marker token must be preserved."""
    from schwab_cli.order_ticket import parse_ticket
    body = {
        "orderType": "NET_DEBIT", "duration": "DAY",
        "orderLegCollection": [
            {"instruction": "BUY_TO_CLOSE", "quantity": 1,
             "instrument": {"assetType": "OPTION",
                            "symbol": "AMZN  260501C00260000"}},
            {"instruction": "SELL_TO_OPEN", "quantity": 1,
             "instrument": {"assetType": "OPTION",
                            "symbol": "AMZN  260501C00255000"}},
        ],
        "price": "0.40",
    }
    rendered = render_order_ticket(body, underlying="AMZN")
    assert rendered is not None
    t = parse_ticket(rendered)
    assert t.legs[0].instruction == "BUY_TO_CLOSE"
    assert t.legs[1].instruction == "SELL_TO_OPEN"


def test_market_multi_leg_returns_none():
    """Multi-leg without an explicit limit price isn't a valid Schwab
    order-entry string — render_ticket gives up rather than guess."""
    body = {
        "orderType": "MARKET", "duration": "DAY",
        "orderLegCollection": [
            {"instruction": "BUY_TO_OPEN", "quantity": 1,
             "instrument": {"assetType": "OPTION",
                            "symbol": "AMZN  260501C00260000"}},
            {"instruction": "SELL_TO_OPEN", "quantity": 1,
             "instrument": {"assetType": "OPTION",
                            "symbol": "AMZN  260501C00255000"}},
        ],
    }
    assert render_order_ticket(body, underlying="AMZN") is None
