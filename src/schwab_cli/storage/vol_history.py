"""SQLite-backed store for daily ATM IV snapshots.

Feeds the IVP percentile in the ``vol`` command: every invocation
appends a row, and a lookback query collapses the series to one value
per NY trading day before the percentile is computed.

Design notes:

* **Schema is additive-only.** We ship a ``schema_version`` row so
  future migrations can be layered without dropping data.
* **INSERT OR IGNORE** is the write mode so a re-run within the same
  millisecond is a no-op (first-write-wins). Same-day duplicates are
  reduced at read time via NY-trading-day bucketing, not by the DB.
* **No cross-process locking logic.** SQLite's own file locking is
  enough for the single-user CLI case.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo

from schwab_cli.storage import storage_dir

_SCHEMA_VERSION = 1

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS vol_snapshots (
    captured_at_ms  INTEGER NOT NULL,
    symbol          TEXT    NOT NULL,
    spot            REAL    NOT NULL,
    atm_iv          REAL    NOT NULL,
    atm_strike      REAL    NOT NULL,
    atm_expiry      TEXT    NOT NULL,
    atm_dte         INTEGER NOT NULL,
    PRIMARY KEY (captured_at_ms, symbol)
);

CREATE INDEX IF NOT EXISTS idx_vol_lookup
    ON vol_snapshots (symbol, captured_at_ms);
"""

_NY = ZoneInfo("America/New_York")


def db_path() -> Path:
    """Absolute path to the vol_history SQLite file."""
    return storage_dir() / "vol_history.db"


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
    try:
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply the schema idempotently and record the schema version."""
    conn.executescript(_SCHEMA_DDL)
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO schema_version VALUES (?)", (_SCHEMA_VERSION,)
        )


def record_snapshot(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    spot: float,
    atm_iv: float,
    atm_strike: float,
    atm_expiry: str,
    atm_dte: int,
    captured_at_ms: int | None = None,
) -> None:
    """Insert a single vol snapshot.

    ``captured_at_ms`` defaults to ``now()`` in UTC milliseconds.
    Idempotent on ``(captured_at_ms, symbol)`` — a second identical
    write is a no-op (first-write-wins via INSERT OR IGNORE).
    """
    if captured_at_ms is None:
        captured_at_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    conn.execute(
        """
        INSERT OR IGNORE INTO vol_snapshots (
            captured_at_ms, symbol, spot, atm_iv,
            atm_strike, atm_expiry, atm_dte
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            captured_at_ms, symbol, spot, atm_iv,
            atm_strike, atm_expiry, atm_dte,
        ),
    )


def read_recent_per_day(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    lookback_days: int,
) -> list[float]:
    """Return at most ``lookback_days`` IV values, one per NY trading day.

    When multiple writes exist for the same NY day, the latest-captured
    value wins. The return list is sorted by day ascending and trimmed
    to the most recent ``lookback_days`` entries.
    """
    rows = conn.execute(
        """
        SELECT captured_at_ms, atm_iv
        FROM vol_snapshots
        WHERE symbol = ?
        ORDER BY captured_at_ms ASC
        """,
        (symbol,),
    ).fetchall()

    per_day: dict[str, float] = {}
    for row in rows:
        ts = datetime.fromtimestamp(row["captured_at_ms"] / 1000, tz=timezone.utc)
        day = ts.astimezone(_NY).date().isoformat()
        per_day[day] = row["atm_iv"]  # later rows overwrite earlier same-day

    days_sorted = sorted(per_day.keys())[-lookback_days:]
    return [per_day[d] for d in days_sorted]
