"""Daily OHLCV cache table + writer/reader/gap detection."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from schwab_cli.storage import vol_history


# ---- table existence + schema --------------------------------------------


def test_ohlcv_daily_table_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    db = tmp_path / "market_data.db"
    monkeypatch.setattr(vol_history, "db_path", lambda: db)
    with vol_history.connect() as conn:
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(ohlcv_daily)"
        ).fetchall()]
    assert cols == [
        "symbol", "day", "open", "high", "low", "close",
        "volume", "captured_at_ms",
    ]


def test_ohlcv_daily_pk_is_symbol_day(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    db = tmp_path / "market_data.db"
    monkeypatch.setattr(vol_history, "db_path", lambda: db)
    with vol_history.connect() as conn:
        pk = [r[1] for r in conn.execute(
            "PRAGMA table_info(ohlcv_daily)"
        ).fetchall() if r[5] > 0]
    assert pk == ["symbol", "day"]


# ---- writer / reader / gap (Task 7) --------------------------------------


def test_upsert_then_read_range_returns_inserted_candles(
    monkeypatch, tmp_path,
):
    from schwab_cli.storage import ohlcv_history
    db = tmp_path / "market_data.db"
    monkeypatch.setattr(vol_history, "db_path", lambda: db)
    candles = [
        {"day": "2026-05-12", "open": 100.0, "high": 102.0,
         "low": 99.5, "close": 101.0, "volume": 1_000_000,
         "captured_at_ms": 1747000000000},
        {"day": "2026-05-13", "open": 101.0, "high": 103.0,
         "low": 100.5, "close": 102.5, "volume": 1_200_000,
         "captured_at_ms": 1747100000000},
    ]
    with vol_history.connect() as conn:
        ohlcv_history.upsert_candles(conn, symbol="AAPL", candles=candles)
        rows = ohlcv_history.read_range(
            conn, symbol="AAPL",
            start=date(2026, 5, 12), end=date(2026, 5, 13),
        )
    assert [r["day"] for r in rows] == ["2026-05-12", "2026-05-13"]
    assert rows[0]["close"] == 101.0


def test_upsert_is_idempotent(monkeypatch, tmp_path):
    from schwab_cli.storage import ohlcv_history
    db = tmp_path / "market_data.db"
    monkeypatch.setattr(vol_history, "db_path", lambda: db)
    initial = [{"day": "2026-05-12", "open": 100.0, "high": 102.0,
                "low": 99.5, "close": 101.0, "volume": 1_000_000,
                "captured_at_ms": 1747000000000}]
    updated = [{"day": "2026-05-12", "open": 100.0, "high": 102.0,
                "low": 99.5, "close": 101.5, "volume": 1_050_000,
                "captured_at_ms": 1747009999999}]
    with vol_history.connect() as conn:
        ohlcv_history.upsert_candles(conn, symbol="AAPL", candles=initial)
        ohlcv_history.upsert_candles(conn, symbol="AAPL", candles=updated)
        rows = ohlcv_history.read_range(
            conn, symbol="AAPL",
            start=date(2026, 5, 12), end=date(2026, 5, 12),
        )
    assert len(rows) == 1
    assert rows[0]["close"] == 101.5


def test_last_cached_day_returns_max(monkeypatch, tmp_path):
    from schwab_cli.storage import ohlcv_history
    db = tmp_path / "market_data.db"
    monkeypatch.setattr(vol_history, "db_path", lambda: db)
    candles = [
        {"day": "2026-05-10", "open": 1.0, "high": 1.0, "low": 1.0,
         "close": 1.0, "volume": 1, "captured_at_ms": 1},
        {"day": "2026-05-13", "open": 1.0, "high": 1.0, "low": 1.0,
         "close": 1.0, "volume": 1, "captured_at_ms": 1},
        {"day": "2026-05-11", "open": 1.0, "high": 1.0, "low": 1.0,
         "close": 1.0, "volume": 1, "captured_at_ms": 1},
    ]
    with vol_history.connect() as conn:
        ohlcv_history.upsert_candles(conn, symbol="AAPL", candles=candles)
        last = ohlcv_history.last_cached_day(conn, symbol="AAPL")
    assert last == date(2026, 5, 13)


def test_last_cached_day_none_when_empty(monkeypatch, tmp_path):
    from schwab_cli.storage import ohlcv_history
    db = tmp_path / "market_data.db"
    monkeypatch.setattr(vol_history, "db_path", lambda: db)
    with vol_history.connect() as conn:
        last = ohlcv_history.last_cached_day(conn, symbol="AAPL")
    assert last is None


def test_gap_returns_full_range_when_cache_empty(monkeypatch, tmp_path):
    from schwab_cli.storage import ohlcv_history
    db = tmp_path / "market_data.db"
    monkeypatch.setattr(vol_history, "db_path", lambda: db)
    with vol_history.connect() as conn:
        g = ohlcv_history.gap(
            conn, symbol="AAPL",
            start=date(2026, 1, 1), end=date(2026, 5, 13),
        )
    assert g == (date(2026, 1, 1), date(2026, 5, 13))


def test_gap_returns_post_last_cached_suffix(monkeypatch, tmp_path):
    from schwab_cli.storage import ohlcv_history
    db = tmp_path / "market_data.db"
    monkeypatch.setattr(vol_history, "db_path", lambda: db)
    with vol_history.connect() as conn:
        ohlcv_history.upsert_candles(conn, symbol="AAPL", candles=[
            {"day": "2026-05-12", "open": 1.0, "high": 1.0, "low": 1.0,
             "close": 1.0, "volume": 1, "captured_at_ms": 1},
        ])
        g = ohlcv_history.gap(
            conn, symbol="AAPL",
            start=date(2026, 1, 1), end=date(2026, 5, 13),
        )
    assert g == (date(2026, 5, 13), date(2026, 5, 13))


def test_gap_returns_none_when_covered(monkeypatch, tmp_path):
    from schwab_cli.storage import ohlcv_history
    db = tmp_path / "market_data.db"
    monkeypatch.setattr(vol_history, "db_path", lambda: db)
    with vol_history.connect() as conn:
        ohlcv_history.upsert_candles(conn, symbol="AAPL", candles=[
            {"day": "2026-05-13", "open": 1.0, "high": 1.0, "low": 1.0,
             "close": 1.0, "volume": 1, "captured_at_ms": 1},
        ])
        g = ohlcv_history.gap(
            conn, symbol="AAPL",
            start=date(2026, 1, 1), end=date(2026, 5, 13),
        )
    assert g is None
