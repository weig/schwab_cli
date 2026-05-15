"""When ohlcv_daily already has today's row, run_volatility_update
must NOT call get_history. When empty, it calls once and caches; the
next run uses the cache."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import pytest

from schwab_cli.storage import vol_history, ohlcv_history
from schwab_cli.storage.groups import GROUP_VOLATILITY, GROUP_OHLCV
from schwab_cli.dataset.update import run_volatility_update, _NY


def _seed_subscription(conn, symbol: str) -> None:
    for group in (GROUP_VOLATILITY, GROUP_OHLCV):
        conn.execute(
            "INSERT INTO subscriptions "
            "(symbol, group_name, source, source_key, subscribed_at) "
            "VALUES (?, ?, 'position', '1234', 1700000000000)",
            (symbol, group),
        )


def _fake_chain(*_a, **_kw):
    return {
        "underlying": {"last": 100.0},
        "expiries": [{
            "expiry": "2026-06-19", "dte": 35,
            "contracts": [{"strike": 100.0, "iv": 0.25, "volume": 100,
                           "type": "call", "delta": 0.5}],
        }],
    }


def _fake_history(client, symbol, **_kw):
    return {
        "candles": [
            {"datetime": 1_700_000_000_000 + i * 86_400_000,
             "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
             "volume": 1_000_000}
            for i in range(110)
        ]
    }


def test_first_run_fetches_and_populates_cache(monkeypatch, tmp_path):
    db = tmp_path / "market_data.db"
    monkeypatch.setattr(vol_history, "db_path", lambda: db)
    with vol_history.connect() as conn:
        _seed_subscription(conn, "AAPL")
        conn.commit()

    with patch("schwab_cli.dataset.update.get_chain", _fake_chain), \
         patch("schwab_cli.dataset.update.get_history",
               side_effect=_fake_history) as mock_hist, \
         vol_history.connect() as conn:
        run_volatility_update(
            conn, client=MagicMock(),
            now_ms=1_700_000_000_000, accounts=[],
        )

    assert mock_hist.call_count == 1
    with vol_history.connect() as conn:
        assert ohlcv_history.last_cached_day(conn, symbol="AAPL") is not None


def test_second_run_skips_history_fetch_when_cache_fresh(monkeypatch, tmp_path):
    db = tmp_path / "market_data.db"
    monkeypatch.setattr(vol_history, "db_path", lambda: db)

    now_ms = 1_700_000_000_000
    today_ny = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc) \
                       .astimezone(_NY).date()
    with vol_history.connect() as conn:
        _seed_subscription(conn, "AAPL")
        candles = [
            {"day": (today_ny - timedelta(days=i)).isoformat(),
             "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0,
             "volume": 1, "captured_at_ms": now_ms}
            for i in range(110)
        ]
        ohlcv_history.upsert_candles(conn, symbol="AAPL", candles=candles)
        conn.commit()

    with patch("schwab_cli.dataset.update.get_chain", _fake_chain), \
         patch("schwab_cli.dataset.update.get_history") as mock_hist, \
         vol_history.connect() as conn:
        run_volatility_update(
            conn, client=MagicMock(),
            now_ms=now_ms, accounts=[],
        )

    mock_hist.assert_not_called()
