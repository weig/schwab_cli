"""Characterization tests for the ``accounts``, ``account``, and ``positions`` commands.

These tests pin the CURRENT observable behaviour of all three commands end-to-end
so that the upcoming service-layer migration can be proven behaviour-preserving
without altering production code.

Seam used: the Layer-1 api function names as they are bound in
``schwab_cli.commands.accounts`` --
  ``schwab_cli.commands.accounts.list_accounts``
  ``schwab_cli.commands.accounts.get_account``
  ``schwab_cli.commands.accounts.get_positions``

``commands/accounts.py`` imports these from ``schwab_cli.api.accounts`` with a
``from ... import`` statement, so the binding lives in the commands namespace.
To intercept the calls while still exercising the full command stack (format
flag handling, rendering) we must patch where the name is used, not where it
is defined.

After the service-layer migration, the commands will call a service shim that
itself calls the Layer-1 api functions.  At that point this file's patch
targets will be updated to ``schwab_cli.api.accounts.*``.

Golden values were captured by running the current code and recording its output
verbatim.  Do NOT alter golden constants without first verifying that the
production code changed intentionally.
"""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from schwab_cli.api.client import ApiError, SessionExpired
from schwab_cli.cli import app
from schwab_cli.config import Config
from schwab_cli.config import save as save_config
from schwab_cli.session import Session
from schwab_cli.session import save as save_session

runner = CliRunner()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _prep(monkeypatch, tmp_path):
    """Isolated HOME with a valid config + non-expired session.

    The session's ``expires_at`` is set to now+3600 so the service-layer
    auth path (``service.auth.get_session``) does NOT attempt a real
    ``oauth.refresh`` — it only mints when the access token looks expired.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save_config(
        Config(
            client_id="cid",
            client_secret="csec",
            redirect_uri="https://127.0.0.1:8443",
        )
    )
    save_session(
        Session(
            access_token="atok",
            refresh_token="rtok",
            expires_at=int(time.time()) + 3600,
            refresh_token_expires_at=int(time.time()) + 7 * 24 * 3600,
        )
    )


# ---------------------------------------------------------------------------
# Canned payloads  (copied from test_commands_accounts.py and extended)
# ---------------------------------------------------------------------------

# Two-account list payload for `accounts` (list) command.
_ACCOUNTS = [
    {
        "securitiesAccount": {
            "accountNumber": "12345678",
            "type": "MARGIN",
            "currentBalances": {
                "liquidationValue": 12345.67,
                "buyingPower": 24691.34,
                "availableFunds": 12345.67,
                "cashBalance": 1000.0,
                "maintenanceRequirement": 3456.78,
            },
            "positions": [{"instrument": {"symbol": "AAPL"}}],
        }
    },
    {
        "securitiesAccount": {
            "accountNumber": "87654321",
            "type": "CASH",
            "currentBalances": {
                "liquidationValue": 7890.12,
                "cashBalance": 100.0,
                # CASH accounts have no buyingPower / maintenanceRequirement
            },
            "positions": [],
        }
    },
]

# Single account payload for `account <number>` (show) command.
_SINGLE_ACCOUNT = {
    "securitiesAccount": {
        "accountNumber": "12345678",
        "type": "MARGIN",
        "currentBalances": {
            "liquidationValue": 12345.67,
            "cashBalance": 1000.0,
            "buyingPower": 24691.34,
            "availableFunds": 12345.67,
            "dayTradingBuyingPower": 49382.68,
            "maintenanceRequirement": 3456.78,
        },
        "initialBalances": {"cashBalance": 1000.0},
        "positions": [],
    }
}

# Two-row position list for `positions` command.
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

# ---------------------------------------------------------------------------
# Golden constants (captured from current code)
# ---------------------------------------------------------------------------

# --- accounts (list) JSON ---
_GOLDEN_ACCOUNTS_JSON_KEYS = {
    "accountNumber",
    "type",
    "liquidationValue",
    "stockBuyingPower",
    "optionBuyingPower",
    "cashBalance",
    "maintenanceRequirement",
    "positionCount",
}

# --- accounts (list) MD ---
_GOLDEN_ACCOUNTS_MD_HEADER = (
    "| Account | Type | Net Liq | BP (Stock) | BP (Option) | Cash | Maint Req | Positions |"
)
_GOLDEN_ACCOUNTS_MD_SEP = (
    "|---------|------|---------|------------|-------------|------|-----------|-----------|"
)
_GOLDEN_ACCOUNTS_MD_MARGIN_ROW = (
    "| ...5678 | MARGIN | 12,345.67 | 24,691.34 | 12,345.67 | 1,000.00 | 3,456.78 | 1 |"
)
_GOLDEN_ACCOUNTS_MD_CASH_ROW = (
    "| ...4321 | CASH | 7,890.12 | — | — | 100.00 | — | 0 |"
)

# --- account (show) MD ---
_GOLDEN_ACCOUNT_MD_HEADING = "# Account ...5678"
_GOLDEN_ACCOUNT_MD_NUMBER_LINE = "- **Number:** 12345678"
_GOLDEN_ACCOUNT_MD_TYPE_LINE = "- **Type:** MARGIN"
_GOLDEN_ACCOUNT_MD_LIQ_LINE = "- **Liquidation Value:** 12,345.67"
_GOLDEN_ACCOUNT_MD_CASH_LINE = "- **Cash Balance:** 1,000.00"
_GOLDEN_ACCOUNT_MD_BP_STOCK_LINE = "- **Buying Power (Stock):** 24,691.34"
_GOLDEN_ACCOUNT_MD_BP_OPT_LINE = "- **Buying Power (Option):** 12,345.67"
_GOLDEN_ACCOUNT_MD_DT_BP_LINE = "- **Day Trading BP:** 49,382.68"
_GOLDEN_ACCOUNT_MD_MAINT_LINE = "- **Maintenance Requirement:** 3,456.78"
_GOLDEN_ACCOUNT_MD_POSITIONS_LINE = "- **Positions:** 0"

# --- positions JSON ---
_GOLDEN_POSITIONS_JSON_KEYS = {
    "account",
    "symbol",
    "qty",
    "avgPrice",
    "marketValue",
    "dayPnL",
    "totalPnL",
}

# --- positions MD ---
_GOLDEN_POSITIONS_MD_HEADER = (
    "| Account | Symbol | Qty | Avg Price | Market Value | Day P&L | Total P&L |"
)
_GOLDEN_POSITIONS_MD_SEP = (
    "|---------|--------|-----|-----------|--------------|---------|-----------| "
)
_GOLDEN_POSITIONS_MD_AAPL_ROW = (
    "| ...5678 | AAPL | 10.0 | 200.00 | 2,321.40 | 4.20 | 321.40 |"
)
_GOLDEN_POSITIONS_MD_MSFT_ROW = (
    "| ...4321 | MSFT | 5.0 | 400.00 | 2,050.00 | -10.00 | 50.00 |"
)


# ===========================================================================
# 1. `accounts` command — list all accounts
# ===========================================================================


# --- 1a. HUMAN output ---


def test_accounts_human_exit_code(monkeypatch, tmp_path):
    """Happy-path HUMAN output must exit 0."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.list_accounts", return_value=_ACCOUNTS
    ):
        result = runner.invoke(app, ["accounts"])
    assert result.exit_code == 0, result.output


def test_accounts_human_contains_accounts_title(monkeypatch, tmp_path):
    """HUMAN output must contain the 'Accounts' table title."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.list_accounts", return_value=_ACCOUNTS
    ):
        result = runner.invoke(app, ["accounts"])
    assert "Accounts" in result.output


def test_accounts_human_contains_masked_account_numbers(monkeypatch, tmp_path):
    """HUMAN output must show masked account numbers (last 4 digits)."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.list_accounts", return_value=_ACCOUNTS
    ):
        result = runner.invoke(app, ["accounts"])
    assert "5678" in result.output
    assert "4321" in result.output


def test_accounts_human_contains_account_types(monkeypatch, tmp_path):
    """HUMAN output must show account types (MARGIN, CASH)."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.list_accounts", return_value=_ACCOUNTS
    ):
        result = runner.invoke(app, ["accounts"])
    assert "MARGIN" in result.output
    assert "CASH" in result.output


def test_accounts_human_contains_column_headers(monkeypatch, tmp_path):
    """HUMAN output must show all expected column headers."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.list_accounts", return_value=_ACCOUNTS
    ):
        result = runner.invoke(app, ["accounts"])
    for header in ("Net Liq", "BP (Stock)", "BP (Option)", "Cash", "Maint Req", "Positions"):
        assert header in result.output, f"Missing column header: {header!r}"


def test_accounts_human_contains_liquidation_value(monkeypatch, tmp_path):
    """HUMAN output must show formatted liquidation values."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.list_accounts", return_value=_ACCOUNTS
    ):
        result = runner.invoke(app, ["accounts"])
    assert "12,345.67" in result.output
    assert "7,890.12" in result.output


def test_accounts_human_does_not_expose_full_account_number(monkeypatch, tmp_path):
    """HUMAN output must NOT contain full account numbers (security mask)."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.list_accounts", return_value=_ACCOUNTS
    ):
        result = runner.invoke(app, ["accounts"])
    # The full 8-digit numbers must not appear in HUMAN table
    assert "12345678" not in result.output
    assert "87654321" not in result.output


# --- 1b. JSON output ---


def test_accounts_json_exit_code(monkeypatch, tmp_path):
    """JSON output must exit 0."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.list_accounts", return_value=_ACCOUNTS
    ):
        result = runner.invoke(app, ["accounts", "--json"])
    assert result.exit_code == 0, result.output


def test_accounts_json_is_list(monkeypatch, tmp_path):
    """JSON output must be a list."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.list_accounts", return_value=_ACCOUNTS
    ):
        result = runner.invoke(app, ["accounts", "--json"])
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    assert len(data) == 2


def test_accounts_json_row_keys(monkeypatch, tmp_path):
    """JSON rows must contain exactly the golden set of keys."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.list_accounts", return_value=_ACCOUNTS
    ):
        result = runner.invoke(app, ["accounts", "--json"])
    data = json.loads(result.stdout)
    assert set(data[0].keys()) == _GOLDEN_ACCOUNTS_JSON_KEYS
    assert set(data[1].keys()) == _GOLDEN_ACCOUNTS_JSON_KEYS


def test_accounts_json_margin_row_values(monkeypatch, tmp_path):
    """JSON MARGIN row must contain exact golden field values."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.list_accounts", return_value=_ACCOUNTS
    ):
        result = runner.invoke(app, ["accounts", "--json"])
    data = json.loads(result.stdout)
    row = data[0]
    assert row["accountNumber"] == "12345678"
    assert row["type"] == "MARGIN"
    assert row["liquidationValue"] == 12345.67
    assert row["stockBuyingPower"] == 24691.34
    assert row["optionBuyingPower"] == 12345.67
    assert row["cashBalance"] == 1000.0
    assert row["maintenanceRequirement"] == 3456.78
    assert row["positionCount"] == 1


def test_accounts_json_cash_row_null_fields(monkeypatch, tmp_path):
    """JSON CASH row must have null for absent buying-power / maintenance fields."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.list_accounts", return_value=_ACCOUNTS
    ):
        result = runner.invoke(app, ["accounts", "--json"])
    data = json.loads(result.stdout)
    cash_row = data[1]
    assert cash_row["accountNumber"] == "87654321"
    assert cash_row["type"] == "CASH"
    assert cash_row["stockBuyingPower"] is None
    assert cash_row["optionBuyingPower"] is None
    assert cash_row["maintenanceRequirement"] is None
    assert cash_row["positionCount"] == 0


# --- 1c. MD output ---


def test_accounts_md_exit_code(monkeypatch, tmp_path):
    """MD output must exit 0."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.list_accounts", return_value=_ACCOUNTS
    ):
        result = runner.invoke(app, ["accounts", "--md"])
    assert result.exit_code == 0, result.output


def test_accounts_md_exact_header_line(monkeypatch, tmp_path):
    """MD output must contain the exact golden header line."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.list_accounts", return_value=_ACCOUNTS
    ):
        result = runner.invoke(app, ["accounts", "--md"])
    assert _GOLDEN_ACCOUNTS_MD_HEADER in result.stdout


def test_accounts_md_exact_separator_line(monkeypatch, tmp_path):
    """MD output must contain the exact golden separator line."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.list_accounts", return_value=_ACCOUNTS
    ):
        result = runner.invoke(app, ["accounts", "--md"])
    assert _GOLDEN_ACCOUNTS_MD_SEP in result.stdout


def test_accounts_md_exact_margin_row(monkeypatch, tmp_path):
    """MD output must contain the exact golden MARGIN account data row."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.list_accounts", return_value=_ACCOUNTS
    ):
        result = runner.invoke(app, ["accounts", "--md"])
    assert _GOLDEN_ACCOUNTS_MD_MARGIN_ROW in result.stdout


def test_accounts_md_exact_cash_row(monkeypatch, tmp_path):
    """MD output must contain the exact golden CASH account data row (with em-dashes)."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.list_accounts", return_value=_ACCOUNTS
    ):
        result = runner.invoke(app, ["accounts", "--md"])
    assert _GOLDEN_ACCOUNTS_MD_CASH_ROW in result.stdout


def test_accounts_md_no_ansi_codes(monkeypatch, tmp_path):
    """MD output must not contain ANSI escape codes."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.list_accounts", return_value=_ACCOUNTS
    ):
        result = runner.invoke(app, ["accounts", "--md"])
    assert "\x1b[" not in result.stdout


# ===========================================================================
# 2. `account <number>` command — show single account
# ===========================================================================


# --- 2a. HUMAN output ---


def test_account_human_exit_code(monkeypatch, tmp_path):
    """Happy-path HUMAN output for account show must exit 0."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_account", return_value=_SINGLE_ACCOUNT
    ):
        result = runner.invoke(app, ["account", "12345678"])
    assert result.exit_code == 0, result.output


def test_account_human_contains_account_title(monkeypatch, tmp_path):
    """HUMAN output must contain the account title with masked number."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_account", return_value=_SINGLE_ACCOUNT
    ):
        result = runner.invoke(app, ["account", "12345678"])
    assert "...5678" in result.output


def test_account_human_contains_type(monkeypatch, tmp_path):
    """HUMAN output must show the account type."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_account", return_value=_SINGLE_ACCOUNT
    ):
        result = runner.invoke(app, ["account", "12345678"])
    assert "MARGIN" in result.output


def test_account_human_contains_full_number_in_number_row(monkeypatch, tmp_path):
    """HUMAN output must show the full account number in the 'Number' field row."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_account", return_value=_SINGLE_ACCOUNT
    ):
        result = runner.invoke(app, ["account", "12345678"])
    assert "12345678" in result.output


def test_account_human_contains_balance_fields(monkeypatch, tmp_path):
    """HUMAN output must show all balance field labels."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_account", return_value=_SINGLE_ACCOUNT
    ):
        result = runner.invoke(app, ["account", "12345678"])
    for label in (
        "Liquidation Value",
        "Cash Balance",
        "Buying Power (Stock)",
        "Buying Power (Option)",
        "Day Trading BP",
        "Maintenance Requirement",
        "Positions",
    ):
        assert label in result.output, f"Missing field label: {label!r}"


def test_account_human_contains_formatted_values(monkeypatch, tmp_path):
    """HUMAN output must show formatted monetary values."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_account", return_value=_SINGLE_ACCOUNT
    ):
        result = runner.invoke(app, ["account", "12345678"])
    assert "12,345.67" in result.output
    assert "24,691.34" in result.output
    assert "49,382.68" in result.output


# --- 2b. JSON output ---


def test_account_json_exit_code(monkeypatch, tmp_path):
    """JSON output must exit 0."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_account", return_value=_SINGLE_ACCOUNT
    ):
        result = runner.invoke(app, ["account", "12345678", "--json"])
    assert result.exit_code == 0, result.output


def test_account_json_top_level_keys(monkeypatch, tmp_path):
    """JSON output must have exactly these top-level keys."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_account", return_value=_SINGLE_ACCOUNT
    ):
        result = runner.invoke(app, ["account", "12345678", "--json"])
    data = json.loads(result.stdout)
    assert set(data.keys()) == {
        "accountNumber",
        "type",
        "currentBalances",
        "initialBalances",
        "positionCount",
    }


def test_account_json_account_number(monkeypatch, tmp_path):
    """JSON output must have the full (unmasked) account number."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_account", return_value=_SINGLE_ACCOUNT
    ):
        result = runner.invoke(app, ["account", "12345678", "--json"])
    data = json.loads(result.stdout)
    assert data["accountNumber"] == "12345678"


def test_account_json_type(monkeypatch, tmp_path):
    """JSON output ``type`` must equal 'MARGIN'."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_account", return_value=_SINGLE_ACCOUNT
    ):
        result = runner.invoke(app, ["account", "12345678", "--json"])
    data = json.loads(result.stdout)
    assert data["type"] == "MARGIN"


def test_account_json_current_balances_values(monkeypatch, tmp_path):
    """JSON currentBalances must pass through raw values from the API payload."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_account", return_value=_SINGLE_ACCOUNT
    ):
        result = runner.invoke(app, ["account", "12345678", "--json"])
    data = json.loads(result.stdout)
    bal = data["currentBalances"]
    assert bal["liquidationValue"] == 12345.67
    assert bal["cashBalance"] == 1000.0
    assert bal["buyingPower"] == 24691.34
    assert bal["availableFunds"] == 12345.67
    assert bal["dayTradingBuyingPower"] == 49382.68
    assert bal["maintenanceRequirement"] == 3456.78


def test_account_json_position_count(monkeypatch, tmp_path):
    """JSON positionCount must be 0 (no positions in canned payload)."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_account", return_value=_SINGLE_ACCOUNT
    ):
        result = runner.invoke(app, ["account", "12345678", "--json"])
    data = json.loads(result.stdout)
    assert data["positionCount"] == 0


# --- 2c. MD output ---


def test_account_md_exit_code(monkeypatch, tmp_path):
    """MD output must exit 0."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_account", return_value=_SINGLE_ACCOUNT
    ):
        result = runner.invoke(app, ["account", "12345678", "--md"])
    assert result.exit_code == 0, result.output


def test_account_md_exact_heading(monkeypatch, tmp_path):
    """MD output must start with the exact golden H1 heading."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_account", return_value=_SINGLE_ACCOUNT
    ):
        result = runner.invoke(app, ["account", "12345678", "--md"])
    assert _GOLDEN_ACCOUNT_MD_HEADING in result.stdout


def test_account_md_exact_number_line(monkeypatch, tmp_path):
    """MD output must contain the exact Number line."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_account", return_value=_SINGLE_ACCOUNT
    ):
        result = runner.invoke(app, ["account", "12345678", "--md"])
    assert _GOLDEN_ACCOUNT_MD_NUMBER_LINE in result.stdout


def test_account_md_exact_type_line(monkeypatch, tmp_path):
    """MD output must contain the exact Type line."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_account", return_value=_SINGLE_ACCOUNT
    ):
        result = runner.invoke(app, ["account", "12345678", "--md"])
    assert _GOLDEN_ACCOUNT_MD_TYPE_LINE in result.stdout


def test_account_md_all_balance_lines(monkeypatch, tmp_path):
    """MD output must contain all exact golden balance lines."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_account", return_value=_SINGLE_ACCOUNT
    ):
        result = runner.invoke(app, ["account", "12345678", "--md"])
    for golden_line in (
        _GOLDEN_ACCOUNT_MD_LIQ_LINE,
        _GOLDEN_ACCOUNT_MD_CASH_LINE,
        _GOLDEN_ACCOUNT_MD_BP_STOCK_LINE,
        _GOLDEN_ACCOUNT_MD_BP_OPT_LINE,
        _GOLDEN_ACCOUNT_MD_DT_BP_LINE,
        _GOLDEN_ACCOUNT_MD_MAINT_LINE,
        _GOLDEN_ACCOUNT_MD_POSITIONS_LINE,
    ):
        assert golden_line in result.stdout, f"Missing MD line: {golden_line!r}"


def test_account_md_no_ansi_codes(monkeypatch, tmp_path):
    """MD output must not contain ANSI escape codes."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_account", return_value=_SINGLE_ACCOUNT
    ):
        result = runner.invoke(app, ["account", "12345678", "--md"])
    assert "\x1b[" not in result.stdout


# ===========================================================================
# 3. `positions` command — all accounts and filtered
# ===========================================================================


# --- 3a. HUMAN output (all accounts) ---


def test_positions_human_exit_code(monkeypatch, tmp_path):
    """Happy-path HUMAN output for positions must exit 0."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_positions", return_value=_POSITION_ROWS
    ):
        result = runner.invoke(app, ["positions"])
    assert result.exit_code == 0, result.output


def test_positions_human_contains_positions_title(monkeypatch, tmp_path):
    """HUMAN output must contain the 'Positions' table title."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_positions", return_value=_POSITION_ROWS
    ):
        result = runner.invoke(app, ["positions"])
    assert "Positions" in result.output


def test_positions_human_contains_symbols(monkeypatch, tmp_path):
    """HUMAN output must show both AAPL and MSFT symbols."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_positions", return_value=_POSITION_ROWS
    ):
        result = runner.invoke(app, ["positions"])
    assert "AAPL" in result.output
    assert "MSFT" in result.output


def test_positions_human_contains_avg_price(monkeypatch, tmp_path):
    """HUMAN output must show formatted average prices."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_positions", return_value=_POSITION_ROWS
    ):
        result = runner.invoke(app, ["positions"])
    assert "200.00" in result.output
    assert "400.00" in result.output


def test_positions_human_contains_column_headers(monkeypatch, tmp_path):
    """HUMAN output must show all expected column headers."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_positions", return_value=_POSITION_ROWS
    ):
        result = runner.invoke(app, ["positions"])
    for header in ("Account", "Symbol", "Qty", "Avg Price", "Market Value", "Day P&L", "Total P&L"):
        assert header in result.output, f"Missing column header: {header!r}"


def test_positions_human_colors_positive_pnl():
    """HUMAN render_positions must emit ANSI green for positive P&L (AAPL).

    Color rendering is tested directly on the renderer because the CliRunner
    captures output through a non-TTY pipe and Rich strips ANSI codes in that
    context.  The existing test_output_accounts.py covers the same invariant;
    this test duplicates it here so the characterization suite is self-contained.
    """
    from schwab_cli.output.accounts import render_positions
    from schwab_cli.output.format import Format

    out = render_positions(_POSITION_ROWS, Format.HUMAN)
    assert "\x1b[32m" in out  # standard ANSI green for positive P&L


def test_positions_human_colors_negative_pnl():
    """HUMAN render_positions must emit ANSI red for negative P&L (MSFT day P&L).

    Color rendering is tested directly on the renderer — see the docstring of
    test_positions_human_colors_positive_pnl for the rationale.
    """
    from schwab_cli.output.accounts import render_positions
    from schwab_cli.output.format import Format

    out = render_positions(_POSITION_ROWS, Format.HUMAN)
    assert "\x1b[31m" in out  # standard ANSI red for negative P&L


def test_positions_human_synthetic_account_key_surfaces(monkeypatch, tmp_path):
    """HUMAN output must show the masked account numbers from the _account key."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_positions", return_value=_POSITION_ROWS
    ):
        result = runner.invoke(app, ["positions"])
    assert "5678" in result.output
    assert "4321" in result.output


# --- 3b. positions JSON output ---


def test_positions_json_exit_code(monkeypatch, tmp_path):
    """JSON output for positions must exit 0."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_positions", return_value=_POSITION_ROWS
    ):
        result = runner.invoke(app, ["positions", "--json"])
    assert result.exit_code == 0, result.output


def test_positions_json_is_list(monkeypatch, tmp_path):
    """JSON output must be a list of two rows."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_positions", return_value=_POSITION_ROWS
    ):
        result = runner.invoke(app, ["positions", "--json"])
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    assert len(data) == 2


def test_positions_json_row_keys(monkeypatch, tmp_path):
    """JSON rows must contain exactly the golden set of keys."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_positions", return_value=_POSITION_ROWS
    ):
        result = runner.invoke(app, ["positions", "--json"])
    data = json.loads(result.stdout)
    assert set(data[0].keys()) == _GOLDEN_POSITIONS_JSON_KEYS
    assert set(data[1].keys()) == _GOLDEN_POSITIONS_JSON_KEYS


def test_positions_json_aapl_row_values(monkeypatch, tmp_path):
    """JSON AAPL row must contain exact golden field values."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_positions", return_value=_POSITION_ROWS
    ):
        result = runner.invoke(app, ["positions", "--json"])
    data = json.loads(result.stdout)
    row = data[0]
    assert row["account"] == "12345678"
    assert row["symbol"] == "AAPL"
    assert row["qty"] == 10.0
    assert row["avgPrice"] == 200.0
    assert row["marketValue"] == 2321.4
    assert row["dayPnL"] == 4.2
    assert row["totalPnL"] == 321.4


def test_positions_json_msft_row_values(monkeypatch, tmp_path):
    """JSON MSFT row must contain exact golden field values including negative P&L."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_positions", return_value=_POSITION_ROWS
    ):
        result = runner.invoke(app, ["positions", "--json"])
    data = json.loads(result.stdout)
    row = data[1]
    assert row["account"] == "87654321"
    assert row["symbol"] == "MSFT"
    assert row["qty"] == 5.0
    assert row["avgPrice"] == 400.0
    assert row["marketValue"] == 2050.0
    assert row["dayPnL"] == -10.0
    assert row["totalPnL"] == 50.0


def test_positions_json_synthetic_account_key_in_account_field(monkeypatch, tmp_path):
    """JSON ``account`` field must equal the synthetic ``_account`` key from the API."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_positions", return_value=_POSITION_ROWS
    ):
        result = runner.invoke(app, ["positions", "--json"])
    data = json.loads(result.stdout)
    accounts = {row["account"] for row in data}
    assert accounts == {"12345678", "87654321"}


def test_positions_json_no_ansi_codes(monkeypatch, tmp_path):
    """JSON output must not contain ANSI escape codes."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_positions", return_value=_POSITION_ROWS
    ):
        result = runner.invoke(app, ["positions", "--json"])
    assert "\x1b[" not in result.stdout


# --- 3c. positions MD output ---


def test_positions_md_exit_code(monkeypatch, tmp_path):
    """MD output for positions must exit 0."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_positions", return_value=_POSITION_ROWS
    ):
        result = runner.invoke(app, ["positions", "--md"])
    assert result.exit_code == 0, result.output


def test_positions_md_exact_header_line(monkeypatch, tmp_path):
    """MD output must contain the exact golden header line."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_positions", return_value=_POSITION_ROWS
    ):
        result = runner.invoke(app, ["positions", "--md"])
    assert _GOLDEN_POSITIONS_MD_HEADER in result.stdout


def test_positions_md_exact_aapl_row(monkeypatch, tmp_path):
    """MD output must contain the exact golden AAPL data row."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_positions", return_value=_POSITION_ROWS
    ):
        result = runner.invoke(app, ["positions", "--md"])
    assert _GOLDEN_POSITIONS_MD_AAPL_ROW in result.stdout


def test_positions_md_exact_msft_row(monkeypatch, tmp_path):
    """MD output must contain the exact golden MSFT data row (negative day P&L)."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_positions", return_value=_POSITION_ROWS
    ):
        result = runner.invoke(app, ["positions", "--md"])
    assert _GOLDEN_POSITIONS_MD_MSFT_ROW in result.stdout


def test_positions_md_no_ansi_codes(monkeypatch, tmp_path):
    """MD output must not contain ANSI escape codes."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_positions", return_value=_POSITION_ROWS
    ):
        result = runner.invoke(app, ["positions", "--md"])
    assert "\x1b[" not in result.stdout


# --- 3d. positions with account filter ---


def test_positions_filtered_passes_account_number_to_api(monkeypatch, tmp_path):
    """Providing an account number must forward it to get_positions."""
    _prep(monkeypatch, tmp_path)
    captured: list = []

    def fake_get_positions(client, account_number):
        captured.append(account_number)
        return _POSITION_ROWS

    with patch(
        "schwab_cli.commands.accounts.get_positions", side_effect=fake_get_positions
    ):
        result = runner.invoke(app, ["positions", "5678"])
    assert result.exit_code == 0, result.output
    assert captured == ["5678"]


def test_positions_filtered_passes_none_when_no_account(monkeypatch, tmp_path):
    """Omitting account number must forward None to get_positions."""
    _prep(monkeypatch, tmp_path)
    captured: list = []

    def fake_get_positions(client, account_number):
        captured.append(account_number)
        return _POSITION_ROWS

    with patch(
        "schwab_cli.commands.accounts.get_positions", side_effect=fake_get_positions
    ):
        result = runner.invoke(app, ["positions"])
    assert result.exit_code == 0, result.output
    assert captured == [None]


def test_positions_filtered_json_contains_correct_rows(monkeypatch, tmp_path):
    """Filtered positions must still render correctly as JSON."""
    _prep(monkeypatch, tmp_path)
    single_row = [_POSITION_ROWS[0]]  # only AAPL

    with patch(
        "schwab_cli.commands.accounts.get_positions", return_value=single_row
    ):
        result = runner.invoke(app, ["positions", "5678", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert len(data) == 1
    assert data[0]["symbol"] == "AAPL"
    assert data[0]["account"] == "12345678"


# ===========================================================================
# 4. Error / exit-code tests — all three commands
# ===========================================================================


# --- 4a. Both flags (--json + --md) → exit 2 ---


@pytest.mark.parametrize(
    "cmd_args",
    [
        ["accounts", "--json", "--md"],
        ["account", "12345678", "--json", "--md"],
        ["positions", "--json", "--md"],
    ],
)
def test_both_flags_exit_2(monkeypatch, tmp_path, cmd_args):
    """--json and --md together must exit 2 for all three commands."""
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, cmd_args)
    assert result.exit_code == 2, f"{cmd_args}: {result.output}"


@pytest.mark.parametrize(
    "cmd_args",
    [
        ["accounts", "--json", "--md"],
        ["account", "12345678", "--json", "--md"],
        ["positions", "--json", "--md"],
    ],
)
def test_both_flags_mutually_exclusive_message(monkeypatch, tmp_path, cmd_args):
    """--json and --md together must print 'mutually exclusive' for all three commands."""
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, cmd_args)
    assert "mutually exclusive" in result.output, f"{cmd_args}: {result.output}"


# --- 4b. Missing config → exit 1 "No config" ---


@pytest.mark.parametrize(
    "cmd_args",
    [
        ["accounts"],
        ["account", "12345678"],
        ["positions"],
    ],
)
def test_no_config_exit_1(monkeypatch, tmp_path, cmd_args):
    """Missing config must exit 1 for all three commands."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    result = runner.invoke(app, cmd_args)
    assert result.exit_code == 1, f"{cmd_args}: {result.output}"


@pytest.mark.parametrize(
    "cmd_args",
    [
        ["accounts"],
        ["account", "12345678"],
        ["positions"],
    ],
)
def test_no_config_message(monkeypatch, tmp_path, cmd_args):
    """Missing config must print 'No config' for all three commands."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    result = runner.invoke(app, cmd_args)
    assert "No config" in result.output, f"{cmd_args}: {result.output}"


# --- 4c. Missing session → exit 1 "No session" ---


@pytest.mark.parametrize(
    "cmd_args",
    [
        ["accounts"],
        ["account", "12345678"],
        ["positions"],
    ],
)
def test_no_session_exit_1(monkeypatch, tmp_path, cmd_args):
    """Config present but missing session must exit 1 for all three commands."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save_config(
        Config(
            client_id="cid",
            client_secret="csec",
            redirect_uri="https://127.0.0.1:8443",
        )
    )
    result = runner.invoke(app, cmd_args)
    assert result.exit_code == 1, f"{cmd_args}: {result.output}"


@pytest.mark.parametrize(
    "cmd_args",
    [
        ["accounts"],
        ["account", "12345678"],
        ["positions"],
    ],
)
def test_no_session_message(monkeypatch, tmp_path, cmd_args):
    """Missing session must print 'No session' for all three commands."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save_config(
        Config(
            client_id="cid",
            client_secret="csec",
            redirect_uri="https://127.0.0.1:8443",
        )
    )
    result = runner.invoke(app, cmd_args)
    assert "No session" in result.output, f"{cmd_args}: {result.output}"


# --- 4d. SessionExpired → exit 1 ---


def test_accounts_session_expired_exit_1(monkeypatch, tmp_path):
    """SessionExpired on list_accounts must exit 1."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.list_accounts",
        side_effect=SessionExpired("Session expired. Run `schwab_cli auth --force`."),
    ):
        result = runner.invoke(app, ["accounts"])
    assert result.exit_code == 1


def test_accounts_session_expired_message(monkeypatch, tmp_path):
    """SessionExpired on list_accounts must surface the exception message."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.list_accounts",
        side_effect=SessionExpired("Session expired. Run `schwab_cli auth --force`."),
    ):
        result = runner.invoke(app, ["accounts"])
    assert "Session expired" in result.output


def test_account_session_expired_exit_1(monkeypatch, tmp_path):
    """SessionExpired on get_account must exit 1."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_account",
        side_effect=SessionExpired("Session expired. Run `schwab_cli auth --force`."),
    ):
        result = runner.invoke(app, ["account", "12345678"])
    assert result.exit_code == 1


def test_account_session_expired_message(monkeypatch, tmp_path):
    """SessionExpired on get_account must surface the exception message."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_account",
        side_effect=SessionExpired("Session expired. Run `schwab_cli auth --force`."),
    ):
        result = runner.invoke(app, ["account", "12345678"])
    assert "Session expired" in result.output


def test_positions_session_expired_exit_1(monkeypatch, tmp_path):
    """SessionExpired on get_positions must exit 1."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_positions",
        side_effect=SessionExpired("Session expired. Run `schwab_cli auth --force`."),
    ):
        result = runner.invoke(app, ["positions"])
    assert result.exit_code == 1


def test_positions_session_expired_message(monkeypatch, tmp_path):
    """SessionExpired on get_positions must surface the exception message."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_positions",
        side_effect=SessionExpired("Session expired. Run `schwab_cli auth --force`."),
    ):
        result = runner.invoke(app, ["positions"])
    assert "Session expired" in result.output


# --- 4e. ApiError → exit 1 with message ---


def test_accounts_api_error_exit_1(monkeypatch, tmp_path):
    """ApiError on list_accounts must exit 1."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.list_accounts",
        side_effect=ApiError("500 internal server error"),
    ):
        result = runner.invoke(app, ["accounts"])
    assert result.exit_code == 1


def test_accounts_api_error_message(monkeypatch, tmp_path):
    """ApiError on list_accounts must surface the error message."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.list_accounts",
        side_effect=ApiError("500 internal server error"),
    ):
        result = runner.invoke(app, ["accounts"])
    assert "500" in result.output


def test_account_api_error_exit_1(monkeypatch, tmp_path):
    """ApiError on get_account must exit 1."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_account",
        side_effect=ApiError("Account '99' not found. Available: ...5678."),
    ):
        result = runner.invoke(app, ["account", "99"])
    assert result.exit_code == 1


def test_account_api_error_message(monkeypatch, tmp_path):
    """ApiError on get_account must surface 'not found' in output."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_account",
        side_effect=ApiError("Account '99' not found. Available: ...5678."),
    ):
        result = runner.invoke(app, ["account", "99"])
    assert "not found" in result.output


def test_positions_api_error_exit_1(monkeypatch, tmp_path):
    """ApiError on get_positions must exit 1."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_positions",
        side_effect=ApiError("503 Service Unavailable"),
    ):
        result = runner.invoke(app, ["positions"])
    assert result.exit_code == 1


def test_positions_api_error_message(monkeypatch, tmp_path):
    """ApiError on get_positions must surface the error message in output."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.get_positions",
        side_effect=ApiError("503 Service Unavailable"),
    ):
        result = runner.invoke(app, ["positions"])
    assert "503" in result.output
