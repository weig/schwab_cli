"""SQLite-backed cache for Schwab transactions.

Sibling of vol_history.py. Stores transactions by ``activity_id``
(verified globally unique per user) with the raw payload as JSON,
plus a coverage table recording which ``[start_ms, end_ms]`` ranges
of activity ``time`` we've already fetched per account.

Schema is additive-only; same migration pattern as vol_history.

The ``time`` column is the source of truth for cache range tracking
because Schwab's transactions endpoint filters its ``startDate`` /
``endDate`` query params against that field, not ``tradeDate``.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from schwab_cli.storage import storage_dir


_SCHEMA_VERSION = 1

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

-- ``activity_id`` is the PRIMARY KEY because Schwab's activityId is
-- globally unique across all transactions for a user (verified
-- 2026-05-04: 105 transactions over 4 months returned 105 distinct
-- ids, no None, no duplicates). We keep ``account_hash`` as a
-- regular column for filtering and indexing.
CREATE TABLE IF NOT EXISTS transactions (
    activity_id     INTEGER PRIMARY KEY,
    account_hash    TEXT    NOT NULL,
    time_ms         INTEGER NOT NULL,   -- activity ``time`` as UTC unix ms
    trade_date_ms   INTEGER,
    type            TEXT    NOT NULL,
    status          TEXT,
    sub_account     TEXT,
    account_number  TEXT,
    symbol          TEXT,                -- best-effort: first non-fee leg symbol
    net_amount      REAL,                -- final amount after fees
    gross_amount    REAL,                -- non-fee leg's cost (the "before fees" amount)
    total_fees      REAL,                -- sum of cost across all feeType legs (signed)
    payload         TEXT    NOT NULL,    -- raw JSON (per-fee-type breakdown lives here)
    cached_at_ms    INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_txn_account_time
    ON transactions (account_hash, time_ms);

CREATE INDEX IF NOT EXISTS idx_txn_type
    ON transactions (account_hash, type);

CREATE INDEX IF NOT EXISTS idx_txn_symbol
    ON transactions (account_hash, symbol);

CREATE TABLE IF NOT EXISTS transactions_coverage (
    account_hash    TEXT    NOT NULL,
    start_ms        INTEGER NOT NULL,
    end_ms          INTEGER NOT NULL,
    fetched_at_ms   INTEGER NOT NULL,
    PRIMARY KEY (account_hash, start_ms)
);

CREATE INDEX IF NOT EXISTS idx_cov_account_range
    ON transactions_coverage (account_hash, start_ms, end_ms);
"""


def db_path() -> Path:
    """Absolute path to the account-level SQLite cache."""
    return storage_dir() / "account.db"


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Open a connection, run migrations, commit on clean exit."""
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA_DDL)
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO schema_version VALUES (?)", (_SCHEMA_VERSION,)
        )
    elif row[0] < _SCHEMA_VERSION:
        conn.execute(
            "UPDATE schema_version SET version = ?", (_SCHEMA_VERSION,)
        )
