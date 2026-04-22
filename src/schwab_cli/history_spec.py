from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

_NY = ZoneInfo("America/New_York")
_UTC = timezone.utc


# ---------------------------------------------------------------------------
# Intervals
# ---------------------------------------------------------------------------

_INTERVALS: dict[str, tuple[Literal["minute", "daily", "weekly", "monthly"], int]] = {
    "1min":  ("minute", 1),
    "5min":  ("minute", 5),
    "10min": ("minute", 10),
    "15min": ("minute", 15),
    "30min": ("minute", 30),
    "1day":  ("daily", 1),
    "1wk":   ("weekly", 1),
    "1mo":   ("monthly", 1),
}

_INTERVAL_LIST = "1min, 5min, 10min, 15min, 30min, 1day, 1wk, 1mo"


@dataclass(frozen=True)
class Interval:
    frequency_type: Literal["minute", "daily", "weekly", "monthly"]
    frequency: int
    label: str


class IntervalSpecError(ValueError):
    """Raised when --interval is not one of the allowed values."""


class RangeSpecError(ValueError):
    """Raised when --range can't be parsed or is semantically invalid.

    `kind` discriminator lets the command layer set the right exit code:
      - "invalid"   → bad grammar              (exit 2)
      - "ordering"  → start >= end             (exit 1)
      - "future"    → start is in the future   (exit 1)
    """

    def __init__(self, message: str, *, kind: str = "invalid") -> None:
        super().__init__(message)
        self.kind = kind


def parse_interval(s: str) -> Interval:
    entry = _INTERVALS.get(s)
    if entry is None:
        raise IntervalSpecError(
            f"--interval must be one of: {_INTERVAL_LIST}"
        )
    freq_type, freq = entry
    return Interval(frequency_type=freq_type, frequency=freq, label=s)


# ---------------------------------------------------------------------------
# Ranges
# ---------------------------------------------------------------------------

_FIXED_RE = re.compile(r"^\d{8}$")
_RELATIVE_RE = re.compile(r"^-(\d+)(d|w|mo|y)$")


def _shift_months(dt: datetime, months: int) -> datetime:
    """Subtract `months` from `dt`, clamping day to the target month's length."""
    total = dt.month - 1 - months
    year = dt.year + total // 12
    month = total % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    day = min(dt.day, last_day)
    return dt.replace(year=year, month=month, day=day)


def _shift_years(dt: datetime, years: int) -> datetime:
    """Subtract `years` from `dt`, clamping Feb 29 → Feb 28 in non-leap years."""
    year = dt.year - years
    last_day = calendar.monthrange(year, dt.month)[1]
    day = min(dt.day, last_day)
    return dt.replace(year=year, day=day)


def _resolve_endpoint(token: str, *, now_ny: datetime) -> datetime:
    """Return a NY-timezone datetime for a start/end token.

    `token` is one of: YYYYMMDD, -Nu (u in d/w/mo/y), or 'now'.
    Does NOT apply the start/end-of-day snap for fixed dates — caller handles that.
    For fixed-date tokens returns the midnight-NY datetime for the given day.
    """
    if token == "now":
        return now_ny

    if _FIXED_RE.match(token):
        year = int(token[0:4])
        month = int(token[4:6])
        day = int(token[6:8])
        try:
            return datetime(year, month, day, 0, 0, 0, tzinfo=_NY)
        except ValueError as e:
            raise RangeSpecError(
                f"invalid endpoint {token!r}: {e}",
                kind="invalid",
            ) from e

    m = _RELATIVE_RE.match(token)
    if m:
        n = int(m.group(1))
        if n < 1:
            raise RangeSpecError(
                f"invalid endpoint {token!r}: N must be >= 1",
                kind="invalid",
            )
        unit = m.group(2)
        if unit == "d":
            return now_ny - timedelta(days=n)
        if unit == "w":
            return now_ny - timedelta(weeks=n)
        if unit == "mo":
            return _shift_months(now_ny, n)
        if unit == "y":
            return _shift_years(now_ny, n)

    raise RangeSpecError(
        f"invalid endpoint {token!r}: expected YYYYMMDD, -Nu (u in d/w/mo/y), or 'now'",
        kind="invalid",
    )


def _shortcut(token: str, *, now_ny: datetime) -> tuple[datetime, datetime] | None:
    if token == "ytd":
        start = datetime(now_ny.year, 1, 1, 0, 0, 0, tzinfo=_NY)
        return start, now_ny
    if token == "mtd":
        start = datetime(now_ny.year, now_ny.month, 1, 0, 0, 0, tzinfo=_NY)
        return start, now_ny
    if token == "wtd":
        # ISO week: Monday is weekday() == 0.
        monday = now_ny - timedelta(days=now_ny.weekday())
        start = datetime(monday.year, monday.month, monday.day, 0, 0, 0, tzinfo=_NY)
        return start, now_ny
    return None


def parse_range(
    s: str, *, now: datetime | None = None
) -> tuple[datetime, datetime]:
    """Return (start_utc, end_utc). Tz-aware datetimes in UTC.

    `now` is injectable for deterministic tests; defaults to datetime.now(tz=UTC).
    Calendar interpretation anchors to America/New_York (e.g. "ytd" means
    Jan 1 of the year in NY time, not UTC year boundary).
    """
    if not s:
        raise RangeSpecError(
            "--range must be '<start>..<end>' or one of: ytd, mtd, wtd",
            kind="invalid",
        )

    if now is None:
        now_utc = datetime.now(tz=_UTC)
    elif now.tzinfo is None:
        raise RangeSpecError("'now' must be tz-aware", kind="invalid")
    else:
        now_utc = now.astimezone(_UTC)
    now_ny = now_utc.astimezone(_NY)

    shortcut = _shortcut(s, now_ny=now_ny)
    if shortcut is not None:
        start_ny, end_ny = shortcut
    else:
        parts = s.split("..")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise RangeSpecError(
                "--range must be '<start>..<end>' or one of: ytd, mtd, wtd",
                kind="invalid",
            )
        start_token, end_token = parts
        start_ny = _resolve_endpoint(start_token, now_ny=now_ny)
        end_ny = _resolve_endpoint(end_token, now_ny=now_ny)

        # Snap fixed-date tokens:
        #   start YYYYMMDD → 00:00:00 NY (already set by _resolve_endpoint)
        #   end   YYYYMMDD → 23:59:59 NY
        if _FIXED_RE.match(end_token):
            end_ny = end_ny.replace(hour=23, minute=59, second=59)

    if start_ny >= end_ny:
        raise RangeSpecError("range start must be before end", kind="ordering")
    if start_ny > now_ny:
        raise RangeSpecError("range start is in the future", kind="future")

    return start_ny.astimezone(_UTC), end_ny.astimezone(_UTC)
