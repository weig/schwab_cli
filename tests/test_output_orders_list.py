"""Tests for `order list` table rendering.

Schwab order IDs are 13-digit ints; OSI option symbols are 21-char
strings (``KO    260529P00073000``) with internal padding. The Rich
table used to truncate both — Order ID to 10 chars (so the displayed
value couldn't be re-used as input to ``cancel`` / ``get``), and OSI
symbol to 14 chars (so the strike got chopped). This test pins the
fixes.
"""
from __future__ import annotations

from schwab_cli.output.orders import render_order_list_human


def _ko_short_put_order() -> dict:
    """Mirrors a real Schwab response for the ticket the operator
    placed earlier in the session — ``SELL -1 KO ... 73 PUT``."""
    return {
        "orderId": 1006141943032,
        "enteredTime": "2026-04-27T14:49:52+0000",
        "status": "WORKING",
        "orderType": "LIMIT",
        "price": 0.8,
        "quantity": 1,
        "orderLegCollection": [
            {
                "instruction": "SELL_TO_OPEN",
                "quantity": 1,
                "instrument": {
                    "assetType": "OPTION",
                    "symbol": "KO    260529P00073000",  # 21-char OSI
                },
            },
        ],
    }


def test_order_list_shows_full_13_digit_order_id():
    """The full Schwab order ID must appear so it can be copied into
    ``order cancel`` / ``order get`` directly."""
    out = render_order_list_human([_ko_short_put_order()])
    assert "1006141943032" in out
    # And not truncated to 10 chars.
    assert " 6141943032 " not in out


def test_order_list_shows_full_osi_symbol_including_strike():
    """OSI symbol carries the strike in the trailing 8 digits; the
    table used to chop everything past 14 chars and drop them."""
    out = render_order_list_human([_ko_short_put_order()])
    # Padding inside the OSI is collapsed to a single space for
    # readability — the strike (00073000) must still be visible.
    assert "00073000" in out
    assert "KO 260529P00073000" in out


def test_order_list_handles_multi_leg_truncation_marker():
    order = _ko_short_put_order()
    leg = order["orderLegCollection"][0]
    order["orderLegCollection"] = [leg, leg, leg]  # 3 legs → ",..."
    out = render_order_list_human([order])
    assert ",..." in out
