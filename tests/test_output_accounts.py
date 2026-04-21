import json

from schwab_cli.output.accounts import render_account, render_accounts, render_positions
from schwab_cli.output.format import Format


_ACCOUNTS_PAYLOAD = [
    {"securitiesAccount": {
        "accountNumber": "12345678",
        "type": "MARGIN",
        "currentBalances": {
            "liquidationValue": 12345.67,
            "buyingPower": 24691.34,
            "cashBalance": 1000.0,
            "maintenanceRequirement": 3456.78,
        },
        "positions": [{"instrument": {"symbol": "AAPL"}}],
    }},
    {"securitiesAccount": {
        "accountNumber": "87654321",
        "type": "CASH",
        "currentBalances": {
            "liquidationValue": 7890.12,
            "cashBalance": 100.0,
            # CASH accounts have no buyingPower / maintenanceRequirement
        },
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
    assert data[0]["buyingPower"] == 24691.34
    assert data[0]["maintenanceRequirement"] == 3456.78
    # Cash account: buying power / maintenance absent from balances → None in output
    assert data[1]["buyingPower"] is None
    assert data[1]["maintenanceRequirement"] is None


def test_render_accounts_md_has_header_and_rows():
    out = render_accounts(_ACCOUNTS_PAYLOAD, Format.MD)
    lines = out.strip().splitlines()
    assert len(lines) >= 4
    # New header includes all requested columns.
    assert "Net Liq" in lines[0]
    assert "Buying Power" in lines[0]
    assert "Cash" in lines[0]
    assert "Maint" in lines[0]
    assert "Positions" in lines[0]
    assert "MARGIN" in out
    assert "5678" in out
    assert "12345678" not in out
    # MARGIN row shows values; CASH row shows em-dashes for absent fields.
    assert "24,691.34" in out  # buying power
    assert "3,456.78" in out   # maintenance
    assert "—" in out          # em-dash from missing CASH fields


def test_render_accounts_human_includes_last_4_mask():
    out = render_accounts(_ACCOUNTS_PAYLOAD, Format.HUMAN)
    assert "5678" in out or "...5678" in out
    # Human table must show the new columns too.
    assert "Net Liq" in out
    assert "Buying Power" in out
    assert "Maint" in out  # header may be "Maint Req" or similar


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


def test_render_account_human_has_table():
    out = render_account(_SINGLE_ACCOUNT, Format.HUMAN)
    assert "MARGIN" in out
    assert "12345678" in out  # HUMAN shows the full number in "Number" row


def test_render_account_md_has_headings():
    out = render_account(_SINGLE_ACCOUNT, Format.MD)
    assert out.startswith("# Account ...5678")
    assert "**Type:**" in out
    assert "MARGIN" in out


def test_render_positions_human_has_rows():
    out = render_positions(_POSITION_ROWS, Format.HUMAN)
    assert "AAPL" in out
    assert "MSFT" in out
    assert "200.00" in out  # avg price formatting
