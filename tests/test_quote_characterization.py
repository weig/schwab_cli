"""Characterization tests for the `schwab quote` command.

These tests pin the CURRENT observable behaviour of the quote command end-to-end
so that a future refactor can be proven to produce identical results.

Seam used: ``schwab_cli.api.client.SchwabClient.get`` — patching at this level
exercises the full stack (ticker normalisation, payload shaping, rendering) while
avoiding real HTTP calls.  This seam will survive the upcoming service-layer
refactor because the new layer will still route through ``SchwabClient.get``.

Golden values were captured by running the current code and recording its output.
Do NOT change these values without first verifying that the production code changed.
"""

from __future__ import annotations

import json
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
    """Set up an isolated HOME with a valid config + non-expired session."""
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
            expires_at=1_000_000,
            refresh_token_expires_at=2_000_000,
        )
    )


# ---------------------------------------------------------------------------
# Canned Schwab payloads
# ---------------------------------------------------------------------------

_AAPL_QUOTE = {
    "lastPrice": 232.14,
    "netChange": 1.20,
    "netPercentChangeInDouble": 0.5194,
    "bidPrice": 232.00,
    "askPrice": 232.28,
    "totalVolume": 1_000_000,
}

_SINGLE_PAYLOAD = {
    "AAPL": {"symbol": "AAPL", "quote": _AAPL_QUOTE},
}

_MSFT_QUOTE = {
    "lastPrice": 451.22,
    "netChange": -2.50,
    "netPercentChangeInDouble": -0.5517,
    "bidPrice": 451.10,
    "askPrice": 451.30,
    "totalVolume": 5_500_000,
}

_MULTI_PAYLOAD = {
    "AAPL": {"symbol": "AAPL", "quote": _AAPL_QUOTE},
    "MSFT": {"symbol": "MSFT", "quote": _MSFT_QUOTE},
}

_INVALID_PAYLOAD = {
    "errors": {"invalidSymbols": ["NOTREAL"]},
    "AAPL": {"symbol": "AAPL", "quote": _AAPL_QUOTE},
}

_BRKB_PAYLOAD = {
    "BRK/B": {
        "symbol": "BRK/B",
        "quote": {
            "lastPrice": 455.67,
            "netChange": 2.10,
            "netPercentChangeInDouble": 0.4631,
            "bidPrice": 455.50,
            "askPrice": 455.75,
            "totalVolume": 750_000,
        },
    }
}

# ---------------------------------------------------------------------------
# 1. Golden human output — single symbol
# ---------------------------------------------------------------------------

def test_human_output_contains_symbol(monkeypatch, tmp_path):
    """Human table must contain the ticker symbol."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["quote", "AAPL"])
    assert result.exit_code == 0, result.output
    assert "AAPL" in result.output


def test_human_output_contains_last_price(monkeypatch, tmp_path):
    """Human table must contain the formatted last price."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["quote", "AAPL"])
    assert result.exit_code == 0, result.output
    assert "232.14" in result.output


def test_human_output_contains_table_title(monkeypatch, tmp_path):
    """Human table must have the 'Quotes' title."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["quote", "AAPL"])
    assert result.exit_code == 0, result.output
    assert "Quotes" in result.output


def test_human_output_contains_column_headers(monkeypatch, tmp_path):
    """Human table must have all expected column headers."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["quote", "AAPL"])
    assert result.exit_code == 0, result.output
    for header in ("Symbol", "Last", "Change", "Bid", "Ask", "Volume"):
        assert header in result.output, f"Missing column header: {header}"


# ---------------------------------------------------------------------------
# 2. Golden JSON output — exact row dict shape and values
# ---------------------------------------------------------------------------

def test_json_output_exit_code(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["quote", "AAPL", "--json"])
    assert result.exit_code == 0, result.output


def test_json_output_is_list(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["quote", "AAPL", "--json"])
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    assert len(data) == 1


def test_json_output_exact_row_shape(monkeypatch, tmp_path):
    """JSON row must contain exactly these keys — no more, no fewer (for valid symbols)."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["quote", "AAPL", "--json"])
    data = json.loads(result.stdout)
    row = data[0]
    assert set(row.keys()) == {"symbol", "last", "change", "changePct", "bid", "ask", "volume"}


def test_json_output_exact_values(monkeypatch, tmp_path):
    """JSON row values must match the payload exactly (golden values)."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["quote", "AAPL", "--json"])
    data = json.loads(result.stdout)
    row = data[0]
    assert row["symbol"] == "AAPL"
    assert row["last"] == 232.14
    assert row["change"] == 1.2          # Golden: float 1.2 (not 1.20)
    assert row["changePct"] == 0.5194
    assert row["bid"] == 232.0           # Golden: float 232.0
    assert row["ask"] == 232.28
    assert row["volume"] == 1_000_000


# ---------------------------------------------------------------------------
# 3. Golden MD output — exact header line and data row
# ---------------------------------------------------------------------------

# Golden values captured from current code:
_GOLDEN_MD_HEADER = "| Symbol | Last | Change | Change% | Bid | Ask | Volume |"
_GOLDEN_MD_SEPARATOR = "|--------|------|--------|---------|-----|-----|--------|"
_GOLDEN_MD_AAPL_ROW = "| AAPL | 232.14 | 1.20 | 0.52 | 232.00 | 232.28 | 1,000,000 |"


def test_md_output_exit_code(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["quote", "AAPL", "--md"])
    assert result.exit_code == 0, result.output


def test_md_output_exact_header_line(monkeypatch, tmp_path):
    """MD output must contain the exact golden header line."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["quote", "AAPL", "--md"])
    assert _GOLDEN_MD_HEADER in result.stdout


def test_md_output_exact_separator_line(monkeypatch, tmp_path):
    """MD output must contain the exact golden separator line."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["quote", "AAPL", "--md"])
    assert _GOLDEN_MD_SEPARATOR in result.stdout


def test_md_output_exact_data_row(monkeypatch, tmp_path):
    """MD data row must match the exact golden string (including _fmt_num formatting)."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["quote", "AAPL", "--md"])
    assert _GOLDEN_MD_AAPL_ROW in result.stdout


def test_md_output_volume_has_no_decimals(monkeypatch, tmp_path):
    """Volume in MD output must be formatted with 0 decimals and comma separators."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["quote", "AAPL", "--md"])
    # 1000000 → "1,000,000" (no decimal point)
    assert "1,000,000 |" in result.stdout
    assert "1,000,000." not in result.stdout


# ---------------------------------------------------------------------------
# 4. Multi-symbol ordering preserved
# ---------------------------------------------------------------------------

def test_multi_symbol_json_ordering(monkeypatch, tmp_path):
    """JSON output must preserve the input order: AAPL first, then MSFT."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_MULTI_PAYLOAD):
        result = runner.invoke(app, ["quote", "AAPL", "MSFT", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert len(data) == 2
    assert data[0]["symbol"] == "AAPL"
    assert data[1]["symbol"] == "MSFT"


def test_multi_symbol_json_reversed_ordering(monkeypatch, tmp_path):
    """JSON output preserves CLI argument order even when reversed."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_MULTI_PAYLOAD):
        result = runner.invoke(app, ["quote", "MSFT", "AAPL", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data[0]["symbol"] == "MSFT"
    assert data[1]["symbol"] == "AAPL"


def test_multi_symbol_md_row_order(monkeypatch, tmp_path):
    """MD output must emit AAPL row before MSFT row when given that order."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_MULTI_PAYLOAD):
        result = runner.invoke(app, ["quote", "AAPL", "MSFT", "--md"])
    assert result.exit_code == 0, result.output
    aapl_pos = result.stdout.index("| AAPL |")
    msft_pos = result.stdout.index("| MSFT |")
    assert aapl_pos < msft_pos


def test_multi_symbol_md_exact_rows(monkeypatch, tmp_path):
    """MD output must contain the exact golden rows for both symbols."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_MULTI_PAYLOAD):
        result = runner.invoke(app, ["quote", "AAPL", "MSFT", "--md"])
    assert "| AAPL | 232.14 | 1.20 | 0.52 | 232.00 | 232.28 | 1,000,000 |" in result.stdout
    assert "| MSFT | 451.22 | -2.50 | -0.55 | 451.10 | 451.30 | 5,500,000 |" in result.stdout


def test_multi_symbol_human_output(monkeypatch, tmp_path):
    """Human output must show both symbols."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_MULTI_PAYLOAD):
        result = runner.invoke(app, ["quote", "AAPL", "MSFT"])
    assert result.exit_code == 0, result.output
    assert "AAPL" in result.output
    assert "MSFT" in result.output
    assert "232.14" in result.output
    assert "451.22" in result.output


# ---------------------------------------------------------------------------
# 5. Invalid-symbol row renders with error "invalid symbol"
# ---------------------------------------------------------------------------

def test_invalid_symbol_json_has_error_key(monkeypatch, tmp_path):
    """Invalid symbol row in JSON must have error='invalid symbol'."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_INVALID_PAYLOAD):
        result = runner.invoke(app, ["quote", "AAPL", "NOTREAL", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    notreal_row = next(r for r in data if r["symbol"] == "NOTREAL")
    assert notreal_row["error"] == "invalid symbol"


def test_invalid_symbol_json_null_fields(monkeypatch, tmp_path):
    """Invalid symbol row must have null for all numeric fields."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_INVALID_PAYLOAD):
        result = runner.invoke(app, ["quote", "AAPL", "NOTREAL", "--json"])
    data = json.loads(result.stdout)
    notreal_row = next(r for r in data if r["symbol"] == "NOTREAL")
    for field in ("last", "change", "changePct", "bid", "ask", "volume"):
        assert notreal_row[field] is None, f"Expected null for {field}"


def test_invalid_symbol_json_valid_row_unaffected(monkeypatch, tmp_path):
    """Valid symbol row must still render correctly alongside an invalid one."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_INVALID_PAYLOAD):
        result = runner.invoke(app, ["quote", "AAPL", "NOTREAL", "--json"])
    data = json.loads(result.stdout)
    aapl_row = next(r for r in data if r["symbol"] == "AAPL")
    assert aapl_row["last"] == 232.14
    assert "error" not in aapl_row


def test_invalid_symbol_md_renders_dash(monkeypatch, tmp_path):
    """Invalid symbol must render em-dash cells in MD output."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_INVALID_PAYLOAD):
        result = runner.invoke(app, ["quote", "AAPL", "NOTREAL", "--md"])
    assert result.exit_code == 0, result.output
    # Golden: "| NOTREAL | — | — | — | — | — | — |"
    assert "| NOTREAL | — | — | — | — | — | — |" in result.stdout


def test_invalid_symbol_human_shows_symbol(monkeypatch, tmp_path):
    """Invalid symbol must still appear in the human table."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_INVALID_PAYLOAD):
        result = runner.invoke(app, ["quote", "AAPL", "NOTREAL"])
    assert result.exit_code == 0, result.output
    assert "NOTREAL" in result.output


# ---------------------------------------------------------------------------
# 6. Ticker normalization: BRK.B → BRK/B
# ---------------------------------------------------------------------------

def test_ticker_normalization_dot_to_slash(monkeypatch, tmp_path):
    """'BRK.B' on the CLI must normalize to 'BRK/B' before hitting the API."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_BRKB_PAYLOAD) as mock_get:
        result = runner.invoke(app, ["quote", "BRK.B", "--json"])
    assert result.exit_code == 0, result.output
    # The SchwabClient.get call must have received 'BRK/B' in the params.
    # call_args.kwargs holds the keyword arguments passed to the method.
    symbols_param = mock_get.call_args.kwargs.get("params", {}).get("symbols", "")
    assert "BRK/B" in symbols_param
    assert "BRK.B" not in symbols_param


def test_ticker_normalization_json_key_is_normalized(monkeypatch, tmp_path):
    """JSON output symbol field must be 'BRK/B', not 'BRK.B'."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_BRKB_PAYLOAD):
        result = runner.invoke(app, ["quote", "BRK.B", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data[0]["symbol"] == "BRK/B"


def test_ticker_normalization_already_normalized(monkeypatch, tmp_path):
    """'BRK/B' passed directly must also work (idempotent normalization)."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_BRKB_PAYLOAD):
        result = runner.invoke(app, ["quote", "BRK/B", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data[0]["symbol"] == "BRK/B"


# ---------------------------------------------------------------------------
# 7. Error paths and exact exit codes
# ---------------------------------------------------------------------------

def test_both_flags_exit_code_2(monkeypatch, tmp_path):
    """--json and --md together must exit with code 2."""
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["quote", "AAPL", "--json", "--md"])
    assert result.exit_code == 2


def test_both_flags_mutually_exclusive_message(monkeypatch, tmp_path):
    """--json and --md together must print 'mutually exclusive'."""
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["quote", "AAPL", "--json", "--md"])
    assert "mutually exclusive" in result.output


def test_no_config_exit_code_1(monkeypatch, tmp_path):
    """Missing config must exit with code 1."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    # No config saved — tmp_path is empty
    result = runner.invoke(app, ["quote", "AAPL"])
    assert result.exit_code == 1


def test_no_config_message(monkeypatch, tmp_path):
    """Missing config must print a message containing 'No config'."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    result = runner.invoke(app, ["quote", "AAPL"])
    assert "No config" in result.output


def test_no_session_exit_code_1(monkeypatch, tmp_path):
    """Config present but missing session must exit with code 1."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save_config(
        Config(
            client_id="cid",
            client_secret="csec",
            redirect_uri="https://127.0.0.1:8443",
        )
    )
    # No session saved
    result = runner.invoke(app, ["quote", "AAPL"])
    assert result.exit_code == 1


def test_no_session_message(monkeypatch, tmp_path):
    """Missing session must print a message containing 'No session'."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save_config(
        Config(
            client_id="cid",
            client_secret="csec",
            redirect_uri="https://127.0.0.1:8443",
        )
    )
    result = runner.invoke(app, ["quote", "AAPL"])
    assert "No session" in result.output


def test_session_expired_exit_code_1(monkeypatch, tmp_path):
    """SessionExpired raised from SchwabClient.get must exit with code 1."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.api.client.SchwabClient.get",
        side_effect=SessionExpired("Session expired. Run `schwab_cli auth --force`."),
    ):
        result = runner.invoke(app, ["quote", "AAPL"])
    assert result.exit_code == 1


def test_session_expired_message(monkeypatch, tmp_path):
    """SessionExpired must print a message containing 'Session expired'."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.api.client.SchwabClient.get",
        side_effect=SessionExpired("Session expired. Run `schwab_cli auth --force`."),
    ):
        result = runner.invoke(app, ["quote", "AAPL"])
    assert "Session expired" in result.output


def test_api_error_exit_code_1(monkeypatch, tmp_path):
    """ApiError raised from SchwabClient.get must exit with code 1."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.api.client.SchwabClient.get",
        side_effect=ApiError("500 Internal Server Error"),
    ):
        result = runner.invoke(app, ["quote", "AAPL"])
    assert result.exit_code == 1


def test_api_error_message_contains_status(monkeypatch, tmp_path):
    """ApiError message must appear in output (status code visible)."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.api.client.SchwabClient.get",
        side_effect=ApiError("500 Internal Server Error"),
    ):
        result = runner.invoke(app, ["quote", "AAPL"])
    assert "500" in result.output
