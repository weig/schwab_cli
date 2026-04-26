"""Cron orchestrators — weekly indices sync + daily volatility sample.

Both functions take a ``sqlite3.Connection`` and an ``httpx.Client``
(passed by the caller, typically the CLI). They return a structured
summary dict suitable for JSON-line logging.

They never raise on per-symbol or per-index errors — those are
captured into the summary so a partial run still produces useful
output and the next run can retry.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

import httpx

from schwab_cli.api.accounts import get_account
from schwab_cli.dataset.indices import fetch_index_members
from schwab_cli.dataset.store import (
    list_active_index_subscriptions,
)


_log = logging.getLogger(__name__)


# ---- indices weekly cron ----------------------------------------------


def run_indices_update(
    conn: sqlite3.Connection,
    *,
    http_client: httpx.Client | None,
    group_name: str = "volatility",
    now_ms: int,
) -> dict[str, dict[str, Any]]:
    """Sync index member rows for every active index subscription."""
    summary: dict[str, dict[str, Any]] = {}

    indices = list_active_index_subscriptions(conn, group_name=group_name)
    for row in indices:
        idx = row["index_name"]
        try:
            upstream = fetch_index_members(idx, client=http_client)
        except NotImplementedError as e:
            summary[idx] = {"error": f"TODO: {e}"}
            continue
        except Exception as e:
            summary[idx] = {"error": str(e)}
            continue

        current = _current_index_members(conn, idx, group_name)
        added = sorted(upstream - current)
        removed = sorted(current - upstream)
        for sym in added:
            conn.execute(
                """
                INSERT INTO subscriptions
                  (symbol, group_name, source, source_key,
                   subscribed_at, unsubscribed_at)
                VALUES (?, ?, 'indices', ?, ?, NULL)
                ON CONFLICT (symbol, group_name, source, source_key) DO UPDATE SET
                  subscribed_at = excluded.subscribed_at,
                  unsubscribed_at = NULL
                """,
                (sym, group_name, idx, now_ms),
            )
        for sym in removed:
            conn.execute(
                """
                UPDATE subscriptions SET unsubscribed_at = ?
                WHERE symbol = ? AND group_name = ?
                  AND source = 'indices' AND source_key = ?
                  AND unsubscribed_at IS NULL
                """,
                (now_ms, sym, group_name, idx),
            )
        summary[idx] = {
            "added":   added,
            "removed": removed,
            "total":   len(upstream),
        }
    return summary


def _current_index_members(
    conn: sqlite3.Connection,
    index_name: str,
    group_name: str,
) -> set[str]:
    rows = conn.execute(
        """
        SELECT symbol FROM subscriptions
        WHERE source = 'indices' AND source_key = ?
          AND group_name = ? AND unsubscribed_at IS NULL
        """,
        (index_name, group_name),
    ).fetchall()
    return {r["symbol"] for r in rows}


# ---- account position reconciliation ----------------------------------


def _last4(hash_str: str) -> str:
    return hash_str[-4:]


def sync_account_positions(
    conn: sqlite3.Connection,
    *,
    client: Any,
    account_hash: str,
    group_name: str,
    now_ms: int,
) -> dict[str, list[str]]:
    """Pull account positions and reconcile with ``subscriptions``.

    Inserts new option-bearing underlyings, soft-deletes ones no longer
    held. Underlyings are deduplicated — multiple option contracts on
    the same underlying produce one ``subscriptions`` row.

    Returns ``{'added': [...], 'closed': [...]}`` for the run summary.
    """
    suffix = _last4(account_hash)

    try:
        acct = get_account(client, account_hash)
    except Exception as e:
        return {"added": [], "closed": [], "error": str(e)}

    positions = (acct.get("securitiesAccount") or {}).get("positions") or []
    upstream: set[str] = set()
    for p in positions:
        instr = p.get("instrument") or {}
        if instr.get("assetType") != "OPTION":
            continue
        und = instr.get("underlyingSymbol")
        if und:
            upstream.add(und)

    current = _current_position_underlyings(conn, suffix, group_name)
    added = sorted(upstream - current)
    closed = sorted(current - upstream)

    for sym in added:
        conn.execute(
            """
            INSERT INTO subscriptions
              (symbol, group_name, source, source_key,
               subscribed_at, unsubscribed_at)
            VALUES (?, ?, 'position', ?, ?, NULL)
            ON CONFLICT (symbol, group_name, source, source_key) DO UPDATE SET
              subscribed_at = excluded.subscribed_at,
              unsubscribed_at = NULL
            """,
            (sym, group_name, suffix, now_ms),
        )
    for sym in closed:
        conn.execute(
            """
            UPDATE subscriptions SET unsubscribed_at = ?
            WHERE symbol = ? AND group_name = ?
              AND source = 'position' AND source_key = ?
              AND unsubscribed_at IS NULL
            """,
            (now_ms, sym, group_name, suffix),
        )

    return {"added": added, "closed": closed}


def _current_position_underlyings(
    conn: sqlite3.Connection,
    source_key: str,
    group_name: str,
) -> set[str]:
    rows = conn.execute(
        """
        SELECT symbol FROM subscriptions
        WHERE source = 'position' AND source_key = ?
          AND group_name = ? AND unsubscribed_at IS NULL
        """,
        (source_key, group_name),
    ).fetchall()
    return {r["symbol"] for r in rows}
