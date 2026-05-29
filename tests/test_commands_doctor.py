"""doctor command — pure helpers (relative-time formatter).

The end-to-end doctor flow touches HTTP / launchctl / SQLite and is
exercised live; these unit tests just lock in the time-formatting
contract.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from schwab_cli.commands.doctor import _format_relative_time


_NOW = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)


# ---- _format_relative_time -------------------------------------------


def test_format_none_returns_dash():
    assert _format_relative_time(None, now=_NOW) == "—"


def test_format_under_one_minute_either_side():
    assert _format_relative_time(_NOW - timedelta(seconds=30), now=_NOW) == "<1m"
    assert _format_relative_time(_NOW + timedelta(seconds=30), now=_NOW) == "<1m"
    assert _format_relative_time(_NOW, now=_NOW) == "<1m"


def test_format_minutes_past_and_future():
    assert _format_relative_time(_NOW - timedelta(minutes=5), now=_NOW) == "5m ago"
    assert _format_relative_time(_NOW - timedelta(minutes=38), now=_NOW) == "38m ago"
    assert _format_relative_time(_NOW + timedelta(minutes=12), now=_NOW) == "in 12m"
    assert _format_relative_time(_NOW + timedelta(minutes=59), now=_NOW) == "in 59m"


def test_format_hours_past_and_future():
    assert _format_relative_time(_NOW - timedelta(hours=3), now=_NOW) == "3.0h ago"
    assert _format_relative_time(_NOW - timedelta(hours=3, minutes=12),
                                 now=_NOW) == "3.2h ago"
    assert _format_relative_time(_NOW + timedelta(hours=10, minutes=6),
                                 now=_NOW) == "in 10.1h"
    assert _format_relative_time(_NOW + timedelta(hours=23, minutes=59),
                                 now=_NOW) == "in 24.0h"


def test_format_days_under_a_week_keeps_one_decimal():
    """1d–6d range: one-decimal day form, both directions."""
    assert _format_relative_time(
        _NOW - timedelta(days=1, hours=6), now=_NOW,
    ) == "1.2d ago"
    assert _format_relative_time(
        _NOW + timedelta(days=3, hours=14), now=_NOW,
    ) == "in 3.6d"


def test_format_days_past_a_week_drops_decimal():
    """7d+ range: integer day form. Decimal would be noise."""
    assert _format_relative_time(
        _NOW - timedelta(days=7, hours=3), now=_NOW,
    ) == "7d ago"
    assert _format_relative_time(
        _NOW + timedelta(days=14, hours=20), now=_NOW,
    ) == "in 14d"


def test_format_beyond_30d_renders_full_local_datetime():
    """≥ 30d either side: relative form stops being useful, switch
    to an absolute datetime."""
    target = _NOW + timedelta(days=45)
    out = _format_relative_time(target, now=_NOW)
    import re
    assert re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", out)
    assert "ago" not in out
    assert "in " not in out


def test_format_handles_naive_datetime():
    """Doctor sometimes hands a naive datetime (e.g. parsed from SQLite
    UTC ms). Treat as UTC so we don't crash on a tz-aware - tz-naive
    subtraction."""
    naive = datetime(2026, 4, 27, 11, 30)   # 30 minutes before _NOW
    out = _format_relative_time(naive, now=_NOW)
    assert out == "30m ago"
