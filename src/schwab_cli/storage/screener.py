"""Persistence for the Options VRP screener (schema v7 tables).

Read/write helpers over ``contract_snapshots``, ``events``,
``index_membership``, ``daily_ranking``, and ``paper_ledger``. All operate
on a connection opened by :func:`schwab_cli.storage.vol_history.connect`
(the tables live in the same ``market_data.db``). Every write is idempotent
on its date-scoped primary key so a same-day job re-run never duplicates.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class ContractSnapshot:
    """One trading day's target-put snapshot for a symbol.

    ``snapshot_date`` is the America/New_York trading-day ISO string; it is
    the idempotency key (with ``symbol``). Nullable numeric fields are None
    when the chain lacked the target contract or a field was missing.
    """

    snapshot_date: str
    symbol: str
    captured_at_ms: int
    target_expiry: str | None = None
    dte: int | None = None
    put_strike: float | None = None
    put_delta_actual: float | None = None
    put_bid: float | None = None
    put_ask: float | None = None
    put_mid: float | None = None
    put_oi: int | None = None
    put_volume: int | None = None
    spread_pct: float | None = None
    underlying_last: float | None = None
    atm_iv_30d: float | None = None
    hv_30d: float | None = None
    ivr: float | None = None
    ivr_low_conf: bool = False
    next_earnings_date: str | None = None
    days_to_earnings: int | None = None
    snapshot_quality: str = "ok"
    filter_reason: str | None = None


# --------------------------------------------------------------------------
# contract_snapshots
# --------------------------------------------------------------------------

_SNAPSHOT_COLS = (
    "snapshot_date", "symbol", "captured_at_ms", "target_expiry", "dte",
    "put_strike", "put_delta_actual", "put_bid", "put_ask", "put_mid",
    "put_oi", "put_volume", "spread_pct", "underlying_last", "atm_iv_30d",
    "hv_30d", "ivr", "ivr_low_conf", "next_earnings_date", "days_to_earnings",
    "snapshot_quality", "filter_reason",
)


def record_contract_snapshot(conn: sqlite3.Connection, snap: ContractSnapshot) -> None:
    """Upsert a snapshot, preserving any already-backfilled ``rv_fwd_21d``.

    ON CONFLICT refreshes every capture-time field (a same-day re-run picks
    up newer quotes) but never touches ``rv_fwd_21d`` — that is written T+21
    by :func:`set_forward_rv` and must survive an intraday re-snapshot.
    """
    values = (
        snap.snapshot_date, snap.symbol, snap.captured_at_ms,
        snap.target_expiry, snap.dte, snap.put_strike, snap.put_delta_actual,
        snap.put_bid, snap.put_ask, snap.put_mid, snap.put_oi, snap.put_volume,
        snap.spread_pct, snap.underlying_last, snap.atm_iv_30d, snap.hv_30d,
        snap.ivr, 1 if snap.ivr_low_conf else 0, snap.next_earnings_date,
        snap.days_to_earnings, snap.snapshot_quality, snap.filter_reason,
    )
    placeholders = ", ".join("?" for _ in _SNAPSHOT_COLS)
    updates = ", ".join(
        f"{c} = excluded.{c}"
        for c in _SNAPSHOT_COLS
        if c not in ("snapshot_date", "symbol")
    )
    conn.execute(
        f"INSERT INTO contract_snapshots ({', '.join(_SNAPSHOT_COLS)}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT (snapshot_date, symbol) DO UPDATE SET {updates}",
        values,
    )


def read_contract_snapshots(
    conn: sqlite3.Connection, *, snapshot_date: str, only_ok: bool = False
) -> list[sqlite3.Row]:
    """All snapshot rows for a trading day (optionally only unfiltered ones)."""
    sql = "SELECT * FROM contract_snapshots WHERE snapshot_date = ?"
    if only_ok:
        sql += " AND snapshot_quality = 'ok'"
    return conn.execute(sql, (snapshot_date,)).fetchall()


def set_forward_rv(
    conn: sqlite3.Connection, *, snapshot_date: str, symbol: str, rv: float
) -> None:
    """Write the T+21 forward realized vol for one snapshot (idempotent)."""
    conn.execute(
        "UPDATE contract_snapshots SET rv_fwd_21d = ? "
        "WHERE snapshot_date = ? AND symbol = ?",
        (rv, snapshot_date, symbol),
    )


def read_snapshots_needing_rv(
    conn: sqlite3.Connection, *, on_or_before: str
) -> list[sqlite3.Row]:
    """Snapshots dated ``on_or_before`` that still lack a forward-RV value."""
    return conn.execute(
        "SELECT snapshot_date, symbol FROM contract_snapshots "
        "WHERE rv_fwd_21d IS NULL AND snapshot_date <= ? "
        "ORDER BY snapshot_date ASC",
        (on_or_before,),
    ).fetchall()


# --------------------------------------------------------------------------
# put_chain_snapshots (permanent raw band; sole writer = dataset job)
# --------------------------------------------------------------------------

def record_put_band(
    conn: sqlite3.Connection,
    *,
    snapshot_date: str,
    symbol: str,
    puts: list[dict],
    underlying_last: float | None,
    now_ms: int,
) -> None:
    """Persist a day's put band. INSERT OR REPLACE per (date, symbol, expiry,
    strike) so an intraday re-capture refreshes quotes idempotently."""
    rows = [
        (
            snapshot_date, symbol, p.get("expiry"), p.get("dte"),
            p.get("strike"), p.get("delta"), p.get("bid"), p.get("ask"),
            p.get("open_interest"), p.get("volume"), underlying_last, now_ms,
        )
        for p in puts
        if p.get("expiry") is not None and p.get("strike") is not None
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO put_chain_snapshots "
        "(snapshot_date, symbol, expiry, dte, strike, delta, bid, ask, "
        " open_interest, volume, underlying_last, captured_at_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


def read_put_band(
    conn: sqlite3.Connection, *, snapshot_date: str, symbol: str
) -> list[sqlite3.Row]:
    """The stored put band for one symbol/day (locator input)."""
    return conn.execute(
        "SELECT * FROM put_chain_snapshots "
        "WHERE snapshot_date = ? AND symbol = ? ORDER BY expiry, strike",
        (snapshot_date, symbol),
    ).fetchall()


def symbols_with_put_band(
    conn: sqlite3.Connection, *, snapshot_date: str
) -> list[str]:
    """Distinct symbols with a captured band on a date (the read universe)."""
    rows = conn.execute(
        "SELECT DISTINCT symbol FROM put_chain_snapshots "
        "WHERE snapshot_date = ? ORDER BY symbol",
        (snapshot_date,),
    ).fetchall()
    return [r["symbol"] for r in rows]


def latest_put_band_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT MAX(snapshot_date) AS d FROM put_chain_snapshots"
    ).fetchone()
    return row["d"] if row and row["d"] else None


# --------------------------------------------------------------------------
# events (earnings calendar)
# --------------------------------------------------------------------------

def upsert_event(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    event_type: str,
    event_date: str,
    confirmed: bool,
    updated_at_ms: int,
) -> None:
    """Insert/refresh a calendar event, keyed by (symbol, type, date)."""
    conn.execute(
        "INSERT INTO events (symbol, event_type, event_date, confirmed, "
        "updated_at_ms) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (symbol, event_type, event_date) DO UPDATE SET "
        "confirmed = excluded.confirmed, updated_at_ms = excluded.updated_at_ms",
        (symbol, event_type, event_date, 1 if confirmed else 0, updated_at_ms),
    )


def next_event_date(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    event_type: str,
    on_or_after: str,
) -> str | None:
    """Earliest ``event_date`` for the symbol at/after a date, else None."""
    row = conn.execute(
        "SELECT event_date FROM events "
        "WHERE symbol = ? AND event_type = ? AND event_date >= ? "
        "ORDER BY event_date ASC LIMIT 1",
        (symbol, event_type, on_or_after),
    ).fetchone()
    return row["event_date"] if row else None


# --------------------------------------------------------------------------
# index_membership (point-in-time)
# --------------------------------------------------------------------------

def record_membership(
    conn: sqlite3.Connection,
    *,
    as_of_date: str,
    index_name: str,
    symbols: list[str],
    captured_at_ms: int,
) -> None:
    """Snapshot an index's members for a date. INSERT OR IGNORE — a dated
    snapshot is written once and never overwritten (survivorship guard)."""
    conn.executemany(
        "INSERT OR IGNORE INTO index_membership "
        "(as_of_date, index_name, symbol, captured_at_ms) VALUES (?, ?, ?, ?)",
        [(as_of_date, index_name, s, captured_at_ms) for s in symbols],
    )


def read_membership(
    conn: sqlite3.Connection, *, as_of_date: str, index_name: str | None = None
) -> list[str]:
    """Symbols recorded for a date (optionally one index)."""
    if index_name is not None:
        rows = conn.execute(
            "SELECT symbol FROM index_membership "
            "WHERE as_of_date = ? AND index_name = ? ORDER BY symbol",
            (as_of_date, index_name),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM index_membership "
            "WHERE as_of_date = ? ORDER BY symbol",
            (as_of_date,),
        ).fetchall()
    return [r["symbol"] for r in rows]


def latest_membership_date(
    conn: sqlite3.Connection, *, on_or_before: str
) -> str | None:
    """Most recent membership snapshot date at/before ``on_or_before``."""
    row = conn.execute(
        "SELECT MAX(as_of_date) AS d FROM index_membership WHERE as_of_date <= ?",
        (on_or_before,),
    ).fetchone()
    return row["d"] if row and row["d"] else None


# --------------------------------------------------------------------------
# daily_ranking
# --------------------------------------------------------------------------

_RANKING_COLS = (
    "ranking_date", "rank", "symbol", "executable_vrp", "premium_yield_bid",
    "fair_yield", "ivr", "ivr_low_conf", "put_strike", "put_delta_actual",
    "put_bid", "dte", "target_expiry", "spread_pct", "underlying_last",
)


def write_ranking(
    conn: sqlite3.Connection, *, ranking_date: str, rows: list[dict]
) -> None:
    """Replace the ranking for a date (delete-then-insert for idempotency).

    ``rows`` are dicts carrying at least the keys in ``_RANKING_COLS`` minus
    ``ranking_date`` (added here). Missing optional keys default to None.
    """
    conn.execute("DELETE FROM daily_ranking WHERE ranking_date = ?", (ranking_date,))
    payload = [
        tuple(
            ranking_date if c == "ranking_date"
            else (1 if r.get(c) else 0) if c == "ivr_low_conf"
            else r.get(c)
            for c in _RANKING_COLS
        )
        for r in rows
    ]
    placeholders = ", ".join("?" for _ in _RANKING_COLS)
    conn.executemany(
        f"INSERT INTO daily_ranking ({', '.join(_RANKING_COLS)}) "
        f"VALUES ({placeholders})",
        payload,
    )


def read_ranking(
    conn: sqlite3.Connection, *, ranking_date: str, limit: int | None = None
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM daily_ranking WHERE ranking_date = ? ORDER BY rank ASC"
    params: tuple = (ranking_date,)
    if limit is not None:
        sql += " LIMIT ?"
        params = (ranking_date, limit)
    return conn.execute(sql, params).fetchall()


def latest_ranking_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(ranking_date) AS d FROM daily_ranking").fetchone()
    return row["d"] if row and row["d"] else None


# --------------------------------------------------------------------------
# paper_ledger
# --------------------------------------------------------------------------

def open_position(
    conn: sqlite3.Connection,
    *,
    open_date: str,
    symbol: str,
    cohort: str,
    strike: float,
    dte: int,
    premium_bid: float,
    expiry: str,
) -> None:
    """Record a virtual put sale. INSERT OR IGNORE — never reopen a same-day
    position for a symbol (idempotent daily open)."""
    conn.execute(
        "INSERT OR IGNORE INTO paper_ledger "
        "(open_date, symbol, cohort, strike, dte, premium_bid, expiry) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (open_date, symbol, cohort, strike, dte, premium_bid, expiry),
    )


def read_unsettled_due(
    conn: sqlite3.Connection, *, on_or_after_expiry: str
) -> list[sqlite3.Row]:
    """Unsettled ledger rows whose expiry has arrived (expiry <= date)."""
    return conn.execute(
        "SELECT * FROM paper_ledger "
        "WHERE settled_at IS NULL AND expiry <= ? ORDER BY expiry ASC",
        (on_or_after_expiry,),
    ).fetchall()


def settle_position(
    conn: sqlite3.Connection,
    *,
    open_date: str,
    symbol: str,
    settle_price: float,
    pnl: float,
    settled_at: int,
) -> None:
    """Write settlement price + PnL for a matured position (idempotent)."""
    # settled_at IS NULL guard: never re-settle a position (would silently
    # rewrite historical paper-ledger PnL, corrupting the validation harness).
    conn.execute(
        "UPDATE paper_ledger SET settle_price = ?, pnl = ?, settled_at = ? "
        "WHERE open_date = ? AND symbol = ? AND settled_at IS NULL",
        (settle_price, pnl, settled_at, open_date, symbol),
    )


def read_ledger(
    conn: sqlite3.Connection,
    *,
    cohort: str | None = None,
    settled_only: bool = False,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM paper_ledger WHERE 1 = 1"
    params: list = []
    if cohort is not None:
        sql += " AND cohort = ?"
        params.append(cohort)
    if settled_only:
        sql += " AND settled_at IS NOT NULL"
    sql += " ORDER BY open_date ASC, symbol ASC"
    return conn.execute(sql, tuple(params)).fetchall()
