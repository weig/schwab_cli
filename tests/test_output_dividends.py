from __future__ import annotations

import json

from schwab_cli.output.dividends import render_dividends
from schwab_cli.output.format import Format


_DIV = {
    "AAPL": {
        "symbol": "AAPL",
        "quote": {"lastPrice": 232.14},
        "fundamental": {
            "dividendAmount": 1.0,
            "dividendYield": 0.44,
            "dividendFreq": 4,
            "dividendDate": "2025-05-12 04:00:00.0",
            "dividendPayAmount": 0.25,
            "dividendPayDate": "2025-05-15 04:00:00.0",
            "declarationDate": "2025-05-01 04:00:00.0",
            "nextDividendDate": "2025-08-12 04:00:00.0",
            "nextDividendPayDate": "2025-08-15 04:00:00.0",
            "divGrowthRate3Year": 4.5,
        },
    },
    "KO": {
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
    },
    "TSLA": {
        "symbol": "TSLA",
        "quote": {"lastPrice": 250.0},
        "fundamental": {
            "dividendAmount": 0,
            "dividendYield": 0,
            "dividendFreq": 0,
        },
    },
}


def test_dividends_human_single_symbol():
    out = render_dividends(["AAPL"], _DIV, Format.HUMAN)
    assert "AAPL" in out
    assert "0.25" in out  # pay amount
    assert "2025-08-12" in out  # next ex-date
    assert "quarterly" in out.lower() or "4" in out  # dividend freq


def test_dividends_human_non_dividend_payer():
    out = render_dividends(["TSLA"], _DIV, Format.HUMAN)
    assert "TSLA" in out
    assert "no dividend" in out.lower() or "n/a" in out.lower() or "—" in out


def test_dividends_json_shape():
    out = render_dividends(["AAPL"], _DIV, Format.JSON)
    data = json.loads(out)
    assert data[0]["symbol"] == "AAPL"
    assert data[0]["yield_pct"] == 0.44
    assert data[0]["next_ex_date"].startswith("2025-08-12")
    assert data[0]["pay_amount"] == 0.25
    assert data[0]["frequency_per_year"] == 4


def test_dividends_md_has_header():
    out = render_dividends(["AAPL", "KO"], _DIV, Format.MD)
    assert "| Symbol" in out
    assert "AAPL" in out
    assert "KO" in out


def test_dividends_upcoming_filter_keeps_in_window(monkeypatch):
    # Freeze 'today' to 2025-07-15 → AAPL's 2025-08-12 ex is 28 days out (in a 30d window),
    # KO's 2025-09-15 ex is 62 days out (outside 30d, inside 90d).
    from datetime import date
    from schwab_cli.output import dividends as div_mod

    class FrozenDate(date):
        @classmethod
        def today(cls):
            return date(2025, 7, 15)

    monkeypatch.setattr(div_mod, "_today", lambda: date(2025, 7, 15))

    out = render_dividends(
        ["AAPL", "KO"], _DIV, Format.HUMAN,
        upcoming_within_days=30,
    )
    assert "AAPL" in out
    assert "KO" not in out  # filtered out

    out2 = render_dividends(
        ["AAPL", "KO"], _DIV, Format.HUMAN,
        upcoming_within_days=90,
    )
    assert "AAPL" in out2
    assert "KO" in out2


def test_dividends_invalid_symbol_noted():
    payload = {"errors": {"invalidSymbols": ["ZZZZ"]}}
    out = render_dividends(["ZZZZ"], payload, Format.HUMAN)
    assert "ZZZZ" in out
    assert "invalid" in out.lower()
