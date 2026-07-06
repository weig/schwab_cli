"""Hard filters (plan §4) — a survivor gate run over ok-quality snapshots.

``hard_filter_reason`` returns the first failing filter's reason string, or
``None`` when the snapshot survives all of them. Filters run in the spec's
order so the recorded reason matches the first disqualifier. All thresholds
come from :class:`ScreenerConfig`. Earnings is fail-closed: a name with an
unknown earnings date is dropped (``earnings_unknown``) so a missing feed
never lets a hidden event through.
"""
from __future__ import annotations

from schwab_cli.screener.config import ScreenerConfig
from schwab_cli.storage.screener import ContractSnapshot


def hard_filter_reason(snap: ContractSnapshot, cfg: ScreenerConfig) -> str | None:
    # 1. earnings window (fail-closed on unknown)
    if snap.next_earnings_date is None or snap.days_to_earnings is None:
        if cfg.require_earnings_date:
            return "earnings_unknown"
    elif snap.days_to_earnings <= cfg.earnings_window_days:
        return "earnings_window"

    # 2. IV sanity (bad-data protection)
    if snap.atm_iv_30d is None:
        return "iv_missing"
    if not (cfg.iv_lo < snap.atm_iv_30d < cfg.iv_hi):
        return "iv_out_of_range"

    # 3. spread (None means mid<=0 — treat as too wide)
    if snap.spread_pct is None or snap.spread_pct > cfg.spread_pct_max:
        return "spread_too_wide"

    # 4. open interest
    if (snap.put_oi or 0) < cfg.put_oi_min:
        return "oi_too_low"

    # 5. volume
    if (snap.put_volume or 0) < cfg.put_volume_min:
        return "volume_too_low"

    # 6. underlying price floor
    if snap.underlying_last is None or snap.underlying_last < cfg.underlying_min:
        return "underlying_too_low"

    # 7. bid validity
    if (snap.put_bid or 0.0) <= cfg.bid_min:
        return "bid_too_low"

    return None
