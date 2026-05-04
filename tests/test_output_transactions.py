import json

from schwab_cli.output.format import Format
from schwab_cli.output.transactions import render_transactions, shape_transactions


_TRADE_BUY = {
    "_account": "12340756",
    "activityId": 1000001,
    "time": "2026-04-18T14:32:11+0000",
    "type": "TRADE",
    "netAmount": -1055.30,
    "transferItems": [
        {
            "instrument": {"assetType": "EQUITY", "symbol": "AMZN"},
            "amount": 5.0, "cost": -1055.30, "price": 211.06,
            "positionEffect": "OPENING",
        },
    ],
}


_TRADE_SELL = {
    "_account": "12340756",
    "activityId": 1000002,
    "time": "2026-04-19T15:01:00+0000",
    "type": "TRADE",
    "netAmount": 1258.50,
    "transferItems": [
        {
            "instrument": {"assetType": "EQUITY", "symbol": "AMZN"},
            "amount": -5.0, "cost": 1258.50, "price": 251.70,
            "positionEffect": "CLOSING",
        },
    ],
}


_TRADE_OPTION_STO = {
    "_account": "12340756",
    "activityId": 1000003,
    "time": "2026-04-22T18:48:00+0000",
    "type": "TRADE",
    "netAmount": 709.33,
    "transferItems": [
        {
            "instrument": {
                "assetType": "OPTION",
                "symbol": "AMZN  260515C00260000",
                "underlyingSymbol": "AMZN",
            },
            "amount": -1.0, "cost": 710.00, "price": 7.10,
            "positionEffect": "OPENING",
        },
        {"feeType": "COMMISSION", "cost": -0.65, "amount": 1},
        {"feeType": "SEC_FEE", "cost": -0.02, "amount": 1},
    ],
}


_DIV = {
    "_account": "12340756",
    "activityId": 1000004,
    "time": "2026-04-15T00:00:00+0000",
    "type": "DIVIDEND_OR_INTEREST",
    "netAmount": 12.43,
    "transferItems": [
        {"instrument": {"assetType": "EQUITY", "symbol": "KO"}, "cost": 12.43},
    ],
}


# Schwab's actual dividend payload: only CURRENCY_USD in transferItems;
# the true source sits in the top-level `description` field.
_DIV_CURRENCY = {
    "_account": "12340756",
    "activityId": 1000005,
    "time": "2026-04-01T12:09:42+0000",
    "type": "DIVIDEND_OR_INTEREST",
    "description": "THE COCA-COLA CO",
    "netAmount": 22.31,
    "transferItems": [
        {
            "instrument": {"assetType": "CURRENCY", "symbol": "CURRENCY_USD"},
            "amount": 22.31, "cost": 0.0, "price": 0.0,
        },
    ],
}


_JOURNAL_CURRENCY = {
    "_account": "12340756",
    "activityId": 1000006,
    "time": "2026-04-01T12:09:42+0000",
    "type": "JOURNAL",
    "description": "THE COCA-COLA CO",
    "netAmount": -2.23,
    "transferItems": [
        {
            "instrument": {"assetType": "CURRENCY", "symbol": "CURRENCY_USD"},
            "amount": -2.23, "cost": 0.0, "price": 0.0,
        },
    ],
}


_SAMPLE = [_TRADE_BUY, _TRADE_SELL, _TRADE_OPTION_STO, _DIV]


# ---------------------------------------------------------------------------
# shape_transactions
# ---------------------------------------------------------------------------

def test_shape_basic_fields():
    rows = shape_transactions(_SAMPLE)
    assert len(rows) == 4
    r = rows[0]
    for key in ("account", "date", "time", "type", "symbol", "qty",
                "price", "effect", "netAmount"):
        assert key in r


def test_shape_trade_buy_extracts_main_leg():
    r = shape_transactions([_TRADE_BUY])[0]
    assert r["account"] == "12340756"
    assert r["date"] == "2026-04-18"
    assert r["symbol"] == "AMZN"
    assert r["qty"] == 5.0
    assert r["price"] == 211.06
    assert r["effect"] == "OPENING"
    assert r["netAmount"] == -1055.30
    assert r["type"] == "TRADE"


def test_shape_trade_sell_negative_qty_preserved():
    r = shape_transactions([_TRADE_SELL])[0]
    assert r["qty"] == -5.0
    assert r["effect"] == "CLOSING"


def test_shape_option_skips_fee_items():
    r = shape_transactions([_TRADE_OPTION_STO])[0]
    # Main leg is the option, not the commission/fee entries.
    assert r["symbol"] == "AMZN  260515C00260000"
    assert r["qty"] == -1.0
    assert r["price"] == 7.10
    assert r["netAmount"] == 709.33


def test_shape_dividend_has_symbol_no_effect():
    r = shape_transactions([_DIV])[0]
    assert r["type"] == "DIVIDEND_OR_INTEREST"
    assert r["symbol"] == "KO"
    assert r["effect"] is None
    assert r["netAmount"] == 12.43


def test_shape_dividend_currency_uses_top_level_description():
    r = shape_transactions([_DIV_CURRENCY])[0]
    # Schwab returned only CURRENCY_USD in transferItems — we must
    # surface the actual company from the top-level description.
    assert r["symbol"] == "THE COCA-COLA CO"
    assert r["netAmount"] == 22.31


def test_shape_journal_currency_uses_top_level_description():
    r = shape_transactions([_JOURNAL_CURRENCY])[0]
    assert r["symbol"] == "THE COCA-COLA CO"
    assert r["type"] == "JOURNAL"


def test_shape_sorts_by_time_ascending():
    rows = shape_transactions([_TRADE_SELL, _DIV, _TRADE_BUY, _TRADE_OPTION_STO])
    times = [r["time"] for r in rows]
    assert times == sorted(times)


# ---------------------------------------------------------------------------
# render_transactions — JSON
# ---------------------------------------------------------------------------

def test_render_json_roundtrip():
    rows = shape_transactions(_SAMPLE)
    out = render_transactions(rows, fmt=Format.JSON)
    assert "\x1b[" not in out
    data = json.loads(out)
    assert len(data) == 4
    assert data[0]["account"]


def test_render_json_empty():
    out = render_transactions([], fmt=Format.JSON)
    assert json.loads(out) == []


# ---------------------------------------------------------------------------
# render_transactions — HUMAN
# ---------------------------------------------------------------------------

def test_render_human_columns_present():
    rows = shape_transactions(_SAMPLE)
    out = render_transactions(rows, fmt=Format.HUMAN)
    for col in ("Date", "Type", "Symbol", "Qty", "Price", "Net"):
        assert col in out


def test_render_human_contains_symbols():
    rows = shape_transactions(_SAMPLE)
    out = render_transactions(rows, fmt=Format.HUMAN)
    assert "AMZN" in out
    assert "KO" in out


def test_render_human_net_color_red_for_debit():
    # -$1,055 is a debit (buy), should render with red ANSI code.
    rows = shape_transactions([_TRADE_BUY])
    out = render_transactions(rows, fmt=Format.HUMAN)
    assert "\x1b[31m" in out


def test_render_human_net_color_green_for_credit():
    # +$1,258 is a credit (sell), should render with green ANSI code.
    rows = shape_transactions([_TRADE_SELL])
    out = render_transactions(rows, fmt=Format.HUMAN)
    assert "\x1b[32m" in out


def test_render_human_empty_does_not_crash():
    out = render_transactions([], fmt=Format.HUMAN)
    # A graceful empty message — any string fine, just no exception.
    assert isinstance(out, str)


def test_render_human_includes_summary():
    rows = shape_transactions(_SAMPLE)
    out = render_transactions(rows, fmt=Format.HUMAN)
    # Should summarize: count of rows + net cashflow.
    assert "4" in out  # 4 transactions


# ---------------------------------------------------------------------------
# render_transactions — MD
# ---------------------------------------------------------------------------

def test_render_md_no_ansi():
    rows = shape_transactions(_SAMPLE)
    out = render_transactions(rows, fmt=Format.MD)
    assert "\x1b[" not in out


def test_render_md_has_heading_and_separator():
    rows = shape_transactions(_SAMPLE)
    out = render_transactions(rows, fmt=Format.MD)
    assert out.startswith("# Transactions")
    assert "|---" in out or "| ---" in out


def test_render_md_has_all_columns():
    rows = shape_transactions(_SAMPLE)
    out = render_transactions(rows, fmt=Format.MD)
    for col in ("Date", "Account", "Type", "Symbol", "Qty", "Price", "Net"):
        assert col in out


def test_render_md_empty():
    out = render_transactions([], fmt=Format.MD)
    assert "Transactions" in out


# ---- show_account toggle --------------------------------------------------

_SAMPLE_ROW = {
    "account": "57410756",
    "date": "2026-04-15",
    "time": "2026-04-15T10:00:00+00:00",
    "type": "TRADE",
    "symbol": "JPM",
    "qty": 1.0,
    "price": 100.0,
    "effect": "OPENING",
    "netAmount": -100.0,
}


def test_render_human_default_shows_account_column():
    out = render_transactions([_SAMPLE_ROW], fmt=Format.HUMAN)
    assert "Account" in out
    assert "0756" in out  # masked suffix


def test_render_human_hides_account_column_when_show_account_false():
    out = render_transactions(
        [_SAMPLE_ROW], fmt=Format.HUMAN, show_account=False,
    )
    assert "Account" not in out
    assert "0756" not in out


def test_render_md_hides_account_column_when_show_account_false():
    out = render_transactions(
        [_SAMPLE_ROW], fmt=Format.MD, show_account=False,
    )
    assert "| Account |" not in out
    assert "57410756" not in out
    assert "...0756" not in out


def test_render_json_always_includes_account_field_regardless_of_flag():
    """JSON consumers want a stable shape. ``show_account=False`` is
    a presentation hint for human/MD only."""
    import json as _json
    out_with = _json.loads(render_transactions([_SAMPLE_ROW], fmt=Format.JSON))
    out_without = _json.loads(
        render_transactions([_SAMPLE_ROW], fmt=Format.JSON, show_account=False),
    )
    assert out_with == out_without
    assert "account" in out_with[0]
