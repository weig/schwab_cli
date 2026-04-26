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


# ---- supported indices ------------------------------------------------

SUPPORTED_INDICES = frozenset({"SPX", "DJI", "NQ", "RUT"})


# ---- index_subscriptions ----------------------------------------------


def subscribe_index(
    conn: sqlite3.Connection,
    *,
    index_name: str,
    group_name: str,
    captured_at_ms: int | None = None,
) -> None:
    if index_name not in SUPPORTED_INDICES:
        raise ValueError(
            f"{index_name!r} not in supported index set "
            f"{sorted(SUPPORTED_INDICES)}"
        )
    if captured_at_ms is None:
        captured_at_ms = _now_ms()
    conn.execute(
        """
        INSERT INTO index_subscriptions
          (index_name, group_name, subscribed_at, unsubscribed_at)
        VALUES (?, ?, ?, NULL)
        ON CONFLICT (index_name, group_name) DO UPDATE SET
          subscribed_at   = excluded.subscribed_at,
          unsubscribed_at = NULL
        WHERE index_subscriptions.unsubscribed_at IS NOT NULL
        """,
        (index_name, group_name, captured_at_ms),
    )


def unsubscribe_index(
    conn: sqlite3.Connection,
    *,
    index_name: str,
    group_name: str,
    captured_at_ms: int | None = None,
) -> None:
    if captured_at_ms is None:
        captured_at_ms = _now_ms()
    conn.execute(
        """
        UPDATE index_subscriptions SET unsubscribed_at = ?
        WHERE index_name = ? AND group_name = ?
          AND unsubscribed_at IS NULL
        """,
        (captured_at_ms, index_name, group_name),
    )


def list_active_index_subscriptions(
    conn: sqlite3.Connection,
    *,
    group_name: str | None = None,
) -> list[sqlite3.Row]:
    if group_name is None:
        return conn.execute(
            "SELECT * FROM index_subscriptions WHERE unsubscribed_at IS NULL "
            "ORDER BY index_name"
        ).fetchall()
    return conn.execute(
        "SELECT * FROM index_subscriptions "
        "WHERE unsubscribed_at IS NULL AND group_name = ? "
        "ORDER BY index_name",
        (group_name,),
    ).fetchall()


# ---- position subscriptions -------------------------------------------


def subscribe_position(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    group_name: str,
    account_hash_last4: str,
    captured_at_ms: int | None = None,
) -> None:
    if captured_at_ms is None:
        captured_at_ms = _now_ms()
    conn.execute(
        """
        INSERT INTO subscriptions
          (symbol, group_name, source, source_key,
           subscribed_at, unsubscribed_at)
        VALUES (?, ?, 'position', ?, ?, NULL)
        ON CONFLICT (symbol, group_name, source, source_key) DO UPDATE SET
          subscribed_at   = excluded.subscribed_at,
          unsubscribed_at = NULL
        WHERE subscriptions.unsubscribed_at IS NOT NULL
        """,
        (symbol, group_name, account_hash_last4, captured_at_ms),
    )


def unsubscribe_position(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    group_name: str,
    account_hash_last4: str,
    captured_at_ms: int | None = None,
) -> None:
    if captured_at_ms is None:
        captured_at_ms = _now_ms()
    conn.execute(
        """
        UPDATE subscriptions SET unsubscribed_at = ?
        WHERE symbol = ? AND group_name = ?
          AND source = 'position' AND source_key = ?
          AND unsubscribed_at IS NULL
        """,
        (captured_at_ms, symbol, group_name, account_hash_last4),
    )


def sources_for_symbol(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    group_name: str,
) -> set[str]:
    """Return the set of distinct active source labels for a symbol."""
    rows = conn.execute(
        "SELECT DISTINCT source FROM subscriptions "
        "WHERE symbol = ? AND group_name = ? AND unsubscribed_at IS NULL",
        (symbol, group_name),
    ).fetchall()
    return {r["source"] for r in rows}


def last_close_at_for_symbol(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    group_name: str,
) -> int | None:
    """Return ms timestamp of the most recent position-source close.

    Returns None if any position-source row for this symbol is still
    active (i.e., the position is currently open).
    """
    rows = conn.execute(
        "SELECT unsubscribed_at FROM subscriptions "
        "WHERE symbol = ? AND group_name = ? AND source = 'position'",
        (symbol, group_name),
    ).fetchall()
    if not rows:
        return None
    if any(r["unsubscribed_at"] is None for r in rows):
        return None
    closes = [r["unsubscribed_at"] for r in rows
              if r["unsubscribed_at"] is not None]
    return max(closes) if closes else None


# ---- ticker_state -----------------------------------------------------


def read_ticker_state(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    group_name: str,
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM ticker_state WHERE symbol = ? AND group_name = ?",
        (symbol, group_name),
    ).fetchone()


def write_ticker_state(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    group_name: str,
    tier: str,
    tier_since: int,
    consecutive_days_below: int,
    last_evaluated_at: int,
) -> None:
    conn.execute(
        """
        INSERT INTO ticker_state
          (symbol, group_name, tier, tier_since,
           consecutive_days_below, last_evaluated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (symbol, group_name) DO UPDATE SET
          tier                   = excluded.tier,
          tier_since             = excluded.tier_since,
          consecutive_days_below = excluded.consecutive_days_below,
          last_evaluated_at      = excluded.last_evaluated_at
        """,
        (symbol, group_name, tier, tier_since,
         consecutive_days_below, last_evaluated_at),
    )


def read_status_rows(
    conn: sqlite3.Connection,
    *,
    group_name: str,
    tier: str | None = None,
    source: str | None = None,
    symbols: list[str] | None = None,
) -> list[dict]:
    """Aggregate per-symbol status: tier, sources, first/last data, etc."""
    where = ["s.unsubscribed_at IS NULL", "s.group_name = ?"]
    params: list[Any] = [group_name]
    if symbols:
        placeholder = ",".join("?" * len(symbols))
        where.append(f"s.symbol IN ({placeholder})")
        params.extend(symbols)

    sub_rows = conn.execute(
        f"""
        SELECT symbol, source, source_key, subscribed_at
        FROM subscriptions s
        WHERE {' AND '.join(where)}
        ORDER BY symbol
        """,
        params,
    ).fetchall()

    by_symbol: dict[str, dict] = {}
    for r in sub_rows:
        d = by_symbol.setdefault(r["symbol"], {
            "symbol":         r["symbol"],
            "group":          group_name,
            "sources":        [],
            "subscribed_at":  r["subscribed_at"],
        })
        label = r["source"] if not r["source_key"] else (
            f"{r['source']}={r['source_key']}"
        )
        d["sources"].append(label)
        d["subscribed_at"] = min(d["subscribed_at"], r["subscribed_at"])

    if source:
        by_symbol = {
            s: d for s, d in by_symbol.items()
            if any(label.startswith(source) for label in d["sources"])
        }

    for symbol, d in by_symbol.items():
        ts = read_ticker_state(conn, symbol=symbol, group_name=group_name)
        d["tier"] = ts["tier"] if ts else "GRACE"
        d["tier_since"] = ts["tier_since"] if ts else d["subscribed_at"]
        first_last = conn.execute(
            "SELECT MIN(archive_date) AS fd, MAX(archive_date) AS ld, "
            "COUNT(*) AS n FROM vol_snapshots WHERE symbol = ?",
            (symbol,),
        ).fetchone()
        d["first_date"] = first_last["fd"]
        d["last_date"]  = first_last["ld"]
        d["n_days"]     = first_last["n"]

    out = list(by_symbol.values())
    if tier:
        out = [d for d in out if d["tier"] == tier]
    rank = {"ACTIVE": 0, "GRACE": 1, "WATCH": 2, "FROZEN": 3}
    out.sort(key=lambda d: (rank.get(d["tier"], 99), d["symbol"]))
    return out


def list_ticker_states(
    conn: sqlite3.Connection,
    *,
    group_name: str,
    tier: str | None = None,
) -> list[sqlite3.Row]:
    if tier is None:
        return conn.execute(
            "SELECT * FROM ticker_state WHERE group_name = ? "
            "ORDER BY symbol",
            (group_name,),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM ticker_state WHERE group_name = ? AND tier = ? "
        "ORDER BY symbol",
        (group_name, tier),
    ).fetchall()
