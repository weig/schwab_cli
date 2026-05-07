"""Tier state machine — pure logic, no I/O, no clock reads.

Caller passes ``now: datetime`` so this module is fully deterministic
and unit-testable. The dataset cron's volatility job is the only writer
that mutates ``ticker_state``; the state machine here computes the
*next* TierState from current TierState + today's signals.

Tiers:
    GRACE  — initial bootstrap state for newly-discovered symbols;
             upgraded on first evaluation.
    ACTIVE — sampled every cron run.
    WATCH  — closed-position only; sampled Mondays only. 90 calendar
             days without re-opening demotes to FROZEN.
    FROZEN — terminal until the position re-opens.

Source-priority dispatch (§6.1):
    ``equity`` (explicit subscribe) → always ACTIVE.
    ``indices`` (current member or within
        :data:`schwab_cli.dataset.store.INDICES_GRACE_DAYS_AFTER_REMOVAL`
        days of removal) → always ACTIVE. The grace window is enforced
        upstream by ``list_active_subscriptions`` / ``sources_for_symbol``;
        once it elapses the row drops out of the working set.
    ``position`` only → position clock (ACTIVE/WATCH/FROZEN by close-age).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Thresholds:
    position_watch_days: int
    position_frozen_days: int


@dataclass(frozen=True)
class TierState:
    tier: str  # 'GRACE' | 'ACTIVE' | 'WATCH' | 'FROZEN'
    tier_since: datetime
    consecutive_days_below: int


def transition_position_clock(
    state: TierState,
    *,
    now: datetime,
    has_active_position: bool,
    last_close_at: datetime | None,
    thr: Thresholds,
) -> TierState:
    """Apply one tick of the position-source clock.

    Re-opening a position (``has_active_position=True``) immediately
    promotes to ACTIVE regardless of prior tier. After the last close,
    the symbol stays ACTIVE for ``position_watch_days`` calendar days,
    then WATCH for ``position_frozen_days - position_watch_days``,
    then FROZEN.
    """
    if has_active_position:
        if state.tier == "ACTIVE":
            return state
        return TierState(tier="ACTIVE", tier_since=now,
                         consecutive_days_below=0)
    if last_close_at is None:
        return state
    days = (now - last_close_at).days
    if days < thr.position_watch_days:
        target = "ACTIVE"
    elif days < thr.position_frozen_days:
        target = "WATCH"
    else:
        target = "FROZEN"
    if state.tier == target:
        return state
    return TierState(tier=target, tier_since=now,
                     consecutive_days_below=0)


def resolve_tier(
    state: TierState,
    *,
    sources: set[str],
    now: datetime,
    has_active_position: bool,
    last_close_at: datetime | None,
    thr: Thresholds,
) -> TierState:
    """Source-priority dispatcher — see module docstring."""
    if "equity" in sources or "indices" in sources:
        if state.tier == "ACTIVE":
            return state
        return TierState(tier="ACTIVE", tier_since=now,
                         consecutive_days_below=0)
    if "position" in sources:
        return transition_position_clock(
            state, now=now,
            has_active_position=has_active_position,
            last_close_at=last_close_at,
            thr=thr,
        )
    return state
