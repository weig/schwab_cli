"""Crontab string → launchd StartCalendarInterval translation.

Supports the standard 5-field crontab grammar (min hour day month dow).
Rejects anything we can't translate (ranges, steps, names, @daily) so
behavior stays predictable.
"""
from __future__ import annotations

import pytest

from schwab_cli.dataset.launchd import crontab_to_calendar_interval


def test_daily_22_00():
    out = crontab_to_calendar_interval("0 22 * * *")
    assert out == [{"Hour": 22, "Minute": 0}]


def test_weekly_sunday_06_00():
    out = crontab_to_calendar_interval("0 6 * * 0")
    assert out == [{"Hour": 6, "Minute": 0, "Weekday": 0}]


def test_specific_dom():
    out = crontab_to_calendar_interval("30 9 1 * *")
    assert out == [{"Hour": 9, "Minute": 30, "Day": 1}]


def test_rejects_step():
    with pytest.raises(ValueError, match="cannot translate"):
        crontab_to_calendar_interval("*/15 * * * *")


def test_rejects_range():
    with pytest.raises(ValueError, match="cannot translate"):
        crontab_to_calendar_interval("0 9-17 * * *")


def test_rejects_named_shorthand():
    with pytest.raises(ValueError, match="cannot translate"):
        crontab_to_calendar_interval("@daily")


def test_rejects_wrong_field_count():
    with pytest.raises(ValueError, match="5 fields"):
        crontab_to_calendar_interval("0 22 * *")


def test_field_value_out_of_range():
    with pytest.raises(ValueError, match="hour"):
        crontab_to_calendar_interval("0 25 * * *")
