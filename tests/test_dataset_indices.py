"""Index member adapters — stockanalysis.com primary + SSGA fallback.

HTTP is mocked via httpx.MockTransport so we never reach the network.
Symbol normalization (BRK.B from stockanalysis, BRK-B from SSGA) is
exercised — both must round-trip to BRK.B.
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from schwab_cli.dataset.indices import (
    fetch_stockanalysis_members,
    INDEX_TO_STOCKANALYSIS_SLUG,
    INDEX_TO_SSGA_ETF,
)


_FIX = Path(__file__).parent / "fixtures"


def _sa_html_bytes() -> bytes:
    """Minimal stockanalysis-shaped HTML with a few /stocks/SYM/ links
    plus a couple of decoy path words (screener, compare) the parser
    must skip."""
    body = (
        '<!doctype html><html><body>'
        '<a href="/stocks/screener/">Screener</a>'
        '<a href="/stocks/compare/">Compare</a>'
        '<a href="/stocks/aapl/">AAPL</a>'
        '<a href="/stocks/msft/">MSFT</a>'
        '<a href="/stocks/nvda/">NVDA</a>'
        '<a href="/stocks/amzn/">AMZN</a>'
        '<a href="/stocks/brk-b/">BRK-B</a>'   # dash form, must normalize
        '<a href="/stocks/googl/">GOOGL</a>'
        '</body></html>'
    )
    return body.encode("utf-8")


def test_stockanalysis_parses_symbols_from_html():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/list/sp-500-stocks/")
        return httpx.Response(200, content=_sa_html_bytes())

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, timeout=10.0) as client:
        out = fetch_stockanalysis_members("SPX", client=client)

    assert "AAPL" in out
    assert "BRK.B" in out          # dash → dot normalization
    assert "screener" not in out
    assert "compare" not in out
    assert len(out) == 6           # 6 real tickers; 2 decoys filtered


def test_stockanalysis_raises_on_404():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)
    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        with pytest.raises(RuntimeError, match="HTTP 404"):
            fetch_stockanalysis_members("SPX", client=client)


def test_stockanalysis_rejects_unknown_index():
    with pytest.raises(ValueError, match="not supported by stockanalysis"):
        fetch_stockanalysis_members("RUT", client=httpx.Client())


def test_index_slug_table_covers_three_indices():
    assert INDEX_TO_STOCKANALYSIS_SLUG == {
        "SPX": "sp-500",
        "DJI": "dow-jones",
        "NQ":  "nasdaq-100",
    }


def test_ssga_etf_table_covers_two_indices():
    assert INDEX_TO_SSGA_ETF == {"SPX": "spy", "DJI": "dia"}


from schwab_cli.dataset.indices import fetch_ssga_members


def test_ssga_parses_xlsx_and_normalizes_dashes():
    xlsx_bytes = (_FIX / "ssga_spy_sample.xlsx").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=xlsx_bytes)
    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, timeout=10.0) as client:
        out = fetch_ssga_members("SPX", client=client)
    assert "AAPL" in out
    assert "BRK.B" in out      # dash → dot
    assert "BRK-B" not in out
    # Cash component / blank rows skipped.
    assert "USD" not in out
    assert "" not in out


def test_ssga_rejects_unknown_index():
    with pytest.raises(ValueError, match="not supported by SSGA"):
        fetch_ssga_members("NQ", client=httpx.Client())


from schwab_cli.dataset.indices import fetch_index_members


def test_fetch_index_members_uses_primary_when_ok():
    calls = []
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(200, content=_sa_html_bytes())
    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, timeout=10.0) as client:
        out = fetch_index_members("SPX", client=client)
    assert "stockanalysis.com" in calls
    assert "ssga.com" not in str(calls)
    assert "AAPL" in out


def test_fetch_index_members_falls_back_to_ssga_for_spx():
    xlsx_bytes = (_FIX / "ssga_spy_sample.xlsx").read_bytes()
    calls = []
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if "stockanalysis" in request.url.host:
            return httpx.Response(503)
        return httpx.Response(200, content=xlsx_bytes)
    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, timeout=10.0) as client:
        out = fetch_index_members("SPX", client=client)
    assert any("stockanalysis" in h for h in calls)
    assert any("ssga" in h for h in calls)
    assert "AAPL" in out


def test_fetch_index_members_no_fallback_for_nq_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)
    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, timeout=10.0) as client:
        with pytest.raises(RuntimeError, match="all providers failed"):
            fetch_index_members("NQ", client=client)


def test_fetch_index_members_rut_not_supported():
    with pytest.raises(NotImplementedError, match="RUT"):
        fetch_index_members("RUT", client=httpx.Client())
