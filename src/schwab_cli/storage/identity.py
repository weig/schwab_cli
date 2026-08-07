"""Ticker-identity store — the CUSIP guard against silent ticker reuse.

Thin persistence around ``underlying_identity`` (schema v10). The
classification logic lives in :mod:`schwab_cli.analytics.corporate_actions`;
this module only reads/writes rows and exposes one orchestration helper.
"""
from __future__ import annotations

import sqlite3

from schwab_cli.analytics.corporate_actions import classify_identity


def read_identity(conn: sqlite3.Connection, symbol: str) -> dict | None:
    row = conn.execute(
        "SELECT symbol, cusip, description, first_seen_ms, last_seen_ms, "
        "quarantined FROM underlying_identity WHERE symbol = ?",
        (symbol,),
    ).fetchone()
    return dict(row) if row else None


def is_quarantined(conn: sqlite3.Connection, symbol: str) -> bool:
    row = conn.execute(
        "SELECT quarantined FROM underlying_identity WHERE symbol = ?",
        (symbol,),
    ).fetchone()
    return bool(row and row["quarantined"])


def _upsert(conn, symbol, cusip, description, now_ms, quarantined) -> None:
    conn.execute(
        """
        INSERT INTO underlying_identity
            (symbol, cusip, description, first_seen_ms, last_seen_ms, quarantined)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol) DO UPDATE SET
            cusip = excluded.cusip,
            description = excluded.description,
            last_seen_ms = excluded.last_seen_ms,
            quarantined = excluded.quarantined
        """,
        (symbol, cusip, description, now_ms, now_ms, quarantined),
    )


def check_and_record_identity(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    cusip: str | None,
    description: str | None,
    now_ms: int,
) -> str:
    """Classify ``symbol``'s current identity, persist the outcome, and return
    the verdict from :func:`classify_identity`.

    * ``new`` / ``ok`` — record/update the identity, keep sampling.
    * ``corporate_action`` — update the stored CUSIP (same issuer); the OHLCV
      overlap check heals prices. Keep sampling.
    * ``reuse`` — a different company took the ticker over. **Quarantine**:
      set the flag and DO NOT update the CUSIP, so the caller skips writing
      cross-company history. Cleared only by an explicit re-key.
    """
    prior = read_identity(conn, symbol)
    verdict = classify_identity(
        prior.get("cusip") if prior else None,
        prior.get("description") if prior else None,
        cusip, description,
    )
    if verdict == "reuse":
        # Keep the OLD identity row but flag it; never overwrite with the
        # new company's CUSIP (that would erase the boundary).
        conn.execute(
            "UPDATE underlying_identity SET quarantined = 1, last_seen_ms = ? "
            "WHERE symbol = ?",
            (now_ms, symbol),
        )
        return verdict
    # new / ok / corporate_action all record the current identity.
    _upsert(conn, symbol, cusip, description, now_ms, quarantined=0)
    return verdict
