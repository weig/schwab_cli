"""Tier state machine — pure logic, no I/O.

Covers the indices-side clock (§6.2 of the spec):
  GRACE  → 7 trading days, then ACTIVE
  ACTIVE → 7 trading days below threshold → WATCH
  WATCH  → 30 calendar days → FROZEN; threshold pass → ACTIVE
  FROZEN → terminal until manual unsubscribe + re-subscribe
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from schwab_cli.analytics.tier import (
    TierState,
    Thresholds,
    transition_indices_clock,
)


def _now(year=2026, month=4, day=15):
    return datetime(year, month, day, 22, 0, tzinfo=timezone.utc)


def _thr():
    return Thresholds(
        active_min_chain_volume=5000,
        active_min_front2_oi=10000,
        watch_demote_after_trading_days=7,
        frozen_demote_after_calendar_days=30,
        position_watch_days=30,
        position_frozen_days=90,
        grace_trading_days=7,
    )


def test_grace_promotes_after_seven_trading_days():
    start = _now(2026, 1, 1)
    state = TierState(tier="GRACE", tier_since=start,
                      consecutive_days_below=0)
    next_state = transition_indices_clock(
        state,
        now=datetime(2026, 1, 12, 22, 0, tzinfo=timezone.utc),
        threshold_pass=False,
        is_trading_day=True,
        thr=_thr(),
        trading_days_since=7,
    )
    assert next_state.tier == "ACTIVE"


def test_grace_does_not_promote_too_early():
    start = _now(2026, 1, 1)
    state = TierState(tier="GRACE", tier_since=start,
                      consecutive_days_below=0)
    next_state = transition_indices_clock(
        state,
        now=datetime(2026, 1, 5, 22, 0, tzinfo=timezone.utc),
        threshold_pass=False,
        is_trading_day=True,
        thr=_thr(),
        trading_days_since=3,
    )
    assert next_state.tier == "GRACE"


def test_active_demotes_after_seven_consecutive_below():
    state = TierState(tier="ACTIVE", tier_since=_now(),
                      consecutive_days_below=6)
    next_state = transition_indices_clock(
        state,
        now=_now() + timedelta(days=1),
        threshold_pass=False,
        is_trading_day=True,
        thr=_thr(),
    )
    assert next_state.tier == "WATCH"
    assert next_state.consecutive_days_below == 7


def test_active_resets_counter_on_pass():
    state = TierState(tier="ACTIVE", tier_since=_now(),
                      consecutive_days_below=5)
    next_state = transition_indices_clock(
        state,
        now=_now() + timedelta(days=1),
        threshold_pass=True,
        is_trading_day=True,
        thr=_thr(),
    )
    assert next_state.tier == "ACTIVE"
    assert next_state.consecutive_days_below == 0


def test_active_does_not_increment_on_non_trading_day():
    state = TierState(tier="ACTIVE", tier_since=_now(),
                      consecutive_days_below=3)
    next_state = transition_indices_clock(
        state,
        now=_now() + timedelta(days=1),
        threshold_pass=False,
        is_trading_day=False,
        thr=_thr(),
    )
    assert next_state.consecutive_days_below == 3


def test_watch_promotes_immediately_on_pass():
    tier_since = _now()
    state = TierState(tier="WATCH", tier_since=tier_since,
                      consecutive_days_below=7)
    next_state = transition_indices_clock(
        state,
        now=tier_since + timedelta(days=10),
        threshold_pass=True,
        is_trading_day=True,
        thr=_thr(),
    )
    assert next_state.tier == "ACTIVE"
    assert next_state.consecutive_days_below == 0


def test_watch_demotes_after_thirty_calendar_days():
    tier_since = _now()
    state = TierState(tier="WATCH", tier_since=tier_since,
                      consecutive_days_below=7)
    next_state = transition_indices_clock(
        state,
        now=tier_since + timedelta(days=30),
        threshold_pass=False,
        is_trading_day=True,
        thr=_thr(),
    )
    assert next_state.tier == "FROZEN"


def test_frozen_is_terminal():
    state = TierState(tier="FROZEN", tier_since=_now(),
                      consecutive_days_below=99)
    next_state = transition_indices_clock(
        state,
        now=_now() + timedelta(days=1),
        threshold_pass=True,
        is_trading_day=True,
        thr=_thr(),
    )
    assert next_state.tier == "FROZEN"
