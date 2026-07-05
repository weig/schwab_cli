"""Stage A — assemble a ContractSnapshot with data-quality guards (§6).

Combines the located target put, per-symbol vol context (atm_iv_30d, hv_30d,
IVR), and the earnings lookup into one snapshot row, tagging its
``snapshot_quality`` and (for ok rows) the first hard-filter reason. Bad or
off-hours rows are kept for diagnosis but marked so ranking excludes them.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import date

from schwab_cli.screener.config import ScreenerConfig
from schwab_cli.screener.filters import hard_filter_reason
from schwab_cli.screener.locate import DELTA_BAND, TargetPut
from schwab_cli.storage.screener import ContractSnapshot


@dataclass(frozen=True)
class VolContext:
    atm_iv_30d: float | None = None
    hv_30d: float | None = None
    ivr: float | None = None
    ivr_low_conf: bool = False


def assess_quality(
    tp: TargetPut | None, locate_reason: str | None, *, market_open: bool
) -> tuple[str, str | None]:
    """Return (snapshot_quality, quality_reason).

    ``stale_quote`` off-hours (spec §6: a real IV=8.43 bad row came from an
    off-hours capture); ``bad_data`` when no target contract, bid>ask, or the
    delta is outside the plausibility band; else ``ok``.
    """
    if not market_open:
        return "stale_quote", "market_closed"
    if tp is None:
        return "bad_data", locate_reason or "no_contract"
    if tp.bid > tp.ask:
        return "bad_data", "bid_gt_ask"
    if not (DELTA_BAND[0] <= tp.delta <= DELTA_BAND[1]):
        return "bad_data", "delta_out_of_band"
    return "ok", None


def _days_between(from_date: str, to_date: str | None) -> int | None:
    if not to_date:
        return None
    try:
        return (date.fromisoformat(to_date) - date.fromisoformat(from_date)).days
    except ValueError:
        return None


def build_snapshot(
    *,
    snapshot_date: str,
    symbol: str,
    captured_at_ms: int,
    tp: TargetPut | None,
    locate_reason: str | None,
    vol_ctx: VolContext,
    underlying_last: float | None,
    next_earnings_date: str | None,
    market_open: bool,
    cfg: ScreenerConfig,
) -> ContractSnapshot:
    quality, qreason = assess_quality(tp, locate_reason, market_open=market_open)
    days_to_earnings = _days_between(snapshot_date, next_earnings_date)
    snap = ContractSnapshot(
        snapshot_date=snapshot_date,
        symbol=symbol,
        captured_at_ms=captured_at_ms,
        target_expiry=tp.expiry if tp else None,
        dte=tp.dte if tp else None,
        put_strike=tp.strike if tp else None,
        put_delta_actual=tp.delta if tp else None,
        put_bid=tp.bid if tp else None,
        put_ask=tp.ask if tp else None,
        put_mid=tp.mid if tp else None,
        put_oi=tp.open_interest if tp else None,
        put_volume=tp.volume if tp else None,
        spread_pct=tp.spread_pct if tp else None,
        underlying_last=underlying_last,
        atm_iv_30d=vol_ctx.atm_iv_30d,
        hv_30d=vol_ctx.hv_30d,
        ivr=vol_ctx.ivr,
        ivr_low_conf=vol_ctx.ivr_low_conf,
        next_earnings_date=next_earnings_date,
        days_to_earnings=days_to_earnings,
        snapshot_quality=quality,
        filter_reason=qreason if quality != "ok" else None,
    )
    if quality == "ok":
        reason = hard_filter_reason(snap, cfg)
        if reason is not None:
            snap = dataclasses.replace(snap, filter_reason=reason)
    return snap


def is_survivor(snap: ContractSnapshot) -> bool:
    """A rankable survivor: ok quality and no hard-filter reason."""
    return snap.snapshot_quality == "ok" and snap.filter_reason is None
