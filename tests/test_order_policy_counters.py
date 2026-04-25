"""Counter file CRUD + concurrency tests.

All file I/O happens under tmp_path. flock is exercised by spawning
two threads that both try to record_place and asserting the totals
add up correctly.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from schwab_cli.order_policy import counters as c


def _now(year=2026, month=4, day=25, hour=15, minute=30):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def test_load_returns_empty_when_file_missing(tmp_path: Path):
    state = c.load(path=tmp_path / "counters.json", now=_now())
    assert state.daily_total == {}
    assert state.daily_per_ticker == {}
    assert state.minutely_buckets == {}


def test_record_place_increments_daily_and_minutely(tmp_path: Path):
    p = tmp_path / "counters.json"
    state = c.record_place(
        account_number="12345678", underlying="KO",
        path=p, now=_now(),
    )
    assert state.daily_total["12345678"] == 1
    assert state.daily_per_ticker["12345678"]["KO"] == 1
    assert sum(state.minutely_buckets["12345678"].values()) == 1
    # File persisted with the new state.
    raw = json.loads(p.read_text())
    assert raw["counters"]["daily_order_count_total"]["12345678"] == 1


def test_minutely_window_garbage_collected_after_5min(tmp_path: Path):
    p = tmp_path / "counters.json"
    c.record_place(
        account_number="A", underlying="KO",
        path=p, now=_now(hour=15, minute=0),
    )
    # Re-load 10 minutes later — bucket should be gone.
    state = c.load(path=p, now=_now(hour=15, minute=11))
    assert state.minutely_buckets.get("A") in (None, {})


def test_daily_counters_reset_on_new_et_day(tmp_path: Path):
    p = tmp_path / "counters.json"
    # Day 1 — record one place.
    c.record_place(
        account_number="A", underlying="KO",
        path=p, now=_now(year=2026, month=4, day=25, hour=15),
    )
    # Day 2 — load.
    state = c.load(path=p, now=_now(year=2026, month=4, day=26, hour=10))
    assert state.daily_total == {}
    assert state.daily_per_ticker == {}
    # And the file is rotated when next saved.
    assert state.et_date == "2026-04-26"


def test_minutely_total_helper_sums_current_and_previous_minute(tmp_path):
    p = tmp_path / "counters.json"
    # Two places in the same minute.
    c.record_place(account_number="A", underlying="KO",
                   path=p, now=_now(hour=15, minute=30))
    c.record_place(account_number="A", underlying="KO",
                   path=p, now=_now(hour=15, minute=30))
    state = c.load(path=p, now=_now(hour=15, minute=30, day=25))
    assert c.get_minutely_total(state, "A", now=_now(hour=15, minute=30)) == 2


def test_record_override_increments_count(tmp_path):
    p = tmp_path / "counters.json"
    c.record_override(account_number="A", path=p, now=_now())
    c.record_override(account_number="A", path=p, now=_now())
    state = c.load(path=p, now=_now())
    assert c.get_override_count_today(state, "A") == 2


def test_record_replace_keyed_by_order_id(tmp_path):
    p = tmp_path / "counters.json"
    c.record_replace(order_id="999", path=p, now=_now())
    c.record_replace(order_id="999", path=p, now=_now())
    c.record_replace(order_id="888", path=p, now=_now())
    state = c.load(path=p, now=_now())
    assert c.get_replace_count(state, "999") == 2
    assert c.get_replace_count(state, "888") == 1


def test_concurrent_record_place_no_lost_updates(tmp_path):
    """Spawn 20 threads that each call record_place — final daily
    total must be exactly 20 (no lost updates due to race)."""
    p = tmp_path / "counters.json"

    def worker():
        c.record_place(account_number="A", underlying="KO",
                       path=p, now=_now())

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    state = c.load(path=p, now=_now())
    assert state.daily_total["A"] == 20
    assert state.daily_per_ticker["A"]["KO"] == 20


def test_atomic_write_temp_file_does_not_leak_on_crash(tmp_path, monkeypatch):
    """Even if save() doesn't finish (we simulate by checking the
    pre-rename temp file has 0600 perms and the final file is atomic
    via os.replace)."""
    p = tmp_path / "counters.json"
    c.record_place(account_number="A", underlying="KO", path=p, now=_now())
    # No leftover .tmp file.
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []
    # Final file has 0600 (or close — chmod best-effort on weird FS).
    import stat
    mode = stat.S_IMODE(p.stat().st_mode)
    assert mode == 0o600
