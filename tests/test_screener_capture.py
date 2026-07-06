"""Tests for the put-band capture (dataset job's permanent raw layer)."""
from __future__ import annotations

import pytest

from schwab_cli.screener.capture import capture_put_band, extract_put_band
from schwab_cli.screener.config import ScreenerConfig
from schwab_cli.storage import screener as store
from schwab_cli.storage.vol_history import connect

CFG = ScreenerConfig()


@pytest.fixture
def conn(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(tmp_path))
    with connect() as c:
        yield c


def _put(strike, delta, bid=1.0, ask=1.2):
    return {"strikePrice": strike, "delta": delta, "bid": bid, "ask": ask,
            "openInterest": 500, "totalVolume": 50}


def _chain(puts_by_expiry: dict, spot=540.0) -> dict:
    put_map = {
        f"{exp}:{dte}": {str(p["strikePrice"]): [p] for p in rows}
        for (exp, dte), rows in puts_by_expiry.items()
    }
    return {"underlying": {"last": spot}, "putExpDateMap": put_map}


def test_extract_band_filters_dte_and_delta():
    chain = _chain({
        ("2026-08-21", 31): [
            _put(520, -0.45),   # |delta| 0.45 > 0.40 → out
            _put(500, -0.25),   # in
            _put(470, -0.12),   # in
            _put(450, -0.05),   # |delta| 0.05 < 0.10 → out
        ],
        ("2026-07-10", 4):  [_put(500, -0.25)],   # dte 4 < 20 → out
        ("2026-10-16", 100): [_put(500, -0.25)],  # dte 100 > 45 → out
    })
    band = extract_put_band(chain, CFG)
    strikes = sorted(p["strike"] for p in band)
    assert strikes == [470.0, 500.0]


def test_capture_persists_and_is_idempotent(conn):
    chain = _chain({("2026-08-21", 31): [_put(500, -0.25, bid=4.0),
                                         _put(480, -0.15, bid=2.0)]})
    n = capture_put_band(conn, snapshot_date="2026-07-06", symbol="QQQ",
                         raw=chain, now_ms=1, cfg=CFG)
    assert n == 2
    # Re-capture same day with a refreshed quote → upsert, no duplication.
    chain["putExpDateMap"]["2026-08-21:31"]["500"][0]["bid"] = 4.5
    capture_put_band(conn, snapshot_date="2026-07-06", symbol="QQQ",
                     raw=chain, now_ms=2, cfg=CFG)
    rows = store.read_put_band(conn, snapshot_date="2026-07-06", symbol="QQQ")
    assert len(rows) == 2
    by_strike = {r["strike"]: r for r in rows}
    assert by_strike[500.0]["bid"] == 4.5
    assert by_strike[500.0]["underlying_last"] == 540.0
    assert store.symbols_with_put_band(conn, snapshot_date="2026-07-06") == ["QQQ"]
    assert store.latest_put_band_date(conn) == "2026-07-06"


def test_capture_empty_band_writes_nothing(conn):
    chain = _chain({("2026-07-10", 4): [_put(500, -0.25)]})  # all out of DTE band
    n = capture_put_band(conn, snapshot_date="2026-07-06", symbol="X",
                         raw=chain, now_ms=1, cfg=CFG)
    assert n == 0
    assert store.read_put_band(conn, snapshot_date="2026-07-06", symbol="X") == []
