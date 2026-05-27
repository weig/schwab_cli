"""Characterization tests for the ``schwab transactions`` command.

These tests pin the CURRENT observable behaviour end-to-end so that the
upcoming service-layer (strangler-fig) migration can be proven
behaviour-preserving without touching production code.

Stable seam (patch target):
    ``schwab_cli.api.transactions_cache.fetch_cached``

    After migration this name will be re-pointed to
    ``schwab_cli.api.transactions_cache.fetch_cached``.
    The golden assertions in this file must NOT change — only the
    patch-target string will be updated.

    A second seam, ``schwab_cli.api.transactions.get_all_transactions``,
    is the legacy surface imported (noqa: F401) in the commands module.
    Tests that need to verify the command does NOT call the raw API patch
    both names simultaneously.

Golden values were captured by running the current code and recording its
output verbatim.  Do NOT alter golden constants without first verifying
that the production code changed intentionally.
"""

from __future__ import annotations

import json
import time
from unittest.mock import call, patch

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
# Fixture helpers
# ---------------------------------------------------------------------------

_FETCH_CACHED = "schwab_cli.api.transactions_cache.fetch_cached"
_GET_ALL_TXN = "schwab_cli.api.transactions.get_all_transactions"


def _prep(monkeypatch, tmp_path) -> None:
    """Isolated HOME with a valid config and a non-expired session.

    ``expires_at`` is set to now+3600 so the service-layer auth path
    (``service.auth.get_session``) will NOT attempt a real
    ``oauth.refresh`` — it only refreshes when the access token looks
    expired.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path / "storage"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)
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
# Canned multi-row payload
#
# Three transactions: TRADE buy AMZN, DIVIDEND KO (currency leg), TRADE sell
# NVDA.  Covers equity TRADE, DIVIDEND_OR_INTEREST with CURRENCY_USD leg, and
# a second TRADE so the type-filter tests have something to cull.
# ---------------------------------------------------------------------------

_CANNED_PAYLOAD = [
    {
        "_account": "12340756",
        "activityId": 1,
        "time": "2026-04-15T10:00:00+0000",
        "type": "TRADE",
        "netAmount": -1055.30,
        "transferItems": [
            {
                "instrument": {"assetType": "EQUITY", "symbol": "AMZN"},
                "amount": 5.0,
                "cost": -1055.30,
                "price": 211.06,
                "positionEffect": "OPENING",
            },
        ],
    },
    {
        "_account": "12340756",
        "activityId": 2,
        "time": "2026-04-16T10:00:00+0000",
        "type": "DIVIDEND_OR_INTEREST",
        "description": "THE COCA-COLA CO",
        "netAmount": 22.31,
        "transferItems": [
            {
                "instrument": {"assetType": "CURRENCY", "symbol": "CURRENCY_USD"},
                "amount": 22.31,
                "cost": 0.0,
                "price": 0.0,
            },
        ],
    },
    {
        "_account": "12340756",
        "activityId": 3,
        "time": "2026-04-17T10:00:00+0000",
        "type": "TRADE",
        "netAmount": 1258.50,
        "transferItems": [
            {
                "instrument": {"assetType": "EQUITY", "symbol": "NVDA"},
                "amount": -3.0,
                "cost": 1258.50,
                "price": 419.50,
                "positionEffect": "CLOSING",
            },
        ],
    },
]

# ---------------------------------------------------------------------------
# Golden constants
# (captured by running current code — see module docstring)
# ---------------------------------------------------------------------------

# JSON — top-level keys of each shaped row
_GOLDEN_JSON_ROW_KEYS = {
    "account",
    "date",
    "time",
    "type",
    "symbol",
    "qty",
    "price",
    "effect",
    "netAmount",
}

# JSON — row 0 (AMZN buy, sorted first by time)
_GOLDEN_JSON_R0_ACCOUNT = "12340756"
_GOLDEN_JSON_R0_DATE = "2026-04-15"
_GOLDEN_JSON_R0_TIME = "2026-04-15T10:00:00+0000"
_GOLDEN_JSON_R0_TYPE = "TRADE"
_GOLDEN_JSON_R0_SYMBOL = "AMZN"
_GOLDEN_JSON_R0_QTY = 5.0
_GOLDEN_JSON_R0_PRICE = 211.06
_GOLDEN_JSON_R0_EFFECT = "OPENING"
_GOLDEN_JSON_R0_NET = -1055.3

# JSON — row 1 (dividend, CURRENCY_USD leg → description used as symbol)
_GOLDEN_JSON_R1_TYPE = "DIVIDEND_OR_INTEREST"
_GOLDEN_JSON_R1_SYMBOL = "THE COCA-COLA CO"
_GOLDEN_JSON_R1_EFFECT = None
_GOLDEN_JSON_R1_NET = 22.31

# JSON — row 2 (NVDA sell)
_GOLDEN_JSON_R2_SYMBOL = "NVDA"
_GOLDEN_JSON_R2_QTY = -3.0
_GOLDEN_JSON_R2_EFFECT = "CLOSING"
_GOLDEN_JSON_R2_NET = 1258.5

# MD with show_account=True (no --account flag, all-accounts view)
_GOLDEN_MD_HEADING_3 = "# Transactions — 3 rows"
_GOLDEN_MD_NET_CASHFLOW = "**Net cashflow:** +225.51"
_GOLDEN_MD_TABLE_HEADER_WITH_ACCT = (
    "| Date | Account | Type | Symbol | Effect | Qty | Price | Net |"
)
_GOLDEN_MD_TABLE_SEP_WITH_ACCT = (
    "|------|---------|------|--------|--------|-----|-------|-----|"
)
_GOLDEN_MD_ROW_AMZN = (
    "| 2026-04-15 | ...0756 | TRADE | AMZN | OPENING | +5 | 211.06 | -1,055.30 |"
)
_GOLDEN_MD_ROW_NVDA = (
    "| 2026-04-17 | ...0756 | TRADE | NVDA | CLOSING | -3 | 419.50 | +1,258.50 |"
)

# MD without account column (--account flag supplied)
_GOLDEN_MD_TABLE_HEADER_NO_ACCT = (
    "| Date | Type | Symbol | Effect | Qty | Price | Net |"
)
_GOLDEN_MD_TABLE_SEP_NO_ACCT = "|------|------|--------|--------|-----|-------|-----|"
_GOLDEN_MD_ROW_AMZN_NO_ACCT = (
    "| 2026-04-15 | TRADE | AMZN | OPENING | +5 | 211.06 | -1,055.30 |"
)

# HUMAN — key substrings (ANSI-free equivalents)
_GOLDEN_HUMAN_ROW_COUNT = "3 rows"
_GOLDEN_HUMAN_SYMBOL_AMZN = "AMZN"
_GOLDEN_HUMAN_SYMBOL_NVDA = "NVDA"
_GOLDEN_HUMAN_COL_DATE = "Date"
_GOLDEN_HUMAN_COL_TYPE = "Type"
_GOLDEN_HUMAN_COL_SYMBOL = "Symbol"
_GOLDEN_HUMAN_COL_QTY = "Qty"
_GOLDEN_HUMAN_COL_PRICE = "Price"
_GOLDEN_HUMAN_COL_NET = "Net"

# Empty result golden substrings
_GOLDEN_EMPTY_HUMAN = "(no transactions in range)"
_GOLDEN_EMPTY_HUMAN_HEADER = "Transactions — 0 rows"
_GOLDEN_EMPTY_MD = "_(no transactions in range)_"
_GOLDEN_EMPTY_MD_HEADING = "# Transactions — 0 rows"
_GOLDEN_EMPTY_MD_NET = "**Net cashflow:** +0.00"


# ===========================================================================
# 1. Golden HUMAN output
# ===========================================================================


class TestGoldenHumanOutput:
    """Pin the human-readable table for a known canned payload.

    Seam: patch ``schwab_cli.api.transactions_cache.fetch_cached``.
    """

    def test_exit_0(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch(_FETCH_CACHED, return_value=_CANNED_PAYLOAD):
            result = runner.invoke(
                app, ["transactions", "--range=20260415..20260418", "--type=ALL"]
            )
        assert result.exit_code == 0, result.output

    def test_header_row_count(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch(_FETCH_CACHED, return_value=_CANNED_PAYLOAD):
            result = runner.invoke(
                app, ["transactions", "--range=20260415..20260418", "--type=ALL"]
            )
        assert _GOLDEN_HUMAN_ROW_COUNT in result.output

    def test_column_headers_present(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch(_FETCH_CACHED, return_value=_CANNED_PAYLOAD):
            result = runner.invoke(
                app, ["transactions", "--range=20260415..20260418", "--type=ALL"]
            )
        for col in (
            _GOLDEN_HUMAN_COL_DATE,
            _GOLDEN_HUMAN_COL_TYPE,
            _GOLDEN_HUMAN_COL_SYMBOL,
            _GOLDEN_HUMAN_COL_QTY,
            _GOLDEN_HUMAN_COL_PRICE,
            _GOLDEN_HUMAN_COL_NET,
            "Account",
        ):
            assert col in result.output, f"Missing column header: {col!r}"

    def test_contains_amzn_symbol(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch(_FETCH_CACHED, return_value=_CANNED_PAYLOAD):
            result = runner.invoke(
                app, ["transactions", "--range=20260415..20260418", "--type=ALL"]
            )
        assert _GOLDEN_HUMAN_SYMBOL_AMZN in result.output

    def test_contains_nvda_symbol(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch(_FETCH_CACHED, return_value=_CANNED_PAYLOAD):
            result = runner.invoke(
                app, ["transactions", "--range=20260415..20260418", "--type=ALL"]
            )
        assert _GOLDEN_HUMAN_SYMBOL_NVDA in result.output

    def test_contains_dividend_description(self, monkeypatch, tmp_path):
        """CURRENCY_USD leg must surface the top-level ``description`` field."""
        _prep(monkeypatch, tmp_path)
        with patch(_FETCH_CACHED, return_value=_CANNED_PAYLOAD):
            result = runner.invoke(
                app, ["transactions", "--range=20260415..20260418", "--type=ALL"]
            )
        assert "THE COCA-COLA CO" in result.output

    def test_debit_amount_in_output(self, monkeypatch, tmp_path):
        """A negative netAmount (debit) must be visible in human output."""
        _prep(monkeypatch, tmp_path)
        with patch(_FETCH_CACHED, return_value=_CANNED_PAYLOAD):
            result = runner.invoke(
                app, ["transactions", "--range=20260415..20260418", "--type=ALL"]
            )
        # CliRunner strips ANSI; pin the formatted value instead.
        # Colour is tested at the renderer unit-test level (test_output_transactions.py).
        assert "-1,055.30" in result.output

    def test_credit_amount_in_output(self, monkeypatch, tmp_path):
        """A positive netAmount (credit) must be visible in human output."""
        _prep(monkeypatch, tmp_path)
        with patch(_FETCH_CACHED, return_value=_CANNED_PAYLOAD):
            result = runner.invoke(
                app, ["transactions", "--range=20260415..20260418", "--type=ALL"]
            )
        assert "+1,258.50" in result.output

    def test_account_column_shows_masked_suffix(self, monkeypatch, tmp_path):
        """Without --account the Account column must show ``...0756``."""
        _prep(monkeypatch, tmp_path)
        with patch(_FETCH_CACHED, return_value=_CANNED_PAYLOAD):
            result = runner.invoke(
                app, ["transactions", "--range=20260415..20260418", "--type=ALL"]
            )
        assert "...0756" in result.output

    def test_account_column_hidden_when_account_flag_given(
        self, monkeypatch, tmp_path
    ):
        """With --account the Account column is dropped (redundant)."""
        _prep(monkeypatch, tmp_path)
        with patch(_FETCH_CACHED, return_value=_CANNED_PAYLOAD):
            result = runner.invoke(
                app,
                [
                    "transactions",
                    "--account=0756",
                    "--range=20260415..20260418",
                    "--type=ALL",
                ],
            )
        assert "Account" not in result.output
        assert "0756" not in result.output

    def test_human_no_json_structure(self, monkeypatch, tmp_path):
        """Human output must NOT be parseable as JSON."""
        _prep(monkeypatch, tmp_path)
        with patch(_FETCH_CACHED, return_value=_CANNED_PAYLOAD):
            result = runner.invoke(
                app, ["transactions", "--range=20260415..20260418", "--type=ALL"]
            )
        with pytest.raises(json.JSONDecodeError):
            json.loads(result.stdout)


# ===========================================================================
# 2. Golden JSON output
# ===========================================================================


class TestGoldenJsonOutput:
    """Pin JSON structure and field values for the canned payload.

    Seam: patch ``schwab_cli.api.transactions_cache.fetch_cached``.
    """

    def _get_data(self, monkeypatch, tmp_path) -> list[dict]:
        _prep(monkeypatch, tmp_path)
        with patch(_FETCH_CACHED, return_value=_CANNED_PAYLOAD):
            result = runner.invoke(
                app,
                ["transactions", "--range=20260415..20260418", "--type=ALL", "--json"],
            )
        assert result.exit_code == 0, result.output
        return json.loads(result.stdout)

    def test_exit_0(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch(_FETCH_CACHED, return_value=_CANNED_PAYLOAD):
            result = runner.invoke(
                app,
                ["transactions", "--range=20260415..20260418", "--type=ALL", "--json"],
            )
        assert result.exit_code == 0, result.output

    def test_row_count(self, monkeypatch, tmp_path):
        data = self._get_data(monkeypatch, tmp_path)
        assert len(data) == 3

    def test_row_keys(self, monkeypatch, tmp_path):
        data = self._get_data(monkeypatch, tmp_path)
        for row in data:
            assert set(row.keys()) == _GOLDEN_JSON_ROW_KEYS

    def test_row_0_account(self, monkeypatch, tmp_path):
        data = self._get_data(monkeypatch, tmp_path)
        assert data[0]["account"] == _GOLDEN_JSON_R0_ACCOUNT

    def test_row_0_date(self, monkeypatch, tmp_path):
        data = self._get_data(monkeypatch, tmp_path)
        assert data[0]["date"] == _GOLDEN_JSON_R0_DATE

    def test_row_0_time(self, monkeypatch, tmp_path):
        data = self._get_data(monkeypatch, tmp_path)
        assert data[0]["time"] == _GOLDEN_JSON_R0_TIME

    def test_row_0_type(self, monkeypatch, tmp_path):
        data = self._get_data(monkeypatch, tmp_path)
        assert data[0]["type"] == _GOLDEN_JSON_R0_TYPE

    def test_row_0_symbol(self, monkeypatch, tmp_path):
        data = self._get_data(monkeypatch, tmp_path)
        assert data[0]["symbol"] == _GOLDEN_JSON_R0_SYMBOL

    def test_row_0_qty(self, monkeypatch, tmp_path):
        data = self._get_data(monkeypatch, tmp_path)
        assert data[0]["qty"] == pytest.approx(_GOLDEN_JSON_R0_QTY)

    def test_row_0_price(self, monkeypatch, tmp_path):
        data = self._get_data(monkeypatch, tmp_path)
        assert data[0]["price"] == pytest.approx(_GOLDEN_JSON_R0_PRICE)

    def test_row_0_effect(self, monkeypatch, tmp_path):
        data = self._get_data(monkeypatch, tmp_path)
        assert data[0]["effect"] == _GOLDEN_JSON_R0_EFFECT

    def test_row_0_net_amount(self, monkeypatch, tmp_path):
        data = self._get_data(monkeypatch, tmp_path)
        assert data[0]["netAmount"] == pytest.approx(_GOLDEN_JSON_R0_NET)

    def test_row_1_dividend_type_and_symbol(self, monkeypatch, tmp_path):
        """Dividend row with CURRENCY_USD leg must surface the description."""
        data = self._get_data(monkeypatch, tmp_path)
        assert data[1]["type"] == _GOLDEN_JSON_R1_TYPE
        assert data[1]["symbol"] == _GOLDEN_JSON_R1_SYMBOL

    def test_row_1_dividend_effect_is_null(self, monkeypatch, tmp_path):
        data = self._get_data(monkeypatch, tmp_path)
        assert data[1]["effect"] is _GOLDEN_JSON_R1_EFFECT

    def test_row_1_dividend_net(self, monkeypatch, tmp_path):
        data = self._get_data(monkeypatch, tmp_path)
        assert data[1]["netAmount"] == pytest.approx(_GOLDEN_JSON_R1_NET)

    def test_row_2_nvda_sell(self, monkeypatch, tmp_path):
        data = self._get_data(monkeypatch, tmp_path)
        assert data[2]["symbol"] == _GOLDEN_JSON_R2_SYMBOL
        assert data[2]["qty"] == pytest.approx(_GOLDEN_JSON_R2_QTY)
        assert data[2]["effect"] == _GOLDEN_JSON_R2_EFFECT
        assert data[2]["netAmount"] == pytest.approx(_GOLDEN_JSON_R2_NET)

    def test_sorted_ascending_by_time(self, monkeypatch, tmp_path):
        """shape_transactions sorts rows by time ascending."""
        data = self._get_data(monkeypatch, tmp_path)
        times = [r["time"] for r in data]
        assert times == sorted(times)

    def test_no_ansi_escapes(self, monkeypatch, tmp_path):
        """JSON output must be free of ANSI colour codes."""
        _prep(monkeypatch, tmp_path)
        with patch(_FETCH_CACHED, return_value=_CANNED_PAYLOAD):
            result = runner.invoke(
                app,
                ["transactions", "--range=20260415..20260418", "--type=ALL", "--json"],
            )
        assert "\x1b[" not in result.stdout

    def test_account_field_always_present_even_with_account_flag(
        self, monkeypatch, tmp_path
    ):
        """JSON shape must be stable regardless of --account flag."""
        _prep(monkeypatch, tmp_path)
        with patch(_FETCH_CACHED, return_value=_CANNED_PAYLOAD):
            result = runner.invoke(
                app,
                [
                    "transactions",
                    "--account=0756",
                    "--range=20260415..20260418",
                    "--type=ALL",
                    "--json",
                ],
            )
        data = json.loads(result.stdout)
        for row in data:
            assert "account" in row


# ===========================================================================
# 3. Golden MD output
# ===========================================================================


class TestGoldenMdOutput:
    """Pin Markdown rendering for the canned payload.

    Seam: patch ``schwab_cli.api.transactions_cache.fetch_cached``.
    """

    def _invoke(self, monkeypatch, tmp_path, extra_args=None):
        _prep(monkeypatch, tmp_path)
        args = [
            "transactions",
            "--range=20260415..20260418",
            "--type=ALL",
            "--md",
        ]
        if extra_args:
            args.extend(extra_args)
        with patch(_FETCH_CACHED, return_value=_CANNED_PAYLOAD):
            return runner.invoke(app, args)

    def test_exit_0(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert result.exit_code == 0, result.output

    def test_heading_exact(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _GOLDEN_MD_HEADING_3 in result.stdout

    def test_net_cashflow_exact(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _GOLDEN_MD_NET_CASHFLOW in result.stdout

    def test_table_header_with_account(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _GOLDEN_MD_TABLE_HEADER_WITH_ACCT in result.stdout

    def test_table_separator_with_account(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _GOLDEN_MD_TABLE_SEP_WITH_ACCT in result.stdout

    def test_amzn_row_exact(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _GOLDEN_MD_ROW_AMZN in result.stdout

    def test_nvda_row_exact(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _GOLDEN_MD_ROW_NVDA in result.stdout

    def test_starts_with_h1_heading(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert result.stdout.startswith("# ")

    def test_contains_pipe_table(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert any("|" in ln for ln in result.stdout.splitlines())

    def test_no_ansi_escapes(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert "\x1b[" not in result.stdout

    def test_table_header_drops_account_with_account_flag(
        self, monkeypatch, tmp_path
    ):
        """When --account is given the Account column is absent from MD."""
        result = self._invoke(monkeypatch, tmp_path, extra_args=["--account=0756"])
        assert _GOLDEN_MD_TABLE_HEADER_NO_ACCT in result.stdout
        assert "Account" not in result.stdout

    def test_separator_drops_account_with_account_flag(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path, extra_args=["--account=0756"])
        assert _GOLDEN_MD_TABLE_SEP_NO_ACCT in result.stdout

    def test_amzn_row_no_account_exact(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path, extra_args=["--account=0756"])
        assert _GOLDEN_MD_ROW_AMZN_NO_ACCT in result.stdout


# ===========================================================================
# 4. Type filter (``_filter_by_type``) — applied locally, not via API
# ===========================================================================


class TestTypeFilter:
    """``_filter_by_type`` is called after ``fetch_cached`` returns.

    fetch_cached is always called WITHOUT a type kwarg; the command
    applies the filter itself on the returned list.

    Seam: patch ``schwab_cli.api.transactions_cache.fetch_cached``.
    """

    def test_trade_filter_keeps_only_trades(self, monkeypatch, tmp_path):
        """--type=TRADE must keep only TRADE rows and drop the dividend."""
        _prep(monkeypatch, tmp_path)
        with patch(_FETCH_CACHED, return_value=_CANNED_PAYLOAD):
            result = runner.invoke(
                app,
                [
                    "transactions",
                    "--range=20260415..20260418",
                    "--type=TRADE",
                    "--json",
                ],
            )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert all(r["type"] == "TRADE" for r in data)
        assert len(data) == 2

    def test_trade_filter_drops_dividend(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch(_FETCH_CACHED, return_value=_CANNED_PAYLOAD):
            result = runner.invoke(
                app,
                [
                    "transactions",
                    "--range=20260415..20260418",
                    "--type=TRADE",
                    "--json",
                ],
            )
        data = json.loads(result.stdout)
        symbols = [r["symbol"] for r in data]
        assert "THE COCA-COLA CO" not in symbols

    def test_dividend_filter_keeps_only_dividend(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch(_FETCH_CACHED, return_value=_CANNED_PAYLOAD):
            result = runner.invoke(
                app,
                [
                    "transactions",
                    "--range=20260415..20260418",
                    "--type=DIVIDEND_OR_INTEREST",
                    "--json",
                ],
            )
        data = json.loads(result.stdout)
        assert len(data) == 1
        assert data[0]["type"] == "DIVIDEND_OR_INTEREST"
        assert data[0]["symbol"] == "THE COCA-COLA CO"

    def test_all_filter_returns_all_rows(self, monkeypatch, tmp_path):
        """--type=ALL is a no-op passthrough (all rows returned)."""
        _prep(monkeypatch, tmp_path)
        with patch(_FETCH_CACHED, return_value=_CANNED_PAYLOAD):
            result = runner.invoke(
                app,
                [
                    "transactions",
                    "--range=20260415..20260418",
                    "--type=ALL",
                    "--json",
                ],
            )
        data = json.loads(result.stdout)
        assert len(data) == 3

    def test_unknown_filter_returns_empty_json(self, monkeypatch, tmp_path):
        """An unknown type filter must return an empty list (exit 0)."""
        _prep(monkeypatch, tmp_path)
        with patch(_FETCH_CACHED, return_value=_CANNED_PAYLOAD):
            result = runner.invoke(
                app,
                [
                    "transactions",
                    "--range=20260415..20260418",
                    "--type=NO_SUCH_TYPE",
                    "--json",
                ],
            )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert data == []

    def test_filter_kwarg_never_reaches_fetch_cached(self, monkeypatch, tmp_path):
        """The type filter is local — never forwarded to the cache layer."""
        _prep(monkeypatch, tmp_path)
        with patch(_FETCH_CACHED, return_value=_CANNED_PAYLOAD) as mock_fetch:
            runner.invoke(
                app,
                [
                    "transactions",
                    "--range=20260415..20260418",
                    "--type=TRADE",
                ],
            )
        _, kwargs = mock_fetch.call_args
        assert "types" not in kwargs
        assert "type_filter" not in kwargs

    def test_default_type_is_trade(self, monkeypatch, tmp_path):
        """Default --type=TRADE is already the CLI default; pin the result."""
        _prep(monkeypatch, tmp_path)
        with patch(_FETCH_CACHED, return_value=_CANNED_PAYLOAD):
            result = runner.invoke(
                app,
                ["transactions", "--range=20260415..20260418", "--json"],
            )
        data = json.loads(result.stdout)
        # Default is TRADE-only; only 2 TRADE rows from canned payload
        assert all(r["type"] == "TRADE" for r in data)
        assert len(data) == 2

    def test_comma_separated_multi_type_filter(self, monkeypatch, tmp_path):
        """Comma-separated types selects the union."""
        _prep(monkeypatch, tmp_path)
        multi_payload = _CANNED_PAYLOAD + [
            {
                "_account": "12340756",
                "activityId": 4,
                "time": "2026-04-18T10:00:00+0000",
                "type": "JOURNAL",
                "netAmount": -2.23,
                "description": "FEE",
                "transferItems": [
                    {
                        "instrument": {
                            "assetType": "CURRENCY",
                            "symbol": "CURRENCY_USD",
                        },
                        "amount": -2.23,
                        "cost": 0.0,
                        "price": 0.0,
                    }
                ],
            }
        ]
        with patch(_FETCH_CACHED, return_value=multi_payload):
            result = runner.invoke(
                app,
                [
                    "transactions",
                    "--range=20260415..20260419",
                    "--type=TRADE,JOURNAL",
                    "--json",
                ],
            )
        data = json.loads(result.stdout)
        types = {r["type"] for r in data}
        assert types == {"TRADE", "JOURNAL"}
        assert "DIVIDEND_OR_INTEREST" not in types


# ===========================================================================
# 5. Range handling — forwarded to fetch_cached
# ===========================================================================


class TestRangeForwarding:
    """Valid range strings must be parsed and forwarded to fetch_cached.

    Seam: patch ``schwab_cli.api.transactions_cache.fetch_cached``.
    """

    def test_explicit_range_start_forwarded(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        captured: dict = {}

        def fake_fetch(client, account_number, **kwargs):
            captured.update(kwargs)
            return []

        with patch(_FETCH_CACHED, side_effect=fake_fetch):
            result = runner.invoke(
                app, ["transactions", "--range=20260101..20260430"]
            )
        assert result.exit_code == 0, result.output
        assert captured["start"].date().isoformat() == "2026-01-01"

    def test_explicit_range_end_forwarded(self, monkeypatch, tmp_path):
        """pin the end datetime forwarded to fetch_cached.

        parse_range("20260101..20260430") interprets "20260430" as
        end-of-day in the NY timezone; in UTC that is 2026-05-01T03:59:59.
        The golden value pins the *UTC* date that actually comes out.
        """
        _prep(monkeypatch, tmp_path)
        captured: dict = {}

        def fake_fetch(client, account_number, **kwargs):
            captured.update(kwargs)
            return []

        with patch(_FETCH_CACHED, side_effect=fake_fetch):
            runner.invoke(app, ["transactions", "--range=20260101..20260430"])
        # end-of-day Apr 30 NY = May 1 UTC  (NY is UTC-4 in EDT)
        assert captured["end"].date().isoformat() == "2026-05-01"

    def test_default_range_is_7_days(self, monkeypatch, tmp_path):
        """Default --range is ``-7d..now``; pin the approximate delta."""
        _prep(monkeypatch, tmp_path)
        captured: dict = {}

        def fake_fetch(client, account_number, **kwargs):
            captured.update(kwargs)
            return []

        with patch(_FETCH_CACHED, side_effect=fake_fetch):
            result = runner.invoke(app, ["transactions"])
        assert result.exit_code == 0, result.output
        delta = captured["end"] - captured["start"]
        assert 6.9 < delta.total_seconds() / 86400 < 7.1

    def test_range_short_flag(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        captured: dict = {}

        def fake_fetch(client, account_number, **kwargs):
            captured.update(kwargs)
            return []

        with patch(_FETCH_CACHED, side_effect=fake_fetch):
            result = runner.invoke(
                app, ["transactions", "-r", "20260201..20260228"]
            )
        assert result.exit_code == 0, result.output
        assert captured["start"].date().isoformat() == "2026-02-01"

    def test_refresh_flag_forwarded_true(self, monkeypatch, tmp_path):
        """``--refresh`` must set ``refresh=True`` in fetch_cached kwargs."""
        _prep(monkeypatch, tmp_path)
        captured: dict = {}

        def fake_fetch(client, account_number, **kwargs):
            captured.update(kwargs)
            return []

        with patch(_FETCH_CACHED, side_effect=fake_fetch):
            runner.invoke(
                app,
                ["transactions", "--range=20260415..20260418", "--refresh"],
            )
        assert captured.get("refresh") is True

    def test_no_refresh_flag_forwarded_false(self, monkeypatch, tmp_path):
        """Without ``--refresh`` the kwarg must be False (default)."""
        _prep(monkeypatch, tmp_path)
        captured: dict = {}

        def fake_fetch(client, account_number, **kwargs):
            captured.update(kwargs)
            return []

        with patch(_FETCH_CACHED, side_effect=fake_fetch):
            runner.invoke(
                app, ["transactions", "--range=20260415..20260418"]
            )
        assert captured.get("refresh", False) is False

    def test_account_flag_forwarded(self, monkeypatch, tmp_path):
        """``--account 0756`` must be the second positional arg to fetch_cached."""
        _prep(monkeypatch, tmp_path)
        with patch(_FETCH_CACHED, return_value=[]) as mock_fetch:
            runner.invoke(
                app,
                ["transactions", "--account=0756", "--range=20260415..20260418"],
            )
        args, _ = mock_fetch.call_args
        assert args[1] == "0756"

    def test_no_account_flag_passes_none(self, monkeypatch, tmp_path):
        """Omitting --account passes ``None`` so all accounts are queried."""
        _prep(monkeypatch, tmp_path)
        with patch(_FETCH_CACHED, return_value=[]) as mock_fetch:
            runner.invoke(app, ["transactions", "--range=20260415..20260418"])
        args, _ = mock_fetch.call_args
        assert args[1] is None

    def test_command_uses_fetch_cached_not_raw_api(self, monkeypatch, tmp_path):
        """The command must call fetch_cached; get_all_transactions is NOT called."""
        _prep(monkeypatch, tmp_path)
        with patch(_FETCH_CACHED, return_value=[]) as mock_cached, patch(
            _GET_ALL_TXN
        ) as mock_raw:
            runner.invoke(app, ["transactions", "--range=20260415..20260418"])
        assert mock_cached.called
        assert not mock_raw.called


# ===========================================================================
# 6. Range errors
# ===========================================================================


class TestRangeErrors:
    """Range parse errors must produce the correct exit codes and messages.

    These tests do NOT need fetch_cached patched because the error is
    raised before the client is built.
    """

    def test_invalid_grammar_exit_2(self, monkeypatch, tmp_path):
        """Unparseable range → exit 2."""
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(app, ["transactions", "--range=garbage"])
        assert result.exit_code == 2

    def test_invalid_grammar_message(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(app, ["transactions", "--range=garbage"])
        assert "--range must be" in result.output

    def test_inverted_range_exit_1(self, monkeypatch, tmp_path):
        """start > end (ordering error) → exit 1, not 2."""
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(
            app, ["transactions", "--range=20260601..20260101"]
        )
        assert result.exit_code == 1

    def test_inverted_range_message(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(
            app, ["transactions", "--range=20260601..20260101"]
        )
        assert "before end" in result.output

    def test_future_start_exit_1(self, monkeypatch, tmp_path):
        """A range entirely in the future → exit 1, not 2."""
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(
            app, ["transactions", "--range=20990101..20990102"]
        )
        assert result.exit_code == 1

    def test_future_start_message(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(
            app, ["transactions", "--range=20990101..20990102"]
        )
        assert "future" in result.output.lower()


# ===========================================================================
# 7. Format flag errors
# ===========================================================================


class TestFormatFlagErrors:
    """--json and --md are mutually exclusive → exit 2.

    The FormatError is raised before the range is parsed so no fetch_cached
    patch is needed.
    """

    def test_both_flags_exit_2(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(
            app, ["transactions", "--json", "--md"]
        )
        assert result.exit_code == 2

    def test_both_flags_message(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(
            app, ["transactions", "--json", "--md"]
        )
        assert "mutually exclusive" in result.output


# ===========================================================================
# 8. Auth / config errors
# ===========================================================================


class TestAuthErrors:
    """Missing config, missing session, and expired session must all
    produce exit 1 with a human-readable message.

    Seam: patch ``schwab_cli.api.transactions_cache.fetch_cached`` for
    SessionExpired and ApiError tests; no patch needed for config/session
    absence tests (the error occurs before the fetch is attempted).
    """

    def test_no_config_exit_1(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)
        result = runner.invoke(app, ["transactions"])
        assert result.exit_code == 1

    def test_no_config_message(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)
        result = runner.invoke(app, ["transactions"])
        assert "No config" in result.output

    def test_no_session_exit_1(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)
        save_config(
            Config(
                client_id="cid",
                client_secret="csec",
                redirect_uri="https://127.0.0.1:8443",
            )
        )
        result = runner.invoke(app, ["transactions"])
        assert result.exit_code == 1

    def test_no_session_message(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)
        save_config(
            Config(
                client_id="cid",
                client_secret="csec",
                redirect_uri="https://127.0.0.1:8443",
            )
        )
        result = runner.invoke(app, ["transactions"])
        assert "No session" in result.output

    def test_session_expired_exit_1(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch(
            _FETCH_CACHED,
            side_effect=SessionExpired(
                "Session expired. Run `schwab_cli auth --force`."
            ),
        ):
            result = runner.invoke(app, ["transactions"])
        assert result.exit_code == 1

    def test_session_expired_message(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch(
            _FETCH_CACHED,
            side_effect=SessionExpired(
                "Session expired. Run `schwab_cli auth --force`."
            ),
        ):
            result = runner.invoke(app, ["transactions"])
        assert "Session expired" in result.output

    def test_api_error_exit_1(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch(
            _FETCH_CACHED,
            side_effect=ApiError("503 Service Unavailable"),
        ):
            result = runner.invoke(app, ["transactions"])
        assert result.exit_code == 1

    def test_api_error_message(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch(
            _FETCH_CACHED,
            side_effect=ApiError("503 Service Unavailable"),
        ):
            result = runner.invoke(app, ["transactions"])
        assert "503" in result.output


# ===========================================================================
# 9. Empty result rendering
# ===========================================================================


class TestEmptyResult:
    """When fetch_cached returns [] the command must exit 0 and render
    gracefully — no crash, no spurious error message.

    Seam: patch ``schwab_cli.api.transactions_cache.fetch_cached``.
    """

    def test_empty_human_exit_0(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch(_FETCH_CACHED, return_value=[]):
            result = runner.invoke(app, ["transactions"])
        assert result.exit_code == 0, result.output

    def test_empty_human_message(self, monkeypatch, tmp_path):
        """Human view must contain the 'no transactions in range' notice."""
        _prep(monkeypatch, tmp_path)
        with patch(_FETCH_CACHED, return_value=[]):
            result = runner.invoke(app, ["transactions"])
        assert _GOLDEN_EMPTY_HUMAN in result.output

    def test_empty_human_header(self, monkeypatch, tmp_path):
        """Header must show 0 rows even when there are no rows."""
        _prep(monkeypatch, tmp_path)
        with patch(_FETCH_CACHED, return_value=[]):
            result = runner.invoke(app, ["transactions"])
        assert _GOLDEN_EMPTY_HUMAN_HEADER in result.output

    def test_empty_json_exit_0(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch(_FETCH_CACHED, return_value=[]):
            result = runner.invoke(app, ["transactions", "--json"])
        assert result.exit_code == 0, result.output

    def test_empty_json_is_empty_array(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch(_FETCH_CACHED, return_value=[]):
            result = runner.invoke(app, ["transactions", "--json"])
        assert json.loads(result.stdout) == []

    def test_empty_md_exit_0(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch(_FETCH_CACHED, return_value=[]):
            result = runner.invoke(app, ["transactions", "--md"])
        assert result.exit_code == 0, result.output

    def test_empty_md_heading(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch(_FETCH_CACHED, return_value=[]):
            result = runner.invoke(app, ["transactions", "--md"])
        assert _GOLDEN_EMPTY_MD_HEADING in result.stdout

    def test_empty_md_net_cashflow(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch(_FETCH_CACHED, return_value=[]):
            result = runner.invoke(app, ["transactions", "--md"])
        assert _GOLDEN_EMPTY_MD_NET in result.stdout

    def test_empty_md_no_rows_notice(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch(_FETCH_CACHED, return_value=[]):
            result = runner.invoke(app, ["transactions", "--md"])
        assert _GOLDEN_EMPTY_MD in result.stdout
