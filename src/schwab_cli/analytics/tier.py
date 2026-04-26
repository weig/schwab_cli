"""Tier state machine — pure logic, no I/O, no clock reads.

Caller passes ``now: datetime`` and ``is_trading_day: bool`` so this
module is fully deterministic and unit-testable. The dataset cron's
volatility job is the only writer that mutates ``ticker_state``; the
state machine here computes the *next* TierState from current
TierState + today's signals.

Tiers (highest → lowest demotion):
    GRACE  — newly subscribed; no threshold eval; 7-trading-day budget.
    ACTIVE — sampled every cron run; below threshold for 7 consec
             trading days demotes to WATCH.
    WATCH  — sampled Mondays only; 30 calendar days without a pass
             demotes to FROZEN; any pass promotes back to ACTIVE.
    FROZEN — terminal; re-subscribe to revive.

For positions (§6.3), see :func:`transition_position_clock`.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Thresholds:
    active_min_chain_volume: int
    active_min_front2_oi: int
    watch_demote_after_trading_days: int
    frozen_demote_after_calendar_days: int
    position_watch_days: int
    position_frozen_days: int
    grace_trading_days: int


@dataclass(frozen=True)
class TierState:
    tier: str  # 'GRACE' | 'ACTIVE' | 'WATCH' | 'FROZEN'
    tier_since: datetime
    consecutive_days_below: int


def transition_indices_clock(
    state: TierState,
    *,
    now: datetime,
    threshold_pass: bool,
    is_trading_day: bool,
    thr: Thresholds,
    trading_days_since: int | None = None,
) -> TierState:
    """Apply one tick of the indices clock.

    ``trading_days_since`` (the count of trading days between
    ``state.tier_since`` and ``now``) is optional — if omitted, we
    estimate it via calendar days × 5/7. Real callers (the dataset
    cron) should pass it explicitly using
    :func:`schwab_cli.order_policy._calendar.trading_days_between`.
    """
    if state.tier == "FROZEN":
        return state

    if state.tier == "GRACE":
        elapsed = trading_days_since
        if elapsed is None:
            elapsed = _approx_trading_days(state.tier_since, now)
        if elapsed >= thr.grace_trading_days:
            return TierState(tier="ACTIVE", tier_since=now,
                             consecutive_days_below=0)
        return state

    if state.tier == "ACTIVE":
        if threshold_pass:
            return replace(state, consecutive_days_below=0)
        if not is_trading_day:
            return state
        new_count = state.consecutive_days_below + 1
        if new_count >= thr.watch_demote_after_trading_days:
            return TierState(tier="WATCH", tier_since=now,
                             consecutive_days_below=new_count)
        return replace(state, consecutive_days_below=new_count)

    if state.tier == "WATCH":
        if threshold_pass:
            return TierState(tier="ACTIVE", tier_since=now,
                             consecutive_days_below=0)
        days_in_watch = (now - state.tier_since).days
        if days_in_watch >= thr.frozen_demote_after_calendar_days:
            return TierState(tier="FROZEN", tier_since=now,
                             consecutive_days_below=state.consecutive_days_below)
        return state

    return state


def _approx_trading_days(start: datetime, end: datetime) -> int:
    """Crude approximation: 5 trading days per 7 calendar days.

    Real callers pass an exact count from order_policy._calendar; this
    fallback exists so unit tests don't need to monkeypatch the calendar.
    """
    return max(0, (end.date() - start.date()).days * 5 // 7)


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
    threshold_pass: bool,
    is_trading_day: bool,
    has_active_position: bool,
    last_close_at: datetime | None,
    thr: Thresholds,
    trading_days_since: int | None = None,
) -> TierState:
    """Source-priority dispatcher (§6.1).

    ``equity`` (explicit subscribe) → always ACTIVE.
    ``position`` with active holdings → always ACTIVE.
    ``position`` with closed holdings → position clock.
    ``indices`` only → indices clock.

    When sources overlap (e.g. {indices, position}), the highest-tier
    rule wins: explicit/position-active beats indices.
    """
    if "equity" in sources:
        if state.tier == "ACTIVE":
            return state
        return TierState(tier="ACTIVE", tier_since=now,
                         consecutive_days_below=0)
    if "position" in sources:
        if has_active_position:
            return transition_position_clock(
                state, now=now,
                has_active_position=True,
                last_close_at=last_close_at,
                thr=thr,
            )
        position_state = transition_position_clock(
            state, now=now,
            has_active_position=False,
            last_close_at=last_close_at,
            thr=thr,
        )
        if "indices" not in sources:
            return position_state
        indices_state = transition_indices_clock(
            state, now=now,
            threshold_pass=threshold_pass,
            is_trading_day=is_trading_day,
            thr=thr,
            trading_days_since=trading_days_since,
        )
        return _higher_tier(position_state, indices_state)
    return transition_indices_clock(
        state, now=now,
        threshold_pass=threshold_pass,
        is_trading_day=is_trading_day,
        thr=thr,
        trading_days_since=trading_days_since,
    )


_TIER_RANK = {"ACTIVE": 3, "GRACE": 2, "WATCH": 1, "FROZEN": 0}


def _higher_tier(a: TierState, b: TierState) -> TierState:
    return a if _TIER_RANK[a.tier] >= _TIER_RANK[b.tier] else b
