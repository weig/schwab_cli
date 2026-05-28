"""`schwab_cli history` opportunistically caches every API response
at daily interval — even for unsubscribed symbols. Subsequent calls
within the cached range skip the API entirely.

Three scenarios:
1. Cache fully covers range  → no API call, response from cache
2. Cache partially covers    → API call, response upserted into cache
3. Non-daily interval        → API call, cache untouched
"""
from __future__ import annotations

import time
from datetime import date, datetime, timezone
from unittest.mock import patch


def _ms(year, month, day, hour=22):
    """UTC ms timestamp at hour:00 — NY EDT = hour-4, so 22:00 UTC =
    18:00 NY (same day)."""
    return int(datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp() * 1000)

import pytest

from schwab_cli.commands import history as history_cmd
from schwab_cli.config import Config
from schwab_cli.config import save as save_config
from schwab_cli.session import Session
from schwab_cli.session import save as save_session
from schwab_cli.storage import ohlcv_history, vol_history


def _prep_auth(monkeypatch, tmp_path) -> None:
    """Config + future-dated session so the service-layer auth path
    (``service.auth.get_session``) does not attempt an ``oauth.refresh``."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save_config(Config(client_id="cid", client_secret="csec",
                       redirect_uri="https://127.0.0.1:8443"))
    save_session(Session(access_token="atok", refresh_token="rtok",
                         expires_at=int(time.time()) + 3600,
                         refresh_token_expires_at=int(time.time()) + 7 * 24 * 3600))


def _seed_cache(monkeypatch, tmp_path, *, symbol, days):
    """Seed ``ohlcv_daily`` with ``days`` consecutive cached rows
    ending on each requested ISO day. Returns the DB path so callers
    can re-monkeypatch if needed."""
    db = tmp_path / "market_data.db"
    monkeypatch.setattr(vol_history, "db_path", lambda: db)
    with vol_history.connect() as conn:
        ohlcv_history.upsert_candles(conn, symbol=symbol, candles=[
            {"day": d, "open": 100.0, "high": 102.0,
             "low": 99.5, "close": 101.0, "volume": 1_000_000,
             "captured_at_ms": 1747000000000}
            for d in days
        ])
    return db


def test_daily_with_full_cache_does_not_call_api(monkeypatch, tmp_path):
    """Unsubscribed symbol + cache happens to cover the range →
    still uses cache."""
    _seed_cache(monkeypatch, tmp_path, symbol="AAPL",
                days=["2026-05-12", "2026-05-13"])

    # Cache HIT — the service must never call the Layer-1 API.
    with patch("schwab_cli.api.history.get_history") as m_api:
        try:
            history_cmd.run(
                symbol="AAPL",
                range_str="20260512..20260513",
                interval_str="1day",
                as_json=True, as_md=False,
            )
        except SystemExit as exc:
            # Cache hit must render cleanly; a non-zero exit would make
            # assert_not_called pass for the wrong reason.
            assert exc.code in (0, None), f"cache-hit run failed: exit {exc.code}"

    m_api.assert_not_called()


def test_daily_with_partial_cache_calls_api_and_upserts(
    monkeypatch, tmp_path,
):
    """Cache only has yesterday's row; user asks for last 4 days.
    Should hit the API once for the full range and write all returned
    candles to the cache."""
    _seed_cache(monkeypatch, tmp_path, symbol="MSFT",
                days=["2026-05-11"])
    _prep_auth(monkeypatch, tmp_path)

    fake_response = {
        "symbol": "MSFT",
        "candles": [
            {"datetime": _ms(2026, 5, 11), "open": 100.0, "high": 102.0,
             "low": 99.5, "close": 101.0, "volume": 1_000_000},
            {"datetime": _ms(2026, 5, 12), "open": 101.0, "high": 103.0,
             "low": 100.5, "close": 102.5, "volume": 1_200_000},
            {"datetime": _ms(2026, 5, 13), "open": 102.0, "high": 104.0,
             "low": 101.5, "close": 103.5, "volume": 1_300_000},
            {"datetime": _ms(2026, 5, 14), "open": 103.0, "high": 105.0,
             "low": 102.5, "close": 104.5, "volume": 1_400_000},
        ],
    }
    with patch("schwab_cli.api.history.get_history",
               return_value=fake_response) as m_api:
        try:
            history_cmd.run(
                symbol="MSFT",
                range_str="20260511..20260514",
                interval_str="1day",
                as_json=True, as_md=False,
            )
        except SystemExit:
            pass

    m_api.assert_called_once()
    # Cache now has all four days upserted.
    with vol_history.connect() as conn:
        cached = ohlcv_history.read_range(
            conn, symbol="MSFT",
            start=date(2026, 5, 11), end=date(2026, 5, 14),
        )
    cached_days = {r["day"] for r in cached}
    assert "2026-05-14" in cached_days
    assert "2026-05-13" in cached_days
    assert "2026-05-12" in cached_days


def test_unsubscribed_symbol_empty_cache_calls_api_then_caches(
    monkeypatch, tmp_path,
):
    """No prior cache for the symbol; user requests daily history.
    Should hit API and seed the cache from the response."""
    db = tmp_path / "market_data.db"
    monkeypatch.setattr(vol_history, "db_path", lambda: db)
    # Force connection so schema is in place before the command runs.
    with vol_history.connect() as _:
        pass
    _prep_auth(monkeypatch, tmp_path)

    fake_response = {
        "symbol": "RANDOM",
        "candles": [
            {"datetime": _ms(2026, 5, 11), "open": 50.0, "high": 51.0,
             "low": 49.0, "close": 50.5, "volume": 100_000},
        ],
    }
    with patch("schwab_cli.api.history.get_history",
               return_value=fake_response) as m_api:
        try:
            history_cmd.run(
                symbol="RANDOM",
                range_str="20260511..20260511",
                interval_str="1day",
                as_json=True, as_md=False,
            )
        except SystemExit:
            pass

    m_api.assert_called_once()
    with vol_history.connect() as conn:
        cached = ohlcv_history.read_range(
            conn, symbol="RANDOM",
            start=date(2026, 5, 11), end=date(2026, 5, 11),
        )
    assert len(cached) == 1
    assert cached[0]["close"] == 50.5


def test_non_daily_interval_does_not_touch_cache(monkeypatch, tmp_path):
    """1min / 5min / 1wk requests bypass the cache entirely — it only
    stores daily."""
    _seed_cache(monkeypatch, tmp_path, symbol="AAPL",
                days=["2026-05-12", "2026-05-13"])
    _prep_auth(monkeypatch, tmp_path)

    fake_response = {
        "symbol": "AAPL",
        "candles": [
            {"datetime": 1747001000000, "open": 100.0, "high": 102.0,
             "low": 99.5, "close": 101.0, "volume": 1_000_000},
        ],
    }
    with patch("schwab_cli.api.history.get_history",
               return_value=fake_response) as m_api, \
         patch.object(ohlcv_history, "upsert_candles") as m_upsert:
        try:
            history_cmd.run(
                symbol="AAPL",
                range_str="20260512..20260513",
                interval_str="5min",
                as_json=True, as_md=False,
            )
        except SystemExit:
            pass

    m_api.assert_called_once()
    m_upsert.assert_not_called()
