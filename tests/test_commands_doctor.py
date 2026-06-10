"""doctor command — pure helpers (relative-time formatter).

The end-to-end doctor flow touches HTTP / launchctl / SQLite and is
exercised live; these unit tests just lock in the time-formatting
contract.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from schwab_cli.commands import doctor as doc
from schwab_cli.commands.doctor import (
    _format_ohlcv_day,
    _format_relative_time,
    _health_ok,
)


_NOW = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
_NY = ZoneInfo("America/New_York")


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


# ---- _format_ohlcv_day -------------------------------------------------

_NY_NOW = datetime(2026, 5, 28, 9, 0, tzinfo=_NY)  # NY date = 2026-05-28


def test_ohlcv_day_none_returns_dash():
    assert _format_ohlcv_day(None, now=_NY_NOW) == "—"
    assert _format_ohlcv_day("", now=_NY_NOW) == "—"


def test_ohlcv_day_today():
    out = _format_ohlcv_day("2026-05-28", now=_NY_NOW)
    assert out == "latest day 2026-05-28 (today)"


def test_ohlcv_day_one_day_ago():
    out = _format_ohlcv_day("2026-05-27", now=_NY_NOW)
    assert out == "latest day 2026-05-27 (1 day ago)"


def test_ohlcv_day_n_days_ago():
    out = _format_ohlcv_day("2026-05-24", now=_NY_NOW)
    assert out == "latest day 2026-05-24 (4 days ago)"


def test_ohlcv_day_future_clamps_to_today():
    """A day ahead of the NY date (shouldn't happen) renders as today."""
    out = _format_ohlcv_day("2026-05-29", now=_NY_NOW)
    assert out == "latest day 2026-05-29 (today)"


def test_ohlcv_day_malformed_renders_raw():
    out = _format_ohlcv_day("not-a-date", now=_NY_NOW)
    assert out == "latest day not-a-date"


# ---- _print_data_freshness OHLCV uses latest day -----------------------


def test_print_data_freshness_renders_ohlcv_latest_day(monkeypatch, capsys):
    """doctor's freshness block renders OHLCV by latest trading day, while
    Volatility/Account keep the relative last-write rendering."""
    import contextlib

    from schwab_cli.commands import doctor as doctor_mod
    from schwab_cli.dataset import store
    from schwab_cli.dataset.store import DatasetFreshness

    @contextlib.contextmanager
    def _fake_connect():
        yield object()

    monkeypatch.setattr(
        "schwab_cli.storage.vol_history.connect", _fake_connect
    )
    monkeypatch.setattr(
        store,
        "read_dataset_freshness",
        lambda conn: DatasetFreshness(
            ohlcv_ms=1,
            volatility_ms=None,
            account_ms=None,
            ohlcv_latest_day="2026-05-28",
        ),
    )

    doctor_mod._print_data_freshness()
    out = capsys.readouterr().out
    assert "OHLCV" in out
    assert "latest day 2026-05-28" in out
    # The OHLCV line must NOT use the write-time "last write" phrasing.
    ohlcv_line = next(line for line in out.splitlines() if "OHLCV" in line)
    assert "last write" not in ohlcv_line


# ---- _health_ok (Server section liveness probe) -----------------------


class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def test_health_ok_true_when_ok_payload(monkeypatch):
    monkeypatch.setattr(doc.httpx, "get", lambda *a, **k: _FakeResp(200, {"ok": True}))
    assert _health_ok() is True


def test_health_ok_false_on_not_ok_payload(monkeypatch):
    monkeypatch.setattr(doc.httpx, "get", lambda *a, **k: _FakeResp(200, {"ok": False}))
    assert _health_ok() is False


def test_health_ok_false_on_non_200(monkeypatch):
    monkeypatch.setattr(doc.httpx, "get", lambda *a, **k: _FakeResp(503, {}))
    assert _health_ok() is False


def test_health_ok_false_on_connection_error(monkeypatch):
    def _boom(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(doc.httpx, "get", _boom)
    assert _health_ok() is False


# ---- _access_token_state ----------------------------------------------


def test_access_token_state_valid_shows_remaining_and_wallclock():
    from schwab_cli.commands.doctor import _access_token_state

    now = 1_781_070_000
    expires = now + 12 * 60 + 30  # 12.5 minutes left
    out = _access_token_state(expires, now=now)
    assert out.startswith("valid for 12m (until ")
    assert out.endswith(")")
    # wall-clock formatted as HH:MM:SS local time
    import re
    assert re.search(r"until \d{2}:\d{2}:\d{2}\)", out)


def test_access_token_state_expired_points_at_daemon():
    from schwab_cli.commands.doctor import _access_token_state

    now = 1_781_070_000
    out = _access_token_state(now - 60, now=now)
    assert out.startswith("expired at ")
    # Phase 3: the CLI no longer self-refreshes — the daemon does.
    assert "daemon" in out
