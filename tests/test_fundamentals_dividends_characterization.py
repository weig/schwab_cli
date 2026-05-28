"""Characterization tests for the ``fundamentals`` and ``dividends`` commands.

Pins the CURRENT observable behaviour end-to-end so that the strangler-fig
refactor (phase-1f-fundamentals) can be proven to produce identical results.

Stable seam: ``schwab_cli.api.client.SchwabClient.get`` — patching here
exercises the full stack (ticker normalisation, payload shaping, rendering)
while avoiding real HTTP calls.  This seam survives the upcoming service-layer
refactor because the new layer still routes through ``SchwabClient.get``.

Golden values were captured by running the current code and recording its
output.  Do NOT change these values without first verifying that the
production code changed.
"""

from __future__ import annotations

import json
import time
from datetime import date
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
    """Isolated HOME with valid config + non-expired session."""
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
    save_session(
        Session(
            access_token="atok",
            refresh_token="rtok",
            expires_at=int(time.time()) + 3600,
            refresh_token_expires_at=int(time.time()) + 7 * 24 * 3600,
        )
    )


# ---------------------------------------------------------------------------
# Canned payloads
# ---------------------------------------------------------------------------

# Full fundamentals payload — field names match the live Schwab API:
# short forms (``eps``, ``divYield``, …), not the longer ``epsTTM``/
# ``dividendYield`` seen in older docs.
_FUND_AAPL: dict = {
    "symbol": "AAPL",
    "quote": {"lastPrice": 232.14},
    "fundamental": {
        "peRatio": 33.85,
        "eps": 6.54,
        "pegRatio": 3.21,
        "marketCap": 3.43e12,
        "divYield": 0.44,
        "divAmount": 1.04,
        "divFreq": 4,
        "beta": 1.25,
        "high52": 260.10,
        "low52": 164.08,
        "sharesOutstanding": 14_855_911_000,
    },
}

_FUND_MSFT: dict = {
    "symbol": "MSFT",
    "quote": {"lastPrice": 451.22},
    "fundamental": {
        "peRatio": 35.0,
        "eps": 12.9,
        "beta": 0.9,
    },
}

_FUND_SINGLE_PAYLOAD: dict = {"AAPL": _FUND_AAPL}
_FUND_MULTI_PAYLOAD: dict = {"AAPL": _FUND_AAPL, "MSFT": _FUND_MSFT}

_FUND_INVALID_PAYLOAD: dict = {
    "errors": {"invalidSymbols": ["NOTREAL"]},
    "AAPL": _FUND_AAPL,
}

_FUND_BRKB_PAYLOAD: dict = {
    "BRK/B": {
        "symbol": "BRK/B",
        "quote": {"lastPrice": 473.90},
        "fundamental": {"peRatio": 9.5, "eps": 49.87},
    }
}

# Dividends canned payload
_DIV_AAPL: dict = {
    "symbol": "AAPL",
    "quote": {"lastPrice": 232.14},
    "fundamental": {
        "dividendAmount": 1.04,
        "dividendYield": 0.44,
        "dividendFreq": 4,
        "dividendDate": "2025-05-12 04:00:00.0",
        "dividendPayAmount": 0.26,
        "dividendPayDate": "2025-05-15 04:00:00.0",
        "declarationDate": "2025-01-30 04:00:00.0",
        "nextDividendDate": "2025-08-12 04:00:00.0",
        "nextDividendPayDate": "2025-08-15 04:00:00.0",
        "divGrowthRate3Year": 5.2,
    },
}

_DIV_KO: dict = {
    "symbol": "KO",
    "quote": {"lastPrice": 70.0},
    "fundamental": {
        "dividendAmount": 2.04,
        "dividendYield": 2.91,
        "dividendFreq": 4,
        "dividendDate": "2025-06-15 04:00:00.0",
        "dividendPayAmount": 0.51,
        "dividendPayDate": "2025-07-01 04:00:00.0",
        "nextDividendDate": "2025-09-15 04:00:00.0",
        "nextDividendPayDate": "2025-10-01 04:00:00.0",
        "divGrowthRate3Year": 5.0,
    },
}

_DIV_TSLA: dict = {
    "symbol": "TSLA",
    "quote": {"lastPrice": 250.0},
    "fundamental": {
        "dividendAmount": 0,
        "dividendYield": 0,
        "dividendFreq": 0,
    },
}

_DIV_PAYLOAD: dict = {"AAPL": _DIV_AAPL, "KO": _DIV_KO, "TSLA": _DIV_TSLA}

_DIV_INVALID_PAYLOAD: dict = {
    "errors": {"invalidSymbols": ["ZZZZ"]},
    "AAPL": _DIV_AAPL,
}

_DIV_BRKB_PAYLOAD: dict = {
    "BRK/B": {
        "symbol": "BRK/B",
        "quote": {"lastPrice": 473.90},
        "fundamental": {
            "dividendAmount": 0,
            "dividendYield": 0,
            "dividendFreq": 0,
        },
    }
}

# Golden strings captured from current code
_FUND_MD_HEADER = (
    "| Symbol | Last | Market Cap | P/E (fwd) | P/E (TTM) | PEG | "
    "EPS (TTM) | EPS Δ (TTM) | Rev Δ (TTM) | Div Yield | Beta | "
    "52W High | 52W Low |"
)
_FUND_MD_SEP = (
    "|--------|-----:|-----------:|----------:|----------:|----:|"
    "----------:|------------:|------------:|----------:|-----:|---------:|--------:|"
)
_FUND_MD_AAPL_ROW = (
    "| AAPL | $232.14 | $3.43T | 33.85 | 35.50 | 3.21 | 6.54 | — | — | 0.44% | 1.25 | 260.10 | 164.08 |"
)

_DIV_MD_HEADER = (
    "| Symbol | Yield | Annual | Pay | Freq | Next ex-date | "
    "Next pay date | Last ex-date | 3yr growth |"
)
_DIV_MD_SEP = (
    "|--------|------:|-------:|----:|------|-------------:|"
    "--------------:|-------------:|-----------:|"
)
_DIV_MD_AAPL_ROW = (
    "| AAPL | 0.44% | $1.04 | $0.26 | quarterly | "
    "2025-08-12 | 2025-08-15 | 2025-05-12 | 5.20% |"
)
_DIV_MD_KO_ROW = (
    "| KO | 2.91% | $2.04 | $0.51 | quarterly | "
    "2025-09-15 | 2025-10-01 | 2025-06-15 | 5.00% |"
)


# ===========================================================================
# FUNDAMENTALS — golden HUMAN output
# ===========================================================================


def test_fund_human_exit_code(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", "AAPL"])
    assert result.exit_code == 0, result.output


def test_fund_human_contains_symbol(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", "AAPL"])
    assert "AAPL" in result.output


def test_fund_human_contains_last_price(monkeypatch, tmp_path):
    """Human output must show the last price in $-formatted money."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", "AAPL"])
    assert "$232.14" in result.output


def test_fund_human_contains_pe_forward(monkeypatch, tmp_path):
    """Human output must show Schwab's forward P/E."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", "AAPL"])
    assert "P/E (fwd)" in result.output
    assert "33.85" in result.output


def test_fund_human_contains_pe_ttm(monkeypatch, tmp_path):
    """Human output must show derived TTM P/E (last / eps_ttm)."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", "AAPL"])
    # 232.14 / 6.54 = 35.495... → rounds to 35.50
    assert "P/E (TTM)" in result.output
    assert "35.50" in result.output


def test_fund_human_contains_eps(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", "AAPL"])
    assert "EPS (TTM)" in result.output
    assert "6.54" in result.output


def test_fund_human_contains_market_cap_in_trillions(monkeypatch, tmp_path):
    """Market cap 3.43T must render as '$3.43T'."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", "AAPL"])
    assert "$3.43T" in result.output


def test_fund_human_contains_div_yield(monkeypatch, tmp_path):
    """Dividend yield must appear with % suffix."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", "AAPL"])
    assert "0.44%" in result.output


def test_fund_human_contains_shares_outstanding(monkeypatch, tmp_path):
    """Shares outstanding must appear formatted with commas."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", "AAPL"])
    assert "14,855,911,000" in result.output


def test_fund_human_section_headers(monkeypatch, tmp_path):
    """All section header labels must appear in human output."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", "AAPL"])
    for section in ("Price", "Valuation", "Profitability", "Balance Sheet", "Dividends", "Ownership"):
        assert section in result.output, f"Missing section: {section}"


# ===========================================================================
# FUNDAMENTALS — golden JSON output
# ===========================================================================


def test_fund_json_exit_code(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", "AAPL", "--json"])
    assert result.exit_code == 0, result.output


def test_fund_json_is_list(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", "AAPL", "--json"])
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    assert len(data) == 1


def test_fund_json_row_top_level_keys(monkeypatch, tmp_path):
    """JSON row for a valid symbol must have exactly these top-level keys."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", "AAPL", "--json"])
    data = json.loads(result.stdout)
    row = data[0]
    assert set(row.keys()) == {"symbol", "last", "fundamental", "valuation", "data_quality_warnings"}


def test_fund_json_symbol(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", "AAPL", "--json"])
    data = json.loads(result.stdout)
    assert data[0]["symbol"] == "AAPL"


def test_fund_json_last_price(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", "AAPL", "--json"])
    data = json.loads(result.stdout)
    assert data[0]["last"] == 232.14


def test_fund_json_fundamental_block_pe_ratio(monkeypatch, tmp_path):
    """Fundamental block must preserve peRatio verbatim from payload."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", "AAPL", "--json"])
    data = json.loads(result.stdout)
    assert data[0]["fundamental"]["peRatio"] == 33.85


def test_fund_json_fundamental_block_eps(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", "AAPL", "--json"])
    data = json.loads(result.stdout)
    assert data[0]["fundamental"]["eps"] == 6.54


def test_fund_json_valuation_pe_forward(monkeypatch, tmp_path):
    """valuation.pe_forward must equal Schwab's peRatio (forward/normalized)."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", "AAPL", "--json"])
    data = json.loads(result.stdout)
    assert data[0]["valuation"]["pe_forward"] == 33.85


def test_fund_json_valuation_pe_ttm(monkeypatch, tmp_path):
    """valuation.pe_ttm must equal last / eps_ttm (= 232.14 / 6.54 ≈ 35.4954)."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", "AAPL", "--json"])
    data = json.loads(result.stdout)
    assert data[0]["valuation"]["pe_ttm"] == pytest.approx(35.4954, rel=1e-4)


def test_fund_json_valuation_eps_ttm(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", "AAPL", "--json"])
    data = json.loads(result.stdout)
    assert data[0]["valuation"]["eps_ttm"] == 6.54


def test_fund_json_data_quality_warnings_empty_for_clean_data(monkeypatch, tmp_path):
    """AAPL has normal financials — data_quality_warnings must be an empty list."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", "AAPL", "--json"])
    data = json.loads(result.stdout)
    assert data[0]["data_quality_warnings"] == []


# ===========================================================================
# FUNDAMENTALS — golden MD output
# ===========================================================================


def test_fund_md_exit_code(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", "AAPL", "--md"])
    assert result.exit_code == 0, result.output


def test_fund_md_exact_header(monkeypatch, tmp_path):
    """MD output must contain the exact golden header line."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", "AAPL", "--md"])
    assert _FUND_MD_HEADER in result.stdout


def test_fund_md_exact_separator(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", "AAPL", "--md"])
    assert _FUND_MD_SEP in result.stdout


def test_fund_md_exact_aapl_row(monkeypatch, tmp_path):
    """MD data row must match the exact golden string."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_SINGLE_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", "AAPL", "--md"])
    assert _FUND_MD_AAPL_ROW in result.stdout


# ===========================================================================
# FUNDAMENTALS — multi-symbol ordering preserved
# ===========================================================================


def test_fund_multi_json_aapl_then_msft(monkeypatch, tmp_path):
    """JSON output must preserve CLI arg order: AAPL first, MSFT second."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_MULTI_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", "AAPL", "MSFT", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert len(data) == 2
    assert data[0]["symbol"] == "AAPL"
    assert data[1]["symbol"] == "MSFT"


def test_fund_multi_json_msft_then_aapl_reversed(monkeypatch, tmp_path):
    """JSON output respects reversed CLI arg order."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_MULTI_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", "MSFT", "AAPL", "--json"])
    data = json.loads(result.stdout)
    assert data[0]["symbol"] == "MSFT"
    assert data[1]["symbol"] == "AAPL"


def test_fund_multi_json_msft_pe_ratio(monkeypatch, tmp_path):
    """Each symbol's fundamental block must map to the correct symbol."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_MULTI_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", "AAPL", "MSFT", "--json"])
    data = json.loads(result.stdout)
    msft = next(r for r in data if r["symbol"] == "MSFT")
    assert msft["fundamental"]["peRatio"] == 35.0


def test_fund_multi_md_row_order(monkeypatch, tmp_path):
    """MD output must emit AAPL row before MSFT row when given that order."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_MULTI_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", "AAPL", "MSFT", "--md"])
    assert result.exit_code == 0, result.output
    aapl_pos = result.stdout.index("| AAPL |")
    msft_pos = result.stdout.index("| MSFT |")
    assert aapl_pos < msft_pos


def test_fund_multi_human_shows_both_symbols(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_MULTI_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", "AAPL", "MSFT"])
    assert "AAPL" in result.output
    assert "MSFT" in result.output


# ===========================================================================
# FUNDAMENTALS — ticker normalization (BRK.B → BRK/B)
# ===========================================================================


@pytest.mark.parametrize("variant", ["BRK.B", "BRK-B", "BRK/B", "brk.b"])
def test_fund_ticker_normalization_json_symbol_field(monkeypatch, tmp_path, variant):
    """All class-share separator variants must normalize to BRK/B in JSON output."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_BRKB_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", variant, "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data[0]["symbol"] == "BRK/B"


@pytest.mark.parametrize("variant", ["BRK.B", "BRK-B", "BRK/B", "brk.b"])
def test_fund_ticker_normalization_json_pe_ratio(monkeypatch, tmp_path, variant):
    """Normalized symbol must look up the correct payload entry (pe=9.5)."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_BRKB_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", variant, "--json"])
    data = json.loads(result.stdout)
    assert data[0]["fundamental"]["peRatio"] == 9.5


# ===========================================================================
# FUNDAMENTALS — symbol missing fundamentals / invalid symbol
# ===========================================================================


def test_fund_invalid_symbol_json_has_error_key(monkeypatch, tmp_path):
    """Invalid symbol row in JSON must have error='invalid symbol'."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_INVALID_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", "AAPL", "NOTREAL", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    notreal = next(r for r in data if r["symbol"] == "NOTREAL")
    assert notreal["error"] == "invalid symbol"


def test_fund_invalid_symbol_json_null_fundamental(monkeypatch, tmp_path):
    """Invalid symbol row must have null fundamental and last."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_INVALID_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", "AAPL", "NOTREAL", "--json"])
    data = json.loads(result.stdout)
    notreal = next(r for r in data if r["symbol"] == "NOTREAL")
    assert notreal["last"] is None
    assert notreal["fundamental"] is None


def test_fund_invalid_symbol_valid_row_unaffected(monkeypatch, tmp_path):
    """Valid AAPL row must still render correctly alongside invalid symbol."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_INVALID_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", "AAPL", "NOTREAL", "--json"])
    data = json.loads(result.stdout)
    aapl = next(r for r in data if r["symbol"] == "AAPL")
    assert aapl["last"] == 232.14
    assert "error" not in aapl


def test_fund_invalid_symbol_md_renders_dashes(monkeypatch, tmp_path):
    """Invalid symbol MD row must render all cells as '—'."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_INVALID_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", "AAPL", "NOTREAL", "--md"])
    # Golden: NOTREAL row has all — cells and the HTML comment
    assert "| NOTREAL | — | — | — | — | — | — | — | — | — | — | — | — |" in result.stdout
    assert "<!-- invalid symbol -->" in result.stdout


def test_fund_invalid_symbol_human_shows_error(monkeypatch, tmp_path):
    """Invalid symbol must appear in human output with 'invalid' text."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_FUND_INVALID_PAYLOAD):
        result = runner.invoke(app, ["fundamentals", "AAPL", "NOTREAL"])
    assert "NOTREAL" in result.output
    assert "invalid" in result.output.lower()


def test_fund_missing_fundamental_block_no_crash(monkeypatch, tmp_path):
    """A symbol present in payload but lacking a fundamental block must not crash."""
    _prep(monkeypatch, tmp_path)
    payload = {"FOO": {"symbol": "FOO", "quote": {"lastPrice": 42.0}}}
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=payload):
        result = runner.invoke(app, ["fundamentals", "FOO"])
    assert result.exit_code == 0, result.output
    assert "FOO" in result.output


# ===========================================================================
# FUNDAMENTALS — error / exit codes
# ===========================================================================


def test_fund_both_flags_exit_code_2(monkeypatch, tmp_path):
    """--json and --md together must exit with code 2."""
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["fundamentals", "AAPL", "--json", "--md"])
    assert result.exit_code == 2


def test_fund_both_flags_mutually_exclusive_message(monkeypatch, tmp_path):
    """--json + --md must print 'mutually exclusive'."""
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["fundamentals", "AAPL", "--json", "--md"])
    assert "mutually exclusive" in result.output


def test_fund_no_config_exit_code_1(monkeypatch, tmp_path):
    """Missing config must exit with code 1."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)
    result = runner.invoke(app, ["fundamentals", "AAPL"])
    assert result.exit_code == 1


def test_fund_no_config_message(monkeypatch, tmp_path):
    """Missing config must print 'No config'."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)
    result = runner.invoke(app, ["fundamentals", "AAPL"])
    assert "No config" in result.output


def test_fund_no_session_exit_code_1(monkeypatch, tmp_path):
    """Config present but missing session must exit with code 1."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)
    save_config(Config(client_id="cid", client_secret="csec", redirect_uri="https://127.0.0.1:8443"))
    result = runner.invoke(app, ["fundamentals", "AAPL"])
    assert result.exit_code == 1


def test_fund_no_session_message(monkeypatch, tmp_path):
    """Missing session must print 'No session'."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)
    save_config(Config(client_id="cid", client_secret="csec", redirect_uri="https://127.0.0.1:8443"))
    result = runner.invoke(app, ["fundamentals", "AAPL"])
    assert "No session" in result.output


def test_fund_session_expired_exit_code_1(monkeypatch, tmp_path):
    """SessionExpired from SchwabClient.get must exit with code 1."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.api.client.SchwabClient.get",
        side_effect=SessionExpired("Session expired. Run schwab_cli auth --force."),
    ):
        result = runner.invoke(app, ["fundamentals", "AAPL"])
    assert result.exit_code == 1


def test_fund_session_expired_message(monkeypatch, tmp_path):
    """SessionExpired must print a message containing 'Session expired'."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.api.client.SchwabClient.get",
        side_effect=SessionExpired("Session expired. Run schwab_cli auth --force."),
    ):
        result = runner.invoke(app, ["fundamentals", "AAPL"])
    assert "Session expired" in result.output


def test_fund_api_error_exit_code_1(monkeypatch, tmp_path):
    """ApiError from SchwabClient.get must exit with code 1."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.api.client.SchwabClient.get",
        side_effect=ApiError("500 Internal Server Error"),
    ):
        result = runner.invoke(app, ["fundamentals", "AAPL"])
    assert result.exit_code == 1


def test_fund_api_error_message_contains_status(monkeypatch, tmp_path):
    """ApiError message text must appear in output."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.api.client.SchwabClient.get",
        side_effect=ApiError("500 Internal Server Error"),
    ):
        result = runner.invoke(app, ["fundamentals", "AAPL"])
    assert "500" in result.output


# ===========================================================================
# DIVIDENDS — golden HUMAN output
# ===========================================================================


def test_div_human_exit_code(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_PAYLOAD):
        result = runner.invoke(app, ["dividends", "AAPL"])
    assert result.exit_code == 0, result.output


def test_div_human_contains_symbol(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_PAYLOAD):
        result = runner.invoke(app, ["dividends", "AAPL"])
    assert "AAPL" in result.output


def test_div_human_contains_last_price(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_PAYLOAD):
        result = runner.invoke(app, ["dividends", "AAPL"])
    assert "$232.14" in result.output


def test_div_human_contains_yield(monkeypatch, tmp_path):
    """Yield must appear with % suffix."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_PAYLOAD):
        result = runner.invoke(app, ["dividends", "AAPL"])
    assert "0.44%" in result.output


def test_div_human_contains_pay_amount(monkeypatch, tmp_path):
    """Per-period pay amount must appear formatted as money."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_PAYLOAD):
        result = runner.invoke(app, ["dividends", "AAPL"])
    assert "$0.26" in result.output


def test_div_human_contains_next_ex_date(monkeypatch, tmp_path):
    """Next ex-date must appear in ISO format."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_PAYLOAD):
        result = runner.invoke(app, ["dividends", "AAPL"])
    assert "2025-08-12" in result.output


def test_div_human_contains_frequency_quarterly(monkeypatch, tmp_path):
    """Frequency=4 must render as 'quarterly'."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_PAYLOAD):
        result = runner.invoke(app, ["dividends", "AAPL"])
    assert "quarterly" in result.output


def test_div_human_non_payer_no_dividend_message(monkeypatch, tmp_path):
    """TSLA (dividendAmount=0, freq=0) must show the non-payer message."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_PAYLOAD):
        result = runner.invoke(app, ["dividends", "TSLA"])
    assert "TSLA" in result.output
    # Golden: "No dividend (non-payer or API reports none)."
    assert "No dividend" in result.output


def test_div_human_non_payer_exact_message(monkeypatch, tmp_path):
    """Non-payer message must match the exact golden text."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_PAYLOAD):
        result = runner.invoke(app, ["dividends", "TSLA"])
    assert "No dividend (non-payer or API reports none)." in result.output


# ===========================================================================
# DIVIDENDS — golden JSON output
# ===========================================================================


def test_div_json_exit_code(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_PAYLOAD):
        result = runner.invoke(app, ["dividends", "AAPL", "--json"])
    assert result.exit_code == 0, result.output


def test_div_json_is_list(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_PAYLOAD):
        result = runner.invoke(app, ["dividends", "AAPL", "--json"])
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    assert len(data) == 1


def test_div_json_row_keys(monkeypatch, tmp_path):
    """JSON row for a payer must have exactly these keys (no _raw_next_ex private field)."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_PAYLOAD):
        result = runner.invoke(app, ["dividends", "AAPL", "--json"])
    data = json.loads(result.stdout)
    row = data[0]
    expected_keys = {
        "symbol", "last", "amount_annual", "yield_pct", "frequency_per_year",
        "pay_amount", "last_ex_date", "last_pay_date", "declaration_date",
        "next_ex_date", "next_pay_date", "growth_rate_3y_pct", "is_payer",
    }
    assert set(row.keys()) == expected_keys


def test_div_json_symbol(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_PAYLOAD):
        result = runner.invoke(app, ["dividends", "AAPL", "--json"])
    data = json.loads(result.stdout)
    assert data[0]["symbol"] == "AAPL"


def test_div_json_last_price(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_PAYLOAD):
        result = runner.invoke(app, ["dividends", "AAPL", "--json"])
    data = json.loads(result.stdout)
    assert data[0]["last"] == 232.14


def test_div_json_yield_pct(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_PAYLOAD):
        result = runner.invoke(app, ["dividends", "AAPL", "--json"])
    data = json.loads(result.stdout)
    assert data[0]["yield_pct"] == 0.44


def test_div_json_amount_annual(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_PAYLOAD):
        result = runner.invoke(app, ["dividends", "AAPL", "--json"])
    data = json.loads(result.stdout)
    assert data[0]["amount_annual"] == 1.04


def test_div_json_frequency_per_year(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_PAYLOAD):
        result = runner.invoke(app, ["dividends", "AAPL", "--json"])
    data = json.loads(result.stdout)
    assert data[0]["frequency_per_year"] == 4


def test_div_json_next_ex_date(monkeypatch, tmp_path):
    """next_ex_date must be the ISO date portion of the Schwab timestamp."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_PAYLOAD):
        result = runner.invoke(app, ["dividends", "AAPL", "--json"])
    data = json.loads(result.stdout)
    assert data[0]["next_ex_date"] == "2025-08-12"


def test_div_json_declaration_date(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_PAYLOAD):
        result = runner.invoke(app, ["dividends", "AAPL", "--json"])
    data = json.loads(result.stdout)
    assert data[0]["declaration_date"] == "2025-01-30"


def test_div_json_is_payer_true(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_PAYLOAD):
        result = runner.invoke(app, ["dividends", "AAPL", "--json"])
    data = json.loads(result.stdout)
    assert data[0]["is_payer"] is True


def test_div_json_no_private_raw_field(monkeypatch, tmp_path):
    """Internal _raw_next_ex must NOT leak into JSON output."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_PAYLOAD):
        result = runner.invoke(app, ["dividends", "AAPL", "--json"])
    data = json.loads(result.stdout)
    for key in data[0]:
        assert not key.startswith("_"), f"Private field leaked: {key}"


# ===========================================================================
# DIVIDENDS — golden MD output
# ===========================================================================


def test_div_md_exit_code(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_PAYLOAD):
        result = runner.invoke(app, ["dividends", "AAPL", "KO", "--md"])
    assert result.exit_code == 0, result.output


def test_div_md_exact_header(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_PAYLOAD):
        result = runner.invoke(app, ["dividends", "AAPL", "KO", "--md"])
    assert _DIV_MD_HEADER in result.stdout


def test_div_md_exact_separator(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_PAYLOAD):
        result = runner.invoke(app, ["dividends", "AAPL", "KO", "--md"])
    assert _DIV_MD_SEP in result.stdout


def test_div_md_exact_aapl_row(monkeypatch, tmp_path):
    """MD AAPL row must match the exact golden string."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_PAYLOAD):
        result = runner.invoke(app, ["dividends", "AAPL", "KO", "--md"])
    assert _DIV_MD_AAPL_ROW in result.stdout


def test_div_md_exact_ko_row(monkeypatch, tmp_path):
    """MD KO row must match the exact golden string."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_PAYLOAD):
        result = runner.invoke(app, ["dividends", "AAPL", "KO", "--md"])
    assert _DIV_MD_KO_ROW in result.stdout


def test_div_md_non_payer_dashes(monkeypatch, tmp_path):
    """Non-payer (TSLA) must render all data cells as '—' in MD."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_PAYLOAD):
        result = runner.invoke(app, ["dividends", "TSLA", "--md"])
    # Golden: "| TSLA | — | — | — | none | — | — | — | — |"
    assert "| TSLA | — | — | — | none | — | — | — | — |" in result.stdout


# ===========================================================================
# DIVIDENDS — multi-symbol ordering preserved
# ===========================================================================


def test_div_multi_json_aapl_then_ko(monkeypatch, tmp_path):
    """JSON output preserves CLI arg order: AAPL first, KO second."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_PAYLOAD):
        result = runner.invoke(app, ["dividends", "AAPL", "KO", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data[0]["symbol"] == "AAPL"
    assert data[1]["symbol"] == "KO"


def test_div_multi_json_ko_then_aapl_reversed(monkeypatch, tmp_path):
    """JSON output respects reversed CLI arg order."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_PAYLOAD):
        result = runner.invoke(app, ["dividends", "KO", "AAPL", "--json"])
    data = json.loads(result.stdout)
    assert data[0]["symbol"] == "KO"
    assert data[1]["symbol"] == "AAPL"


def test_div_multi_md_row_order(monkeypatch, tmp_path):
    """MD output must emit AAPL row before KO row when given that order."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_PAYLOAD):
        result = runner.invoke(app, ["dividends", "AAPL", "KO", "--md"])
    aapl_pos = result.stdout.index("| AAPL |")
    ko_pos = result.stdout.index("| KO |")
    assert aapl_pos < ko_pos


# ===========================================================================
# DIVIDENDS — ticker normalization (BRK.B → BRK/B)
# ===========================================================================


@pytest.mark.parametrize("variant", ["BRK.B", "BRK-B", "BRK/B", "brk.b"])
def test_div_ticker_normalization_json_symbol_field(monkeypatch, tmp_path, variant):
    """All class-share separator variants must normalize to BRK/B in JSON output."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_BRKB_PAYLOAD):
        result = runner.invoke(app, ["dividends", variant, "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data[0]["symbol"] == "BRK/B"


# ===========================================================================
# DIVIDENDS — invalid symbol renders gracefully
# ===========================================================================


def test_div_invalid_symbol_json_has_error_key(monkeypatch, tmp_path):
    """Invalid symbol row in JSON must have error='invalid symbol'."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_INVALID_PAYLOAD):
        result = runner.invoke(app, ["dividends", "AAPL", "ZZZZ", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    zzzz = next(r for r in data if r["symbol"] == "ZZZZ")
    assert zzzz["error"] == "invalid symbol"


def test_div_invalid_symbol_md_renders_dashes_and_comment(monkeypatch, tmp_path):
    """Invalid symbol MD row must render '—' cells with HTML comment."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_INVALID_PAYLOAD):
        result = runner.invoke(app, ["dividends", "AAPL", "ZZZZ", "--md"])
    # Golden: "| ZZZZ | — | — | — | — | — | — | — | — |  <!-- invalid symbol -->"
    assert "| ZZZZ | — | — | — | — | — | — | — | — |" in result.stdout
    assert "<!-- invalid symbol -->" in result.stdout


def test_div_invalid_symbol_human_shows_error(monkeypatch, tmp_path):
    """Invalid symbol must appear in human output with 'invalid' text."""
    _prep(monkeypatch, tmp_path)
    payload = {"errors": {"invalidSymbols": ["ZZZZ"]}}
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=payload):
        result = runner.invoke(app, ["dividends", "ZZZZ"])
    assert "ZZZZ" in result.output
    assert "invalid" in result.output.lower()


def test_div_valid_row_unaffected_by_invalid_sibling(monkeypatch, tmp_path):
    """Valid AAPL row must still render correctly alongside invalid ZZZZ."""
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_INVALID_PAYLOAD):
        result = runner.invoke(app, ["dividends", "AAPL", "ZZZZ", "--json"])
    data = json.loads(result.stdout)
    aapl = next(r for r in data if r["symbol"] == "AAPL")
    assert aapl["last"] == 232.14
    assert "error" not in aapl


# ===========================================================================
# DIVIDENDS — upcoming filter
# ===========================================================================


def test_div_upcoming_filter_within_30_days_keeps_aapl(monkeypatch, tmp_path):
    """AAPL ex-date 2025-08-12 is 28 days from 2025-07-15 → kept in 30d window."""
    _prep(monkeypatch, tmp_path)
    from schwab_cli.output import dividends as div_out
    monkeypatch.setattr(div_out, "_today", lambda: date(2025, 7, 15))
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_PAYLOAD):
        result = runner.invoke(app, ["dividends", "AAPL", "KO", "--upcoming", "--within-days", "30"])
    assert result.exit_code == 0, result.output
    assert "AAPL" in result.output


def test_div_upcoming_filter_within_30_days_drops_ko(monkeypatch, tmp_path):
    """KO ex-date 2025-09-15 is 62 days from 2025-07-15 → filtered out of 30d window."""
    _prep(monkeypatch, tmp_path)
    from schwab_cli.output import dividends as div_out
    monkeypatch.setattr(div_out, "_today", lambda: date(2025, 7, 15))
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_PAYLOAD):
        result = runner.invoke(app, ["dividends", "AAPL", "KO", "--upcoming", "--within-days", "30"])
    assert "KO" not in result.output


def test_div_upcoming_filter_within_90_days_keeps_both(monkeypatch, tmp_path):
    """90-day window from 2025-07-15 keeps both AAPL (28d) and KO (62d)."""
    _prep(monkeypatch, tmp_path)
    from schwab_cli.output import dividends as div_out
    monkeypatch.setattr(div_out, "_today", lambda: date(2025, 7, 15))
    with patch("schwab_cli.api.client.SchwabClient.get", return_value=_DIV_PAYLOAD):
        result = runner.invoke(app, ["dividends", "AAPL", "KO", "--upcoming", "--within-days", "90"])
    assert "AAPL" in result.output
    assert "KO" in result.output


# ===========================================================================
# DIVIDENDS — error / exit codes
# ===========================================================================


def test_div_both_flags_exit_code_2(monkeypatch, tmp_path):
    """--json and --md together must exit with code 2."""
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["dividends", "AAPL", "--json", "--md"])
    assert result.exit_code == 2


def test_div_both_flags_mutually_exclusive_message(monkeypatch, tmp_path):
    """--json + --md must print 'mutually exclusive'."""
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["dividends", "AAPL", "--json", "--md"])
    assert "mutually exclusive" in result.output


def test_div_no_config_exit_code_1(monkeypatch, tmp_path):
    """Missing config must exit with code 1."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)
    result = runner.invoke(app, ["dividends", "AAPL"])
    assert result.exit_code == 1


def test_div_no_config_message(monkeypatch, tmp_path):
    """Missing config must print 'No config'."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)
    result = runner.invoke(app, ["dividends", "AAPL"])
    assert "No config" in result.output


def test_div_no_session_exit_code_1(monkeypatch, tmp_path):
    """Config present but missing session must exit with code 1."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)
    save_config(Config(client_id="cid", client_secret="csec", redirect_uri="https://127.0.0.1:8443"))
    result = runner.invoke(app, ["dividends", "AAPL"])
    assert result.exit_code == 1


def test_div_no_session_message(monkeypatch, tmp_path):
    """Missing session must print 'No session'."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)
    save_config(Config(client_id="cid", client_secret="csec", redirect_uri="https://127.0.0.1:8443"))
    result = runner.invoke(app, ["dividends", "AAPL"])
    assert "No session" in result.output


def test_div_session_expired_exit_code_1(monkeypatch, tmp_path):
    """SessionExpired from SchwabClient.get must exit with code 1."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.api.client.SchwabClient.get",
        side_effect=SessionExpired("Session expired. Run schwab_cli auth --force."),
    ):
        result = runner.invoke(app, ["dividends", "AAPL"])
    assert result.exit_code == 1


def test_div_session_expired_message(monkeypatch, tmp_path):
    """SessionExpired must print a message containing 'Session expired'."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.api.client.SchwabClient.get",
        side_effect=SessionExpired("Session expired. Run schwab_cli auth --force."),
    ):
        result = runner.invoke(app, ["dividends", "AAPL"])
    assert "Session expired" in result.output


def test_div_api_error_exit_code_1(monkeypatch, tmp_path):
    """ApiError from SchwabClient.get must exit with code 1."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.api.client.SchwabClient.get",
        side_effect=ApiError("503 Service Unavailable"),
    ):
        result = runner.invoke(app, ["dividends", "AAPL"])
    assert result.exit_code == 1


def test_div_api_error_message_contains_status(monkeypatch, tmp_path):
    """ApiError message text must appear in output."""
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.api.client.SchwabClient.get",
        side_effect=ApiError("503 Service Unavailable"),
    ):
        result = runner.invoke(app, ["dividends", "AAPL"])
    assert "503" in result.output
