"""doctor command — pure helpers (formatter + plist next-run).

The end-to-end doctor flow touches HTTP / launchctl / SQLite and is
exercised live; these unit tests just lock in the time-formatting
contract and the StartCalendarInterval next-fire calculator.
"""
from __future__ import annotations

import plistlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from schwab_cli.commands.doctor import (
    _format_relative_time,
    _next_calendar_interval_run,
)


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


# ---- _next_calendar_interval_run -------------------------------------


def _write_plist(tmp_path: Path, calendar: list[dict]) -> Path:
    """Write a minimal plist with the given StartCalendarInterval."""
    p = tmp_path / "test.plist"
    body = {
        "Label": "com.test.cron",
        "ProgramArguments": ["/bin/true"],
        "StartCalendarInterval": calendar,
    }
    p.write_bytes(plistlib.dumps(body, fmt=plistlib.FMT_XML))
    return p


def test_next_run_daily_at_22_00(tmp_path):
    """Daily 22:00 — at noon local, next firing is today 22:00."""
    p = _write_plist(tmp_path, [{"Hour": 22, "Minute": 0}])
    now = datetime(2026, 4, 27, 12, 0).astimezone()
    nxt = _next_calendar_interval_run(p, now=now.astimezone(timezone.utc))
    assert nxt is not None
    local = nxt.astimezone()
    assert local.hour == 22
    assert local.minute == 0
    assert local.date() == now.date()


def test_next_run_daily_at_22_00_after_midnight(tmp_path):
    """At 23:30 local, next 22:00 is tomorrow, not today."""
    p = _write_plist(tmp_path, [{"Hour": 22, "Minute": 0}])
    now = datetime(2026, 4, 27, 23, 30).astimezone()
    nxt = _next_calendar_interval_run(p, now=now.astimezone(timezone.utc))
    assert nxt is not None
    local = nxt.astimezone()
    assert local.hour == 22
    assert local.date() == (now.date() + timedelta(days=1))


def test_next_run_weekly_sunday(tmp_path):
    """Sunday 06:00 — in launchd's convention Weekday=0 means Sunday."""
    p = _write_plist(tmp_path, [{"Hour": 6, "Minute": 0, "Weekday": 0}])
    # Tuesday 2026-04-28 12:00 UTC; next Sunday is 2026-05-03 06:00 local.
    now = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    nxt = _next_calendar_interval_run(p, now=now)
    assert nxt is not None
    local = nxt.astimezone()
    assert local.isoweekday() == 7   # Sunday in Python's iso convention
    assert local.hour == 6
    assert local.minute == 0


def test_next_run_returns_none_when_plist_missing(tmp_path):
    assert _next_calendar_interval_run(tmp_path / "absent.plist") is None


def test_next_run_returns_none_when_no_interval(tmp_path):
    """Plist with no StartCalendarInterval (e.g. the MCP daemon's
    KeepAlive plist) has no schedule to compute."""
    p = tmp_path / "keepalive.plist"
    p.write_bytes(plistlib.dumps({
        "Label": "com.test.daemon",
        "ProgramArguments": ["/bin/true"],
        "KeepAlive": True,
    }, fmt=plistlib.FMT_XML))
    assert _next_calendar_interval_run(p) is None


# ---- indices last-run audit-log parser -------------------------------


def test_parse_last_indices_run_extracts_deltas(monkeypatch, tmp_path):
    """The most recent ``[indices] finished`` block in the audit log
    is the authoritative source for last-run timestamp + per-index
    deltas. Stale prior runs are skipped."""
    from schwab_cli.commands import doctor as doc
    log = tmp_path / "scheduler.log"
    log.write_text(
        # Earlier run — should be ignored.
        "2026-05-10T22:00:00Z [indices] start\n"
        "2026-05-10T22:00:01Z [indices] SPX: total=500 +1 -0\n"
        "2026-05-10T22:00:01Z [indices] finished, 1 indices processed, 0 errored\n"
        # Latest run — what we should parse.
        "2026-05-18T22:00:04Z [indices] start\n"
        "2026-05-18T22:00:04Z [indices] DJI: total=30 +0 -0\n"
        "2026-05-18T22:00:04Z [indices] NQ: total=101 +1 -0\n"
        "2026-05-18T22:00:04Z [indices] SPX: total=502 +2 -2\n"
        "2026-05-18T22:00:04Z [indices] finished, 3 indices processed, 0 errored\n"
    )
    monkeypatch.setattr(
        "schwab_cli.dataset.audit_log.audit_log_path", lambda: log,
    )
    info = doc._parse_last_indices_run()
    assert info is not None
    assert info["finished_at"] == datetime(
        2026, 5, 18, 22, 0, 4, tzinfo=timezone.utc,
    )
    assert info["errored"] == 0
    assert info["deltas"] == [
        ("DJI", 0, 0, 30),
        ("NQ",  1, 0, 101),
        ("SPX", 2, 2, 502),
    ]


def test_parse_last_indices_run_returns_none_for_empty_log(
    monkeypatch, tmp_path,
):
    from schwab_cli.commands import doctor as doc
    log = tmp_path / "scheduler.log"
    log.write_text("")
    monkeypatch.setattr(
        "schwab_cli.dataset.audit_log.audit_log_path", lambda: log,
    )
    assert doc._parse_last_indices_run() is None


def test_parse_last_indices_run_returns_none_when_log_missing(
    monkeypatch, tmp_path,
):
    from schwab_cli.commands import doctor as doc
    monkeypatch.setattr(
        "schwab_cli.dataset.audit_log.audit_log_path",
        lambda: tmp_path / "absent.log",
    )
    assert doc._parse_last_indices_run() is None


def test_format_indices_deltas_collapses_no_change_to_zero():
    """``+0 -0`` is visual noise; render as ``0`` (dim)."""
    from schwab_cli.commands import doctor as doc
    out = doc._format_indices_deltas([
        ("DJI", 0, 0, 30),
        ("NQ",  1, 0, 101),
        ("SPX", 2, 2, 502),
    ])
    # Strip ANSI escapes for assertion.
    import re as _re
    plain = _re.sub(r"\x1b\[[0-9;]*m", "", out)
    assert plain == "[DJI: 0, NQ: +1, SPX: +2 -2]"
