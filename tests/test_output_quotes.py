import json

from schwab_cli.output.format import Format
from schwab_cli.output.quotes import render_quotes


_QUOTES_PAYLOAD = {
    "AAPL": {
        "symbol": "AAPL",
        "quote": {
            "lastPrice": 232.14,
            "bidPrice": 232.13,
            "askPrice": 232.15,
            "netChange": 0.42,
            "netPercentChangeInDouble": 0.18,
            "totalVolume": 1234567,
        },
    },
    "errors": {"invalidSymbols": ["ZZZZZ"]},
}


def test_render_quotes_json_includes_all_symbols():
    out = render_quotes(["AAPL", "ZZZZZ"], _QUOTES_PAYLOAD, Format.JSON)
    data = json.loads(out)
    symbols = [row["symbol"] for row in data]
    assert "AAPL" in symbols
    assert "ZZZZZ" in symbols


def test_render_quotes_json_marks_invalid():
    out = render_quotes(["AAPL", "ZZZZZ"], _QUOTES_PAYLOAD, Format.JSON)
    data = json.loads(out)
    zz = next(r for r in data if r["symbol"] == "ZZZZZ")
    assert zz["last"] is None
    assert zz.get("error") == "invalid symbol"


def test_render_quotes_md_includes_invalid_row():
    out = render_quotes(["AAPL", "ZZZZZ"], _QUOTES_PAYLOAD, Format.MD)
    assert "AAPL" in out
    assert "ZZZZZ" in out
    assert "—" in out


def test_render_quotes_human_table():
    out = render_quotes(["AAPL"], _QUOTES_PAYLOAD, Format.HUMAN)
    assert "AAPL" in out
    assert "232.14" in out


def test_render_quotes_handles_missing_quote_dict():
    """Symbol absent from payload (not in `errors` either) — produce a blank row without crashing."""
    out = render_quotes(["WAT"], {}, Format.JSON)
    data = json.loads(out)
    assert data[0]["symbol"] == "WAT"
    assert data[0]["last"] is None
