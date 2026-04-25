"""Lightweight NYSE calendar — just what the policy temporal fields need.

Returns ``market_session`` ∈ {PRE, REGULAR, POST, CLOSED}, plus
``minutes_since_open`` / ``minutes_to_close`` during REGULAR, and
``is_holiday`` for full closures.

Holiday list is a small static table updated yearly. Half-days
(early closes) currently roll over to the regular CLOSED state once
the half-day's close passes; we don't model the 1pm close
explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time

# Static list of full NYSE closures. Keep small — extend as years
# tick over. Half-day closes (Black Friday, day before Christmas etc.)
# are not in this list because policies that care about them are rare.
_FULL_HOLIDAYS: set[date] = {
    # 2026
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # MLK Jr Day
    date(2026, 2, 16),   # Presidents Day
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day (observed)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
    # 2027
    date(2027, 1, 1),    # New Year's Day
    date(2027, 1, 18),   # MLK Jr Day
    date(2027, 2, 15),   # Presidents Day
    date(2027, 3, 26),   # Good Friday
    date(2027, 5, 31),   # Memorial Day
    date(2027, 6, 18),   # Juneteenth (observed)
    date(2027, 7, 5),    # Independence Day (observed)
    date(2027, 9, 6),    # Labor Day
    date(2027, 11, 25),  # Thanksgiving
    date(2027, 12, 24),  # Christmas (observed)
}


@dataclass(frozen=True)
class SessionStatus:
    session: str                    # "PRE" | "REGULAR" | "POST" | "CLOSED"
    is_holiday: bool
    minutes_since_open: int         # only meaningful in REGULAR; -1 otherwise
    minutes_to_close: int           # ditto


_PRE_OPEN = time(4, 0)              # 4:00 ET
_REGULAR_OPEN = time(9, 30)
_REGULAR_CLOSE = time(16, 0)
_POST_CLOSE = time(20, 0)


def session_status(now_et: datetime) -> SessionStatus:
    """Classify ``now_et`` into PRE/REGULAR/POST/CLOSED.

    ``now_et`` MUST be tz-aware in America/New_York. (We don't
    enforce, just rely on the caller — the field provider does.)
    """
    today = now_et.date()
    is_holiday = today in _FULL_HOLIDAYS or today.weekday() >= 5
    t = now_et.time()

    if is_holiday:
        return SessionStatus(
            session="CLOSED", is_holiday=is_holiday,
            minutes_since_open=-1, minutes_to_close=-1,
        )

    if _REGULAR_OPEN <= t < _REGULAR_CLOSE:
        # Compute minute counts.
        open_dt = datetime.combine(today, _REGULAR_OPEN, tzinfo=now_et.tzinfo)
        close_dt = datetime.combine(today, _REGULAR_CLOSE, tzinfo=now_et.tzinfo)
        return SessionStatus(
            session="REGULAR", is_holiday=False,
            minutes_since_open=int((now_et - open_dt).total_seconds() // 60),
            minutes_to_close=int((close_dt - now_et).total_seconds() // 60),
        )
    if _PRE_OPEN <= t < _REGULAR_OPEN:
        return SessionStatus(
            session="PRE", is_holiday=False,
            minutes_since_open=-1, minutes_to_close=-1,
        )
    if _REGULAR_CLOSE <= t < _POST_CLOSE:
        return SessionStatus(
            session="POST", is_holiday=False,
            minutes_since_open=-1, minutes_to_close=-1,
        )
    return SessionStatus(
        session="CLOSED", is_holiday=False,
        minutes_since_open=-1, minutes_to_close=-1,
    )
