from __future__ import annotations

import json

import pytest

from schwab_cli.output.format import Format
from schwab_cli.output.fundamentals import render_fundamentals


# Mirrors the live Schwab ``/quotes?fields=all`` response shape — short
# names (``eps``, ``divAmount``, ``divYield``) NOT the longer-form
# ``epsTTM`` / ``dividendAmount`` that older docs reference. Anyone
# updating this fixture: dump a real response with `get_quotes(client,
# [...], fields="all")` and copy the keys verbatim.
_FUND = {
    "AAPL": {
        "symbol": "AAPL",
        "quote": {"lastPrice": 232.14, "52WeekHigh": 260.10, "52WeekLow": 164.08},
        "fundamental": {
            "peRatio": 33.85,
            "eps": 6.54,
            "sharesOutstanding": 14_855_911_000,
            "divYield": 0.44,
            "divAmount": 1.0,
        },
    }
}


def test_fundamentals_human_single_symbol():
    out = render_fundamentals(["AAPL"], _FUND, Format.HUMAN)
    assert "AAPL" in out
    assert "33.85" in out  # peRatio
    assert "6.54" in out   # eps
    assert "Market Cap" in out


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
        "fundamental": {"peRatio": 35.0, "eps": 12.9},
    }
    out = render_fundamentals(["AAPL", "MSFT"], two, Format.HUMAN)
    assert "AAPL" in out
    assert "MSFT" in out
    assert "35.00" in out  # MSFT peRatio


# Schwab's ``peRatio`` field is forward / normalized; ``eps`` is the
# trailing 12-month figure. They use different EPS basis, so for any
# growing company ``last / eps != peRatio`` — that's not a bug, but
# downstream consumers can't tell which is which without help. We
# surface both in the derived ``valuation`` section.
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
            "fundamental": {"peRatio": 12.0},  # no eps
        }
    }
    out = render_fundamentals(["FOO"], payload, Format.JSON)
    data = json.loads(out)
    val = data[0].get("valuation") or {}
    assert val.get("pe_forward") == 12.0
    assert val.get("pe_ttm") is None


def test_fundamentals_human_shows_both_pe_labels():
    out = render_fundamentals(["AAPL"], _FUND, Format.HUMAN)
    assert "P/E (fwd)" in out
    assert "P/E (TTM)" in out


# Smoke test against the EXACT live API shape so silent field-name
# drift can't recur. If Schwab ever renames ``eps`` -> ``epsTTM``
# (etc.) this test fails immediately.
def test_fundamentals_real_schwab_shape_populates_pe_ttm():
    payload = {
        "AAPL": {
            "symbol": "AAPL",
            "quote": {"lastPrice": 232.14},
            "fundamental": {
                "avg10DaysVolume": 5e7,
                "avg1YearVolume": 5e7,
                "declarationDate": "2026-01-29T00:00:00Z",
                "divAmount": 1.04,
                "divFreq": 4,
                "divPayAmount": 0.26,
                "divYield": 0.44,
                "eps": 7.46,
                "fundLeverageFactor": 1.0,
                "lastEarningsDate": "2026-01-30T00:00:00Z",
                "peRatio": 34.33,
                "sharesOutstanding": 14_855_911_000,
            },
        }
    }
    out = render_fundamentals(["AAPL"], payload, Format.JSON)
    data = json.loads(out)
    val = data[0]["valuation"]
    assert val["pe_forward"] == 34.33
    assert val["pe_ttm"] == pytest.approx(232.14 / 7.46, rel=1e-3)
    assert val["eps_ttm"] == 7.46
    assert not (data[0].get("data_quality_warnings") or [])


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
            "eps": 46563.01561,
            "sharesOutstanding": 1_300_000_000,
        },
    }
}


def test_fundamentals_warns_on_dual_class_eps_leak_json():
    out = render_fundamentals(["BRK/B"], _BRK_B_SMEARED, Format.JSON)
    data = json.loads(out)
    assert data[0]["symbol"] == "BRK/B"
    warnings = data[0].get("data_quality_warnings") or []
    assert len(warnings) == 1
    w = warnings[0]
    assert w["code"] == "POSSIBLE_DUAL_CLASS_LEAK"
    assert "EPS" in w["message"] and "P/E" in w["message"]
    assert "share class" in w["message"].lower()
    assert w.get("guidance")


def test_fundamentals_warns_on_dual_class_eps_leak_human():
    out = render_fundamentals(["BRK/B"], _BRK_B_SMEARED, Format.HUMAN)
    assert "POSSIBLE_DUAL_CLASS_LEAK" in out
    assert "share class" in out.lower()


def test_fundamentals_warns_on_dual_class_known_symbol_clean_data():
    """Membership in the dual-class set alone is enough to warn — even
    if EPS / P/E happen to look fine, a future smearing event would go
    silently undetected without this safety net."""
    payload = {
        "BRK/B": {
            "symbol": "BRK/B",
            "quote": {"lastPrice": 473.90},
            "fundamental": {"peRatio": 15.3, "eps": 31.0},
        }
    }
    out = render_fundamentals(["BRK/B"], payload, Format.JSON)
    data = json.loads(out)
    warnings = data[0].get("data_quality_warnings") or []
    assert len(warnings) == 1
    assert warnings[0]["code"] == "POSSIBLE_DUAL_CLASS_LEAK"


def test_fundamentals_warns_on_anomalous_eps_for_unknown_symbol():
    """Even a symbol not in the dual-class set must warn when EPS is
    absurd — the smearing pattern can hit any class-share equity Schwab
    hasn't been audited against yet."""
    payload = {
        "ZZZZ": {
            "symbol": "ZZZZ",
            "quote": {"lastPrice": 100.0},
            "fundamental": {"peRatio": 0.05, "eps": 2000.0},
        }
    }
    out = render_fundamentals(["ZZZZ"], payload, Format.JSON)
    data = json.loads(out)
    warnings = data[0].get("data_quality_warnings") or []
    assert len(warnings) == 1
    assert warnings[0]["code"] == "POSSIBLE_DUAL_CLASS_LEAK"


def test_fundamentals_no_warning_for_normal_single_class():
    """AAPL has a forward / TTM mismatch — that's expected, not a warning."""
    out = render_fundamentals(["AAPL"], _FUND, Format.JSON)
    data = json.loads(out)
    assert not (data[0].get("data_quality_warnings") or [])
