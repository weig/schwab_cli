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
