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
_SCHEMA_VERSION = 7

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

-- Daily OHLCV cache. Populated by the market-data cron + by the
-- `history` command on miss. ``day`` is an ISO date anchored to the
-- America/New_York trading day; PK (symbol, day) makes re-pulls
-- idempotent.
CREATE TABLE IF NOT EXISTS ohlcv_daily (
    symbol         TEXT    NOT NULL,
    day            TEXT    NOT NULL,
    open           REAL    NOT NULL,
    high           REAL    NOT NULL,
    low            REAL    NOT NULL,
    close          REAL    NOT NULL,
    volume         INTEGER NOT NULL,
    captured_at_ms INTEGER NOT NULL,
    PRIMARY KEY (symbol, day)
);

CREATE INDEX IF NOT EXISTS idx_ohlcv_symbol_day
    ON ohlcv_daily (symbol, day);

-- Daily account NAV snapshots — what powers period-bounded
-- performance attribution. ``is_estimated = 1`` indicates the day
-- used BS-reconstructed option prices instead of true historical
-- marketValue; the performance command surfaces a warning when any
-- queried day carries this flag.
CREATE TABLE IF NOT EXISTS account_nav_daily (
    account_hash    TEXT    NOT NULL,
    day             TEXT    NOT NULL,
    market_value    REAL    NOT NULL,
    cash            REAL    NOT NULL,
    total_value     REAL    NOT NULL,
    is_estimated    INTEGER NOT NULL DEFAULT 0,
    captured_at_ms  INTEGER NOT NULL,
    PRIMARY KEY (account_hash, day)
);

CREATE INDEX IF NOT EXISTS idx_account_nav_account_day
    ON account_nav_daily (account_hash, day);

-- ===================== Options VRP Screener (v7) =====================
-- Per-symbol per-trading-day snapshot of the target ~30 DTE / ~-0.25Δ
-- put located on the live chain. PK (snapshot_date, symbol) makes daily
-- re-runs idempotent. captured_at_ms retained for audit. rv_fwd_21d is
-- NULL at capture and backfilled T+21. snapshot_quality/filter_reason
-- annotate why a row is excluded from ranking (kept for diagnosis).
CREATE TABLE IF NOT EXISTS contract_snapshots (
    snapshot_date       TEXT    NOT NULL,
    symbol              TEXT    NOT NULL,
    captured_at_ms      INTEGER NOT NULL,
    target_expiry       TEXT,
    dte                 INTEGER,
    put_strike          REAL,
    put_delta_actual    REAL,
    put_bid             REAL,
    put_ask             REAL,
    put_mid             REAL,
    put_oi              INTEGER,
    put_volume          INTEGER,
    spread_pct          REAL,
    underlying_last     REAL,
    atm_iv_30d          REAL,
    hv_30d              REAL,
    ivr                 REAL,
    ivr_low_conf        INTEGER NOT NULL DEFAULT 0,
    next_earnings_date  TEXT,
    days_to_earnings    INTEGER,
    rv_fwd_21d          REAL,
    snapshot_quality    TEXT    NOT NULL DEFAULT 'ok',
    filter_reason       TEXT,
    PRIMARY KEY (snapshot_date, symbol)
);

CREATE INDEX IF NOT EXISTS idx_contract_snap_symbol
    ON contract_snapshots (symbol, snapshot_date);

-- Generic event calendar. v1 populates only event_type='earnings';
-- confirmed=0 means an estimated date (still treated as valid — we
-- would rather over-exclude than sell into an event).
CREATE TABLE IF NOT EXISTS events (
    symbol        TEXT    NOT NULL,
    event_type    TEXT    NOT NULL,
    event_date    TEXT    NOT NULL,
    confirmed     INTEGER NOT NULL DEFAULT 0,
    updated_at_ms INTEGER NOT NULL,
    PRIMARY KEY (symbol, event_type, event_date)
);

CREATE INDEX IF NOT EXISTS idx_events_symbol_type
    ON events (symbol, event_type, event_date);

-- Point-in-time index membership. Dated so any historical candidate
-- universe is reconstructable (survivorship guard). Never overwritten.
CREATE TABLE IF NOT EXISTS index_membership (
    as_of_date     TEXT    NOT NULL,
    index_name     TEXT    NOT NULL,
    symbol         TEXT    NOT NULL,
    captured_at_ms INTEGER NOT NULL,
    PRIMARY KEY (as_of_date, index_name, symbol)
);

-- Daily screener output. Candidate pool = ranks 1..10. Consumed by the
-- portfolio layer only; the screener never places an order.
CREATE TABLE IF NOT EXISTS daily_ranking (
    ranking_date      TEXT    NOT NULL,
    rank              INTEGER NOT NULL,
    symbol            TEXT    NOT NULL,
    executable_vrp    REAL    NOT NULL,
    premium_yield_bid REAL,
    fair_yield        REAL,
    ivr               REAL,
    ivr_low_conf      INTEGER NOT NULL DEFAULT 0,
    put_strike        REAL,
    put_delta_actual  REAL,
    put_bid           REAL,
    dte               INTEGER,
    target_expiry     TEXT,
    spread_pct        REAL,
    underlying_last   REAL,
    PRIMARY KEY (ranking_date, symbol)
);

CREATE INDEX IF NOT EXISTS idx_daily_ranking_date_rank
    ON daily_ranking (ranking_date, rank);

-- Paper (virtual) ledger — validation harness. Records top-10 and
-- bottom-10 virtual 1-contract put sales daily; settled at expiry. No
-- roll / no early close: tests raw ranking discrimination only.
CREATE TABLE IF NOT EXISTS paper_ledger (
    open_date     TEXT    NOT NULL,
    symbol        TEXT    NOT NULL,
    cohort        TEXT    NOT NULL,
    strike        REAL    NOT NULL,
    dte           INTEGER NOT NULL,
    premium_bid   REAL    NOT NULL,
    expiry        TEXT    NOT NULL,
    settle_price  REAL,
    pnl           REAL,
    settled_at    INTEGER,
    PRIMARY KEY (open_date, symbol)
);

CREATE INDEX IF NOT EXISTS idx_paper_ledger_unsettled
    ON paper_ledger (expiry) WHERE settled_at IS NULL;
"""

# Allowed values for the `source` column.
SOURCE_OBSERVED = "observed"    # captured live from Schwab's chain endpoint
SOURCE_SYNTHETIC = "synthetic"  # BS-reconstructed from option + underlying history

_NY = ZoneInfo("America/New_York")


def db_path() -> Path:
    """Absolute path to the market_data SQLite file.

    Renamed from ``vol_history.db`` in v4 — the same physical store
    now backs OHLCV, volatility, and any future per-symbol time series.
    """
    return storage_dir() / "market_data.db"


_LEGACY_DB_NAME = "vol_history.db"


def _rename_legacy_db_in_place(new_path: Path) -> None:
    """Idempotently move legacy ``vol_history.db`` (+ WAL/SHM sidecars)
    to ``market_data.db``.

    Refuses (``RuntimeError``) if both files exist — silent clobber
    would destroy data.
    """
    legacy = new_path.parent / _LEGACY_DB_NAME
    if not legacy.exists():
        return
    if new_path.exists():
        raise RuntimeError(
            f"both files exist — refusing to clobber. "
            f"legacy: {legacy} | new: {new_path}. "
            f"resolve manually before retrying."
        )
    legacy.rename(new_path)
    for suffix in ("-wal", "-shm"):
        side = legacy.with_name(legacy.name + suffix)
        if side.exists():
            side.rename(new_path.with_name(new_path.name + suffix))


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Open a connection, run migrations, commit on clean exit."""
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    _rename_legacy_db_in_place(path)
    # ``timeout`` is the connection-level busy timeout: how long
    # sqlite3 waits to acquire a write lock before raising
    # OperationalError("database is locked"). The scheduler runs
    # market-data, accounts, and indices in parallel — all three
    # write to this same file. Without a generous timeout one of
    # them hits the lock on the others' commit and fails the whole
    # job. 30s is comfortably longer than any single transaction we
    # do (per-symbol upserts complete in ms), so writers wait their
    # turn instead of failing.
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    # Defence-in-depth: also set busy_timeout at the SQLite layer.
    # Python's ``timeout=`` argument hooks the busy handler; both
    # mechanisms target the same problem but the explicit pragma
    # survives connection inheritance into spawned helpers.
    conn.execute("PRAGMA busy_timeout = 30000")
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

    # v3 → v4: mirror every currently-active volatility subscription
    # into a parallel ohlcv subscription so the new market-data cron
    # can iterate the ohlcv group without an explicit user opt-in.
    # ON CONFLICT DO NOTHING keeps this idempotent.
    if current is None or current < 4:
        conn.execute(
            """
            INSERT INTO subscriptions
                (symbol, group_name, source, source_key,
                 subscribed_at, unsubscribed_at)
            SELECT symbol, 'ohlcv', source, source_key,
                   subscribed_at, unsubscribed_at
            FROM subscriptions
            WHERE group_name = 'volatility'
              AND unsubscribed_at IS NULL
            ON CONFLICT (symbol, group_name, source, source_key)
            DO NOTHING
            """
        )

    # v5 → v6: one-time purge of historical rows that stored Schwab's
    # `-999` "IV unavailable" sentinel (as a -9.99 atm_iv) or any other
    # non-positive IV. The ingestion guard in `flatten_chain` now drops
    # these at the source; this sweep clears legacy junk so IVR/IVP ranges
    # aren't skewed. Skipped on fresh DBs (current is None → no rows yet).
    if current is not None and current < 6:
        delete_implausible_iv_snapshots(conn)

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


def delete_implausible_iv_snapshots(conn: sqlite3.Connection) -> int:
    """Delete vol_snapshots rows with a non-positive atm_iv (Schwab's
    -999 'IV unavailable' sentinel stored as -9.99, or other junk).
    Returns the number of rows deleted.

    Like :func:`record_snapshot`, this does not commit — the ``connect()``
    context manager commits on clean exit.
    """
    cur = conn.execute("DELETE FROM vol_snapshots WHERE atm_iv <= 0")
    return cur.rowcount


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
        WHERE symbol = ? AND atm_iv > 0
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


def read_atm_iv_30d_per_day(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    lookback_days: int,
) -> list[float]:
    """Like :func:`read_recent_per_day` but reads ``atm_iv_30d`` and
    skips NULL rows.

    Same NY-trading-day bucketing — last write wins. Returned in
    chronological order, trimmed to ``lookback_days`` entries.
    """
    rows = conn.execute(
        """
        SELECT captured_at_ms, atm_iv_30d
        FROM vol_snapshots
        WHERE symbol = ? AND atm_iv_30d IS NOT NULL
        ORDER BY captured_at_ms ASC
        """,
        (symbol,),
    ).fetchall()
    per_day: dict[str, float] = {}
    for row in rows:
        ts = datetime.fromtimestamp(row["captured_at_ms"] / 1000, tz=timezone.utc)
        day = ts.astimezone(_NY).date().isoformat()
        per_day[day] = row["atm_iv_30d"]
    days = sorted(per_day.keys())[-lookback_days:]
    return [per_day[d] for d in days]
