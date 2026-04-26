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

# Schema version bumps when the on-disk layout changes. _migrate() is
# responsible for stepping v(N) databases up to the current version
# via additive-only DDL (ALTER TABLE) so we never lose captured data.
_SCHEMA_VERSION = 3

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
    source          TEXT    NOT NULL DEFAULT 'observed',
    PRIMARY KEY (captured_at_ms, symbol)
);

CREATE INDEX IF NOT EXISTS idx_vol_lookup
    ON vol_snapshots (symbol, captured_at_ms);

CREATE TABLE IF NOT EXISTS subscriptions (
    symbol           TEXT    NOT NULL,
    group_name       TEXT    NOT NULL,
    source           TEXT    NOT NULL,
    source_key       TEXT    NOT NULL DEFAULT '',
    subscribed_at    INTEGER NOT NULL,
    unsubscribed_at  INTEGER,
    PRIMARY KEY (symbol, group_name, source, source_key)
);

CREATE INDEX IF NOT EXISTS idx_subs_active
    ON subscriptions (group_name)
    WHERE unsubscribed_at IS NULL;

CREATE TABLE IF NOT EXISTS index_subscriptions (
    index_name       TEXT    NOT NULL,
    group_name       TEXT    NOT NULL,
    subscribed_at    INTEGER NOT NULL,
    unsubscribed_at  INTEGER,
    PRIMARY KEY (index_name, group_name)
);

CREATE TABLE IF NOT EXISTS ticker_state (
    symbol                  TEXT    NOT NULL,
    group_name              TEXT    NOT NULL,
    tier                    TEXT    NOT NULL,
    tier_since              INTEGER NOT NULL,
    consecutive_days_below  INTEGER NOT NULL DEFAULT 0,
    last_evaluated_at       INTEGER NOT NULL,
    PRIMARY KEY (symbol, group_name)
);
"""

# Allowed values for the `source` column.
SOURCE_OBSERVED = "observed"    # captured live from Schwab's chain endpoint
SOURCE_SYNTHETIC = "synthetic"  # BS-reconstructed from option + underlying history

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
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply the schema idempotently and record the schema version.

    v1 → v2 added the ``source`` column so we can distinguish live
    observations from BS-reconstructed (synthetic) historical values.
    Pre-v2 rows are assumed to be live observations.
    """
    conn.executescript(_SCHEMA_DDL)
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    current = row[0] if row else None

    # v1 DBs (from before we added the `source` column) lack it even
    # though the DDL above is idempotent — CREATE TABLE IF NOT EXISTS
    # doesn't alter existing tables. Back-fill the column explicitly.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(vol_snapshots)").fetchall()}
    if "source" not in cols:
        conn.execute(
            "ALTER TABLE vol_snapshots "
            "ADD COLUMN source TEXT NOT NULL DEFAULT 'observed'"
        )

    # v2 → v3: add interpolated tenors, 25Δ wings, hv_30d, raw chain summary,
    # and a generated `archive_date` for fast trading-day lookups.
    v3_columns = {
        "atm_iv_30d":        "REAL",
        "atm_iv_60d":        "REAL",
        "atm_iv_90d":        "REAL",
        "iv_25d_put_30d":    "REAL",
        "iv_25d_call_30d":   "REAL",
        "iv_25d_put_60d":    "REAL",
        "iv_25d_call_60d":   "REAL",
        "iv_25d_put_90d":    "REAL",
        "iv_25d_call_90d":   "REAL",
        "hv_30d":             "REAL",
        "raw_chain_summary":  "TEXT",
    }
    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(vol_snapshots)"
    ).fetchall()}
    for name, sql_type in v3_columns.items():
        if name not in cols:
            conn.execute(
                f"ALTER TABLE vol_snapshots ADD COLUMN {name} {sql_type}"
            )
    # Generated columns don't appear in PRAGMA table_info — use table_xinfo.
    all_cols = {r[1] for r in conn.execute(
        "PRAGMA table_xinfo(vol_snapshots)"
    ).fetchall()}
    if "archive_date" not in all_cols:
        # SQLite generated column (virtual). Uses UTC epoch → UTC date;
        # 'localtime' is rejected by SQLite as non-deterministic in
        # generated columns, so we omit it. The application-layer
        # read_recent_per_day() already handles NY-TZ bucketing in
        # Python, so a UTC date here is sufficient for index lookups.
        conn.execute(
            "ALTER TABLE vol_snapshots "
            "ADD COLUMN archive_date AS ("
            "date(captured_at_ms / 1000, 'unixepoch'))"
        )

    # Create index on (symbol, archive_date) after the generated column is
    # guaranteed to exist. This cannot live in _SCHEMA_DDL because that
    # script runs before the ALTER TABLE above adds archive_date on fresh DBs.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_vol_archive_date "
        "ON vol_snapshots (symbol, archive_date)"
    )

    if current is None:
        conn.execute(
            "INSERT INTO schema_version VALUES (?)", (_SCHEMA_VERSION,)
        )
    elif current < _SCHEMA_VERSION:
        conn.execute(
            "UPDATE schema_version SET version = ?", (_SCHEMA_VERSION,)
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
    source: str = SOURCE_OBSERVED,
) -> None:
    """Insert a single vol snapshot.

    ``captured_at_ms`` defaults to ``now()`` in UTC milliseconds.
    ``source`` is ``'observed'`` for live captures or ``'synthetic'``
    for BS-reconstructed backfill rows.

    Idempotent on ``(captured_at_ms, symbol)`` — a second write for the
    same key is a no-op (first-write-wins via INSERT OR IGNORE), so
    synthetic rows are never clobbered by later observations and vice
    versa.
    """
    if captured_at_ms is None:
        captured_at_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    conn.execute(
        """
        INSERT OR IGNORE INTO vol_snapshots (
            captured_at_ms, symbol, spot, atm_iv,
            atm_strike, atm_expiry, atm_dte, source
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            captured_at_ms, symbol, spot, atm_iv,
            atm_strike, atm_expiry, atm_dte, source,
        ),
    )


def count_snapshots(conn: sqlite3.Connection, *, symbol: str) -> int:
    """Return the total number of rows for ``symbol``."""
    row = conn.execute(
        "SELECT COUNT(*) FROM vol_snapshots WHERE symbol = ?",
        (symbol,),
    ).fetchone()
    return int(row[0]) if row else 0


def count_by_source(conn: sqlite3.Connection, *, symbol: str) -> dict[str, int]:
    """Return ``{'observed': N, 'synthetic': N}`` for ``symbol``.

    Used by the backfill trigger: we should run the one-shot BS
    reconstruction when the user has no synthetics yet AND not enough
    real observations to support a meaningful IVP on their own.
    """
    rows = conn.execute(
        "SELECT source, COUNT(*) FROM vol_snapshots "
        "WHERE symbol = ? GROUP BY source",
        (symbol,),
    ).fetchall()
    out = {SOURCE_OBSERVED: 0, SOURCE_SYNTHETIC: 0}
    for source, n in rows:
        out[source or SOURCE_OBSERVED] = int(n)
    return out


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
    rows = _recent_rows(conn, symbol=symbol)
    per_day: dict[str, float] = {}
    for row in rows:
        ts = datetime.fromtimestamp(row["captured_at_ms"] / 1000, tz=timezone.utc)
        day = ts.astimezone(_NY).date().isoformat()
        per_day[day] = row["atm_iv"]
    days_sorted = sorted(per_day.keys())[-lookback_days:]
    return [per_day[d] for d in days_sorted]


def read_recent_per_day_with_source(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    lookback_days: int,
) -> list[tuple[float, str]]:
    """Like :func:`read_recent_per_day` but returns ``(iv, source)`` tuples
    so the caller can annotate IVP output with observed/synthetic splits."""
    rows = _recent_rows(conn, symbol=symbol)
    per_day: dict[str, tuple[float, str]] = {}
    for row in rows:
        ts = datetime.fromtimestamp(row["captured_at_ms"] / 1000, tz=timezone.utc)
        day = ts.astimezone(_NY).date().isoformat()
        per_day[day] = (row["atm_iv"], row["source"] or SOURCE_OBSERVED)
    days_sorted = sorted(per_day.keys())[-lookback_days:]
    return [per_day[d] for d in days_sorted]


def _recent_rows(conn: sqlite3.Connection, *, symbol: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT captured_at_ms, atm_iv, source
        FROM vol_snapshots
        WHERE symbol = ?
        ORDER BY captured_at_ms ASC
        """,
        (symbol,),
    ).fetchall()


def record_extended_snapshot(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    spot: float,
    atm_iv: float,
    atm_strike: float,
    atm_expiry: str,
    atm_dte: int,
    captured_at_ms: int | None = None,
    source: str = SOURCE_OBSERVED,
    atm_iv_30d: float | None = None,
    atm_iv_60d: float | None = None,
    atm_iv_90d: float | None = None,
    iv_25d_put_30d: float | None = None,
    iv_25d_call_30d: float | None = None,
    iv_25d_put_60d: float | None = None,
    iv_25d_call_60d: float | None = None,
    iv_25d_put_90d: float | None = None,
    iv_25d_call_90d: float | None = None,
    hv_30d: float | None = None,
    raw_chain_summary: dict | None = None,
) -> None:
    """v3-aware single-row insert.

    Wraps :func:`record_snapshot`'s legacy columns and additionally
    writes the v3 columns. Same INSERT OR IGNORE first-write-wins
    contract on ``(captured_at_ms, symbol)``.
    """
    import json
    if captured_at_ms is None:
        captured_at_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    summary_blob = (
        json.dumps(raw_chain_summary, separators=(",", ":"))
        if raw_chain_summary is not None else None
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO vol_snapshots (
            captured_at_ms, symbol, spot, atm_iv,
            atm_strike, atm_expiry, atm_dte, source,
            atm_iv_30d, atm_iv_60d, atm_iv_90d,
            iv_25d_put_30d, iv_25d_call_30d,
            iv_25d_put_60d, iv_25d_call_60d,
            iv_25d_put_90d, iv_25d_call_90d,
            hv_30d, raw_chain_summary
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            captured_at_ms, symbol, spot, atm_iv,
            atm_strike, atm_expiry, atm_dte, source,
            atm_iv_30d, atm_iv_60d, atm_iv_90d,
            iv_25d_put_30d, iv_25d_call_30d,
            iv_25d_put_60d, iv_25d_call_60d,
            iv_25d_put_90d, iv_25d_call_90d,
            hv_30d, summary_blob,
        ),
    )
