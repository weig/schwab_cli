import json

from schwab_cli.output.accounts import render_account, render_accounts, render_positions
from schwab_cli.output.format import Format


_ACCOUNTS_PAYLOAD = [
    {"securitiesAccount": {
        "accountNumber": "12345678",
        "type": "MARGIN",
        "currentBalances": {"liquidationValue": 12345.67, "cashBalance": 1000.0},
        "positions": [{"instrument": {"symbol": "AAPL"}}],
    }},
    {"securitiesAccount": {
        "accountNumber": "87654321",
        "type": "CASH",
        "currentBalances": {"liquidationValue": 7890.12, "cashBalance": 100.0},
        "positions": [],
    }},
]


def test_render_accounts_json_is_parseable():
    out = render_accounts(_ACCOUNTS_PAYLOAD, Format.JSON)
    data = json.loads(out)
    assert isinstance(data, list)
    assert data[0]["accountNumber"] == "12345678"
    assert data[0]["type"] == "MARGIN"
    assert data[0]["liquidationValue"] == 12345.67


def test_render_accounts_md_has_header_and_rows():
    out = render_accounts(_ACCOUNTS_PAYLOAD, Format.MD)
    lines = out.strip().splitlines()
    assert len(lines) >= 4
    assert "|" in lines[0]
    assert "MARGIN" in out
    assert "12345678" in out


def test_render_accounts_human_includes_last_4_mask():
    out = render_accounts(_ACCOUNTS_PAYLOAD, Format.HUMAN)
    assert "5678" in out or "...5678" in out


_SINGLE_ACCOUNT = {"securitiesAccount": {
    "accountNumber": "12345678",
    "type": "MARGIN",
    "currentBalances": {
        "liquidationValue": 12345.67,
        "cashBalance": 1000.0,
        "buyingPower": 24691.34,
    },
    "initialBalances": {"cashBalance": 1000.0},
}}


def test_render_account_json_has_all_fields():
    out = render_account(_SINGLE_ACCOUNT, Format.JSON)
    data = json.loads(out)
    assert data["accountNumber"] == "12345678"
    assert data["type"] == "MARGIN"
    assert data["currentBalances"]["buyingPower"] == 24691.34


_POSITION_ROWS = [
    {
        "_account": "12345678",
        "instrument": {"symbol": "AAPL"},
        "longQuantity": 10.0,
        "averagePrice": 200.0,
        "marketValue": 2321.40,
        "currentDayProfitLoss": 4.20,
        "longOpenProfitLoss": 321.40,
    },
    {
        "_account": "87654321",
        "instrument": {"symbol": "MSFT"},
        "longQuantity": 5.0,
        "averagePrice": 400.0,
        "marketValue": 2050.0,
        "currentDayProfitLoss": -10.0,
        "longOpenProfitLoss": 50.0,
    },
]


def test_render_positions_json_shapes_rows():
    out = render_positions(_POSITION_ROWS, Format.JSON)
    data = json.loads(out)
    assert len(data) == 2
    assert data[0]["symbol"] == "AAPL"
    assert data[0]["account"] == "12345678"
    assert data[0]["qty"] == 10.0
    assert data[0]["avgPrice"] == 200.0


def test_render_positions_md_contains_symbols():
    out = render_positions(_POSITION_ROWS, Format.MD)
    assert "AAPL" in out
    assert "MSFT" in out
    assert "|" in out.splitlines()[0]
