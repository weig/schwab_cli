"""SQLite I/O for the dataset feature — subscriptions, indices, ticker_state.

All functions take an explicit ``sqlite3.Connection`` so callers
control the transaction boundary. None of these functions ``commit()``
themselves — the caller (typically :func:`vol_history.connect`'s
context manager) is responsible.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any


def _now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


# ---- equity subscriptions ----------------------------------------------


def subscribe_equity(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    group_name: str,
    captured_at_ms: int | None = None,
) -> None:
    """Insert or revive an equity subscription.

    Idempotent — a re-subscribe of an already-active row is a no-op;
    a re-subscribe after unsubscribe clears ``unsubscribed_at``.
    """
    if captured_at_ms is None:
        captured_at_ms = _now_ms()
    conn.execute(
        """
        INSERT INTO subscriptions
          (symbol, group_name, source, source_key,
           subscribed_at, unsubscribed_at)
        VALUES (?, ?, 'equity', '', ?, NULL)
        ON CONFLICT (symbol, group_name, source, source_key) DO UPDATE SET
          subscribed_at   = excluded.subscribed_at,
          unsubscribed_at = NULL
        WHERE subscriptions.unsubscribed_at IS NOT NULL
        """,
        (symbol, group_name, captured_at_ms),
    )


def unsubscribe_equity(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    group_name: str,
    captured_at_ms: int | None = None,
) -> None:
    if captured_at_ms is None:
        captured_at_ms = _now_ms()
    conn.execute(
        """
        UPDATE subscriptions SET unsubscribed_at = ?
        WHERE symbol = ? AND group_name = ?
          AND source = 'equity' AND source_key = ''
          AND unsubscribed_at IS NULL
        """,
        (captured_at_ms, symbol, group_name),
    )


def list_active_subscriptions(
    conn: sqlite3.Connection,
    *,
    group_name: str | None = None,
) -> list[sqlite3.Row]:
    """Return all rows in ``subscriptions`` where ``unsubscribed_at`` is NULL."""
    if group_name is None:
        rows = conn.execute(
            "SELECT * FROM subscriptions WHERE unsubscribed_at IS NULL "
            "ORDER BY symbol, source, source_key"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM subscriptions "
            "WHERE unsubscribed_at IS NULL AND group_name = ? "
            "ORDER BY symbol, source, source_key",
            (group_name,),
        ).fetchall()
    return rows
