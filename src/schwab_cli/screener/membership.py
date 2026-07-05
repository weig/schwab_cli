"""Point-in-time index membership (plan §6, survivorship guard).

Records a dated snapshot of each index's current members so any historical
candidate universe is reconstructable. The live ``subscriptions`` table only
holds current state (with a grace window); this keeps immutable dated rows
alongside it. Snapshots are write-once per (as_of_date, index, symbol).
"""
from __future__ import annotations

from schwab_cli.storage import screener as store

# Index source_key values used in the subscriptions table (source='indices').
_INDEX_SOURCE = "indices"


def current_members_by_index(conn) -> dict[str, list[str]]:
    """Current index members grouped by index, read from subscriptions."""
    rows = conn.execute(
        "SELECT symbol, source_key FROM subscriptions "
        "WHERE source = ? AND unsubscribed_at IS NULL AND source_key != '' "
        "ORDER BY source_key, symbol",
        (_INDEX_SOURCE,),
    ).fetchall()
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["source_key"], []).append(r["symbol"])
    return out


def record_membership_snapshot(
    conn, *, as_of_date: str, now_ms: int,
    members_by_index: dict[str, list[str]] | None = None,
) -> dict:
    """Snapshot current (or supplied) members into ``index_membership``."""
    members = (
        members_by_index
        if members_by_index is not None
        else current_members_by_index(conn)
    )
    total = 0
    for index_name, symbols in members.items():
        store.record_membership(
            conn, as_of_date=as_of_date, index_name=index_name,
            symbols=symbols, captured_at_ms=now_ms,
        )
        total += len(symbols)
    conn.commit()
    return {"indices": len(members), "symbols": total, "as_of_date": as_of_date}
