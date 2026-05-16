"""Daily account NAV cache. Same SQLite as ``vol_history`` — schema is
shared, this module only owns reader/writer for ``account_nav_daily``.

Keyed by ``(account_hash, day)`` where ``day`` is the NY trading-day
ISO date. ``is_estimated = 1`` flags any day whose NAV included
BS-reconstructed option valuations rather than true historical
marketValue from a live snapshot.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class NavRow:
    account_hash: str
    day: date
    market_value: float
    cash: float
    total_value: float
    is_estimated: bool
    captured_at_ms: int


def upsert(
    conn: sqlite3.Connection,
    *,
    account_hash: str,
    day: date,
    market_value: float,
    cash: float,
    is_estimated: bool,
    captured_at_ms: int,
) -> None:
    """Insert or overwrite a NAV row. Re-running on the same day
    replaces the existing row — useful when a backfill re-prices a
    day previously stored as estimated with fresher inputs."""
    total = market_value + cash
    conn.execute(
        """
        INSERT INTO account_nav_daily
          (account_hash, day, market_value, cash, total_value,
           is_estimated, captured_at_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (account_hash, day) DO UPDATE SET
          market_value   = excluded.market_value,
          cash           = excluded.cash,
          total_value    = excluded.total_value,
          is_estimated   = excluded.is_estimated,
          captured_at_ms = excluded.captured_at_ms
        """,
        (account_hash, day.isoformat(), float(market_value), float(cash),
         float(total), 1 if is_estimated else 0, int(captured_at_ms)),
    )


def read_range(
    conn: sqlite3.Connection,
    *,
    account_hash: str,
    start: date,
    end: date,
) -> list[NavRow]:
    """Cached NAV rows in ``[start, end]`` inclusive, ordered ascending."""
    rows = conn.execute(
        """
        SELECT account_hash, day, market_value, cash, total_value,
               is_estimated, captured_at_ms
        FROM account_nav_daily
        WHERE account_hash = ?
          AND day >= ?
          AND day <= ?
        ORDER BY day ASC
        """,
        (account_hash, start.isoformat(), end.isoformat()),
    ).fetchall()
    return [
        NavRow(
            account_hash=r["account_hash"],
            day=date.fromisoformat(r["day"]),
            market_value=float(r["market_value"]),
            cash=float(r["cash"]),
            total_value=float(r["total_value"]),
            is_estimated=bool(r["is_estimated"]),
            captured_at_ms=int(r["captured_at_ms"]),
        )
        for r in rows
    ]


def has_estimated(
    conn: sqlite3.Connection,
    *,
    account_hash: str,
    start: date,
    end: date,
) -> bool:
    """True if any cached day in ``[start, end]`` was BS-estimated.
    Used by performance to decide whether to surface the warning."""
    row = conn.execute(
        """
        SELECT 1 FROM account_nav_daily
        WHERE account_hash = ?
          AND day >= ? AND day <= ?
          AND is_estimated = 1
        LIMIT 1
        """,
        (account_hash, start.isoformat(), end.isoformat()),
    ).fetchone()
    return row is not None


def last_cached_day(
    conn: sqlite3.Connection, *, account_hash: str,
) -> date | None:
    """Most recent cached NAV date for ``account_hash``, or ``None``."""
    row = conn.execute(
        "SELECT max(day) AS d FROM account_nav_daily WHERE account_hash = ?",
        (account_hash,),
    ).fetchone()
    if row is None or row["d"] is None:
        return None
    return date.fromisoformat(row["d"])


def first_cached_day(
    conn: sqlite3.Connection, *, account_hash: str,
) -> date | None:
    row = conn.execute(
        "SELECT min(day) AS d FROM account_nav_daily WHERE account_hash = ?",
        (account_hash,),
    ).fetchone()
    if row is None or row["d"] is None:
        return None
    return date.fromisoformat(row["d"])
