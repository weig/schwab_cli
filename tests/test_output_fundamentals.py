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
