"""NY-anchored sleep helper.

launchd schedules in the system's local TZ only. To anchor the
market-data cron to 17:00 America/New_York regardless of DST, the
launchd plist fires at a fixed local time guaranteed *before* the NY
target in either DST mode, and the Python entry point calls
``sleep_until_ny(17, 0)`` here.

Loop is wall-clock based and re-checks every 60s so a clock jump
(system wake) doesn't strand us — next iteration sees the jump.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo


_NY = ZoneInfo("America/New_York")


def _default_now() -> datetime:
    return datetime.now(tz=_NY)


def sleep_until_ny(
    hour: int, minute: int,
    *,
    now_provider: Callable[[], datetime] = _default_now,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    """Block until NY wall-clock reaches ``hour:minute`` today.

    Returns immediately when called past target (catch-up branch for
    ``RunAtLoad``-fired late starts). Robust to system sleep — loop
    re-checks wall clock each iteration. Sleep intervals capped at
    60s so we re-check reality every minute.
    """
    target_today = now_provider().replace(
        hour=hour, minute=minute, second=0, microsecond=0,
    )
    while True:
        now = now_provider()
        if now >= target_today:
            return
        remaining = (target_today - now).total_seconds()
        sleep_fn(min(60.0, remaining))
