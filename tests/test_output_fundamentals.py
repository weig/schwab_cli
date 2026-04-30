from __future__ import annotations

import json

import pytest

from schwab_cli.output.format import Format
from schwab_cli.output.fundamentals import render_fundamentals


_FUND = {
    "AAPL": {
        "symbol": "AAPL",
        "quote": {"lastPrice": 232.14},
        "fundamental": {
            "high52": 260.1,
            "low52": 164.08,
            "peRatio": 33.85,
            "pegRatio": 3.21,
            "pbRatio": 63.52,
            "epsTTM": 6.54,
            "epsChangePercentTTM": 10.85,
            "revChangeTTM": 4.81,
            "grossMarginTTM": 46.86,
            "netProfitMarginTTM": 24.3,
            "operatingMarginTTM": 31.03,
            "returnOnEquity": 160.58,
            "currentRatio": 0.87,
            "totalDebtToEquity": 146.99,
            "sharesOutstanding": 14_855_911_000,
            "marketCap": 3.43e12,
            "beta": 1.25,
            "dividendYield": 0.44,
            "dividendAmount": 1.0,
        },
    }
}


def test_fundamentals_human_single_symbol():
    out = render_fundamentals(["AAPL"], _FUND, Format.HUMAN)
    assert "AAPL" in out
    assert "33.85" in out  # peRatio
    assert "6.54" in out   # epsTTM
    assert "Market Cap" in out
    # spot check: high/low 52 shown
    assert "260.10" in out
    assert "164.08" in out


def test_fundamentals_json_shape():
    out = render_fundamentals(["AAPL"], _FUND, Format.JSON)
    data = json.loads(out)
    assert isinstance(data, list)
    assert data[0]["symbol"] == "AAPL"
    # JSON surfaces the underlying fundamental block as-is plus last price
    assert data[0]["fundamental"]["peRatio"] == 33.85
    assert data[0]["last"] == 232.14


def test_fundamentals_md_has_header():
    out = render_fundamentals(["AAPL"], _FUND, Format.MD)
    assert "AAPL" in out
    assert "| Metric" in out or "| Symbol" in out


def test_fundamentals_invalid_symbol_included():
    payload = {"errors": {"invalidSymbols": ["ZZZZ"]}}
    out = render_fundamentals(["ZZZZ"], payload, Format.HUMAN)
    assert "ZZZZ" in out
    assert "invalid" in out.lower()


def test_fundamentals_missing_fundamental_block():
    payload = {"FOO": {"symbol": "FOO", "quote": {"lastPrice": 1.0}}}
    out = render_fundamentals(["FOO"], payload, Format.HUMAN)
    # No crash, should still print symbol header, but metrics dashed
    assert "FOO" in out


def test_fundamentals_multi_symbol_stacks():
    two = dict(_FUND)
    two["MSFT"] = {
        "symbol": "MSFT",
        "quote": {"lastPrice": 450.0},
        "fundamental": {"peRatio": 35.0, "epsTTM": 12.9, "marketCap": 3.35e12},
    }
    out = render_fundamentals(["AAPL", "MSFT"], two, Format.HUMAN)
    assert "AAPL" in out
    assert "MSFT" in out
    assert "35.00" in out  # MSFT peRatio


# Schwab's ``peRatio`` field is forward / normalized; ``epsTTM`` is the
# trailing 12-month figure. They use different EPS basis, so for any
# growing company ``last / epsTTM != peRatio`` — that's not a bug, but
# downstream consumers can't tell which is which without help. We
# surface both: ``valuation.pe_forward`` (Schwab's peRatio) and
# ``valuation.pe_ttm`` (computed ``last / epsTTM``).
def test_fundamentals_json_exposes_pe_forward_and_pe_ttm():
    out = render_fundamentals(["AAPL"], _FUND, Format.JSON)
    data = json.loads(out)
    val = data[0].get("valuation") or {}
    assert val.get("pe_forward") == 33.85
    # AAPL: 232.14 / 6.54 = 35.495...
    assert val.get("pe_ttm") == pytest.approx(35.495, rel=1e-3)
    assert val.get("eps_ttm") == 6.54


def test_fundamentals_json_pe_ttm_none_when_eps_missing():
    payload = {
        "FOO": {
            "symbol": "FOO",
            "quote": {"lastPrice": 10.0},
            "fundamental": {"peRatio": 12.0},  # no epsTTM
        }
    }
    out = render_fundamentals(["FOO"], payload, Format.JSON)
    data = json.loads(out)
    val = data[0].get("valuation") or {}
    assert val.get("pe_forward") == 12.0
    assert val.get("pe_ttm") is None


def test_fundamentals_human_shows_both_pe_labels():
    out = render_fundamentals(["AAPL"], _FUND, Format.HUMAN)
    assert "P/E (fwd)" in out or "P/E (forward)" in out
    assert "P/E (TTM)" in out


# Dual-class EPS smearing — Schwab occasionally serves the BRK/A EPS
# (~$46k) on the BRK/B response, which crashes downstream P/E to ~0.01.
# The renderer can't fix the upstream number, but it must annotate the
# row so downstream consumers know not to trust it.
_BRK_B_SMEARED = {
    "BRK/B": {
        "symbol": "BRK/B",
        "quote": {"lastPrice": 473.90},
        "fundamental": {
            "peRatio": 0.01027,
            "epsTTM": 46563.01561,
            "marketCap": 1.0e12,
            "beta": 0.85,
        },
    }
}


def test_fundamentals_warns_on_dual_class_eps_leak_json():
    out = render_fundamentals(["BRK/B"], _BRK_B_SMEARED, Format.JSON)
    data = json.loads(out)
    assert data[0]["symbol"] == "BRK/B"
    warnings = data[0].get("data_quality_warnings") or []
    assert warnings, "expected at least one warning for smeared EPS"
    joined = " ".join(warnings).lower()
    assert "dual-class" in joined and "eps" in joined


def test_fundamentals_warns_on_dual_class_eps_leak_human():
    out = render_fundamentals(["BRK/B"], _BRK_B_SMEARED, Format.HUMAN)
    lowered = out.lower()
    assert "dual-class" in lowered or "warning" in lowered


def test_fundamentals_no_warning_for_normal_dual_class_payload():
    """Healthy dual-class payload (sane EPS) must NOT trip the warning.

    Heuristic is ``"/" in symbol`` AND ``epsTTM > 1000``; BRK/B with
    EPS=$31 should pass clean.
    """
    payload = {
        "BRK/B": {
            "symbol": "BRK/B",
            "quote": {"lastPrice": 473.90},
            "fundamental": {
                "peRatio": 15.3,
                "epsTTM": 31.0,
                "marketCap": 1.0e12,
            },
        }
    }
    out = render_fundamentals(["BRK/B"], payload, Format.JSON)
    data = json.loads(out)
    assert not (data[0].get("data_quality_warnings") or [])


def test_fundamentals_no_warning_for_normal_single_class():
    """AAPL has a forward / TTM mismatch — that's expected, not a warning."""
    out = render_fundamentals(["AAPL"], _FUND, Format.JSON)
    data = json.loads(out)
    assert not (data[0].get("data_quality_warnings") or [])


def test_fundamentals_no_warning_for_high_eps_non_dual_class():
    """A non-dual-class symbol with a freakishly high EPS must NOT trigger.

    Heuristic must require BOTH ``/`` in symbol AND large EPS — high EPS
    alone is not sufficient (could be a legitimate stock split / unusual
    share structure on a single-class name).
    """
    payload = {
        "FOO": {
            "symbol": "FOO",
            "quote": {"lastPrice": 5000.0},
            "fundamental": {"peRatio": 5.0, "epsTTM": 1500.0},
        }
    }
    out = render_fundamentals(["FOO"], payload, Format.JSON)
    data = json.loads(out)
    assert not (data[0].get("data_quality_warnings") or [])
