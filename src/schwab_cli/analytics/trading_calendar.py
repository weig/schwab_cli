"""Minimal NYSE trading-calendar check — dependency-free.

The volatility ingest must not sample on non-trading days: a weekend/holiday
run pulls a stale or empty chain (the GOOG 2026-05-17 Sunday incident), and
even after the sentinel filter it wastes API calls and writes null rows. A
full market-calendar library (pandas_market_calendars) would drag in pandas
for what is a weekend test plus a small fixed holiday set, so we compute the
handful of NYSE holidays directly.

Scope: regular full-day closures only. Half-days (day after Thanksgiving,
Christmas Eve) are treated as trading days — the market IS open, we just note
them. Ad-hoc closures (e.g. national days of mourning) are not modelled;
they are rare and a stale-chain run on one is caught by the sentinel filter.
"""
from __future__ import annotations

from datetime import date


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The ``n``-th ``weekday`` (Mon=0) of ``month`` — e.g. 3rd Monday."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return date(year, month, 1 + offset + (n - 1) * 7)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """The last ``weekday`` of ``month`` (used for Memorial Day)."""
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    from datetime import timedelta
    d = nxt - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    """Fixed-date holidays shift off weekends: Sat→Fri, Sun→Mon (NYSE rule)."""
    from datetime import timedelta
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _easter(year: int) -> date:
    """Anonymous Gregorian algorithm — anchors Good Friday."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ll = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ll) // 451
    month = (h + ll - 7 * m + 114) // 31
    day = ((h + ll - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def nyse_holidays(year: int) -> set[date]:
    """Regular full-day NYSE closures for ``year``."""
    from datetime import timedelta
    good_friday = _easter(year) - timedelta(days=2)
    return {
        _observed(date(year, 1, 1)),                      # New Year's Day
        _nth_weekday(year, 1, 0, 3),                      # MLK Day (3rd Mon)
        _nth_weekday(year, 2, 0, 3),                      # Presidents' Day
        good_friday,                                      # Good Friday
        _last_weekday(year, 5, 0),                        # Memorial Day
        _observed(date(year, 6, 19)),                     # Juneteenth
        _observed(date(year, 7, 4)),                      # Independence Day
        _nth_weekday(year, 9, 0, 1),                      # Labor Day (1st Mon)
        _nth_weekday(year, 11, 3, 4),                     # Thanksgiving (4th Thu)
        _observed(date(year, 12, 25)),                    # Christmas
    }


def is_trading_day(d: date) -> bool:
    """True if the NYSE holds a regular session on ``d`` (weekday, non-holiday).

    Half-days count as trading days. Callers wanting to note a half-day can
    use :func:`is_half_day`.
    """
    if d.weekday() >= 5:
        return False
    return d not in nyse_holidays(d.year)


def is_half_day(d: date) -> bool:
    """True on the common NYSE early-close sessions (day after Thanksgiving,
    Christmas Eve when a weekday). These are trading days; flagged for logs."""
    from datetime import timedelta
    if not is_trading_day(d):
        return False
    thanksgiving = _nth_weekday(d.year, 11, 3, 4)
    if d == thanksgiving + timedelta(days=1):
        return True
    return d.month == 12 and d.day == 24
