"""Daily screener orchestration (Stages A–F in one idempotent pass).

``run_screener_update`` is written against an injected :class:`ScreenerDeps`
so the whole daily flow is unit-testable with fakes (no network, no clock).
``build_live_deps`` wires the real services/storage. One pass does, in order:

1. settle matured paper-ledger positions,
2. backfill forward realized vol for snapshots whose window has elapsed,
3. snapshot + quality-check + hard-filter every active symbol,
4. rank survivors by executable VRP,
5. open top/bottom virtual positions for the day.

Every write is idempotent on a date-scoped key, so a same-day re-run is safe.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from schwab_cli.screener.config import ScreenerConfig
from schwab_cli.screener.forward_rv import forward_rv
from schwab_cli.screener.ledger import select_cohorts, settle_pnl
from schwab_cli.screener.locate import locate_target_put, underlying_last
from schwab_cli.screener.ranking import rank_survivors
from schwab_cli.screener.snapshot import VolContext, build_snapshot, is_survivor
from schwab_cli.storage import screener as store
from schwab_cli.storage.screener import ContractSnapshot

_NY = timezone(timedelta(hours=-4))  # snapshot-day derivation only; see build_live_deps
_RV_CUTOFF_DAYS = 30  # only attempt forward-RV once the ~21-trading-day window elapsed


@dataclass
class ScreenerDeps:
    universe: Callable[[], list[str]]
    fetch_chain: Callable[[str], dict]
    vol_context: Callable[[str], VolContext]
    earnings_date: Callable[[str], str | None]
    forward_closes: Callable[[str, str], list[float]]
    settle_price: Callable[[str, str], float | None]
    market_open: bool


def _settle_due(conn, deps: ScreenerDeps, *, snapshot_date: str, now_ms: int) -> int:
    n = 0
    for row in store.read_unsettled_due(conn, on_or_after_expiry=snapshot_date):
        s_exp = deps.settle_price(row["symbol"], row["expiry"])
        if s_exp is None:
            continue
        pnl = settle_pnl(row["premium_bid"], row["strike"], s_exp)
        store.settle_position(
            conn, open_date=row["open_date"], symbol=row["symbol"],
            settle_price=s_exp, pnl=pnl, settled_at=now_ms,
        )
        n += 1
    return n


def _backfill_rv(conn, deps: ScreenerDeps, *, snapshot_date: str) -> int:
    cutoff = (date.fromisoformat(snapshot_date) - timedelta(days=_RV_CUTOFF_DAYS)).isoformat()
    n = 0
    for row in store.read_snapshots_needing_rv(conn, on_or_before=cutoff):
        rv = forward_rv(deps.forward_closes(row["symbol"], row["snapshot_date"]))
        if rv is None:
            continue
        store.set_forward_rv(
            conn, snapshot_date=row["snapshot_date"], symbol=row["symbol"], rv=rv
        )
        n += 1
    return n


def _snapshot_symbol(
    deps: ScreenerDeps, symbol: str, *, snapshot_date: str, now_ms: int,
    cfg: ScreenerConfig,
) -> ContractSnapshot:
    try:
        raw = deps.fetch_chain(symbol)
        tp, reason = locate_target_put(raw)
        return build_snapshot(
            snapshot_date=snapshot_date, symbol=symbol, captured_at_ms=now_ms,
            tp=tp, locate_reason=reason, vol_ctx=deps.vol_context(symbol),
            underlying_last=underlying_last(raw),
            next_earnings_date=deps.earnings_date(symbol),
            market_open=deps.market_open, cfg=cfg,
        )
    except Exception as e:  # noqa: BLE001 — capture per-symbol, never abort the run
        return ContractSnapshot(
            snapshot_date=snapshot_date, symbol=symbol, captured_at_ms=now_ms,
            snapshot_quality="error", filter_reason=f"{type(e).__name__}: {e}"[:120],
        )


def run_screener_update(
    conn, deps: ScreenerDeps, cfg: ScreenerConfig, *, snapshot_date: str, now_ms: int
) -> dict:
    settled = _settle_due(conn, deps, snapshot_date=snapshot_date, now_ms=now_ms)
    rv_filled = _backfill_rv(conn, deps, snapshot_date=snapshot_date)

    snaps: list[ContractSnapshot] = []
    for symbol in deps.universe():
        snap = _snapshot_symbol(
            deps, symbol, snapshot_date=snapshot_date, now_ms=now_ms, cfg=cfg
        )
        store.record_contract_snapshot(conn, snap)
        snaps.append(snap)

    survivors = [s for s in snaps if is_survivor(s)]
    ranked = rank_survivors(survivors, cfg)
    store.write_ranking(conn, ranking_date=snapshot_date, rows=ranked)

    opened = 0
    for cohort, row in select_cohorts(ranked, cfg):
        if row.get("target_expiry") is None or row.get("put_bid") is None:
            continue
        store.open_position(
            conn, open_date=snapshot_date, symbol=row["symbol"], cohort=cohort,
            strike=row["put_strike"], dte=row["dte"] or 0,
            premium_bid=row["put_bid"], expiry=row["target_expiry"],
        )
        opened += 1

    conn.commit()
    return {
        "snapshot_date": snapshot_date,
        "universe": len(snaps),
        "survivors": len(survivors),
        "ranked": len(ranked),
        "positions_opened": opened,
        "settled": settled,
        "rv_backfilled": rv_filled,
        "filtered": _filter_counts(snaps),
    }


def _filter_counts(snaps: list[ContractSnapshot]) -> dict:
    counts: dict[str, int] = {}
    for s in snaps:
        key = s.filter_reason if s.snapshot_quality == "ok" else s.snapshot_quality
        if key:
            counts[key] = counts.get(key, 0) + 1
    return counts


# --------------------------------------------------------------------------
# Live wiring
# --------------------------------------------------------------------------

def build_live_deps(conn, client, cfg: ScreenerConfig, *, snapshot_date: str) -> ScreenerDeps:
    """Construct real deps from an authed client + the market_data.db conn."""
    from schwab_cli.analytics.vol import realized_vol
    from schwab_cli.api.chains import get_chain
    from schwab_cli.dataset.store import list_active_subscriptions
    from schwab_cli.service.vol import compute_iv_rank_and_percentile
    from schwab_cli.storage import ohlcv_history
    from schwab_cli.storage.vol_history import (
        read_atm_iv_30d_per_day,
        read_recent_per_day,
    )

    anchor = date.fromisoformat(snapshot_date)

    def universe() -> list[str]:
        rows = list_active_subscriptions(conn, group_name="volatility")
        return sorted({r["symbol"] for r in rows})

    def fetch_chain(symbol: str) -> dict:
        return get_chain(
            client, symbol, contract_type="PUT", strike_count=40,
            from_date=anchor + timedelta(days=20),
            to_date=anchor + timedelta(days=45),
        )

    def vol_context(symbol: str) -> VolContext:
        closes = [
            r["close"]
            for r in ohlcv_history.read_range(
                conn, symbol=symbol, start=anchor - timedelta(days=90), end=anchor
            )
        ]
        hv_30d = realized_vol(closes, window=30)
        s30 = read_atm_iv_30d_per_day(conn, symbol=symbol, lookback_days=1)
        atm_iv_30d = s30[-1] if s30 else None
        recent = read_recent_per_day(conn, symbol=symbol, lookback_days=1)
        today_atm_iv = recent[-1] if recent else None
        ivr_res = compute_iv_rank_and_percentile(
            conn, symbol=symbol, today_iv_30d=atm_iv_30d,
            today_atm_iv=today_atm_iv, lookback=252,
        )
        low_conf = bool(ivr_res.get("low_history")) or (
            ivr_res.get("n_days", 0) < cfg.ivr_low_conf_days
        )
        ivr = ivr_res.get("ivr")
        return VolContext(
            atm_iv_30d=atm_iv_30d, hv_30d=hv_30d,
            ivr=(ivr / 100.0 if ivr is not None else None), ivr_low_conf=low_conf,
        )

    def earnings_date(symbol: str) -> str | None:
        return store.next_event_date(
            conn, symbol=symbol, event_type="earnings", on_or_after=snapshot_date
        )

    def forward_closes(symbol: str, anchor_date: str) -> list[float]:
        a = date.fromisoformat(anchor_date)
        return [
            r["close"]
            for r in ohlcv_history.read_range(
                conn, symbol=symbol, start=a, end=a + timedelta(days=45)
            )
        ]

    def settle_price(symbol: str, expiry: str) -> float | None:
        e = date.fromisoformat(expiry)
        rows = ohlcv_history.read_range(
            conn, symbol=symbol, start=e, end=e + timedelta(days=7)
        )
        return rows[0]["close"] if rows else None

    return ScreenerDeps(
        universe=universe, fetch_chain=fetch_chain, vol_context=vol_context,
        earnings_date=earnings_date, forward_closes=forward_closes,
        settle_price=settle_price, market_open=_is_ny_weekday(snapshot_date),
    )


def _is_ny_weekday(iso_date: str) -> bool:
    return date.fromisoformat(iso_date).weekday() < 5


def ny_snapshot_date(now_ms: int) -> str:
    """NY trading-day ISO string for a UTC-ms timestamp."""
    return datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).astimezone(_NY).date().isoformat()


def run_daily(cfg: ScreenerConfig | None = None) -> dict:
    """Entry point wired to real services (called by the CLI/job)."""
    from schwab_cli.service.base import BaseService
    from schwab_cli.storage.vol_history import connect

    cfg = cfg or ScreenerConfig()
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    snapshot_date = ny_snapshot_date(now_ms)
    svc = BaseService()
    with svc._authed_client() as client, connect() as conn:
        # Capture point-in-time membership before ranking (survivorship guard).
        from schwab_cli.screener.membership import record_membership_snapshot

        record_membership_snapshot(conn, as_of_date=snapshot_date, now_ms=now_ms)
        deps = build_live_deps(conn, client, cfg, snapshot_date=snapshot_date)
        return run_screener_update(
            conn, deps, cfg, snapshot_date=snapshot_date, now_ms=now_ms
        )


# re-exported for callers/tests that build partial snapshots
__all__ = ["ScreenerDeps", "run_screener_update", "build_live_deps", "run_daily",
           "ny_snapshot_date", "dataclasses"]
