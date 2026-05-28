"""TDD red-phase tests for schwab_cli.server.jobs.schedule.next_run_after.

All imports are expected to fail (ModuleNotFoundError) until the module is implemented.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from schwab_cli.server.jobs.schedule import next_run_after


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

UTC = timezone.utc


def utc(year, month, day, hour, minute=0, second=0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Basic correctness
# ---------------------------------------------------------------------------


def test_daily_job_ET_winter_returns_correct_UTC():
    """
    "0 9 * * *" in America/New_York during EST (UTC-5).
    09:00 ET = 14:00 UTC.
    Given after=2024-01-15 13:59:00 UTC (just before 09:00 ET),
    next fire must be 2024-01-15 14:00:00 UTC.
    """
    after = utc(2024, 1, 15, 13, 59, 0)
    result = next_run_after("0 9 * * *", "America/New_York", after)
    expected = utc(2024, 1, 15, 14, 0, 0)
    assert result == expected


def test_daily_job_ET_summer_returns_correct_UTC():
    """
    "0 9 * * *" in America/New_York during EDT (UTC-4).
    09:00 ET = 13:00 UTC.
    Given after=2024-07-10 12:59:00 UTC, next fire must be 2024-07-10 13:00:00 UTC.
    """
    after = utc(2024, 7, 10, 12, 59, 0)
    result = next_run_after("0 9 * * *", "America/New_York", after)
    expected = utc(2024, 7, 10, 13, 0, 0)
    assert result == expected


def test_result_is_strictly_after_input():
    after = utc(2024, 3, 10, 0, 0, 0)
    result = next_run_after("0 9 * * *", "America/New_York", after)
    assert result > after


def test_result_is_timezone_aware():
    after = utc(2024, 1, 1, 0, 0, 0)
    result = next_run_after("0 9 * * *", "America/New_York", after)
    assert result.tzinfo is not None
    # Must be expressible as UTC (offset zero or explicit UTC)
    result_utc = result.astimezone(UTC)
    assert result_utc.utcoffset().total_seconds() == 0


# ---------------------------------------------------------------------------
# Monotonic sequence
# ---------------------------------------------------------------------------


def test_repeated_calls_yield_strictly_increasing_sequence():
    """Feed each result back as 'after'; sequence must be strictly increasing
    and every result must land on 09:00 New York wall-clock time."""
    import zoneinfo

    cron = "0 9 * * *"
    tz_name = "America/New_York"
    ny = zoneinfo.ZoneInfo(tz_name)

    after = utc(2024, 6, 1, 0, 0, 0)
    previous = after
    for _ in range(7):
        result = next_run_after(cron, tz_name, previous)
        assert result > previous
        local = result.astimezone(ny)
        assert local.hour == 9
        assert local.minute == 0
        previous = result


# ---------------------------------------------------------------------------
# DST spring-forward: 02:30 America/New_York does not exist on transition day
# ---------------------------------------------------------------------------


def test_dst_spring_forward_does_not_raise():
    """
    In 2024, US DST spring-forward is 2024-03-10: clocks jump from 02:00 to 03:00.
    A job at "30 2 * * *" in America/New_York means 02:30 which does not
    exist on 2024-03-10.  The implementation must NOT raise and must return
    a strictly-later valid instant.

    Policy: advance to the next valid instant (the exact resulting time is
    implementation-defined, but it must be strictly after `after` and tz-aware).
    """
    # after = just before where 02:30 ET would have been on the spring-forward day
    after = utc(2024, 3, 10, 6, 0, 0)  # 01:00 EST = 06:00 UTC (pre-skip)
    result = next_run_after("30 2 * * *", "America/New_York", after)
    # Must not raise; result must be strictly after `after` and tz-aware
    assert result > after
    assert result.tzinfo is not None


def test_dst_spring_forward_sequence_stays_monotonic():
    """Generating several fire times across the spring-forward boundary
    must yield a strictly increasing sequence with no stuck instants."""
    cron = "30 2 * * *"
    tz_name = "America/New_York"

    # Start a few days before the 2024 spring-forward (2024-03-10)
    after = utc(2024, 3, 8, 0, 0, 0)
    previous = after
    for _ in range(5):
        result = next_run_after(cron, tz_name, previous)
        assert result > previous, f"sequence stalled: {previous} -> {result}"
        assert result.tzinfo is not None
        previous = result


# ---------------------------------------------------------------------------
# DST fall-back: 01:30 America/New_York occurs twice on transition day
# ---------------------------------------------------------------------------


def test_dst_fall_back_does_not_produce_stuck_instant():
    """
    In 2024, US DST fall-back is 2024-11-03: clocks repeat 01:00-01:59 twice.
    A daily job at "30 1 * * *" (01:30 ET) should still advance past the
    ambiguous instant and never return the same UTC time twice.
    """
    cron = "30 1 * * *"
    tz_name = "America/New_York"

    # Start just before the fall-back day
    after = utc(2024, 11, 2, 0, 0, 0)
    previous = after
    for _ in range(4):
        result = next_run_after(cron, tz_name, previous)
        assert result > previous, f"sequence stalled at fall-back: {previous} -> {result}"
        assert result.tzinfo is not None
        previous = result


def test_dst_fall_back_result_is_valid_aware_datetime():
    """Result from the fall-back transition day is a tz-aware datetime."""
    after = utc(2024, 11, 3, 5, 0, 0)  # after both ambiguous occurrences
    result = next_run_after("30 1 * * *", "America/New_York", after)
    assert result > after
    assert result.tzinfo is not None


# ---------------------------------------------------------------------------
# Naive datetime guard
# ---------------------------------------------------------------------------


def test_naive_after_raises_value_error():
    naive = datetime(2024, 1, 15, 13, 59, 0)  # no tzinfo
    with pytest.raises(ValueError, match="tz-aware"):
        next_run_after("0 9 * * *", "America/New_York", naive)
