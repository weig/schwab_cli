"""Persisted order counters — daily / minutely / per-order.

File: ``~/.config/schwab_cli/order_counters.json`` (override via
``SCHWAB_CLI_COUNTERS_FILE``). Concurrency protected by
``fcntl.flock`` (advisory) so concurrent CLI invocations and the
MCP daemon don't corrupt each other. All writes are atomic via
temp-file + ``os.replace``.

Counter rules (per spec §13):

* ``daily_order_count_total`` — total today on the account.
* ``daily_order_count_per_ticker`` — today per underlying.
* ``minutely_buckets`` — orders in the last 5 minutes (1-minute
  resolution); rolling window, garbage collected on read.
* ``replace_count_per_order`` — replace count per Schwab order id;
  Phase 3 increments these. Retained 24h after the order is in a
  terminal state (we don't currently track terminal status here,
  so we just keep them indefinitely; cleanup is conservative).
* ``daily_*`` counters reset at 00:00 ET on first read after the
  day rolls.

Public API:

* :func:`load` — read + auto-rotate state, returning :class:`Counters`.
* :func:`record_place` — increment daily + per-ticker + minutely on
  a successful place.
* :func:`record_override` — bump per-day override count for the
  per-profile ``override_max_per_day`` cap (Phase 2e).
* :func:`record_replace` — bump replace count for a Schwab order id
  (Phase 3 placeholder; safe to call earlier).
"""

from __future__ import annotations

import contextlib
import fcntl
import threading
import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")
_UTC = timezone.utc

DEFAULT_COUNTERS_FILE = (
    Path.home() / ".config" / "schwab_cli" / "order_counters.json"
)


# ---- in-memory shape -----------------------------------------------------


@dataclass
class Counters:
    """In-memory view of the counter file. Mutate in place; call
    :func:`save` to persist."""

    et_date: str                      # ISO date — the ET day we're tracking
    daily_total: dict[str, int] = field(default_factory=dict)             # acct → N
    daily_per_ticker: dict[str, dict[str, int]] = field(default_factory=dict)
    minutely_buckets: dict[str, dict[str, int]] = field(default_factory=dict)
    replace_count_per_order: dict[str, int] = field(default_factory=dict)
    override_count_per_day: dict[str, int] = field(default_factory=dict)  # acct → N

    def to_json(self) -> dict[str, Any]:
        return {
            "date": self.et_date,
            "tz": "America/New_York",
            "counters": {
                "daily_order_count_total": self.daily_total,
                "daily_order_count_per_ticker": self.daily_per_ticker,
                "minutely_buckets": self.minutely_buckets,
                "replace_count_per_order": self.replace_count_per_order,
                "override_count_per_day": self.override_count_per_day,
            },
        }


def _empty(today_et: date) -> Counters:
    return Counters(et_date=today_et.isoformat())


def _from_json(data: dict[str, Any], *, today_et: date) -> Counters:
    """Parse a counters file. If the file's ``date`` doesn't match
    today (ET), zero out the daily-resetting counters but keep the
    rolling minutely buckets and replace count."""
    cur_date = data.get("date")
    counters = data.get("counters") or {}
    daily_total = dict(counters.get("daily_order_count_total") or {})
    daily_per_ticker = {
        k: dict(v or {})
        for k, v in (counters.get("daily_order_count_per_ticker") or {}).items()
    }
    minutely = {
        k: dict(v or {})
        for k, v in (counters.get("minutely_buckets") or {}).items()
    }
    replace_count = dict(counters.get("replace_count_per_order") or {})
    override_count = dict(counters.get("override_count_per_day") or {})

    if cur_date != today_et.isoformat():
        # Rotate: clear daily fields, keep minutely + replace_count.
        daily_total = {}
        daily_per_ticker = {}
        override_count = {}

    return Counters(
        et_date=today_et.isoformat(),
        daily_total=daily_total,
        daily_per_ticker=daily_per_ticker,
        minutely_buckets=minutely,
        replace_count_per_order=replace_count,
        override_count_per_day=override_count,
    )


# ---- file I/O ------------------------------------------------------------


def counters_file_path() -> Path:
    env = os.environ.get("SCHWAB_CLI_COUNTERS_FILE")
    if env:
        return Path(env).expanduser()
    return DEFAULT_COUNTERS_FILE


def load(
    *,
    path: Path | None = None,
    now: datetime | None = None,
) -> Counters:
    """Read the counter file (creating one if missing), apply rotation
    + minutely-window GC, and return the in-memory :class:`Counters`.

    Caller mutates and then calls :func:`save` to persist; or use
    :func:`record_place` / similar which load+mutate+save in one
    locked transaction.
    """
    fp = path or counters_file_path()
    now_utc = now or datetime.now(tz=_UTC)
    today_et = now_utc.astimezone(_ET).date()

    if not fp.exists():
        return _empty(today_et)
    try:
        with open(fp, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return _empty(today_et)
    counters = _from_json(raw, today_et=today_et)
    _gc_minutely(counters, now_utc=now_utc)
    return counters


def save(counters: Counters, *, path: Path | None = None) -> None:
    """Persist the counters object atomically (temp + os.replace)."""
    fp = path or counters_file_path()
    fp.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(fp.parent, 0o700)
    except OSError:
        pass
    tmp = fp.with_suffix(fp.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(counters.to_json(), f, indent=2, sort_keys=True)
    os.replace(tmp, fp)
    try:
        os.chmod(fp, 0o600)
    except OSError:
        pass


# In-process serialization: fcntl.flock is per file-description, so two
# threads in ONE process (concurrent REST placements in the daemon) can
# both hold the "exclusive" lock. The thread lock closes that gap; flock
# keeps cross-process (CLI vs daemon) access serialized.
_THREAD_LOCK = threading.Lock()


@contextlib.contextmanager
def _locked(path: Path):
    """File-locked context — opens (or creates) ``path`` with an
    exclusive ``fcntl.flock``. The lock auto-releases when the
    context exits even on exception."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_suffix(path.suffix + ".lock")
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    _THREAD_LOCK.acquire()
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            _THREAD_LOCK.release()
            os.close(fd)


# ---- minutely-window GC --------------------------------------------------


def _gc_minutely(counters: Counters, *, now_utc: datetime) -> None:
    """Drop minutely buckets older than 5 minutes (in UTC). Each bucket
    key is the minute floor formatted as ISO-8601 to-minute, e.g.
    ``2026-04-25T13:32``."""
    cutoff = now_utc - timedelta(minutes=5)
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M")
    for acct in list(counters.minutely_buckets.keys()):
        buckets = counters.minutely_buckets[acct]
        for ts in list(buckets.keys()):
            if ts < cutoff_str:
                del buckets[ts]
        if not buckets:
            del counters.minutely_buckets[acct]


# ---- public increment APIs ------------------------------------------------


def record_place(
    *,
    account_number: str,
    underlying: str | None,
    path: Path | None = None,
    now: datetime | None = None,
) -> Counters:
    """Increment daily + per-ticker + minutely on a successful place.

    Returns the updated counters (so callers can audit the new
    values without re-reading)."""
    fp = path or counters_file_path()
    with _locked(fp):
        c = load(path=fp, now=now)
        now_utc = now or datetime.now(tz=_UTC)
        # Daily total.
        c.daily_total[account_number] = c.daily_total.get(account_number, 0) + 1
        # Per-ticker.
        if underlying:
            sub = c.daily_per_ticker.setdefault(account_number, {})
            sub[underlying] = sub.get(underlying, 0) + 1
        # Minutely.
        bucket_key = now_utc.strftime("%Y-%m-%dT%H:%M")
        sub = c.minutely_buckets.setdefault(account_number, {})
        sub[bucket_key] = sub.get(bucket_key, 0) + 1
        save(c, path=fp)
        return c


def record_override(
    *,
    account_number: str,
    path: Path | None = None,
    now: datetime | None = None,
) -> Counters:
    """Increment override count for the day. Used by Phase 2e to
    enforce ``override_max_per_day``."""
    fp = path or counters_file_path()
    with _locked(fp):
        c = load(path=fp, now=now)
        c.override_count_per_day[account_number] = (
            c.override_count_per_day.get(account_number, 0) + 1
        )
        save(c, path=fp)
        return c


def record_replace(
    *,
    order_id: str,
    path: Path | None = None,
    now: datetime | None = None,
) -> Counters:
    """Increment replace count for a Schwab order id. Phase 3 calls
    this on each ``order replace``."""
    fp = path or counters_file_path()
    with _locked(fp):
        c = load(path=fp, now=now)
        c.replace_count_per_order[order_id] = (
            c.replace_count_per_order.get(order_id, 0) + 1
        )
        save(c, path=fp)
        return c


# ---- read-only field helpers (used by the field provider) ---------------


def get_daily_total(counters: Counters, account_number: str) -> int:
    return counters.daily_total.get(account_number, 0)


def get_daily_per_ticker(
    counters: Counters, account_number: str, underlying: str,
) -> int:
    return (counters.daily_per_ticker.get(account_number) or {}).get(underlying, 0)


def get_minutely_total(
    counters: Counters, account_number: str, *, now: datetime | None = None,
) -> int:
    """Sum of orders placed in the last 60 seconds (minute-resolution).

    The current minute and the previous minute are both included so
    "the last 60 seconds" is roughly accurate at any wall-clock instant.
    """
    now_utc = now or datetime.now(tz=_UTC)
    keys = [
        now_utc.strftime("%Y-%m-%dT%H:%M"),
        (now_utc - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M"),
    ]
    sub = counters.minutely_buckets.get(account_number) or {}
    return sum(sub.get(k, 0) for k in keys)


def get_override_count_today(
    counters: Counters, account_number: str,
) -> int:
    return counters.override_count_per_day.get(account_number, 0)


def get_replace_count(counters: Counters, order_id: str) -> int:
    return counters.replace_count_per_order.get(order_id, 0)
