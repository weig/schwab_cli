from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from schwab_cli.history_spec import (
    Interval,
    IntervalSpecError,
    RangeSpecError,
    parse_interval,
    parse_range,
)


_NY = ZoneInfo("America/New_York")
_UTC = timezone.utc
# Deterministic "now" used throughout range tests: 2024-04-22 14:30 NY (EDT, UTC-4).
_NOW_NY = datetime(2024, 4, 22, 14, 30, tzinfo=_NY)


# ---------------------------------------------------------------------------
# parse_interval
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("token,freq_type,freq", [
    ("1min",  "minute",  1),
    ("5min",  "minute",  5),
    ("10min", "minute",  10),
    ("15min", "minute",  15),
    ("30min", "minute",  30),
    ("1day",  "daily",   1),
    ("1wk",   "weekly",  1),
    ("1mo",   "monthly", 1),
])
def test_parse_interval_happy(token, freq_type, freq):
    iv = parse_interval(token)
    assert iv == Interval(frequency_type=freq_type, frequency=freq, label=token)


@pytest.mark.parametrize("bad", ["", "1m", "2min", "45min", "1hr", "1y", "daily", "garbage"])
def test_parse_interval_rejects(bad):
    with pytest.raises(IntervalSpecError):
        parse_interval(bad)


def test_parse_interval_error_message_lists_allowed():
    with pytest.raises(IntervalSpecError) as exc:
        parse_interval("1m")
    msg = str(exc.value)
    for t in ("1min", "5min", "10min", "15min", "30min", "1day", "1wk", "1mo"):
        assert t in msg


# ---------------------------------------------------------------------------
# parse_range — shortcuts
# ---------------------------------------------------------------------------

def test_parse_range_ytd():
    start, end = parse_range("ytd", now=_NOW_NY)
    assert start == datetime(2024, 1, 1, 0, 0, tzinfo=_NY).astimezone(_UTC)
    assert end == _NOW_NY.astimezone(_UTC)


def test_parse_range_mtd():
    start, end = parse_range("mtd", now=_NOW_NY)
    assert start == datetime(2024, 4, 1, 0, 0, tzinfo=_NY).astimezone(_UTC)
    assert end == _NOW_NY.astimezone(_UTC)


def test_parse_range_wtd_resolves_to_monday():
    # 2024-04-22 is a Monday → Monday of current ISO week is 2024-04-22 00:00 NY.
    start, end = parse_range("wtd", now=_NOW_NY)
    assert start == datetime(2024, 4, 22, 0, 0, tzinfo=_NY).astimezone(_UTC)
    assert end == _NOW_NY.astimezone(_UTC)


def test_parse_range_wtd_mid_week():
    # 2024-04-24 is a Wednesday → Monday is 2024-04-22.
    now = datetime(2024, 4, 24, 10, 0, tzinfo=_NY)
    start, _ = parse_range("wtd", now=now)
    assert start == datetime(2024, 4, 22, 0, 0, tzinfo=_NY).astimezone(_UTC)


def test_parse_range_wtd_sunday():
    # 2024-04-28 is a Sunday → Monday of its ISO week is 2024-04-22.
    now = datetime(2024, 4, 28, 10, 0, tzinfo=_NY)
    start, _ = parse_range("wtd", now=now)
    assert start == datetime(2024, 4, 22, 0, 0, tzinfo=_NY).astimezone(_UTC)


# ---------------------------------------------------------------------------
# parse_range — explicit forms
# ---------------------------------------------------------------------------

def test_parse_range_fixed_fixed():
    start, end = parse_range("20240101..20240630", now=_NOW_NY)
    assert start == datetime(2024, 1, 1, 0, 0, 0, tzinfo=_NY).astimezone(_UTC)
    assert end == datetime(2024, 6, 30, 23, 59, 59, tzinfo=_NY).astimezone(_UTC)


def test_parse_range_fixed_to_now():
    start, end = parse_range("20240101..now", now=_NOW_NY)
    assert start == datetime(2024, 1, 1, 0, 0, 0, tzinfo=_NY).astimezone(_UTC)
    assert end == _NOW_NY.astimezone(_UTC)


def test_parse_range_relative_to_now_days():
    start, end = parse_range("-7d..now", now=_NOW_NY)
    assert end == _NOW_NY.astimezone(_UTC)
    assert end - start == timedelta(days=7)


def test_parse_range_relative_weeks():
    start, end = parse_range("-2w..now", now=_NOW_NY)
    assert end - start == timedelta(weeks=2)


def test_parse_range_relative_months():
    # 2024-04-22 minus 3 months → 2024-01-22 (same clock time in NY).
    start, _ = parse_range("-3mo..now", now=_NOW_NY)
    assert start == datetime(2024, 1, 22, 14, 30, tzinfo=_NY).astimezone(_UTC)


def test_parse_range_relative_years():
    start, _ = parse_range("-1y..now", now=_NOW_NY)
    assert start == datetime(2023, 4, 22, 14, 30, tzinfo=_NY).astimezone(_UTC)


def test_parse_range_relative_months_clamps_day():
    # 2024-03-31 minus 1 month → 2024-02-29 (leap-year last day).
    now = datetime(2024, 3, 31, 12, 0, tzinfo=_NY)
    start, _ = parse_range("-1mo..now", now=now)
    assert start == datetime(2024, 2, 29, 12, 0, tzinfo=_NY).astimezone(_UTC)


def test_parse_range_relative_years_clamps_leap():
    # 2024-02-29 minus 1 year → 2023-02-28.
    now = datetime(2024, 2, 29, 9, 0, tzinfo=_NY)
    start, _ = parse_range("-1y..now", now=now)
    assert start == datetime(2023, 2, 28, 9, 0, tzinfo=_NY).astimezone(_UTC)


def test_parse_range_relative_relative():
    start, end = parse_range("-30d..-1d", now=_NOW_NY)
    assert _NOW_NY.astimezone(_UTC) - end == timedelta(days=1)
    assert end - start == timedelta(days=29)


def test_parse_range_fixed_relative():
    start, end = parse_range("20240101..-1d", now=_NOW_NY)
    assert start == datetime(2024, 1, 1, 0, 0, 0, tzinfo=_NY).astimezone(_UTC)
    assert _NOW_NY.astimezone(_UTC) - end == timedelta(days=1)


def test_parse_range_returns_utc():
    start, end = parse_range("ytd", now=_NOW_NY)
    assert start.tzinfo is _UTC or start.utcoffset() == timedelta(0)
    assert end.tzinfo is _UTC or end.utcoffset() == timedelta(0)


# ---------------------------------------------------------------------------
# parse_range — negative cases (grammar + semantics)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "",
    "20240101",                 # missing ".."
    "20240101..",               # empty endpoint
    "..20240101",               # empty endpoint
    "20240101..20240101..x",    # too many parts
    "notadate..now",
])
def test_parse_range_grammar_miss(bad):
    with pytest.raises(RangeSpecError) as exc:
        parse_range(bad, now=_NOW_NY)
    assert exc.value.kind == "invalid"


@pytest.mark.parametrize("bad_endpoint", ["-2x", "-d", "7d", "-0d", "-1xyz", "2024-01-01"])
def test_parse_range_bad_endpoint(bad_endpoint):
    with pytest.raises(RangeSpecError) as exc:
        parse_range(f"{bad_endpoint}..now", now=_NOW_NY)
    assert exc.value.kind == "invalid"


def test_parse_range_start_after_end_is_ordering():
    with pytest.raises(RangeSpecError) as exc:
        parse_range("20240601..20240101", now=_NOW_NY)
    assert exc.value.kind == "ordering"


def test_parse_range_start_equals_end_is_ordering():
    with pytest.raises(RangeSpecError) as exc:
        parse_range("-0d..now", now=_NOW_NY)
    # -0d is also rejected as invalid endpoint (N must be >=1), so either
    # ordering or invalid is acceptable — but the grammar forbids -0.
    assert exc.value.kind in {"ordering", "invalid"}


def test_parse_range_future_start_is_future():
    # Construct a fixed-date start strictly after "now".
    with pytest.raises(RangeSpecError) as exc:
        parse_range("20990101..20990102", now=_NOW_NY)
    assert exc.value.kind == "future"


def test_parse_range_error_messages_nonempty():
    with pytest.raises(RangeSpecError) as exc:
        parse_range("garbage", now=_NOW_NY)
    assert str(exc.value)


def test_parse_range_shortcut_keywords_listed_in_error():
    with pytest.raises(RangeSpecError) as exc:
        parse_range("xyz", now=_NOW_NY)
    msg = str(exc.value)
    assert "ytd" in msg and "mtd" in msg and "wtd" in msg
