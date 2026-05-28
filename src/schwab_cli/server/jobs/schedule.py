"""Cron scheduling helpers.

Computes the next fire time for a cron expression interpreted in a job's
wall-clock timezone, returning a tz-aware UTC datetime.
"""
from __future__ import annotations

import zoneinfo
from datetime import datetime, timezone

from croniter import croniter


def next_run_after(cron: str, timezone_name: str, after: datetime) -> datetime:
    """Return the next cron fire strictly after ``after`` as a UTC datetime.

    The cron expression is interpreted in ``timezone_name`` wall-clock time. ``after`` must
    be tz-aware; it is converted into the job timezone, used as the croniter
    base, and the resulting next instant is converted back to UTC.

    DST policy: croniter advances to the next valid instant for non-existent
    (spring-forward) wall times and does not stick or duplicate on fall-back.
    Because each call asks for the value strictly greater than the base, feeding
    results back as ``after`` yields a strictly-increasing sequence with no
    stuck instants across either DST transition.
    """
    if after.tzinfo is None:
        raise ValueError("after must be a tz-aware datetime; got naive datetime")
    zone = zoneinfo.ZoneInfo(timezone_name)
    local_after = after.astimezone(zone)
    itr = croniter(cron, local_after)
    local_next: datetime = itr.get_next(datetime)
    return local_next.astimezone(timezone.utc)
