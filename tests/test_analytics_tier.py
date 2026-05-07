"""Tier state machine — pure logic, no I/O.

Indices-source symbols are always ACTIVE — current members because
they're current; recently-removed members because the
:data:`schwab_cli.dataset.store.INDICES_GRACE_DAYS_AFTER_REMOVAL`
window keeps them in the working set. The position clock retains its
ACTIVE → WATCH (30d) → FROZEN (90d) ladder for closed positions.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from schwab_cli.analytics.tier import (
    TierState,
    Thresholds,
    resolve_tier,
    transition_position_clock,
)


def _now(year=2026, month=4, day=15):
    return datetime(year, month, day, 22, 0, tzinfo=timezone.utc)


def _thr():
    return Thresholds(position_watch_days=30, position_frozen_days=90)


# ---- position clock ---------------------------------------------------


def test_position_active_when_holding():
    state = TierState(tier="ACTIVE", tier_since=_now(),
                      consecutive_days_below=0)
    next_state = transition_position_clock(
        state, now=_now() + timedelta(days=5),
        has_active_position=True,
        last_close_at=None,
        thr=_thr(),
    )
    assert next_state.tier == "ACTIVE"


def test_position_active_for_thirty_calendar_days_after_close():
    last_close = _now()
    state = TierState(tier="ACTIVE", tier_since=last_close,
                      consecutive_days_below=0)
    next_state = transition_position_clock(
        state, now=last_close + timedelta(days=29),
        has_active_position=False,
        last_close_at=last_close,
        thr=_thr(),
    )
    assert next_state.tier == "ACTIVE"


def test_position_demotes_to_watch_after_thirty_days():
    last_close = _now()
    state = TierState(tier="ACTIVE", tier_since=last_close,
                      consecutive_days_below=0)
    next_state = transition_position_clock(
        state, now=last_close + timedelta(days=30),
        has_active_position=False,
        last_close_at=last_close,
        thr=_thr(),
    )
    assert next_state.tier == "WATCH"


def test_position_demotes_to_frozen_after_ninety_days():
    last_close = _now()
    state = TierState(tier="WATCH", tier_since=last_close + timedelta(days=30),
                      consecutive_days_below=0)
    next_state = transition_position_clock(
        state, now=last_close + timedelta(days=90),
        has_active_position=False,
        last_close_at=last_close,
        thr=_thr(),
    )
    assert next_state.tier == "FROZEN"


def test_position_revives_to_active_when_reopened():
    last_close = _now()
    state = TierState(tier="FROZEN", tier_since=last_close + timedelta(days=90),
                      consecutive_days_below=0)
    next_state = transition_position_clock(
        state, now=last_close + timedelta(days=120),
        has_active_position=True,
        last_close_at=last_close,
        thr=_thr(),
    )
    assert next_state.tier == "ACTIVE"


# ---- resolve_tier dispatcher ------------------------------------------


def test_explicit_equity_always_active():
    state = TierState(tier="WATCH", tier_since=_now(),
                      consecutive_days_below=99)
    next_state = resolve_tier(
        state,
        sources={"equity"},
        now=_now() + timedelta(days=1),
        has_active_position=False,
        last_close_at=None,
        thr=_thr(),
    )
    assert next_state.tier == "ACTIVE"


def test_indices_source_always_active():
    """Current member or in-grace removed member → both ACTIVE.

    The grace window is enforced upstream by
    :func:`list_active_subscriptions` / :func:`sources_for_symbol`;
    once the row drops out of the working set those calls stop
    returning 'indices' for the symbol and ``resolve_tier`` falls
    through to whatever other source rule applies.
    """
    state = TierState(tier="WATCH", tier_since=_now(),
                      consecutive_days_below=99)
    next_state = resolve_tier(
        state,
        sources={"indices"},
        now=_now() + timedelta(days=1),
        has_active_position=False,
        last_close_at=None,
        thr=_thr(),
    )
    assert next_state.tier == "ACTIVE"


def test_indices_overrides_closed_position():
    """A closed position that's also a current index member stays ACTIVE."""
    last_close = _now()
    state = TierState(tier="WATCH", tier_since=last_close,
                      consecutive_days_below=0)
    next_state = resolve_tier(
        state,
        sources={"position", "indices"},
        now=last_close + timedelta(days=60),  # past position-watch cutoff
        has_active_position=False,
        last_close_at=last_close,
        thr=_thr(),
    )
    assert next_state.tier == "ACTIVE"


def test_active_position_uses_position_clock():
    state = TierState(tier="WATCH", tier_since=_now(),
                      consecutive_days_below=7)
    next_state = resolve_tier(
        state,
        sources={"position"},
        now=_now() + timedelta(days=1),
        has_active_position=True,
        last_close_at=None,
        thr=_thr(),
    )
    assert next_state.tier == "ACTIVE"


def test_closed_position_only_uses_position_clock():
    last_close = _now()
    state = TierState(tier="ACTIVE", tier_since=last_close,
                      consecutive_days_below=0)
    next_state = resolve_tier(
        state,
        sources={"position"},
        now=last_close + timedelta(days=30),
        has_active_position=False,
        last_close_at=last_close,
        thr=_thr(),
    )
    assert next_state.tier == "WATCH"
