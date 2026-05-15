"""sleep_until_ny: wall-clock loop; past target → no-op; future
target → blocks; clock-jump robustness."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from schwab_cli.dataset.scheduler import sleep_until_ny


_NY = ZoneInfo("America/New_York")


def test_past_target_is_noop():
    fake_now = MagicMock(return_value=datetime(2026, 5, 14, 18, 0, tzinfo=_NY))
    fake_sleep = MagicMock()
    sleep_until_ny(17, 0, now_provider=fake_now, sleep_fn=fake_sleep)
    fake_sleep.assert_not_called()


def test_future_target_sleeps_remaining_seconds():
    times = iter([
        datetime(2026, 5, 14, 15, 0, tzinfo=_NY),  # initial replace anchor
        datetime(2026, 5, 14, 15, 0, tzinfo=_NY),  # first loop check
        datetime(2026, 5, 14, 17, 0, tzinfo=_NY),  # post-sleep — at target
    ])
    fake_sleep = MagicMock()
    sleep_until_ny(
        17, 0,
        now_provider=lambda: next(times),
        sleep_fn=fake_sleep,
    )
    # First iteration sees a 7200s gap; loop caps at 60s.
    assert fake_sleep.call_count == 1
    assert fake_sleep.call_args[0][0] == 60.0


def test_clock_jump_exits_on_next_iteration():
    times = iter([
        datetime(2026, 5, 14, 15, 0, tzinfo=_NY),  # anchor
        datetime(2026, 5, 14, 15, 0, tzinfo=_NY),  # first check
        datetime(2026, 5, 14, 17, 30, tzinfo=_NY),  # post-sleep jumped past
    ])
    fake_sleep = MagicMock()
    sleep_until_ny(
        17, 0,
        now_provider=lambda: next(times),
        sleep_fn=fake_sleep,
    )
    assert fake_sleep.call_count == 1
