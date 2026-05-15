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
from schwab_cli.api.chains import get_chain, flatten_chain
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
from schwab_cli.storage import ohlcv_history
from schwab_cli.storage.groups import GROUP_VOLATILITY
from schwab_cli.storage.vol_history import record_extended_snapshot


_log = logging.getLogger(__name__)


# ---- indices weekly cron ----------------------------------------------


def run_indices_update(
    conn: sqlite3.Connection,
    *,
    http_client: httpx.Client | None,
    group_name: str = GROUP_VOLATILITY,
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
        # Commit after each index so a later provider failure can't
        # roll back an earlier index's successful diff.
        conn.commit()
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

    Tracks the underlying for every EQUITY (stock / ETF) and OPTION
    position in the account — both forms of exposure benefit from a
    maintained IV / IVR / IVP series. Multiple positions on the same
    underlying (e.g. shares + several option strikes) collapse to one
    ``subscriptions`` row. Mutual funds, fixed income, cash, and
    currencies are skipped — no useful options chain.

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
        sym = _underlying_for_position(p)
        if sym:
            upstream.add(sym)

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


def _underlying_for_position(p: dict) -> str | None:
    """Map one Schwab position row to the underlying symbol we want to
    track, or ``None`` if the asset type isn't options-relevant.

    EQUITY → ``instrument.symbol`` (the stock / ETF ticker itself).
    OPTION → ``instrument.underlyingSymbol`` (the option's underlying).
    Everything else (MUTUAL_FUND, CASH_EQUIVALENT, FIXED_INCOME,
    CURRENCY, COLLECTIVE_INVESTMENT) → None.
    """
    instr = p.get("instrument") or {}
    asset_type = instr.get("assetType")
    if asset_type == "OPTION":
        sym = instr.get("underlyingSymbol")
    elif asset_type == "EQUITY":
        sym = instr.get("symbol")
    else:
        sym = None
    if not sym or not isinstance(sym, str):
        return None
    return sym.strip().upper() or None


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

# Commit every N successful samples so a mid-run crash doesn't lose
# the rows we already wrote. 50 ≈ a few minutes of work at typical
# Schwab response latency, balancing durability vs the (small) cost
# of more fsyncs.
_COMMIT_BATCH = 50


def _ensure_ohlcv_cached(
    conn: sqlite3.Connection,
    *,
    client: Any,
    symbol: str,
    start,
    end,
) -> None:
    """Pull only the un-cached suffix of ``[start, end]`` for ``symbol``
    and write candles to ``ohlcv_daily``. No-op when the cache already
    covers ``end``.

    ``start``/``end`` are ``datetime.date`` (NY trading day). Schwab's
    ``get_history`` is called with UTC datetimes spanning the missing
    days; the returned timestamps are bucketed back to NY dates before
    upsert.
    """
    g = ohlcv_history.gap(conn, symbol=symbol, start=start, end=end)
    if g is None:
        return
    fetch_start, fetch_end = g
    hist = get_history(
        client, symbol,
        frequency_type="daily", frequency=1,
        start=datetime(fetch_start.year, fetch_start.month, fetch_start.day,
                       tzinfo=timezone.utc),
        end=datetime(fetch_end.year, fetch_end.month, fetch_end.day,
                     tzinfo=timezone.utc) + timedelta(days=1),
    )
    candles = []
    for c in (hist.get("candles") or []):
        dt_ms = c.get("datetime")
        if dt_ms is None:
            continue
        day = (datetime.fromtimestamp(int(dt_ms) / 1000, tz=timezone.utc)
                       .astimezone(_NY).date().isoformat())
        try:
            candles.append({
                "day": day,
                "open":  float(c["open"]),
                "high":  float(c["high"]),
                "low":   float(c["low"]),
                "close": float(c["close"]),
                "volume": int(c.get("volume") or 0),
                "captured_at_ms": int(dt_ms),
            })
        except (KeyError, TypeError, ValueError):
            # Skip malformed rows rather than failing the whole symbol.
            continue
    ohlcv_history.upsert_candles(conn, symbol=symbol, candles=candles)


def run_volatility_update(
    conn: sqlite3.Connection,
    *,
    client: Any,
    group_name: str = GROUP_VOLATILITY,
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
    # Persist position-source rows before the long sample loop so a
    # mid-run crash doesn't lose the position reconciliation work.
    conn.commit()

    # Step 2 — build working set. Pass ``now_ms`` so indices members
    # removed within the last 30 days stay sampled through the exit
    # (see store.INDICES_GRACE_DAYS_AFTER_REMOVAL).
    active_rows = list_active_subscriptions(
        conn, group_name=group_name, now_ms=now_ms,
    )
    symbols = sorted({r["symbol"] for r in active_rows})

    # Step 3 — partition.
    now_dt = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    is_monday = now_dt.astimezone(_NY).weekday() == 0

    sampled: list[str] = []
    skipped: list[str] = []
    transitions: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    total = len(symbols)
    archive_date = now_dt.astimezone(_NY).date().isoformat()

    # Pre-pass: which symbols already have a live ('observed') row for
    # today's NY day. Lets us skip the chain pull for them entirely so a
    # manual run + the cron + the `vol` command can't double-write the
    # same trading day.
    observed_today = _symbols_observed_on_ny_day(conn, archive_date)

    # How many sampled rows we've written since the last commit. Reset
    # to 0 each time we flush; flush every _COMMIT_BATCH symbols so a
    # mid-run crash loses at most that many writes.
    sampled_since_commit = 0

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
        if sym in observed_today:
            skipped.append(sym)
            _emit(event="skipped", index=i, total=total, symbol=sym,
                  reason="already sampled today",
                  archive_date=archive_date)
            continue

        _emit(event="start", index=i, total=total, symbol=sym,
              tier=tier, archive_date=archive_date)

        # Step 4 — sample.
        try:
            raw = get_chain(client, sym, contract_type="ALL", strike_count=60)
            # Schwab returns ``callExpDateMap``/``putExpDateMap``;
            # flatten into the ``[{expiry, dte, contracts}, ...]``
            # shape ``sample_volatility`` consumes. Pre-flattened
            # input (e.g. from test fixtures) is passed through.
            if "expiries" in raw:
                chain = raw
            else:
                expiries, _flat = flatten_chain(raw)
                spot_now = (raw.get("underlying") or {}).get("last")
                if spot_now is None:
                    spot_now = raw.get("underlyingPrice")
                chain = {
                    "underlying": {"last": spot_now},
                    "expiries":   expiries,
                }
            hist_end_dt   = now_dt
            hist_start_dt = hist_end_dt - timedelta(days=_HISTORY_LOOKBACK_DAYS)
            hist_start_ny = hist_start_dt.astimezone(_NY).date()
            hist_end_ny   = hist_end_dt.astimezone(_NY).date()
            _ensure_ohlcv_cached(
                conn, client=client, symbol=sym,
                start=hist_start_ny, end=hist_end_ny,
            )
            rows = ohlcv_history.read_range(
                conn, symbol=sym,
                start=hist_start_ny, end=hist_end_ny,
            )
            closes = [r["close"] for r in rows if r["close"] is not None]
            bundle = sample_volatility(chain=chain, underlying_closes=closes)
        except Exception as e:
            errors.append({"symbol": sym, "error": str(e)})
            _emit(event="errored", index=i, total=total, symbol=sym,
                  error=str(e), archive_date=archive_date)
            continue

        # No valid ATM contract → ``pick_atm_contract`` couldn't find an
        # expiry with enough volume + a non-NULL IV. Happens on
        # weekend / pre-market runs and on illiquid names. Don't write
        # a placeholder row — it would pollute IVR / IVP series with
        # spurious zeros.
        if (bundle["atm_iv"] is None
                or bundle["atm_strike"] is None
                or not bundle["atm_expiry"]
                or bundle["atm_dte"] is None):
            errors.append({
                "symbol": sym,
                "error": "no ATM contract (chain has no liquid quotes)",
            })
            _emit(event="errored", index=i, total=total, symbol=sym,
                  error="no ATM contract", archive_date=archive_date)
            continue

        record_extended_snapshot(
            conn,
            symbol=sym,
            spot=bundle["spot"],
            atm_iv=bundle["atm_iv"],
            atm_strike=bundle["atm_strike"],
            atm_expiry=bundle["atm_expiry"],
            atm_dte=bundle["atm_dte"],
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
        sources = sources_for_symbol(
            conn, symbol=sym, group_name=group_name, now_ms=now_ms,
        )
        last_close_ms = last_close_at_for_symbol(
            conn, symbol=sym, group_name=group_name
        )
        last_close_dt = (
            datetime.fromtimestamp(last_close_ms / 1000, tz=timezone.utc)
            if last_close_ms is not None else None
        )
        thr = Thresholds(
            position_watch_days=cfg["thresholds"]["position"]["watch_demote_after_calendar_days"],
            position_frozen_days=cfg["thresholds"]["position"]["frozen_demote_after_calendar_days"],
        )
        old = TierState(
            tier=tier,
            tier_since=datetime.fromtimestamp(tier_since / 1000, tz=timezone.utc),
            consecutive_days_below=cdb,
        )
        new = resolve_tier(
            old, sources=sources, now=now_dt,
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

        # Periodic flush — durability over a single fat transaction.
        sampled_since_commit += 1
        if sampled_since_commit >= _COMMIT_BATCH:
            conn.commit()
            sampled_since_commit = 0

    # Final flush for the trailing partial batch (the connect() context
    # manager would commit on exit anyway, but committing here keeps
    # the durability boundary explicit at the end of the orchestrator).
    if sampled_since_commit > 0:
        conn.commit()

    return {
        "sampled":     sampled,
        "skipped":     skipped,
        "transitions": transitions,
        "errors":      errors,
        "positions":   pos_summary,
    }


def _symbols_observed_on_ny_day(
    conn: sqlite3.Connection, ny_day: str,
) -> set[str]:
    """Return the set of symbols with an ``observed`` row whose
    NY trading day matches ``ny_day`` (YYYY-MM-DD).

    The query pre-filters by ``archive_date`` (UTC-bucketed) within
    a one-day window of ``ny_day`` so we don't scan the whole table —
    NY's UTC offset is at most 5 hours, so any row whose NY day equals
    ``ny_day`` has UTC archive_date in ``{ny_day - 1, ny_day, ny_day + 1}``.
    The exact NY-day match is then verified in Python.
    """
    rows = conn.execute(
        """
        SELECT symbol, captured_at_ms FROM vol_snapshots
        WHERE source = 'observed'
          AND archive_date BETWEEN date(?, '-1 day') AND date(?, '+1 day')
        """,
        (ny_day, ny_day),
    ).fetchall()
    out: set[str] = set()
    for r in rows:
        ts = datetime.fromtimestamp(r["captured_at_ms"] / 1000, tz=timezone.utc)
        if ts.astimezone(_NY).date().isoformat() == ny_day:
            out.add(r["symbol"])
    return out
