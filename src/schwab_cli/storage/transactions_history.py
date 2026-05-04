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

import json
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


def _parse_iso_ms(iso: str | None) -> int | None:
    """Schwab times look like '2026-04-30T20:49:16+0000'. Convert to UTC ms."""
    if not iso:
        return None
    s = iso.replace("Z", "+00:00")
    # Normalise '+0000' → '+00:00' for fromisoformat tolerance.
    if len(s) >= 5 and s[-5] in "+-" and s[-3] != ":":
        s = s[:-2] + ":" + s[-2:]
    return int(datetime.fromisoformat(s).timestamp() * 1000)


def _first_non_fee_leg(payload: dict) -> dict | None:
    """The first transferItem whose ``feeType`` is None — the economic
    instrument leg. Mirrors output/transactions.py:_main_leg semantics."""
    for it in (payload.get("transferItems") or []):
        if it.get("feeType") is None:
            return it
    return None


def _first_non_fee_symbol(payload: dict) -> str | None:
    """Best-effort symbol for index lookups.

    Currency-only legs (cash receipts, dividends) return None — those
    are still queryable by ``description`` via the JSON payload.
    """
    main = _first_non_fee_leg(payload)
    if main is None:
        return None
    inst = main.get("instrument") or {}
    sym = inst.get("symbol")
    if sym and inst.get("assetType") != "CURRENCY":
        return sym
    return None


def _gross_amount(payload: dict) -> float | None:
    """The non-fee leg's ``cost`` — the trade amount before fees."""
    main = _first_non_fee_leg(payload)
    if main is None:
        return None
    cost = main.get("cost")
    try:
        return float(cost) if cost is not None else None
    except (TypeError, ValueError):
        return None


def _total_fees(payload: dict) -> float | None:
    """Sum of ``cost`` across all transferItems with ``feeType`` set.

    Signed: negative when the user pays (typical), positive on rebate.
    Returns 0.0 when there are zero fee legs but ``transferItems``
    exists. Returns None only when ``transferItems`` is missing/null.
    """
    items = payload.get("transferItems")
    if items is None:
        return None
    total = 0.0
    for it in items:
        if it.get("feeType") is None:
            continue
        cost = it.get("cost")
        try:
            if cost is not None:
                total += float(cost)
        except (TypeError, ValueError):
            pass
    return total


def upsert_many(
    conn: sqlite3.Connection, account_hash: str, payloads: list[dict],
) -> int:
    """Upsert transaction payloads. Returns the number of rows touched.

    PRIMARY KEY is ``activity_id`` alone — Schwab's activityId is
    globally unique across all transactions for a user (verified
    empirically; see DDL comment). ``account_hash`` is captured as a
    column for filtering but not part of the key.

    Last-write-wins on ``activity_id``: a Schwab status update or
    netAmount correction replaces the prior row in full.
    """
    if not payloads:
        return 0
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    rows = []
    for p in payloads:
        activity_id = p.get("activityId")
        if activity_id is None:
            continue  # malformed — skip rather than crash
        net = p.get("netAmount")
        rows.append((
            int(activity_id),
            account_hash,
            _parse_iso_ms(p.get("time")) or 0,
            _parse_iso_ms(p.get("tradeDate")),
            p.get("type") or "",
            p.get("status"),
            p.get("subAccount"),
            p.get("accountNumber"),
            _first_non_fee_symbol(p),
            float(net) if net is not None else None,
            _gross_amount(p),
            _total_fees(p),
            json.dumps(p, separators=(",", ":"), default=str),
            now_ms,
        ))
    conn.executemany(
        """
        INSERT INTO transactions (
            activity_id, account_hash, time_ms, trade_date_ms, type,
            status, sub_account, account_number, symbol, net_amount,
            gross_amount, total_fees, payload, cached_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (activity_id) DO UPDATE SET
            account_hash   = excluded.account_hash,
            time_ms        = excluded.time_ms,
            trade_date_ms  = excluded.trade_date_ms,
            type           = excluded.type,
            status         = excluded.status,
            sub_account    = excluded.sub_account,
            account_number = excluded.account_number,
            symbol         = excluded.symbol,
            net_amount     = excluded.net_amount,
            gross_amount   = excluded.gross_amount,
            total_fees     = excluded.total_fees,
            payload        = excluded.payload,
            cached_at_ms   = excluded.cached_at_ms
        """,
        rows,
    )
    return len(rows)


def read_range(
    conn: sqlite3.Connection,
    *,
    account_hash: str,
    start_ms: int,
    end_ms: int,
) -> list[dict]:
    """Return raw payloads with ``time_ms`` in [start_ms, end_ms].

    Inclusive on both ends. Sorted by time_ms ascending. Type / symbol
    filters are applied by the caller — this layer is range-only so
    the cache never accidentally suppresses rows it has on disk.
    """
    rows = conn.execute(
        """
        SELECT payload FROM transactions
        WHERE account_hash = ? AND time_ms BETWEEN ? AND ?
        ORDER BY time_ms ASC
        """,
        (account_hash, start_ms, end_ms),
    ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


def read_coverage(
    conn: sqlite3.Connection, *, account_hash: str,
) -> list[tuple[int, int]]:
    """Return [(start_ms, end_ms), ...] sorted ascending. Non-overlapping."""
    rows = conn.execute(
        """
        SELECT start_ms, end_ms FROM transactions_coverage
        WHERE account_hash = ?
        ORDER BY start_ms ASC
        """,
        (account_hash,),
    ).fetchall()
    return [(int(r["start_ms"]), int(r["end_ms"])) for r in rows]


def merge_coverage(
    conn: sqlite3.Connection,
    account_hash: str,
    *,
    start_ms: int,
    end_ms: int,
) -> None:
    """Insert [start_ms, end_ms] and merge with overlapping/adjacent rows.

    "Adjacent" = ``end_ms + 1 == next start_ms``. We treat ms-touching
    as contiguous so 60-day chunked fetches collapse into one row
    instead of fragmenting the table.

    Implementation: pull all overlapping/adjacent rows, compute the
    union, delete them, insert one merged row. O(n) per call but n is
    tiny in practice (typically <10 rows per account).
    """
    if start_ms > end_ms:
        return
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    # Adjacent + overlapping selector: any existing row whose
    # [s, e] satisfies s <= end_ms + 1 AND e >= start_ms - 1.
    rows = conn.execute(
        """
        SELECT start_ms, end_ms FROM transactions_coverage
        WHERE account_hash = ? AND start_ms <= ? AND end_ms >= ?
        """,
        (account_hash, end_ms + 1, start_ms - 1),
    ).fetchall()
    new_start = start_ms
    new_end = end_ms
    for r in rows:
        new_start = min(new_start, int(r["start_ms"]))
        new_end = max(new_end, int(r["end_ms"]))
    conn.execute(
        """
        DELETE FROM transactions_coverage
        WHERE account_hash = ? AND start_ms <= ? AND end_ms >= ?
        """,
        (account_hash, end_ms + 1, start_ms - 1),
    )
    conn.execute(
        """
        INSERT INTO transactions_coverage (
            account_hash, start_ms, end_ms, fetched_at_ms
        ) VALUES (?, ?, ?, ?)
        """,
        (account_hash, new_start, new_end, now_ms),
    )


def coverage_gaps(
    conn: sqlite3.Connection,
    *,
    account_hash: str,
    start_ms: int,
    end_ms: int,
) -> list[tuple[int, int]]:
    """Return sub-ranges of [start_ms, end_ms] not covered by cache.

    Each returned range is inclusive on both ends. Caller is expected
    to fetch each range from the API and call ``merge_coverage`` after.

    Empty list ⇒ fully covered by cache.
    """
    if start_ms > end_ms:
        return []
    cov = read_coverage(conn, account_hash=account_hash)
    gaps: list[tuple[int, int]] = []
    cursor = start_ms
    for cs, ce in cov:
        if ce < cursor:
            continue
        if cs > end_ms:
            break
        if cs > cursor:
            gaps.append((cursor, cs - 1))
        cursor = max(cursor, ce + 1)
        if cursor > end_ms:
            return gaps
    if cursor <= end_ms:
        gaps.append((cursor, end_ms))
    return gaps
