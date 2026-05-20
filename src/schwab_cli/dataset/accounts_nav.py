"""Account NAV snapshot + backfill orchestrator.

Two entry points used by the CLI / cron:

- :func:`snapshot_all_accounts` — write today's NAV from a live
  positions-API call. Cron's daily job calls this.
- :func:`backfill_range` — replay transactions and BS-price every
  trading day in a date range. Manual command for history reconstruction.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

from schwab_cli.analytics.nav_history import (
    backfill_day,
    snapshot_today_from_payload,
)
from schwab_cli.api.accounts import list_accounts
from schwab_cli.api.client import SchwabClient
from schwab_cli.api.history import get_history
from schwab_cli.api.transactions_cache import fetch_cached as fetch_txns
from schwab_cli.commands.history import _cache_api_response
from schwab_cli.storage import account_nav, ohlcv_history, vol_history


_NY = ZoneInfo("America/New_York")


# ---- snapshot ---------------------------------------------------------


@dataclass
class SnapshotResult:
    account_hash: str
    account_number: str
    market_value: float
    cash: float
    total_value: float


def snapshot_all_accounts(client: SchwabClient) -> list[SnapshotResult]:
    """Hit the accounts API once and persist today's NAV for every
    account. Returns the list of writes for caller reporting / cron
    log output."""
    payload = list_accounts(client) or []
    if not isinstance(payload, list):
        return []
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    results: list[SnapshotResult] = []
    with vol_history.connect() as conn:
        for item in payload:
            sec = item.get("securitiesAccount", {}) or {}
            acct_no = sec.get("accountNumber") or ""
            acct_hash = sec.get("hashValue") or _resolve_hash(client, acct_no)
            if not acct_hash:
                continue
            nav = snapshot_today_from_payload(sec)
            account_nav.upsert(
                conn,
                account_hash=acct_hash, day=nav.day,
                market_value=nav.market_value, cash=nav.cash,
                is_estimated=nav.estimated,
                captured_at_ms=now_ms,
            )
            results.append(SnapshotResult(
                account_hash=acct_hash, account_number=acct_no,
                market_value=nav.market_value, cash=nav.cash,
                total_value=nav.market_value + nav.cash,
            ))
    return results


def _resolve_hash(client: SchwabClient, account_number: str) -> str:
    try:
        return client.resolve_account(account_number).hash_value
    except Exception:
        return ""


# ---- backfill ---------------------------------------------------------


@dataclass
class BackfillResult:
    account_hash: str
    account_number: str
    days_written: int
    days_estimated: int


def backfill_range(
    client: SchwabClient,
    *,
    account_number: str | None,
    start: date,
    end: date,
    progress_cb=None,
) -> list[BackfillResult]:
    """Backfill NAV history for one account (or all) over ``[start, end]``.

    For each day in the range we replay transactions back from today
    and price every held position. Equity uses the OHLCV cache (lazy
    backfilled from Schwab on miss); options are BS-priced from
    ``vol_snapshots``. Days that touched any option are flagged
    ``is_estimated`` so the performance command can warn.
    """
    payload = list_accounts(client) or []
    if not isinstance(payload, list):
        return []
    selected = [
        item for item in payload
        if account_number is None
        or (item.get("securitiesAccount", {}) or {})
            .get("accountNumber", "").endswith(account_number)
    ]
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    today = datetime.now(tz=_NY).date()
    trading_days = _trading_days(start, end)
    results: list[BackfillResult] = []

    for item in selected:
        sec = item.get("securitiesAccount", {}) or {}
        acct_no = sec.get("accountNumber") or ""
        acct_hash = sec.get("hashValue") or _resolve_hash(client, acct_no)
        if not acct_hash:
            continue
        cash = float(
            (sec.get("currentBalances") or {}).get("cashBalance") or 0.0
        )
        positions = _extract_positions(sec)
        avg_price = _avg_price(sec)

        # Transactions spanning [start, today] cover every reverse-walk we'll
        # need. Caller-side filtering by date happens inside backfill_day.
        start_dt = datetime(start.year, start.month, start.day, tzinfo=_NY)
        end_dt = datetime.now(tz=_NY)
        txns = fetch_txns(
            client, acct_no, start=start_dt, end=end_dt, refresh=False,
        )

        # Pre-load price + IV inputs for every held + traded symbol.
        held_symbols = set(positions)
        option_underlyings = {
            _underlying_of(s) for s in held_symbols if _is_option(s)
        }
        equity_symbols = sorted(
            {s for s in held_symbols if not _is_option(s)}
            | option_underlyings
        )
        equity_close = _ensure_closes(
            client, equity_symbols, start=start, end=end,
        )
        underlying_close = {
            u: equity_close.get(u, {}) for u in option_underlyings
        }
        atm_iv = _load_atm_iv(option_underlyings, start=start, end=end)

        # Pre-compute the set of true trading days: any day on which at
        # least one equity has an OHLCV close. Market holidays
        # (New Year's, MLK Day, etc.) land on weekdays but have no
        # closes — pricing positions there forward-fills from the
        # prior trading day, which produces a sensible NAV; the
        # corner case we *must* avoid is the very first day of the
        # range being a holiday with no prior cached close, where
        # positions value at $0 and BV collapses.
        real_trading_days: set[date] = set()
        for sym, sym_closes in equity_close.items():
            real_trading_days |= set(sym_closes.keys())

        written = 0
        estimated = 0
        with vol_history.connect() as conn:
            for d in trading_days:
                if d not in real_trading_days and d != today:
                    continue  # market holiday — skip the row entirely
                point = backfill_day(
                    day=d, today=today,
                    today_cash=cash, today_positions=positions,
                    transactions=txns,
                    equity_close=equity_close,
                    underlying_close=underlying_close,
                    atm_iv=atm_iv,
                    avg_price=avg_price,
                )
                account_nav.upsert(
                    conn,
                    account_hash=acct_hash, day=point.day,
                    market_value=point.market_value, cash=point.cash,
                    is_estimated=point.estimated,
                    captured_at_ms=now_ms,
                )
                written += 1
                if point.estimated:
                    estimated += 1
                if progress_cb is not None:
                    progress_cb(account=acct_no, day=d,
                                written=written, estimated=estimated,
                                total=len(trading_days))
        results.append(BackfillResult(
            account_hash=acct_hash, account_number=acct_no,
            days_written=written, days_estimated=estimated,
        ))
    return results


# ---- helpers ----------------------------------------------------------


def _trading_days(start: date, end: date) -> list[date]:
    out: list[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _extract_positions(sec: dict) -> dict[str, float]:
    out: dict[str, float] = {}
    for pos in (sec.get("positions") or []):
        inst = pos.get("instrument") or {}
        sym = inst.get("symbol")
        atype = (inst.get("assetType") or "").upper()
        if not sym or atype == "CURRENCY":
            continue
        try:
            qty = float(pos.get("longQuantity") or 0) - float(
                pos.get("shortQuantity") or 0
            )
        except (TypeError, ValueError):
            qty = 0.0
        if qty != 0.0:
            out[sym] = out.get(sym, 0.0) + qty
    return out


def _avg_price(sec: dict) -> dict[str, float]:
    out: dict[str, float] = {}
    for pos in (sec.get("positions") or []):
        inst = pos.get("instrument") or {}
        sym = inst.get("symbol")
        if not sym:
            continue
        try:
            avg = float(pos.get("averagePrice") or 0.0)
        except (TypeError, ValueError):
            continue
        if avg > 0:
            out[sym] = avg
    return out


def _is_option(symbol: str) -> bool:
    return " " in symbol or len(symbol) > 6


def _underlying_of(option_symbol: str) -> str:
    """Pull the underlying ticker off an OSI symbol. ``"NVDA  260116C00200000"``
    → ``"NVDA"``."""
    head = option_symbol.split(" ", 1)[0]
    # Strip trailing digits — handles forms like NVDA260116C200 where
    # no padding spaces appear and digits run right after the ticker.
    cut = next((i for i, c in enumerate(head) if c.isdigit()), len(head))
    return head[:cut] if cut > 0 else head


def _ensure_closes(
    client: SchwabClient,
    symbols: Iterable[str],
    *, start: date, end: date,
) -> dict[str, dict[date, float]]:
    out: dict[str, dict[date, float]] = {}
    symbols = list(symbols)
    if not symbols:
        return out
    needs_fetch: list[str] = []
    with vol_history.connect() as conn:
        for sym in symbols:
            rows = ohlcv_history.read_range(
                conn, symbol=sym, start=start, end=end,
            )
            out[sym] = {
                date.fromisoformat(r["day"]): float(r["close"])
                for r in rows
            }
            earliest = (
                date.fromisoformat(rows[0]["day"]) if rows else None
            )
            if earliest is None or earliest > start:
                needs_fetch.append(sym)
    for sym in needs_fetch:
        try:
            raw = get_history(
                client, sym,
                frequency_type="daily", frequency=1,
                start=_at_midnight_utc(start),
                end=_at_midnight_utc(end + timedelta(days=1)),
            )
        except Exception:
            continue
        _cache_api_response(sym, raw)
        with vol_history.connect() as conn:
            rows = ohlcv_history.read_range(
                conn, symbol=sym, start=start, end=end,
            )
        out[sym] = {
            date.fromisoformat(r["day"]): float(r["close"])
            for r in rows
        }
    return out


def _load_atm_iv(
    underlyings: Iterable[str], *, start: date, end: date,
) -> dict[str, dict[date, float]]:
    """Pull ATM IV per underlying per NY trading day from vol_snapshots."""
    out: dict[str, dict[date, float]] = {}
    underlyings = sorted(underlyings)
    if not underlyings:
        return out
    placeholders = ",".join("?" * len(underlyings))
    start_ms = int(datetime(start.year, start.month, start.day,
                            tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = int(datetime(end.year, end.month, end.day + 1,
                          tzinfo=timezone.utc).timestamp() * 1000)
    with vol_history.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT symbol, captured_at_ms, atm_iv
            FROM vol_snapshots
            WHERE symbol IN ({placeholders})
              AND captured_at_ms >= ?
              AND captured_at_ms < ?
            ORDER BY symbol, captured_at_ms
            """,
            (*underlyings, start_ms, end_ms),
        ).fetchall()
    for r in rows:
        sym = r["symbol"]
        ts = datetime.fromtimestamp(
            int(r["captured_at_ms"]) / 1000, tz=timezone.utc,
        ).astimezone(_NY).date()
        out.setdefault(sym, {})[ts] = float(r["atm_iv"])
    return out


def _at_midnight_utc(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
