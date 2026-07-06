"""Daily screener computation — a pure READER over captured data.

The dataset vol job is the sole fetcher/writer of option quotes (it persists
the put band to ``put_chain_snapshots``). This module never touches the
network or auth: it reads the captured band + stored vol context + earnings,
locates each symbol's target put, applies filters, ranks by executable VRP,
and maintains the paper ledger. Because it reads from storage, historical
rankings are fully reproducible and nothing the screener needs is ever lost.

``run_screener_update`` is written against an injected :class:`ScreenerDeps`
so the flow is unit-testable with fakes; ``build_read_deps`` wires the real
storage reads. One idempotent daily pass: settle → forward-RV backfill →
snapshot+filter → rank → open top/bottom.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from schwab_cli.screener.config import ScreenerConfig
from schwab_cli.screener.forward_rv import forward_rv
from schwab_cli.screener.ledger import select_cohorts, settle_pnl
from schwab_cli.screener.locate import locate_from_puts
from schwab_cli.screener.ranking import rank_survivors
from schwab_cli.screener.snapshot import VolContext, build_snapshot, is_survivor
from schwab_cli.storage import screener as store
from schwab_cli.storage.screener import ContractSnapshot

_NY = ZoneInfo("America/New_York")  # NY trading-day derivation
_RV_CUTOFF_DAYS = 30  # only attempt forward-RV once the ~21-trading-day window elapsed


@dataclass
class ScreenerDeps:
    universe: Callable[[], list[str]]
    put_band: Callable[[str], list[dict]]           # stored band rows for a symbol
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


def _underlying_from_band(band: list[dict]) -> float | None:
    for row in band:
        u = row.get("underlying_last")
        if isinstance(u, (int, float)):
            return float(u)
    return None


def _snapshot_symbol(
    deps: ScreenerDeps, symbol: str, *, snapshot_date: str, now_ms: int,
    cfg: ScreenerConfig,
) -> ContractSnapshot:
    try:
        band = deps.put_band(symbol)
        tp, reason = locate_from_puts(band)
        return build_snapshot(
            snapshot_date=snapshot_date, symbol=symbol, captured_at_ms=now_ms,
            tp=tp, locate_reason=reason, vol_ctx=deps.vol_context(symbol),
            underlying_last=_underlying_from_band(band),
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
# Read wiring (storage only — no network, no auth)
# --------------------------------------------------------------------------

def build_read_deps(conn, cfg: ScreenerConfig, *, snapshot_date: str) -> ScreenerDeps:
    """Construct storage-only deps for the daily screener computation."""
    from schwab_cli.analytics.vol import realized_vol
    from schwab_cli.service.vol import compute_iv_rank_and_percentile
    from schwab_cli.storage import ohlcv_history
    from schwab_cli.storage.vol_history import (
        read_atm_iv_30d_per_day,
        read_recent_per_day,
    )

    anchor = date.fromisoformat(snapshot_date)

    def universe() -> list[str]:
        return store.symbols_with_put_band(conn, snapshot_date=snapshot_date)

    def put_band(symbol: str) -> list[dict]:
        return [
            dict(r)
            for r in store.read_put_band(
                conn, snapshot_date=snapshot_date, symbol=symbol
            )
        ]

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
        universe=universe, put_band=put_band, vol_context=vol_context,
        earnings_date=earnings_date, forward_closes=forward_closes,
        settle_price=settle_price, market_open=_is_ny_weekday(snapshot_date),
    )


def _is_ny_weekday(iso_date: str) -> bool:
    return date.fromisoformat(iso_date).weekday() < 5


def ny_snapshot_date(now_ms: int) -> str:
    """NY trading-day ISO string for a UTC-ms timestamp."""
    return datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).astimezone(_NY).date().isoformat()


def run_daily(cfg: ScreenerConfig | None = None) -> dict:
    """Entry point for the screener CLI/job — storage-only, no auth/network."""
    from schwab_cli.screener.config import load_screener_config
    from schwab_cli.screener.membership import record_membership_snapshot
    from schwab_cli.storage.vol_history import connect

    cfg = cfg or load_screener_config()
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    snapshot_date = ny_snapshot_date(now_ms)
    with connect() as conn:
        record_membership_snapshot(conn, as_of_date=snapshot_date, now_ms=now_ms)
        deps = build_read_deps(conn, cfg, snapshot_date=snapshot_date)
        return run_screener_update(
            conn, deps, cfg, snapshot_date=snapshot_date, now_ms=now_ms
        )


__all__ = ["ScreenerDeps", "run_screener_update", "build_read_deps", "run_daily",
           "ny_snapshot_date", "dataclasses"]
