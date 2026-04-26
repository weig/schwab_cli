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


def _csv_bytes() -> bytes:
    return (_FIX / "sa_sp500_sample.csv").read_bytes()


def test_stockanalysis_parses_symbols_from_csv():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/list/sp-500-stocks/")
        assert request.url.params["p"] == "csv"
        return httpx.Response(200, content=_csv_bytes())

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, timeout=10.0) as client:
        out = fetch_stockanalysis_members("SPX", client=client)

    assert "AAPL" in out
    assert "BRK.B" in out  # dot form preserved
    assert len(out) == 6


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
