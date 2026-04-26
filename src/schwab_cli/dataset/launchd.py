"""Crontab string → launchd plist generators.

We only support the standard 5-field grammar with literal integers
or ``*``. No steps (``*/15``), no ranges (``9-17``), no name lists
(``MON,FRI``), no named shorthand (``@daily``). The error is
explicit so the user knows to rewrite their crontab into the simple
form rather than wonder why their job didn't fire.
"""
from __future__ import annotations

from typing import Any


_FIELD_RANGES = [
    ("minute",   0, 59),
    ("hour",     0, 23),
    ("day",      1, 31),
    ("month",    1, 12),
    ("weekday",  0, 6),
]

_FIELD_TO_LAUNCHD_KEY = {
    "minute":  "Minute",
    "hour":    "Hour",
    "day":     "Day",
    "month":   "Month",
    "weekday": "Weekday",
}


def crontab_to_calendar_interval(expr: str) -> list[dict[str, int]]:
    """Translate a 5-field crontab to launchd StartCalendarInterval.

    Returns a list of dicts (one entry — launchd accepts arrays for
    multi-time triggers, but we only emit one). Literal ``*`` becomes
    "match every value", which in launchd is achieved by *omitting*
    the key. So ``"0 22 * * *"`` → ``[{"Hour": 22, "Minute": 0}]``.
    """
    stripped = expr.strip()
    if stripped.startswith("@"):
        raise ValueError(
            f"crontab expression {stripped!r}: cannot translate "
            f"named shorthand (@daily, @weekly, …) into launchd StartCalendarInterval"
        )
    fields = stripped.split()
    if len(fields) != 5:
        raise ValueError(
            f"crontab expression must have 5 fields, got {len(fields)}: "
            f"{expr!r}"
        )
    out: dict[str, int] = {}
    for value, (name, lo, hi) in zip(fields, _FIELD_RANGES):
        if value == "*":
            continue
        if any(c in value for c in "/-,"):
            raise ValueError(
                f"crontab field {name}={value!r}: cannot translate "
                f"steps/ranges/lists into launchd StartCalendarInterval"
            )
        try:
            n = int(value)
        except ValueError:
            raise ValueError(
                f"crontab field {name}={value!r}: cannot translate "
                f"named shorthand into launchd"
            )
        if n < lo or n > hi:
            raise ValueError(
                f"crontab field {name}={n} out of range [{lo}, {hi}]"
            )
        out[_FIELD_TO_LAUNCHD_KEY[name]] = n
    return [out]
