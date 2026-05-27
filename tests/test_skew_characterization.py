"""Characterization tests for the ``schwab skew`` command.

These tests pin the CURRENT observable behaviour of every mode so that the
upcoming service-layer migration can be proven behaviour-preserving without
altering production code.

Stable seam (post service-layer migration):
    ``schwab_cli.api.chains.get_chain`` — the Layer-1 fetch the service
    calls via module attribute. Patching the definition site intercepts
    every mode's fetch while exercising the full auth + render stack.

Modes covered:
    L1  — ``skew SYMBOL YYMMDD``                  (default, single chain)
    L2t — ``skew SYMBOL --term EXP EXP ...``      (term structure, explicit)
    L2d — ``skew SYMBOL --dtes N N ...``           (term structure, discovery)
    L3c — ``skew --cross YYMMDD SYM SYM ...``     (cross-ticker, fixed expiry)
    L3x — ``skew --cross --dtes N SYM SYM ...``   (cross-ticker, DTE-based)

Golden values were captured by running the CURRENT code and recording its
output verbatim. Do NOT alter golden constants without first verifying that
the production code changed intentionally.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
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
# Seam constant — update ONLY this string when the migration re-points the
# import to the service layer.
# ---------------------------------------------------------------------------
_GET_CHAIN = "schwab_cli.api.chains.get_chain"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _prep(monkeypatch, tmp_path) -> None:
    """Isolated HOME with a valid config + a non-expired session.

    ``expires_at`` is set to epoch 9_000_000_000 (~year 2255) so
    ``service.auth.get_session`` will never fire ``oauth.refresh`` during
    tests — it only attempts a refresh when the token appears expired.
    """
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
            expires_at=9_000_000_000,
            refresh_token_expires_at=9_000_000_000,
        )
    )


def _future_yymmdd(days_out: int = 30) -> tuple[str, str]:
    """Return ``(YYMMDD, ISO)`` for a date N days in the future.

    ``parse_option_spec`` rejects past expiries, so tests use a moving
    target relative to today.
    """
    d = date.today() + timedelta(days=days_out)
    return d.strftime("%y%m%d"), d.isoformat()


def _chain_resp(iso_expiry: str, *, symbol: str = "AMZN", dte: int = 30) -> dict:
    """Build a Schwab-shaped chain response dense enough for compute_skew
    to land all three delta targets (ATM / 25Δ / 10Δ) and compute a slope.

    Identical to the fixture in ``test_commands_skew.py`` — reused here so
    golden analytics values are numerically consistent across both test
    modules.
    """
    calls = {
        245.0: (0.75, 0.65),
        250.0: (0.60, 0.63),
        255.0: (0.53, 0.620),
        257.5: (0.50, 0.6162),  # ATM
        260.0: (0.46, 0.612),
        265.0: (0.38, 0.605),
        270.0: (0.30, 0.600),
        272.5: (0.26, 0.5951),  # 25Δ call
        275.0: (0.22, 0.597),
        280.0: (0.17, 0.6002),  # 10Δ call
    }
    puts = {
        232.5: (-0.16, 0.6380),  # 10Δ put
        240.0: (-0.25, 0.6280),  # 25Δ put
        250.0: (-0.40, 0.622),
        255.0: (-0.47, 0.619),
        257.5: (-0.50, 0.6158),
    }

    def _call_row(strike: float, delta: float, iv: float) -> dict:
        return {
            "symbol": f"{symbol}  XXXXXX{strike:08.0f}",
            "putCall": "CALL",
            "strikePrice": strike,
            "delta": delta,
            "volatility": iv * 100,
            "bid": 1.0,
            "ask": 1.05,
            "last": 1.02,
            "totalVolume": 100,
            "openInterest": 100,
            "expirationDate": iso_expiry,
            "daysToExpiration": dte,
        }

    def _put_row(strike: float, delta: float, iv: float) -> dict:
        return {
            "symbol": f"{symbol}  XXXXXX{strike:08.0f}",
            "putCall": "PUT",
            "strikePrice": strike,
            "delta": delta,
            "volatility": iv * 100,
            "bid": 1.0,
            "ask": 1.05,
            "last": 1.02,
            "totalVolume": 100,
            "openInterest": 100,
            "expirationDate": iso_expiry,
            "daysToExpiration": dte,
        }

    call_map = {
        f"{iso_expiry}:{dte}": {
            f"{s:.1f}": [_call_row(s, d, iv)] for s, (d, iv) in calls.items()
        }
    }
    put_map = {
        f"{iso_expiry}:{dte}": {
            f"{s:.1f}": [_put_row(s, d, iv)] for s, (d, iv) in puts.items()
        }
    }
    return {
        "symbol": symbol,
        "underlying": {"last": 255.36, "change": 0.0, "percentChange": 0.0},
        "callExpDateMap": call_map,
        "putExpDateMap": put_map,
    }


# ---------------------------------------------------------------------------
# Golden constants — L1 (single chain, AMZN, DTE 30)
# Captured from current render_skew / compute_skew output.
# ---------------------------------------------------------------------------

# HUMAN golden anchors
_L1_HUMAN_PREFIX = "=== AMZN Skew — exp"
_L1_HUMAN_SPOT = "Spot: $255.36"
_L1_HUMAN_ATM_LINE = "ATM  strike $257.50   IV 61.62%"
_L1_HUMAN_D25_HEADER = "25Δ Skew:"
_L1_HUMAN_D25_PUT = "Put   K $240.00   Δ -0.25   IV 62.80%"
_L1_HUMAN_D25_CALL = "Call  K $272.50   Δ +0.26   IV 59.51%"
_L1_HUMAN_D25_RR = "Risk Reversal:  +3.29 vol pt   (put premium)"
_L1_HUMAN_D25_BF = "Butterfly:      -0.46 vol pt   (inverted smile)"
_L1_HUMAN_D10_HEADER = "10Δ Skew:"
_L1_HUMAN_D10_PUT = "Put   K $232.50   Δ -0.16   IV 63.80%"
_L1_HUMAN_D10_CALL = "Call  K $280.00   Δ +0.17   IV 60.02%"
_L1_HUMAN_D10_RR = "Risk Reversal:  +3.78 vol pt   (put premium)"
_L1_HUMAN_D10_BF = "Butterfly:      +0.29 vol pt   (convex smile)"
_L1_HUMAN_ATM_SLOPE = "ATM Slope:  -0.1933 vol pt / $1   (-1.93 per $10, put skew)"
_L1_HUMAN_IV_RANGE = "IV Range:   59.51% – 65.00%   (spread +5.49 pt)"

# JSON golden values
_L1_JSON_SYMBOL = "AMZN"
_L1_JSON_ATM_STRIKE = 257.5
_L1_JSON_ATM_IV_PCT = 61.62
_L1_JSON_D25_RR = pytest.approx(3.29, abs=0.01)
_L1_JSON_D25_BF = pytest.approx(-0.465, abs=0.001)
_L1_JSON_D10_RR = pytest.approx(3.78, abs=0.01)
_L1_JSON_D10_BF = pytest.approx(0.29, abs=0.001)
_L1_JSON_ATM_SLOPE = pytest.approx(-0.1933, abs=0.0001)
_L1_JSON_IV_RANGE_MIN = pytest.approx(59.51, abs=0.01)
_L1_JSON_IV_RANGE_MAX = pytest.approx(65.0, abs=0.01)
_L1_JSON_IV_RANGE_SPREAD = pytest.approx(5.49, abs=0.01)

# MD golden anchors
_L1_MD_H1_PREFIX = "# AMZN Skew — `"
_L1_MD_SPOT = "**Spot:** $255.36"
_L1_MD_ATM_LINE = "**ATM:** strike $257.50, IV 61.62%"
_L1_MD_LEG_SECTION = "## Skew Legs"
_L1_MD_LEG_TABLE_HEADER = "| Leg | Strike | Δ | IV |"
_L1_MD_D25_PUT_ROW = "| 25Δ Put | $240.00 | -0.25 | 62.80% |"
_L1_MD_D25_CALL_ROW = "| 25Δ Call | $272.50 | +0.26 | 59.51% |"
_L1_MD_D10_PUT_ROW = "| 10Δ Put | $232.50 | -0.16 | 63.80% |"
_L1_MD_D10_CALL_ROW = "| 10Δ Call | $280.00 | +0.17 | 60.02% |"
_L1_MD_DERIVED_SECTION = "## Derived Metrics"
_L1_MD_DERIVED_TABLE_HEADER = "| Metric | Value | Interpretation |"
_L1_MD_D25_RR_ROW = "| 25Δ Risk Reversal | +3.29 vol pt | put premium |"
_L1_MD_D25_BF_ROW = "| 25Δ Butterfly | -0.46 vol pt | inverted smile |"
_L1_MD_D10_RR_ROW = "| 10Δ Wing Skew | +3.78 vol pt | put premium |"
_L1_MD_D10_BF_ROW = "| 10Δ Butterfly | +0.29 vol pt | convex smile |"
_L1_MD_SLOPE_ROW = "| ATM Slope | -0.1933 vol pt / $1 | put skew |"
_L1_MD_RANGE_ROW = "| IV Range | 59.51% – 65.00% | +5.49 pt spread |"

# ---------------------------------------------------------------------------
# Golden constants — L2 term structure (AMZN, DTE 10 + 40)
# ---------------------------------------------------------------------------

_TERM_HUMAN_HEADER = "=== AMZN Term Structure ==="
_TERM_HUMAN_COL_HEADER_FRAGMENT = "ATM IV"
_TERM_HUMAN_D25_RR_LABEL = "25Δ RR"

_TERM_MD_H1 = "# AMZN Term Structure — Skew"
_TERM_MD_TABLE_HEADER = "| Expiry | DTE | ATM IV | 25Δ RR | 25Δ BF | Slope/$ |"

# ---------------------------------------------------------------------------
# Golden constants — L3 cross-ticker (AAPL + NVDA, DTE 30)
# ---------------------------------------------------------------------------

_CROSS_HUMAN_HEADER_PREFIX = "=== Cross-Ticker Skew"
_CROSS_HUMAN_COL_HEADER_FRAGMENT = "ATM IV"
_CROSS_MD_H1_PREFIX = "# Cross-Ticker Skew"
_CROSS_MD_TABLE_HEADER = (
    "| Ticker | DTE | ATM IV | 25Δ RR | 10Δ Wing | 25Δ BF | Slope/$ |"
)


# ===========================================================================
# 1. L1 — HUMAN format
# ===========================================================================


class TestL1Human:
    """Pin the HUMAN text output for a single-chain skew (L1 mode)."""

    def _invoke(self, monkeypatch, tmp_path, extra_args: list[str] | None = None):
        _prep(monkeypatch, tmp_path)
        yymmdd, iso = _future_yymmdd(30)
        with patch(_GET_CHAIN, return_value=_chain_resp(iso)):
            return runner.invoke(app, ["skew", "AMZN", yymmdd] + (extra_args or []))

    def test_exit_code_0(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert result.exit_code == 0, result.output

    def test_header_prefix(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _L1_HUMAN_PREFIX in result.output

    def test_spot(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _L1_HUMAN_SPOT in result.output

    def test_atm_line(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _L1_HUMAN_ATM_LINE in result.output

    def test_d25_header(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _L1_HUMAN_D25_HEADER in result.output

    def test_d25_put_leg(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _L1_HUMAN_D25_PUT in result.output

    def test_d25_call_leg(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _L1_HUMAN_D25_CALL in result.output

    def test_d25_rr(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _L1_HUMAN_D25_RR in result.output

    def test_d25_bf(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _L1_HUMAN_D25_BF in result.output

    def test_d10_header(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _L1_HUMAN_D10_HEADER in result.output

    def test_d10_put_leg(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _L1_HUMAN_D10_PUT in result.output

    def test_d10_call_leg(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _L1_HUMAN_D10_CALL in result.output

    def test_d10_rr(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _L1_HUMAN_D10_RR in result.output

    def test_d10_bf(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _L1_HUMAN_D10_BF in result.output

    def test_atm_slope(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _L1_HUMAN_ATM_SLOPE in result.output

    def test_iv_range(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _L1_HUMAN_IV_RANGE in result.output

    def test_api_called_once(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        yymmdd, iso = _future_yymmdd(30)
        with patch(_GET_CHAIN, return_value=_chain_resp(iso)) as mock:
            runner.invoke(app, ["skew", "AMZN", yymmdd])
        mock.assert_called_once()

    def test_api_called_with_single_expiry_window(self, monkeypatch, tmp_path):
        """from_date == to_date verifies the single-expiry fetch."""
        _prep(monkeypatch, tmp_path)
        yymmdd, iso = _future_yymmdd(30)
        with patch(_GET_CHAIN, return_value=_chain_resp(iso)) as mock:
            runner.invoke(app, ["skew", "AMZN", yymmdd])
        _, kwargs = mock.call_args
        assert kwargs["from_date"] == kwargs["to_date"]

    def test_symbol_uppercased(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        yymmdd, iso = _future_yymmdd(30)
        with patch(_GET_CHAIN, return_value=_chain_resp(iso)) as mock:
            runner.invoke(app, ["skew", "amzn", yymmdd])
        args, _ = mock.call_args
        assert args[1] == "AMZN"

    def test_strikes_flag_forwarded(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        yymmdd, iso = _future_yymmdd(30)
        with patch(_GET_CHAIN, return_value=_chain_resp(iso)) as mock:
            result = runner.invoke(app, ["skew", "AMZN", yymmdd, "--strikes", "10"])
        assert result.exit_code == 0, result.output
        _, kwargs = mock.call_args
        assert kwargs["strike_count"] == 10


# ===========================================================================
# 2. L1 — JSON format
# ===========================================================================


class TestL1Json:
    """Pin the JSON envelope for a single-chain skew (L1 mode)."""

    def _invoke(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        yymmdd, iso = _future_yymmdd(30)
        with patch(_GET_CHAIN, return_value=_chain_resp(iso)):
            return runner.invoke(app, ["skew", "AMZN", yymmdd, "--json"])

    def test_exit_code_0(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert result.exit_code == 0, result.output

    def test_output_is_valid_json(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        data = json.loads(result.output)
        assert isinstance(data, dict)

    def test_symbol(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        data = json.loads(result.output)
        assert data["symbol"] == _L1_JSON_SYMBOL

    def test_atm_strike(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        data = json.loads(result.output)
        assert data["atm"]["strike"] == _L1_JSON_ATM_STRIKE

    def test_atm_iv_pct(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        data = json.loads(result.output)
        assert data["atm"]["iv_pct"] == pytest.approx(_L1_JSON_ATM_IV_PCT, abs=0.01)

    def test_d25_rr(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        data = json.loads(result.output)
        assert data["d25"]["rr"] == _L1_JSON_D25_RR

    def test_d25_bf(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        data = json.loads(result.output)
        assert data["d25"]["bf"] == _L1_JSON_D25_BF

    def test_d10_rr(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        data = json.loads(result.output)
        assert data["d10"]["rr"] == _L1_JSON_D10_RR

    def test_d10_bf(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        data = json.loads(result.output)
        assert data["d10"]["bf"] == _L1_JSON_D10_BF

    def test_atm_slope(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        data = json.loads(result.output)
        assert data["atm_slope_per_dollar"] == _L1_JSON_ATM_SLOPE

    def test_iv_range_min(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        data = json.loads(result.output)
        assert data["iv_range"]["min_pct"] == _L1_JSON_IV_RANGE_MIN

    def test_iv_range_max(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        data = json.loads(result.output)
        assert data["iv_range"]["max_pct"] == _L1_JSON_IV_RANGE_MAX

    def test_iv_range_spread(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        data = json.loads(result.output)
        assert data["iv_range"]["spread_pct"] == _L1_JSON_IV_RANGE_SPREAD

    def test_top_level_keys(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        data = json.loads(result.output)
        assert {"symbol", "expiry", "dte", "spot", "atm", "d25", "d10",
                "atm_slope_per_dollar", "iv_range"}.issubset(data.keys())


# ===========================================================================
# 3. L1 — MD format
# ===========================================================================


class TestL1Md:
    """Pin the Markdown output for a single-chain skew (L1 mode)."""

    def _invoke(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        yymmdd, iso = _future_yymmdd(30)
        with patch(_GET_CHAIN, return_value=_chain_resp(iso)):
            return runner.invoke(app, ["skew", "AMZN", yymmdd, "--md"])

    def test_exit_code_0(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert result.exit_code == 0, result.output

    def test_h1_prefix(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert result.output.startswith(_L1_MD_H1_PREFIX)

    def test_spot_line(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _L1_MD_SPOT in result.output

    def test_atm_line(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _L1_MD_ATM_LINE in result.output

    def test_skew_legs_section(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _L1_MD_LEG_SECTION in result.output

    def test_leg_table_header(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _L1_MD_LEG_TABLE_HEADER in result.output

    def test_d25_put_row(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _L1_MD_D25_PUT_ROW in result.output

    def test_d25_call_row(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _L1_MD_D25_CALL_ROW in result.output

    def test_d10_put_row(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _L1_MD_D10_PUT_ROW in result.output

    def test_d10_call_row(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _L1_MD_D10_CALL_ROW in result.output

    def test_derived_metrics_section(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _L1_MD_DERIVED_SECTION in result.output

    def test_derived_table_header(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _L1_MD_DERIVED_TABLE_HEADER in result.output

    def test_d25_rr_row(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _L1_MD_D25_RR_ROW in result.output

    def test_d25_bf_row(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _L1_MD_D25_BF_ROW in result.output

    def test_d10_rr_row(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _L1_MD_D10_RR_ROW in result.output

    def test_d10_bf_row(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _L1_MD_D10_BF_ROW in result.output

    def test_slope_row(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _L1_MD_SLOPE_ROW in result.output

    def test_iv_range_row(self, monkeypatch, tmp_path):
        result = self._invoke(monkeypatch, tmp_path)
        assert _L1_MD_RANGE_ROW in result.output

    def test_output_starts_with_hash(self, monkeypatch, tmp_path):
        """MD must start with an H1 heading."""
        result = self._invoke(monkeypatch, tmp_path)
        assert result.output.lstrip().startswith("# ")

    def test_pipe_table_present(self, monkeypatch, tmp_path):
        """MD must contain at least one GFM pipe-table row."""
        result = self._invoke(monkeypatch, tmp_path)
        assert any("|" in line for line in result.output.splitlines())


# ===========================================================================
# 4. L2 — term structure (--term, explicit expiry list)
# ===========================================================================


class TestL2Term:
    """Pin the term-structure output (L2 --term mode)."""

    def _fake_get_chain(self, iso1: str, iso2: str):
        """Return a side_effect function dispatching on from_date."""

        def _inner(client, symbol, **kwargs):
            fd = kwargs["from_date"].isoformat()
            if fd == iso1:
                return _chain_resp(iso1, dte=10)
            return _chain_resp(iso2, dte=40)

        return _inner

    def test_human_exit_code_0(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        y1, iso1 = _future_yymmdd(10)
        y2, iso2 = _future_yymmdd(40)
        with patch(_GET_CHAIN, side_effect=self._fake_get_chain(iso1, iso2)):
            result = runner.invoke(app, ["skew", "AMZN", "--term", y1, y2])
        assert result.exit_code == 0, result.output

    def test_human_header(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        y1, iso1 = _future_yymmdd(10)
        y2, iso2 = _future_yymmdd(40)
        with patch(_GET_CHAIN, side_effect=self._fake_get_chain(iso1, iso2)):
            result = runner.invoke(app, ["skew", "AMZN", "--term", y1, y2])
        assert _TERM_HUMAN_HEADER in result.output

    def test_human_column_headers_present(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        y1, iso1 = _future_yymmdd(10)
        y2, iso2 = _future_yymmdd(40)
        with patch(_GET_CHAIN, side_effect=self._fake_get_chain(iso1, iso2)):
            result = runner.invoke(app, ["skew", "AMZN", "--term", y1, y2])
        assert _TERM_HUMAN_COL_HEADER_FRAGMENT in result.output
        assert _TERM_HUMAN_D25_RR_LABEL in result.output

    def test_human_two_data_rows(self, monkeypatch, tmp_path):
        """Both expiries appear in the output as data rows."""
        _prep(monkeypatch, tmp_path)
        y1, iso1 = _future_yymmdd(10)
        y2, iso2 = _future_yymmdd(40)
        with patch(_GET_CHAIN, side_effect=self._fake_get_chain(iso1, iso2)):
            result = runner.invoke(app, ["skew", "AMZN", "--term", y1, y2])
        assert iso1 in result.output
        assert iso2 in result.output

    def test_api_called_twice(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        y1, iso1 = _future_yymmdd(10)
        y2, iso2 = _future_yymmdd(40)
        with patch(_GET_CHAIN, side_effect=self._fake_get_chain(iso1, iso2)) as mock:
            runner.invoke(app, ["skew", "AMZN", "--term", y1, y2])
        assert mock.call_count == 2

    def test_json_exit_code_0(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        y1, iso1 = _future_yymmdd(10)
        y2, iso2 = _future_yymmdd(40)
        with patch(_GET_CHAIN, side_effect=self._fake_get_chain(iso1, iso2)):
            result = runner.invoke(app, ["skew", "AMZN", "--term", y1, y2, "--json"])
        assert result.exit_code == 0, result.output

    def test_json_is_list(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        y1, iso1 = _future_yymmdd(10)
        y2, iso2 = _future_yymmdd(40)
        with patch(_GET_CHAIN, side_effect=self._fake_get_chain(iso1, iso2)):
            result = runner.invoke(app, ["skew", "AMZN", "--term", y1, y2, "--json"])
        data = json.loads(result.output)
        assert isinstance(data, list)

    def test_json_sorted_by_dte_ascending(self, monkeypatch, tmp_path):
        """Expiries passed out of order must be sorted DTE-ascending in output."""
        _prep(monkeypatch, tmp_path)
        y1, iso1 = _future_yymmdd(10)
        y2, iso2 = _future_yymmdd(40)
        with patch(_GET_CHAIN, side_effect=self._fake_get_chain(iso1, iso2)):
            # Deliberately pass the later expiry first.
            result = runner.invoke(app, ["skew", "AMZN", "--term", y2, y1, "--json"])
        data = json.loads(result.output)
        dtes = [m["dte"] for m in data]
        assert dtes == sorted(dtes)

    def test_json_dte_values(self, monkeypatch, tmp_path):
        """Golden DTE values must match canned chain payload DTEs."""
        _prep(monkeypatch, tmp_path)
        y1, iso1 = _future_yymmdd(10)
        y2, iso2 = _future_yymmdd(40)
        with patch(_GET_CHAIN, side_effect=self._fake_get_chain(iso1, iso2)):
            result = runner.invoke(app, ["skew", "AMZN", "--term", y1, y2, "--json"])
        data = json.loads(result.output)
        assert [m["dte"] for m in data] == [10, 40]

    def test_json_d25_rr_present(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        y1, iso1 = _future_yymmdd(10)
        y2, iso2 = _future_yymmdd(40)
        with patch(_GET_CHAIN, side_effect=self._fake_get_chain(iso1, iso2)):
            result = runner.invoke(app, ["skew", "AMZN", "--term", y1, y2, "--json"])
        data = json.loads(result.output)
        for row in data:
            assert row["d25"]["rr"] == pytest.approx(3.29, abs=0.01)

    def test_md_exit_code_0(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        y1, iso1 = _future_yymmdd(10)
        y2, iso2 = _future_yymmdd(40)
        with patch(_GET_CHAIN, side_effect=self._fake_get_chain(iso1, iso2)):
            result = runner.invoke(app, ["skew", "AMZN", "--term", y1, y2, "--md"])
        assert result.exit_code == 0, result.output

    def test_md_h1(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        y1, iso1 = _future_yymmdd(10)
        y2, iso2 = _future_yymmdd(40)
        with patch(_GET_CHAIN, side_effect=self._fake_get_chain(iso1, iso2)):
            result = runner.invoke(app, ["skew", "AMZN", "--term", y1, y2, "--md"])
        assert _TERM_MD_H1 in result.output

    def test_md_table_header(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        y1, iso1 = _future_yymmdd(10)
        y2, iso2 = _future_yymmdd(40)
        with patch(_GET_CHAIN, side_effect=self._fake_get_chain(iso1, iso2)):
            result = runner.invoke(app, ["skew", "AMZN", "--term", y1, y2, "--md"])
        assert _TERM_MD_TABLE_HEADER in result.output

    def test_partial_failure_continues(self, monkeypatch, tmp_path):
        """One failing expiry → warn on stderr, but render the remaining chain."""
        _prep(monkeypatch, tmp_path)
        y1, iso1 = _future_yymmdd(10)
        y2, _iso2 = _future_yymmdd(40)
        n = {"calls": 0}

        def _side(client, symbol, **kwargs):
            n["calls"] += 1
            if n["calls"] == 2:
                raise ApiError("timeout")
            return _chain_resp(iso1, dte=10)

        with patch(_GET_CHAIN, side_effect=_side):
            result = runner.invoke(app, ["skew", "AMZN", "--term", y1, y2])
        assert result.exit_code == 0, result.output
        assert _TERM_HUMAN_HEADER in result.output

    def test_all_fail_exits_1(self, monkeypatch, tmp_path):
        """All expiries failing → exit 1 (no usable chains)."""
        _prep(monkeypatch, tmp_path)
        y1, _ = _future_yymmdd(10)
        y2, _ = _future_yymmdd(40)
        with patch(_GET_CHAIN, side_effect=ApiError("down")):
            result = runner.invoke(app, ["skew", "AMZN", "--term", y1, y2])
        assert result.exit_code == 1


# ===========================================================================
# 5. L2 — DTE discovery mode (--dtes)
# ===========================================================================


class TestL2Dtes:
    """Pin the DTE-discovery term-structure output (L2 --dtes mode)."""

    def _discovery_resp(self, exp_dtes: list[tuple[date, int]], symbol: str = "AMZN") -> dict:
        """Build a minimal Schwab discovery response with explicit expiry keys."""
        return {
            "symbol": symbol,
            "underlying": {"last": 255.36, "change": 0, "percentChange": 0},
            "callExpDateMap": {
                f"{exp.isoformat()}:{dte}": {
                    "255.0": [
                        {
                            "putCall": "CALL",
                            "strikePrice": 255.0,
                            "delta": 0.50,
                            "volatility": 60.0,
                        }
                    ]
                }
                for exp, dte in exp_dtes
            },
            "putExpDateMap": {},
        }

    def _make_side(self, discovery: dict, today: date) -> object:
        def _inner(client, symbol, **kwargs):
            if kwargs.get("strike_count") == 2:
                return discovery
            return _chain_resp(
                kwargs["from_date"].isoformat(),
                dte=kwargs["from_date"].toordinal() - today.toordinal(),
            )

        return _inner

    def test_exit_code_0(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        today = date.today()
        exp_dtes = [(today + timedelta(days=d), d) for d in (7, 35, 90)]
        disc = self._discovery_resp(exp_dtes)
        with patch(_GET_CHAIN, side_effect=self._make_side(disc, today)):
            result = runner.invoke(app, ["skew", "AMZN", "--dtes", "7", "30"])
        assert result.exit_code == 0, result.output

    def test_term_header_in_output(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        today = date.today()
        exp_dtes = [(today + timedelta(days=d), d) for d in (7, 35)]
        disc = self._discovery_resp(exp_dtes)
        with patch(_GET_CHAIN, side_effect=self._make_side(disc, today)):
            result = runner.invoke(app, ["skew", "AMZN", "--dtes", "7", "30"])
        assert _TERM_HUMAN_HEADER in result.output

    def test_discovery_then_fetches(self, monkeypatch, tmp_path):
        """1 discovery + N full fetches: verify total call count."""
        _prep(monkeypatch, tmp_path)
        today = date.today()
        exp_dtes = [(today + timedelta(days=d), d) for d in (7, 35, 90)]
        disc = self._discovery_resp(exp_dtes)
        with patch(_GET_CHAIN, side_effect=self._make_side(disc, today)) as mock:
            result = runner.invoke(app, ["skew", "AMZN", "--dtes", "7", "30"])
        assert result.exit_code == 0, result.output
        # 1 discovery + 2 full fetches (closest to 7 and 30).
        assert mock.call_count == 3

    def test_dedup_picks_one_expiry_for_colliding_dtes(self, monkeypatch, tmp_path):
        """Two targets that collapse to the same expiry result in one full fetch."""
        _prep(monkeypatch, tmp_path)
        today = date.today()
        # Only one available expiry: DTE 30.
        exp_dtes = [(today + timedelta(days=30), 30)]
        disc = self._discovery_resp(exp_dtes)
        with patch(_GET_CHAIN, side_effect=self._make_side(disc, today)) as mock:
            result = runner.invoke(app, ["skew", "AMZN", "--dtes", "30", "35"])
        assert result.exit_code == 0, result.output
        # 1 discovery + 1 deduplicated full fetch.
        assert mock.call_count == 2

    def test_discovery_uses_strike_count_2(self, monkeypatch, tmp_path):
        """Discovery fetch must pass strike_count=2 (cheapest Schwab call)."""
        _prep(monkeypatch, tmp_path)
        today = date.today()
        exp_dtes = [(today + timedelta(days=30), 30)]
        disc = self._discovery_resp(exp_dtes)
        strike_counts: list[int] = []

        def _side(client, symbol, **kwargs):
            strike_counts.append(kwargs.get("strike_count"))
            if kwargs.get("strike_count") == 2:
                return disc
            return _chain_resp(kwargs["from_date"].isoformat(), dte=30)

        with patch(_GET_CHAIN, side_effect=_side):
            runner.invoke(app, ["skew", "AMZN", "--dtes", "30"])
        assert 2 in strike_counts

    def test_json_sorted_by_dte(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        today = date.today()
        exp_dtes = [(today + timedelta(days=d), d) for d in (7, 35)]
        disc = self._discovery_resp(exp_dtes)
        with patch(_GET_CHAIN, side_effect=self._make_side(disc, today)):
            result = runner.invoke(app, ["skew", "AMZN", "--dtes", "7", "35", "--json"])
        assert result.exit_code == 0, result.output
        rows = json.loads(result.output)
        dtes = [r["dte"] for r in rows]
        assert dtes == sorted(dtes)

    def test_no_expiries_found_exits_1(self, monkeypatch, tmp_path):
        """Empty discovery response → exit 1 with a descriptive message."""
        _prep(monkeypatch, tmp_path)
        empty_disc = {
            "symbol": "AMZN",
            "underlying": {"last": 255.36, "change": 0, "percentChange": 0},
            "callExpDateMap": {},
            "putExpDateMap": {},
        }
        with patch(_GET_CHAIN, return_value=empty_disc):
            result = runner.invoke(app, ["skew", "AMZN", "--dtes", "30"])
        assert result.exit_code == 1

    def test_no_expiries_message(self, monkeypatch, tmp_path):
        """Empty discovery should mention the symbol in the error message."""
        _prep(monkeypatch, tmp_path)
        empty_disc = {
            "symbol": "AMZN",
            "underlying": {"last": 255.36, "change": 0, "percentChange": 0},
            "callExpDateMap": {},
            "putExpDateMap": {},
        }
        with patch(_GET_CHAIN, return_value=empty_disc):
            result = runner.invoke(app, ["skew", "AMZN", "--dtes", "30"])
        assert "AMZN" in result.output


# ===========================================================================
# 6. L3 — cross-ticker (--cross, fixed expiry)
# ===========================================================================


class TestL3Cross:
    """Pin the cross-ticker output (L3 --cross mode)."""

    def test_human_exit_code_0(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        yymmdd, iso = _future_yymmdd(30)

        def _side(client, symbol, **kwargs):
            return _chain_resp(iso, symbol=symbol, dte=30)

        with patch(_GET_CHAIN, side_effect=_side):
            result = runner.invoke(
                app, ["skew", "--cross", yymmdd, "AAPL", "NVDA", "AMZN"]
            )
        assert result.exit_code == 0, result.output

    def test_human_header(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        yymmdd, iso = _future_yymmdd(30)

        def _side(client, symbol, **kwargs):
            return _chain_resp(iso, symbol=symbol, dte=30)

        with patch(_GET_CHAIN, side_effect=_side):
            result = runner.invoke(
                app, ["skew", "--cross", yymmdd, "AAPL", "NVDA"]
            )
        assert _CROSS_HUMAN_HEADER_PREFIX in result.output

    def test_human_all_symbols_present(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        yymmdd, iso = _future_yymmdd(30)

        def _side(client, symbol, **kwargs):
            return _chain_resp(iso, symbol=symbol, dte=30)

        with patch(_GET_CHAIN, side_effect=_side):
            result = runner.invoke(
                app, ["skew", "--cross", yymmdd, "AAPL", "NVDA"]
            )
        assert "AAPL" in result.output
        assert "NVDA" in result.output

    def test_human_atm_iv_column(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        yymmdd, iso = _future_yymmdd(30)

        def _side(client, symbol, **kwargs):
            return _chain_resp(iso, symbol=symbol, dte=30)

        with patch(_GET_CHAIN, side_effect=_side):
            result = runner.invoke(
                app, ["skew", "--cross", yymmdd, "AAPL", "NVDA"]
            )
        assert _CROSS_HUMAN_COL_HEADER_FRAGMENT in result.output

    def test_api_called_per_symbol_in_order(self, monkeypatch, tmp_path):
        """get_chain must be called once per symbol in the CLI order."""
        _prep(monkeypatch, tmp_path)
        yymmdd, iso = _future_yymmdd(30)
        symbols_seen: list[str] = []

        def _side(client, symbol, **kwargs):
            symbols_seen.append(symbol)
            return _chain_resp(iso, symbol=symbol, dte=30)

        with patch(_GET_CHAIN, side_effect=_side):
            runner.invoke(app, ["skew", "--cross", yymmdd, "AAPL", "NVDA", "AMZN"])
        assert symbols_seen == ["AAPL", "NVDA", "AMZN"]

    def test_json_exit_code_0(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        yymmdd, iso = _future_yymmdd(30)

        def _side(client, symbol, **kwargs):
            return _chain_resp(iso, symbol=symbol, dte=30)

        with patch(_GET_CHAIN, side_effect=_side):
            result = runner.invoke(
                app, ["skew", "--cross", yymmdd, "AAPL", "NVDA", "--json"]
            )
        assert result.exit_code == 0, result.output

    def test_json_is_list(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        yymmdd, iso = _future_yymmdd(30)

        def _side(client, symbol, **kwargs):
            return _chain_resp(iso, symbol=symbol, dte=30)

        with patch(_GET_CHAIN, side_effect=_side):
            result = runner.invoke(
                app, ["skew", "--cross", yymmdd, "AAPL", "NVDA", "--json"]
            )
        data = json.loads(result.output)
        assert isinstance(data, list)

    def test_json_symbol_set(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        yymmdd, iso = _future_yymmdd(30)

        def _side(client, symbol, **kwargs):
            return _chain_resp(iso, symbol=symbol, dte=30)

        with patch(_GET_CHAIN, side_effect=_side):
            result = runner.invoke(
                app, ["skew", "--cross", yymmdd, "AAPL", "NVDA", "--json"]
            )
        data = json.loads(result.output)
        assert {m["symbol"] for m in data} == {"AAPL", "NVDA"}

    def test_json_sorted_by_d25_rr_descending(self, monkeypatch, tmp_path):
        """compare_across_tickers sorts by 25Δ RR descending (largest put premium first)."""
        _prep(monkeypatch, tmp_path)
        yymmdd, iso = _future_yymmdd(30)

        def _side(client, symbol, **kwargs):
            return _chain_resp(iso, symbol=symbol, dte=30)

        with patch(_GET_CHAIN, side_effect=_side):
            result = runner.invoke(
                app, ["skew", "--cross", yymmdd, "AAPL", "NVDA", "--json"]
            )
        data = json.loads(result.output)
        rrs = [m["d25"]["rr"] for m in data if m["d25"]["rr"] is not None]
        assert rrs == sorted(rrs, reverse=True)

    def test_md_exit_code_0(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        yymmdd, iso = _future_yymmdd(30)

        def _side(client, symbol, **kwargs):
            return _chain_resp(iso, symbol=symbol, dte=30)

        with patch(_GET_CHAIN, side_effect=_side):
            result = runner.invoke(
                app, ["skew", "--cross", yymmdd, "AAPL", "NVDA", "--md"]
            )
        assert result.exit_code == 0, result.output

    def test_md_h1(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        yymmdd, iso = _future_yymmdd(30)

        def _side(client, symbol, **kwargs):
            return _chain_resp(iso, symbol=symbol, dte=30)

        with patch(_GET_CHAIN, side_effect=_side):
            result = runner.invoke(
                app, ["skew", "--cross", yymmdd, "AAPL", "NVDA", "--md"]
            )
        assert _CROSS_MD_H1_PREFIX in result.output

    def test_md_table_header(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        yymmdd, iso = _future_yymmdd(30)

        def _side(client, symbol, **kwargs):
            return _chain_resp(iso, symbol=symbol, dte=30)

        with patch(_GET_CHAIN, side_effect=_side):
            result = runner.invoke(
                app, ["skew", "--cross", yymmdd, "AAPL", "NVDA", "--md"]
            )
        assert _CROSS_MD_TABLE_HEADER in result.output

    def test_partial_failure_continues(self, monkeypatch, tmp_path):
        """One symbol failing → warn on stderr, render the rest."""
        _prep(monkeypatch, tmp_path)
        yymmdd, iso = _future_yymmdd(30)
        n = {"calls": 0}

        def _side(client, symbol, **kwargs):
            n["calls"] += 1
            if n["calls"] == 2:
                raise ApiError("timeout")
            return _chain_resp(iso, symbol=symbol, dte=30)

        with patch(_GET_CHAIN, side_effect=_side):
            result = runner.invoke(
                app, ["skew", "--cross", yymmdd, "AAPL", "NVDA", "AMZN"]
            )
        assert result.exit_code == 0, result.output
        assert _CROSS_HUMAN_HEADER_PREFIX in result.output


# ===========================================================================
# 7. L3 — cross-ticker at target DTE (--cross --dtes)
# ===========================================================================


class TestL3CrossDtes:
    """Pin the cross-ticker DTE-discovery output (L3 --cross --dtes mode)."""

    def _disc_for(self, symbol: str, dte: int = 30) -> dict:
        today = date.today()
        exp = today + timedelta(days=dte)
        return {
            "symbol": symbol,
            "underlying": {"last": 100.0, "change": 0, "percentChange": 0},
            "callExpDateMap": {
                f"{exp.isoformat()}:{dte}": {
                    "100.0": [
                        {
                            "putCall": "CALL",
                            "strikePrice": 100.0,
                            "delta": 0.50,
                            "volatility": 30.0,
                        }
                    ]
                }
            },
            "putExpDateMap": {},
        }

    def _make_side(self, symbols: list[str]) -> object:
        today = date.today()

        def _inner(client, symbol, **kwargs):
            if kwargs.get("strike_count") == 2:
                return self._disc_for(symbol)
            exp = today + timedelta(days=30)
            return _chain_resp(exp.isoformat(), symbol=symbol, dte=30)

        return _inner

    def test_exit_code_0(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch(_GET_CHAIN, side_effect=self._make_side(["AAPL", "NVDA"])):
            result = runner.invoke(
                app, ["skew", "--cross", "--dtes", "30", "AAPL", "NVDA"]
            )
        assert result.exit_code == 0, result.output

    def test_cross_header_in_output(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch(_GET_CHAIN, side_effect=self._make_side(["AAPL", "NVDA"])):
            result = runner.invoke(
                app, ["skew", "--cross", "--dtes", "30", "AAPL", "NVDA"]
            )
        assert _CROSS_HUMAN_HEADER_PREFIX in result.output

    def test_api_call_count(self, monkeypatch, tmp_path):
        """2 symbols → 2 discovery + 2 full fetches = 4 total calls."""
        _prep(monkeypatch, tmp_path)
        with patch(_GET_CHAIN, side_effect=self._make_side(["AAPL", "NVDA"])) as mock:
            result = runner.invoke(
                app, ["skew", "--cross", "--dtes", "30", "AAPL", "NVDA"]
            )
        assert result.exit_code == 0, result.output
        assert mock.call_count == 4

    def test_json_exit_code_0(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch(_GET_CHAIN, side_effect=self._make_side(["AAPL", "NVDA"])):
            result = runner.invoke(
                app, ["skew", "--cross", "--dtes", "30", "AAPL", "NVDA", "--json"]
            )
        assert result.exit_code == 0, result.output

    def test_json_is_list_of_metrics(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch(_GET_CHAIN, side_effect=self._make_side(["AAPL", "NVDA"])):
            result = runner.invoke(
                app, ["skew", "--cross", "--dtes", "30", "AAPL", "NVDA", "--json"]
            )
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert {m["symbol"] for m in data} == {"AAPL", "NVDA"}

    def test_md_h1(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        with patch(_GET_CHAIN, side_effect=self._make_side(["AAPL", "NVDA"])):
            result = runner.invoke(
                app, ["skew", "--cross", "--dtes", "30", "AAPL", "NVDA", "--md"]
            )
        assert result.exit_code == 0, result.output
        assert _CROSS_MD_H1_PREFIX in result.output

    def test_no_usable_chains_exits_1(self, monkeypatch, tmp_path):
        """All discovery + fetch attempts failing → exit 1."""
        _prep(monkeypatch, tmp_path)
        today = date.today()
        empty_disc = {
            "symbol": "X",
            "underlying": {"last": 100.0, "change": 0, "percentChange": 0},
            "callExpDateMap": {},
            "putExpDateMap": {},
        }
        with patch(_GET_CHAIN, return_value=empty_disc):
            result = runner.invoke(
                app, ["skew", "--cross", "--dtes", "30", "AAPL", "NVDA"]
            )
        assert result.exit_code == 1


# ===========================================================================
# 8. Argument-validation errors (exit 2)
# ===========================================================================


class TestArgValidation:
    """Pin exit-code-2 paths for all user-input errors."""

    def test_l1_missing_expiry_exits_2(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(app, ["skew", "AMZN"])
        assert result.exit_code == 2
        assert "Usage" in result.output

    def test_l1_too_many_args_exits_2(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        yymmdd, _ = _future_yymmdd(30)
        result = runner.invoke(app, ["skew", "AMZN", yymmdd, "extra"])
        assert result.exit_code == 2

    def test_l1_invalid_yymmdd_exits_nonzero(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(app, ["skew", "AMZN", "notadate"])
        assert result.exit_code in (1, 2)

    def test_l1_past_expiry_exits_nonzero(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        past = (date.today() - timedelta(days=30)).strftime("%y%m%d")
        result = runner.invoke(app, ["skew", "AMZN", past])
        assert result.exit_code != 0

    def test_both_json_md_exits_2(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        yymmdd, _ = _future_yymmdd(30)
        result = runner.invoke(app, ["skew", "AMZN", yymmdd, "--json", "--md"])
        assert result.exit_code == 2

    def test_term_and_cross_mutual_exclusion_exits_2(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        yymmdd, _ = _future_yymmdd(30)
        result = runner.invoke(
            app, ["skew", "AMZN", yymmdd, "--term", "--cross"]
        )
        assert result.exit_code == 2
        assert "mutually exclusive" in result.output

    def test_term_and_dtes_mutual_exclusion_exits_2(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(app, ["skew", "AMZN", "--term", "--dtes", "30"])
        assert result.exit_code == 2
        assert "mutually exclusive" in result.output

    def test_term_missing_expiry_arg_exits_2(self, monkeypatch, tmp_path):
        """--term with only a symbol (no expiry dates) → exit 2."""
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(app, ["skew", "AMZN", "--term"])
        assert result.exit_code == 2

    def test_dtes_non_integer_exits_2(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(app, ["skew", "AMZN", "--dtes", "notanint"])
        assert result.exit_code == 2

    def test_dtes_zero_exits_2(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(app, ["skew", "AMZN", "--dtes", "0"])
        assert result.exit_code == 2

    def test_dtes_negative_exits_2(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(app, ["skew", "AMZN", "--dtes", "-5"])
        assert result.exit_code == 2

    def test_dtes_missing_symbol_exits_2(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(app, ["skew", "--dtes", "30"])
        assert result.exit_code == 2

    def test_cross_missing_symbol_exits_2(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        yymmdd, _ = _future_yymmdd(30)
        result = runner.invoke(app, ["skew", "--cross", yymmdd])
        assert result.exit_code == 2

    def test_cross_dtes_non_integer_exits_2(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(
            app, ["skew", "--cross", "--dtes", "notanint", "AAPL"]
        )
        assert result.exit_code == 2
        assert "integer" in result.output.lower()

    def test_cross_dtes_zero_exits_2(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(app, ["skew", "--cross", "--dtes", "0", "AAPL"])
        assert result.exit_code == 2

    def test_cross_dtes_missing_symbols_exits_2(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        result = runner.invoke(app, ["skew", "--cross", "--dtes", "30"])
        assert result.exit_code == 2


# ===========================================================================
# 9. Runtime / infrastructure errors (exit 1)
# ===========================================================================


class TestRuntimeErrors:
    """Pin exit-code-1 paths for infrastructure failures."""

    def test_no_config_exits_1(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)
        yymmdd, _ = _future_yymmdd(30)
        result = runner.invoke(app, ["skew", "AMZN", yymmdd])
        assert result.exit_code == 1

    def test_no_config_message(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.delenv("SCHWAB_CLI_CONFIG", raising=False)
        yymmdd, _ = _future_yymmdd(30)
        result = runner.invoke(app, ["skew", "AMZN", yymmdd])
        assert "No config" in result.output

    def test_no_session_exits_1(self, monkeypatch, tmp_path):
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
        yymmdd, _ = _future_yymmdd(30)
        result = runner.invoke(app, ["skew", "AMZN", yymmdd])
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
        yymmdd, _ = _future_yymmdd(30)
        result = runner.invoke(app, ["skew", "AMZN", yymmdd])
        assert "No session" in result.output

    def test_api_error_exits_1(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        yymmdd, _ = _future_yymmdd(30)
        with patch(_GET_CHAIN, side_effect=ApiError("503 unavailable")):
            result = runner.invoke(app, ["skew", "AMZN", yymmdd])
        assert result.exit_code == 1

    def test_api_error_message_includes_detail(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        yymmdd, _ = _future_yymmdd(30)
        with patch(_GET_CHAIN, side_effect=ApiError("503 unavailable")):
            result = runner.invoke(app, ["skew", "AMZN", yymmdd])
        assert "503" in result.output

    def test_session_expired_exits_1(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        yymmdd, _ = _future_yymmdd(30)
        with patch(_GET_CHAIN, side_effect=SessionExpired("token stale")):
            result = runner.invoke(app, ["skew", "AMZN", yymmdd])
        assert result.exit_code == 1

    def test_session_expired_message(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        yymmdd, _ = _future_yymmdd(30)
        with patch(_GET_CHAIN, side_effect=SessionExpired("token stale")):
            result = runner.invoke(app, ["skew", "AMZN", yymmdd])
        assert "token stale" in result.output

    def test_empty_envelope_exits_1(self, monkeypatch, tmp_path):
        """A chain that shapes to zero contracts → exit 1 with a message."""
        _prep(monkeypatch, tmp_path)
        yymmdd, iso = _future_yymmdd(30)
        # Return a valid Schwab structure but with empty maps → no contracts.
        empty_chain = {
            "symbol": "AMZN",
            "underlying": {"last": 255.36, "change": 0, "percentChange": 0},
            "callExpDateMap": {},
            "putExpDateMap": {},
        }
        with patch(_GET_CHAIN, return_value=empty_chain):
            result = runner.invoke(app, ["skew", "AMZN", yymmdd])
        assert result.exit_code == 1

    def test_empty_envelope_message_mentions_symbol(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        yymmdd, iso = _future_yymmdd(30)
        empty_chain = {
            "symbol": "AMZN",
            "underlying": {"last": 255.36, "change": 0, "percentChange": 0},
            "callExpDateMap": {},
            "putExpDateMap": {},
        }
        with patch(_GET_CHAIN, return_value=empty_chain):
            result = runner.invoke(app, ["skew", "AMZN", yymmdd])
        assert "AMZN" in result.output

    def test_term_all_expiries_fail_exits_1(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        y1, _ = _future_yymmdd(10)
        y2, _ = _future_yymmdd(40)
        with patch(_GET_CHAIN, side_effect=ApiError("down")):
            result = runner.invoke(app, ["skew", "AMZN", "--term", y1, y2])
        assert result.exit_code == 1

    def test_cross_all_symbols_fail_exits_1(self, monkeypatch, tmp_path):
        _prep(monkeypatch, tmp_path)
        yymmdd, _ = _future_yymmdd(30)
        with patch(_GET_CHAIN, side_effect=ApiError("down")):
            result = runner.invoke(
                app, ["skew", "--cross", yymmdd, "AAPL", "NVDA"]
            )
        assert result.exit_code == 1
