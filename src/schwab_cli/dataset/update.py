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
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

import httpx

from schwab_cli.api.accounts import get_account
from schwab_cli.api.chains import get_chain
from schwab_cli.api.history import get_history
from schwab_cli.analytics.tier import TierState, Thresholds, resolve_tier
from schwab_cli.dataset.config import load_config_or_default
from schwab_cli.dataset.indices import fetch_index_members
from schwab_cli.dataset.store import (
    list_active_index_subscriptions,
    list_active_subscriptions,
    read_ticker_state,
    write_ticker_state,
    sources_for_symbol,
    last_close_at_for_symbol,
)
from schwab_cli.dataset.volatility import sample_volatility
from schwab_cli.storage.vol_history import record_extended_snapshot


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


# ---- daily volatility cron --------------------------------------------

_NY = ZoneInfo("America/New_York")

# How far back to fetch price history for HV computation.
_HISTORY_LOOKBACK_DAYS = 110  # ~90 trading days + buffer


def run_volatility_update(
    conn: sqlite3.Connection,
    *,
    client: Any,
    group_name: str = "volatility",
    now_ms: int,
    accounts: list[str],
    progress: Callable[[dict], None] | None = None,
) -> dict[str, Any]:
    """Daily volatility cron.

    Steps:
      1. Sync each account's position rows.
      2. Build the working set from active subscriptions.
      3. Decide what to sample (skip FROZEN; WATCH only Monday NY).
      4. Sample — write extended snapshot + return bundle.
      5. Re-evaluate tier; persist.

    ``progress`` is an optional callback invoked once per symbol with
    ``{event, index, total, symbol, ...}``. Events: ``start`` before
    the API calls, ``sampled`` on success, ``skipped`` for FROZEN /
    non-Monday WATCH, ``errored`` for per-symbol failures. Used by
    the CLI to print live progress; tests pass ``None``.
    """
    cfg = load_config_or_default()

    # Step 1 — reconcile account positions.
    pos_summary: dict[str, dict] = {}
    for acct in accounts:
        pos_summary[acct] = sync_account_positions(
            conn, client=client, account_hash=acct,
            group_name=group_name, now_ms=now_ms,
        )

    # Step 2 — build working set.
    active_rows = list_active_subscriptions(conn, group_name=group_name)
    symbols = sorted({r["symbol"] for r in active_rows})

    # Step 3 — partition.
    now_dt = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    is_monday = now_dt.astimezone(_NY).weekday() == 0
    is_trading_day = now_dt.astimezone(_NY).weekday() < 5

    sampled: list[str] = []
    skipped: list[str] = []
    transitions: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    total = len(symbols)
    archive_date = now_dt.astimezone(_NY).date().isoformat()

    def _emit(**evt: Any) -> None:
        if progress is not None:
            progress(evt)

    for i, sym in enumerate(symbols, start=1):
        state_row = read_ticker_state(conn, symbol=sym, group_name=group_name)
        if state_row is None:
            tier = "GRACE"
            tier_since = now_ms
            cdb = 0
        else:
            tier = state_row["tier"]
            tier_since = state_row["tier_since"]
            cdb = state_row["consecutive_days_below"]

        if tier == "FROZEN":
            skipped.append(sym)
            _emit(event="skipped", index=i, total=total, symbol=sym,
                  reason="FROZEN", archive_date=archive_date)
            continue
        if tier == "WATCH" and not is_monday:
            skipped.append(sym)
            _emit(event="skipped", index=i, total=total, symbol=sym,
                  reason="WATCH (non-Monday)", archive_date=archive_date)
            continue

        _emit(event="start", index=i, total=total, symbol=sym,
              tier=tier, archive_date=archive_date)

        # Step 4 — sample.
        try:
            chain = get_chain(client, sym, contract_type="ALL", strike_count=60)
            hist_end = now_dt
            hist_start = hist_end - timedelta(days=_HISTORY_LOOKBACK_DAYS)
            hist = get_history(
                client, sym,
                frequency_type="daily",
                frequency=1,
                start=hist_start,
                end=hist_end,
            )
            closes = [
                c["close"] for c in (hist.get("candles") or [])
                if c.get("close") is not None
            ]
            bundle = sample_volatility(chain=chain, underlying_closes=closes)
        except Exception as e:
            errors.append({"symbol": sym, "error": str(e)})
            _emit(event="errored", index=i, total=total, symbol=sym,
                  error=str(e), archive_date=archive_date)
            continue

        record_extended_snapshot(
            conn,
            symbol=sym,
            spot=bundle["spot"],
            atm_iv=bundle["atm_iv"] or 0.0,
            atm_strike=bundle["atm_strike"] or bundle["spot"],
            atm_expiry=bundle["atm_expiry"] or "",
            atm_dte=bundle["atm_dte"] or 30,
            captured_at_ms=now_ms,
            atm_iv_30d=bundle["atm_iv_30d"],
            atm_iv_60d=bundle["atm_iv_60d"],
            atm_iv_90d=bundle["atm_iv_90d"],
            iv_25d_put_30d=bundle.get("iv_25d_put_30d"),
            iv_25d_call_30d=bundle.get("iv_25d_call_30d"),
            iv_25d_put_60d=bundle.get("iv_25d_put_60d"),
            iv_25d_call_60d=bundle.get("iv_25d_call_60d"),
            iv_25d_put_90d=bundle.get("iv_25d_put_90d"),
            iv_25d_call_90d=bundle.get("iv_25d_call_90d"),
            hv_30d=bundle.get("hv_30d"),
            raw_chain_summary=bundle.get("raw_chain_summary"),
        )
        sampled.append(sym)

        # Step 5 — tier re-evaluation.
        chain_volume = _today_chain_volume(chain)
        front2_oi = _front2_oi(chain)
        threshold_pass = (
            chain_volume >= cfg["thresholds"]["indices"]["active_min_chain_volume"]
            or front2_oi >= cfg["thresholds"]["indices"]["active_min_front2_oi"]
        )
        sources = sources_for_symbol(conn, symbol=sym, group_name=group_name)
        last_close_ms = last_close_at_for_symbol(
            conn, symbol=sym, group_name=group_name
        )
        last_close_dt = (
            datetime.fromtimestamp(last_close_ms / 1000, tz=timezone.utc)
            if last_close_ms is not None else None
        )
        thr = Thresholds(
            active_min_chain_volume=cfg["thresholds"]["indices"]["active_min_chain_volume"],
            active_min_front2_oi=cfg["thresholds"]["indices"]["active_min_front2_oi"],
            watch_demote_after_trading_days=cfg["thresholds"]["indices"]["watch_demote_after_trading_days"],
            frozen_demote_after_calendar_days=cfg["thresholds"]["indices"]["frozen_demote_after_calendar_days"],
            position_watch_days=cfg["thresholds"]["position"]["watch_demote_after_calendar_days"],
            position_frozen_days=cfg["thresholds"]["position"]["frozen_demote_after_calendar_days"],
            grace_trading_days=cfg["thresholds"]["grace_trading_days"],
        )
        old = TierState(
            tier=tier,
            tier_since=datetime.fromtimestamp(tier_since / 1000, tz=timezone.utc),
            consecutive_days_below=cdb,
        )
        new = resolve_tier(
            old, sources=sources, now=now_dt,
            threshold_pass=threshold_pass, is_trading_day=is_trading_day,
            has_active_position=("position" in sources and last_close_dt is None),
            last_close_at=last_close_dt, thr=thr,
        )
        if new.tier != old.tier:
            transitions.append({"symbol": sym, "from": old.tier, "to": new.tier})
        write_ticker_state(
            conn,
            symbol=sym, group_name=group_name,
            tier=new.tier,
            tier_since=int(new.tier_since.timestamp() * 1000),
            consecutive_days_below=new.consecutive_days_below,
            last_evaluated_at=now_ms,
        )

        _emit(event="sampled", index=i, total=total, symbol=sym,
              archive_date=archive_date,
              atm_iv_30d=bundle.get("atm_iv_30d"),
              hv_30d=bundle.get("hv_30d"),
              tier_from=old.tier, tier_to=new.tier)

    return {
        "sampled":     sampled,
        "skipped":     skipped,
        "transitions": transitions,
        "errors":      errors,
        "positions":   pos_summary,
    }


def _today_chain_volume(chain: dict) -> int:
    total = 0
    for exp in chain.get("expiries") or []:
        for c in (exp.get("contracts") or []):
            total += int(c.get("volume") or 0)
    return total


def _front2_oi(chain: dict) -> int:
    expiries = sorted(
        chain.get("expiries") or [],
        key=lambda e: e.get("dte") or 99999,
    )[:2]
    total = 0
    for exp in expiries:
        for c in (exp.get("contracts") or []):
            total += int(c.get("openInterest") or 0)
    return total
