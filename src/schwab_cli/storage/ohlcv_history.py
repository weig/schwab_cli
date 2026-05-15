"""Daily OHLCV cache. Same SQLite as ``vol_history`` — schema is
shared, this module only owns the writer/reader for the ``ohlcv_daily``
table. ``vol_history.connect()`` runs migrations for both tables.

Keys: ``(symbol, day)`` where ``day`` is the NY trading-day ISO date.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Iterable


def upsert_candles(
    conn: sqlite3.Connection, *, symbol: str,
    candles: Iterable[dict],
) -> None:
    """Insert or overwrite candles. Re-pulling the same day replaces
    the existing row — last write wins."""
    rows = [
        (
            symbol, c["day"],
            float(c["open"]), float(c["high"]),
            float(c["low"]), float(c["close"]),
            int(c["volume"]), int(c["captured_at_ms"]),
        )
        for c in candles
    ]
    if not rows:
        return
    conn.executemany(
        """
        INSERT INTO ohlcv_daily
            (symbol, day, open, high, low, close, volume, captured_at_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (symbol, day) DO UPDATE SET
            open   = excluded.open,
            high   = excluded.high,
            low    = excluded.low,
            close  = excluded.close,
            volume = excluded.volume,
            captured_at_ms = excluded.captured_at_ms
        """,
        rows,
    )


def read_range(
    conn: sqlite3.Connection, *, symbol: str,
    start: date, end: date,
) -> list[sqlite3.Row]:
    """Cached rows for ``symbol`` in ``[start, end]`` inclusive,
    ordered by day ascending."""
    return conn.execute(
        """
        SELECT symbol, day, open, high, low, close, volume, captured_at_ms
        FROM ohlcv_daily
        WHERE symbol = ?
          AND day >= ?
          AND day <= ?
        ORDER BY day ASC
        """,
        (symbol, start.isoformat(), end.isoformat()),
    ).fetchall()


def last_cached_day(
    conn: sqlite3.Connection, *, symbol: str,
) -> date | None:
    row = conn.execute(
        "SELECT max(day) AS d FROM ohlcv_daily WHERE symbol = ?",
        (symbol,),
    ).fetchone()
    if row is None or row["d"] is None:
        return None
    return date.fromisoformat(row["d"])


def gap(
    conn: sqlite3.Connection, *, symbol: str,
    start: date, end: date,
) -> tuple[date, date] | None:
    """Un-cached suffix to fetch. Returns ``(fetch_start, end)`` when
    cache is missing data, or ``None`` when cache already covers ``end``.

    Suffix-only — middle-of-range holes aren't auto-filled; Schwab's
    history endpoint returns the full range cheap so callers can pass
    ``[start, end]`` manually for hole-fill.
    """
    last = last_cached_day(conn, symbol=symbol)
    if last is None:
        return (start, end)
    if last >= end:
        return None
    return (last + timedelta(days=1), end)
