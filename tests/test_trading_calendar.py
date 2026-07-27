"""NYSE trading-calendar checks (INC-1 weekend/holiday gate)."""
from __future__ import annotations

from datetime import date

import pytest

from schwab_cli.analytics.trading_calendar import (
    is_half_day,
    is_trading_day,
    nyse_holidays,
)


def test_the_incident_sunday_is_not_a_trading_day():
    assert is_trading_day(date(2026, 5, 17)) is False   # Sunday — the incident


@pytest.mark.parametrize("d", [
    date(2026, 5, 2), date(2026, 5, 3), date(2026, 5, 9),
    date(2026, 5, 10), date(2026, 5, 16), date(2026, 5, 31),
])
def test_all_polluted_weekend_dates_rejected(d):
    assert is_trading_day(d) is False


def test_a_normal_weekday_is_a_trading_day():
    assert is_trading_day(date(2026, 7, 24)) is True    # Friday, GOOG baseline


@pytest.mark.parametrize("holiday", [
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # MLK (3rd Mon Jan)
    date(2026, 2, 16),   # Presidents' Day (3rd Mon Feb)
    date(2026, 4, 3),    # Good Friday 2026
    date(2026, 5, 25),   # Memorial Day (last Mon May)
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # July 4 (Sat) observed Fri 7/3
    date(2026, 9, 7),    # Labor Day (1st Mon Sep)
    date(2026, 11, 26),  # Thanksgiving (4th Thu Nov)
    date(2026, 12, 25),  # Christmas
])
def test_nyse_holidays_2026(holiday):
    assert holiday in nyse_holidays(2026)
    assert is_trading_day(holiday) is False


def test_july4_2026_observed_friday_not_saturday():
    # 2026-07-04 is Saturday → observed Friday 7/3; 7/6 Monday is open.
    assert date(2026, 7, 3) in nyse_holidays(2026)
    assert is_trading_day(date(2026, 7, 6)) is True


def test_half_day_is_still_a_trading_day():
    day_after_thanksgiving = date(2026, 11, 27)   # Friday after 11/26
    assert is_trading_day(day_after_thanksgiving) is True
    assert is_half_day(day_after_thanksgiving) is True
    assert is_half_day(date(2026, 7, 24)) is False
