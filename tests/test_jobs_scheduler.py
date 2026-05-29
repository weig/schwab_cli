"""TDD red-phase tests for schwab_cli.server.jobs.scheduler.

All imports are expected to fail (ModuleNotFoundError) until the module is
implemented.

Design principles
-----------------
* FakeClock — a settable float clock; injected as the ``now`` callable.
* FakeHandle — records terminate/kill calls and has a settable exit-code.
* fake_spawn — returns a new FakeHandle per call; records (cfg, log_path) pairs.
* Fake next_run — deterministically returns now + 300 seconds.
* All renew / notify callables are simple recording stubs.
* NO real sleeping, NO real subprocesses, NO real signals.

Run with:
    uv run --frozen --extra dev python -m pytest tests/test_jobs_scheduler.py -q
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pytest

from schwab_cli._exit_codes import EXIT_AUTH_FAILED
from schwab_cli.server.jobs.config import JobConfig
from schwab_cli.server.jobs.runner import JobHandle
from schwab_cli.server.jobs.scheduler import (
    MAX_CONCURRENT_JOBS,
    WATCHDOG_S,
    JobScheduler,
    Transition,
)
from schwab_cli.server.jobs.state import SchedulerState, load_state


# ---------------------------------------------------------------------------
# Fake infrastructure
# ---------------------------------------------------------------------------


class FakeClock:
    """Settable clock injected as the ``now`` callable."""

    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@dataclass
class FakeHandle:
    """Fake JobHandle with controllable exit code and call recording."""

    pid: int
    pgid: int
    _exit_code: int | None = field(default=None, repr=False)
    terminate_calls: int = field(default=0, repr=False)
    kill_calls: int = field(default=0, repr=False)

    def poll(self) -> int | None:
        return self._exit_code

    def set_exit_code(self, code: int | None) -> None:
        self._exit_code = code

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1


class FakeSpawner:
    """Records spawn calls and vends FakeHandles."""

    def __init__(self, start_pid: int = 10000) -> None:
        self._next_pid = start_pid
        self.calls: list[tuple[JobConfig, Path]] = []
        self.handles: list[FakeHandle] = []

    def __call__(self, cfg: JobConfig, log_path: Path) -> FakeHandle:
        pid = self._next_pid
        self._next_pid += 1
        handle = FakeHandle(pid=pid, pgid=pid)
        self.calls.append((cfg, log_path))
        self.handles.append(handle)
        return handle

    @property
    def last_handle(self) -> FakeHandle:
        return self.handles[-1]


def _stub_next_run(cron: str, tz: str, after: datetime) -> datetime:
    """Return a fixed point 300 s after ``after`` (epoch-based, UTC)."""
    epoch = after.timestamp() + 300
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def _make_cfg(
    job_id: str = "j1",
    enabled: bool = True,
    command: tuple[str, ...] = ("dataset", "update"),
    retries: int = 0,
    retry_delay_s: int = 120,
    timeout_s: int = 3600,
    cron: str = "*/5 * * * *",
    timezone: str = "UTC",
) -> JobConfig:
    return JobConfig(
        id=job_id,
        name=f"Job {job_id}",
        enabled=enabled,
        cron=cron,
        timezone=timezone,
        type="command",
        command=command,
        retries=retries,
        retry_delay_s=retry_delay_s,
        timeout_s=timeout_s,
    )


def _make_scheduler(
    *,
    current_dir: Path,
    jobs: list[JobConfig],
    clock: FakeClock | None = None,
    spawner: FakeSpawner | None = None,
    renew: Callable[[], None] | None = None,
    notify: Callable[..., None] | None = None,
    max_concurrent: int = MAX_CONCURRENT_JOBS,
    watchdog_s: float = WATCHDOG_S,
) -> JobScheduler:
    clock = clock or FakeClock()
    spawner = spawner or FakeSpawner()
    return JobScheduler(
        current_dir=current_dir,
        jobs=jobs,
        now=clock,
        spawn=spawner,
        next_run=_stub_next_run,
        renew=renew,
        notify=notify,
        watchdog_s=watchdog_s,
        max_concurrent=max_concurrent,
    )


# ---------------------------------------------------------------------------
# schedule_all
# ---------------------------------------------------------------------------


def test_schedule_all_sets_next_run_at_for_enabled_jobs(tmp_path):
    clock = FakeClock(t=1_000_000.0)
    cfg = _make_cfg("j1", enabled=True)
    sched = _make_scheduler(current_dir=tmp_path, jobs=[cfg], clock=clock)

    sched.schedule_all()

    state = sched.snapshot()
    assert state.jobs["j1"].next_run_at is not None
    assert state.jobs["j1"].next_run_at > clock.t


def test_schedule_all_disabled_jobs_get_no_next_run_at(tmp_path):
    clock = FakeClock(t=1_000_000.0)
    cfg = _make_cfg("j1", enabled=False)
    sched = _make_scheduler(current_dir=tmp_path, jobs=[cfg], clock=clock)

    sched.schedule_all()

    state = sched.snapshot()
    assert state.jobs["j1"].next_run_at is None


def test_schedule_all_sets_multiple_enabled_jobs(tmp_path):
    clock = FakeClock(t=1_000_000.0)
    jobs = [_make_cfg("j1"), _make_cfg("j2"), _make_cfg("j3", enabled=False)]
    sched = _make_scheduler(current_dir=tmp_path, jobs=jobs, clock=clock)

    sched.schedule_all()

    state = sched.snapshot()
    assert state.jobs["j1"].next_run_at is not None
    assert state.jobs["j2"].next_run_at is not None
    assert state.jobs["j3"].next_run_at is None


# ---------------------------------------------------------------------------
# fire_due
# ---------------------------------------------------------------------------


def test_fire_due_spawns_when_past_next_run_at(tmp_path):
    clock = FakeClock(t=1_000_000.0)
    spawner = FakeSpawner()
    cfg = _make_cfg("j1")
    sched = _make_scheduler(current_dir=tmp_path, jobs=[cfg], clock=clock, spawner=spawner)

    sched.schedule_all()
    # Advance clock past the scheduled next_run_at (now + 300)
    clock.advance(400)

    sched.fire_due()

    assert len(spawner.calls) == 1
    assert spawner.calls[0][0].id == "j1"


def test_fire_due_does_not_spawn_before_next_run_at(tmp_path):
    clock = FakeClock(t=1_000_000.0)
    spawner = FakeSpawner()
    cfg = _make_cfg("j1")
    sched = _make_scheduler(current_dir=tmp_path, jobs=[cfg], clock=clock, spawner=spawner)

    sched.schedule_all()
    # Do NOT advance the clock — it's still before next_run_at
    sched.fire_due()

    assert len(spawner.calls) == 0


def test_fire_due_records_running_pid_pgid_started_at(tmp_path):
    clock = FakeClock(t=1_000_000.0)
    spawner = FakeSpawner(start_pid=5555)
    cfg = _make_cfg("j1")
    sched = _make_scheduler(current_dir=tmp_path, jobs=[cfg], clock=clock, spawner=spawner)

    sched.schedule_all()
    clock.advance(400)
    sched.fire_due()

    state = sched.snapshot()
    j1 = state.jobs["j1"]
    assert j1.running_pid == 5555
    assert j1.running_pgid == 5555
    assert j1.started_at == pytest.approx(clock.t)


def test_fire_due_skip_already_running(tmp_path):
    """A job that is already running must not be spawned again."""
    clock = FakeClock(t=1_000_000.0)
    spawner = FakeSpawner()
    cfg = _make_cfg("j1")
    sched = _make_scheduler(current_dir=tmp_path, jobs=[cfg], clock=clock, spawner=spawner)

    sched.schedule_all()
    clock.advance(400)
    sched.fire_due()  # first spawn
    assert len(spawner.calls) == 1

    # Advance again — job is still running (poll returns None)
    clock.advance(400)
    sched.fire_due()  # must NOT spawn again

    assert len(spawner.calls) == 1


def test_fire_due_concurrency_cap(tmp_path):
    """With max_concurrent=2 and 3 due jobs, only 2 are spawned per pass."""
    clock = FakeClock(t=1_000_000.0)
    spawner = FakeSpawner()
    jobs = [_make_cfg("j1"), _make_cfg("j2"), _make_cfg("j3")]
    sched = _make_scheduler(
        current_dir=tmp_path, jobs=jobs, clock=clock, spawner=spawner, max_concurrent=2
    )

    sched.schedule_all()
    clock.advance(400)
    sched.fire_due()

    assert len(spawner.calls) == 2, (
        f"Expected 2 spawns with max_concurrent=2, got {len(spawner.calls)}"
    )


def test_fire_due_third_job_fires_after_one_finishes(tmp_path):
    """After one running job finishes, the 3rd due job gets spawned on the next pass."""
    clock = FakeClock(t=1_000_000.0)
    spawner = FakeSpawner()
    jobs = [_make_cfg("j1"), _make_cfg("j2"), _make_cfg("j3")]
    sched = _make_scheduler(
        current_dir=tmp_path, jobs=jobs, clock=clock, spawner=spawner, max_concurrent=2
    )

    sched.schedule_all()
    clock.advance(400)
    sched.fire_due()  # spawns j1, j2
    assert len(spawner.calls) == 2

    # Simulate j1 finishing
    spawner.handles[0].set_exit_code(0)
    sched.reap()

    # Now there is one free slot; j3 must fire
    clock.advance(1)
    sched.fire_due()

    assert len(spawner.calls) == 3, "j3 must have been spawned after j1 finished"


# ---------------------------------------------------------------------------
# reap — success
# ---------------------------------------------------------------------------


def test_reap_success_sets_ok_status(tmp_path):
    clock = FakeClock(t=1_000_000.0)
    spawner = FakeSpawner()
    cfg = _make_cfg("j1")
    sched = _make_scheduler(current_dir=tmp_path, jobs=[cfg], clock=clock, spawner=spawner)

    sched.schedule_all()
    clock.advance(400)
    sched.fire_due()

    # Mark as finished with exit 0
    spawner.last_handle.set_exit_code(0)
    sched.reap()

    state = sched.snapshot()
    j1 = state.jobs["j1"]
    assert j1.last_status == "ok"
    assert j1.last_exit_code == 0
    assert j1.running_pid is None


def test_reap_success_reschedules_job(tmp_path):
    """After a successful run, the job's next_run_at must be updated."""
    clock = FakeClock(t=1_000_000.0)
    spawner = FakeSpawner()
    cfg = _make_cfg("j1")
    sched = _make_scheduler(current_dir=tmp_path, jobs=[cfg], clock=clock, spawner=spawner)

    sched.schedule_all()
    first_next = sched.snapshot().jobs["j1"].next_run_at
    clock.advance(400)
    sched.fire_due()

    spawner.last_handle.set_exit_code(0)
    sched.reap()

    second_next = sched.snapshot().jobs["j1"].next_run_at
    assert second_next is not None
    assert second_next > first_next, "next_run_at must advance after successful reap"


# ---------------------------------------------------------------------------
# reap — generic failure
# ---------------------------------------------------------------------------


def test_reap_generic_failure_sets_failed_status(tmp_path):
    notify_calls: list[tuple] = []
    clock = FakeClock(t=1_000_000.0)
    spawner = FakeSpawner()
    cfg = _make_cfg("j1", retries=0)
    sched = _make_scheduler(
        current_dir=tmp_path,
        jobs=[cfg],
        clock=clock,
        spawner=spawner,
        notify=lambda *a, **kw: notify_calls.append((a, kw)),
    )

    sched.schedule_all()
    clock.advance(400)
    sched.fire_due()

    spawner.last_handle.set_exit_code(1)  # generic failure
    sched.reap()

    state = sched.snapshot()
    j1 = state.jobs["j1"]
    assert j1.last_status == "failed"
    assert j1.last_exit_code == 1
    assert j1.running_pid is None


def test_reap_generic_failure_calls_notify(tmp_path):
    notify_calls: list[tuple] = []
    clock = FakeClock(t=1_000_000.0)
    spawner = FakeSpawner()
    cfg = _make_cfg("j1", retries=0)
    sched = _make_scheduler(
        current_dir=tmp_path,
        jobs=[cfg],
        clock=clock,
        spawner=spawner,
        notify=lambda *a, **kw: notify_calls.append((a, kw)),
    )

    sched.schedule_all()
    clock.advance(400)
    sched.fire_due()
    spawner.last_handle.set_exit_code(1)
    sched.reap()

    assert notify_calls, "notify must be called on generic failure"


def test_reap_generic_failure_next_run_is_normal_not_immediate(tmp_path):
    """
    A generic failure must NOT retry immediately; next_run_at must be the
    normal next occurrence (~300 s out via _stub_next_run), not now+retry_delay.
    """
    clock = FakeClock(t=1_000_000.0)
    spawner = FakeSpawner()
    cfg = _make_cfg("j1", retries=0, retry_delay_s=60)
    sched = _make_scheduler(current_dir=tmp_path, jobs=[cfg], clock=clock, spawner=spawner)

    sched.schedule_all()
    clock.advance(400)
    sched.fire_due()
    spawner.last_handle.set_exit_code(1)
    sched.reap()

    state = sched.snapshot()
    j1 = state.jobs["j1"]
    # next_run_at must be roughly now+300 (stub), not now+60 (retry_delay_s)
    assert j1.next_run_at is not None
    assert j1.next_run_at > clock.t + 200, (
        "Generic failure must NOT use retry_delay_s for scheduling"
    )


# ---------------------------------------------------------------------------
# reap — auth failure / retry
# ---------------------------------------------------------------------------


def test_reap_auth_failure_first_retry_calls_renew_and_reschedules(tmp_path):
    """
    First auth failure with retries=1:
    - renew() must be called
    - next_run_at set to now + retry_delay_s
    """
    renew_calls: list[None] = []
    clock = FakeClock(t=1_000_000.0)
    spawner = FakeSpawner()
    cfg = _make_cfg("j1", retries=1, retry_delay_s=120)
    sched = _make_scheduler(
        current_dir=tmp_path,
        jobs=[cfg],
        clock=clock,
        spawner=spawner,
        renew=lambda: renew_calls.append(None),
    )

    sched.schedule_all()
    clock.advance(400)
    sched.fire_due()
    spawner.last_handle.set_exit_code(EXIT_AUTH_FAILED)
    sched.reap()

    assert renew_calls, "renew() must be called on first auth failure"

    state = sched.snapshot()
    j1 = state.jobs["j1"]
    assert j1.next_run_at == pytest.approx(clock.t + 120, abs=1), (
        "next_run_at must be now + retry_delay_s after first auth failure"
    )


def test_reap_auth_failure_second_no_retry_is_treated_as_failure(tmp_path):
    """
    Second auth failure with no retries left:
    - renew() is NOT called again
    - last_status is 'failed' (or 'auth-failed') — NOT retried
    - notify is called
    """
    notify_calls: list[tuple] = []
    renew_calls: list[None] = []
    clock = FakeClock(t=1_000_000.0)
    spawner = FakeSpawner()
    cfg = _make_cfg("j1", retries=1, retry_delay_s=120)
    sched = _make_scheduler(
        current_dir=tmp_path,
        jobs=[cfg],
        clock=clock,
        spawner=spawner,
        renew=lambda: renew_calls.append(None),
        notify=lambda *a, **kw: notify_calls.append((a, kw)),
    )

    # --- First auth failure (retry consumed) ---
    sched.schedule_all()
    clock.advance(400)
    sched.fire_due()
    spawner.last_handle.set_exit_code(EXIT_AUTH_FAILED)
    sched.reap()
    first_renew_count = len(renew_calls)

    # --- Second auth failure (no retries left) ---
    clock.advance(130)  # past retry_delay_s
    sched.fire_due()
    assert len(spawner.calls) == 2, "job must be respawned for the retry"
    spawner.last_handle.set_exit_code(EXIT_AUTH_FAILED)
    sched.reap()

    assert len(renew_calls) == first_renew_count, (
        "renew() must NOT be called again when retries are exhausted"
    )
    assert notify_calls, "notify must be called when retries are exhausted"

    state = sched.snapshot()
    j1 = state.jobs["j1"]
    assert j1.last_status in ("failed", "auth-failed"), (
        f"Expected failed/auth-failed, got {j1.last_status!r}"
    )


# ---------------------------------------------------------------------------
# enforce_timeouts
# ---------------------------------------------------------------------------


def test_enforce_timeouts_terminates_overdue_handle(tmp_path):
    clock = FakeClock(t=1_000_000.0)
    spawner = FakeSpawner()
    cfg = _make_cfg("j1", timeout_s=60)
    sched = _make_scheduler(current_dir=tmp_path, jobs=[cfg], clock=clock, spawner=spawner)

    sched.schedule_all()
    clock.advance(400)
    sched.fire_due()

    # Advance past timeout
    clock.advance(61)
    sched.enforce_timeouts()

    assert spawner.last_handle.terminate_calls >= 1, (
        "terminate() must be called when the deadline is exceeded"
    )


def test_enforce_timeouts_then_reap_sets_timeout_status(tmp_path):
    """After a terminate() is sent and the process exits, status must be 'timeout'."""
    clock = FakeClock(t=1_000_000.0)
    spawner = FakeSpawner()
    cfg = _make_cfg("j1", timeout_s=60)
    sched = _make_scheduler(current_dir=tmp_path, jobs=[cfg], clock=clock, spawner=spawner)

    sched.schedule_all()
    clock.advance(400)
    sched.fire_due()

    clock.advance(61)
    sched.enforce_timeouts()

    # Simulate the process dying after terminate
    spawner.last_handle.set_exit_code(-15)
    sched.reap()

    state = sched.snapshot()
    assert state.jobs["j1"].last_status == "timeout"


# ---------------------------------------------------------------------------
# tick
# ---------------------------------------------------------------------------


def test_tick_integrates_and_persists_state(tmp_path):
    """
    After tick(), state.json must exist and match in-memory state.
    """
    clock = FakeClock(t=1_000_000.0)
    spawner = FakeSpawner()
    cfg = _make_cfg("j1")
    sched = _make_scheduler(current_dir=tmp_path, jobs=[cfg], clock=clock, spawner=spawner)

    sched.schedule_all()
    clock.advance(400)
    sched.tick()

    # state.json must have been written
    on_disk = load_state(tmp_path)
    assert "j1" in on_disk.jobs

    # The on-disk state must reflect that the job was fired (running_pid set)
    j1_disk = on_disk.jobs["j1"]
    j1_mem = sched.snapshot().jobs["j1"]
    assert j1_disk.running_pid == j1_mem.running_pid


def test_tick_calls_reap_enforce_fire_in_that_order(tmp_path):
    """
    tick() must call reap, enforce_timeouts, fire_due (verified via side-effects).
    A finished job must be reaped and rescheduled within the same tick if
    a new one is due.
    """
    clock = FakeClock(t=1_000_000.0)
    spawner = FakeSpawner()
    cfg = _make_cfg("j1")
    sched = _make_scheduler(current_dir=tmp_path, jobs=[cfg], clock=clock, spawner=spawner)

    sched.schedule_all()
    clock.advance(400)
    sched.fire_due()  # manually spawn j1
    spawner.last_handle.set_exit_code(0)  # j1 finishes

    # Advance past the next due time (300 s from reap time)
    clock.advance(350)
    sched.tick()  # must reap j1, reschedule it, then fire it again

    # Two spawns total: original + rescheduled
    assert len(spawner.calls) == 2, "tick must reap and re-fire within the same call"


def test_tick_ingests_pending_run_report(tmp_path):
    """A manual run-report marker is merged into _states and persisted by tick.

    Seeds a report for a job whose state has never run; after tick() the
    in-memory state AND state.json must reflect the manual run's last_run.
    """
    from schwab_cli.server.jobs.state import write_run_report

    clock = FakeClock(t=1_000_000.0)
    cfg = _make_cfg("j1", enabled=False)  # disabled so fire_due never spawns
    sched = _make_scheduler(current_dir=tmp_path, jobs=[cfg], clock=clock)
    sched.schedule_all()

    # Manual run happened externally: drop a marker the scheduler will ingest.
    write_run_report(
        tmp_path, "j1", last_run_at=999_999.0, last_status="ok", last_exit_code=0
    )

    sched.tick()

    mem = sched.snapshot().jobs["j1"]
    assert mem.last_run_at == pytest.approx(999_999.0)
    assert mem.last_status == "ok"
    assert mem.last_exit_code == 0

    # Persisted by the same tick's save_state.
    on_disk = load_state(tmp_path).jobs["j1"]
    assert on_disk.last_run_at == pytest.approx(999_999.0)
    assert on_disk.last_status == "ok"

    # The marker was drained (consumed exactly once).
    assert list((tmp_path / "reports").glob("*.json")) == []


def test_tick_ingests_report_for_unknown_job_id(tmp_path):
    """A report for a job not yet in _states creates a JobRunState entry."""
    from schwab_cli.server.jobs.state import write_run_report

    clock = FakeClock(t=1_000_000.0)
    sched = _make_scheduler(current_dir=tmp_path, jobs=[], clock=clock)

    write_run_report(
        tmp_path, "ghost", last_run_at=42.0, last_status="failed", last_exit_code=1
    )
    sched.tick()

    mem = sched.snapshot().jobs.get("ghost")
    assert mem is not None
    assert mem.last_status == "failed"
    assert mem.last_exit_code == 1


def test_tick_reap_overrides_older_manual_report(tmp_path):
    """No double-count: when a job is reaped in the same tick a manual marker
    is ingested, the scheduler's (newer) reap result wins. Ingest runs before
    reap, so the reap unconditionally overwrites the older ingested value."""
    from schwab_cli.server.jobs.state import write_run_report

    clock = FakeClock(t=1_000_000.0)
    spawner = FakeSpawner()
    cfg = _make_cfg("j1")
    sched = _make_scheduler(current_dir=tmp_path, jobs=[cfg], clock=clock, spawner=spawner)

    sched.schedule_all()
    clock.advance(400)
    sched.fire_due()  # j1 now running
    spawner.last_handle.set_exit_code(0)  # scheduler run will reap as ok

    # An OLDER manual marker for the same job (a prior failed manual run).
    write_run_report(
        tmp_path, "j1", last_run_at=clock.t - 5000,
        last_status="failed", last_exit_code=1,
    )

    sched.tick()  # ingest(old marker) -> reap(newer scheduler run)

    j1 = sched.snapshot().jobs["j1"]
    assert j1.last_status == "ok", "scheduler reap must win over the older marker"
    assert j1.last_exit_code == 0
    assert j1.last_run_at == pytest.approx(clock.t)  # reap time, not the marker


def test_tick_ingest_skips_marker_without_run_time(tmp_path):
    """A malformed marker missing last_run_at must NOT clobber a recorded run."""
    import json

    from schwab_cli.server.jobs.state import reports_dir, write_run_report

    clock = FakeClock(t=1_000_000.0)
    cfg = _make_cfg("j1", enabled=False)
    sched = _make_scheduler(current_dir=tmp_path, jobs=[cfg], clock=clock)
    sched.schedule_all()

    # Record a good manual run first.
    write_run_report(
        tmp_path, "j1", last_run_at=500.0, last_status="ok", last_exit_code=0
    )
    sched.tick()
    assert sched.snapshot().jobs["j1"].last_run_at == pytest.approx(500.0)

    # Now drop a malformed marker (no last_run_at) and tick again.
    rd = reports_dir(tmp_path)
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "j1.json").write_text(
        json.dumps({"last_status": "failed", "last_exit_code": 1}), encoding="utf-8"
    )
    sched.tick()

    j1 = sched.snapshot().jobs["j1"]
    assert j1.last_run_at == pytest.approx(500.0), "malformed marker must not clobber"
    assert j1.last_status == "ok"


# ---------------------------------------------------------------------------
# next_wakeup
# ---------------------------------------------------------------------------


def test_next_wakeup_returns_soonest_next_run_when_closer_than_watchdog(tmp_path):
    clock = FakeClock(t=1_000_000.0)
    cfg = _make_cfg("j1")
    sched = _make_scheduler(
        current_dir=tmp_path, jobs=[cfg], clock=clock, watchdog_s=300.0
    )

    sched.schedule_all()
    state = sched.snapshot()
    next_run = state.jobs["j1"].next_run_at  # now + 300 s via stub

    wakeup = sched.next_wakeup()
    # next_run (now+300) <= now+watchdog (now+300) — either is valid at equality
    # but must be <= now + watchdog_s
    assert wakeup <= clock.t + 300.0 + 1  # allow 1 s tolerance


def test_next_wakeup_returns_now_plus_watchdog_when_no_sooner_job(tmp_path):
    clock = FakeClock(t=1_000_000.0)
    # No jobs — nothing scheduled
    sched = _make_scheduler(
        current_dir=tmp_path, jobs=[], clock=clock, watchdog_s=10.0
    )

    wakeup = sched.next_wakeup()
    assert wakeup == pytest.approx(clock.t + 10.0, abs=1)


# ---------------------------------------------------------------------------
# terminate_children
# ---------------------------------------------------------------------------


def test_terminate_children_calls_terminate_on_all_running_handles(tmp_path):
    clock = FakeClock(t=1_000_000.0)
    spawner = FakeSpawner()
    jobs = [_make_cfg("j1"), _make_cfg("j2")]
    sched = _make_scheduler(current_dir=tmp_path, jobs=jobs, clock=clock, spawner=spawner)

    sched.schedule_all()
    clock.advance(400)
    sched.fire_due()  # spawns both
    assert len(spawner.calls) == 2

    # Both children exit promptly after SIGTERM -> grace loop returns at once.
    for handle in spawner.handles:
        handle.set_exit_code(-15)

    sleep_calls: list[float] = []
    sched.terminate_children(sleep=lambda s: sleep_calls.append(s))

    for handle in spawner.handles:
        assert handle.terminate_calls >= 1, (
            f"terminate() must be called on every running handle; "
            f"handle pid={handle.pid} had {handle.terminate_calls} calls"
        )
        # Children exited, so they must NOT be force-killed.
        assert handle.kill_calls == 0


def test_terminate_children_kills_survivors_after_grace(tmp_path):
    """A child that never exits after SIGTERM must be SIGKILL'd past the grace."""
    clock = FakeClock(t=1_000_000.0)
    spawner = FakeSpawner()
    cfg = _make_cfg("j1")
    sched = _make_scheduler(current_dir=tmp_path, jobs=[cfg], clock=clock, spawner=spawner)

    sched.schedule_all()
    clock.advance(400)
    sched.fire_due()
    handle = spawner.last_handle
    # poll() keeps returning None -> the child is stuck and must be killed.

    # Use a tiny grace so the monotonic loop ends quickly; fake sleep is a no-op.
    sched.terminate_children(grace_s=0.0, sleep=lambda s: None)

    assert handle.terminate_calls >= 1
    assert handle.kill_calls >= 1, "a surviving child must be SIGKILL'd after grace"


def test_terminate_children_does_not_kill_child_that_exits(tmp_path):
    """A child that exits after SIGTERM must NOT be force-killed."""
    clock = FakeClock(t=1_000_000.0)
    spawner = FakeSpawner()
    cfg = _make_cfg("j1")
    sched = _make_scheduler(current_dir=tmp_path, jobs=[cfg], clock=clock, spawner=spawner)

    sched.schedule_all()
    clock.advance(400)
    sched.fire_due()
    handle = spawner.last_handle
    handle.set_exit_code(0)  # exits cleanly after terminate

    sched.terminate_children(grace_s=5.0, sleep=lambda s: None)

    assert handle.terminate_calls >= 1
    assert handle.kill_calls == 0


def test_terminate_children_clears_running_state(tmp_path):
    """After shutdown, snapshot() must show no running_pid for affected jobs."""
    clock = FakeClock(t=1_000_000.0)
    spawner = FakeSpawner()
    cfg = _make_cfg("j1")
    sched = _make_scheduler(current_dir=tmp_path, jobs=[cfg], clock=clock, spawner=spawner)

    sched.schedule_all()
    clock.advance(400)
    sched.fire_due()
    assert sched.snapshot().jobs["j1"].running_pid is not None

    spawner.last_handle.set_exit_code(-15)
    sched.terminate_children(grace_s=5.0, sleep=lambda s: None)

    j1 = sched.snapshot().jobs["j1"]
    assert j1.running_pid is None
    assert j1.running_pgid is None
    assert j1.started_at is None
    assert j1.last_status == "interrupted"


def test_terminate_children_no_running_handles_is_noop(tmp_path):
    """With nothing running, terminate_children returns without sleeping."""
    clock = FakeClock(t=1_000_000.0)
    sched = _make_scheduler(current_dir=tmp_path, jobs=[_make_cfg("j1")], clock=clock)
    sched.schedule_all()

    sleep_calls: list[float] = []
    sched.terminate_children(sleep=lambda s: sleep_calls.append(s))

    assert sleep_calls == []


# ---------------------------------------------------------------------------
# reap — last_log points at the actual .log file
# ---------------------------------------------------------------------------


def test_reap_last_log_is_actual_log_file(tmp_path):
    """last_log must reference the real ``*.log`` file, not the log directory."""
    clock = FakeClock(t=1_000_000.0)
    spawner = FakeSpawner()
    cfg = _make_cfg("j1")
    sched = _make_scheduler(current_dir=tmp_path, jobs=[cfg], clock=clock, spawner=spawner)

    sched.schedule_all()
    clock.advance(400)
    sched.fire_due()
    spawner.last_handle.set_exit_code(0)
    sched.reap()

    last_log = sched.snapshot().jobs["j1"].last_log
    assert last_log is not None
    assert last_log.endswith(".log"), f"last_log must be the file, got {last_log!r}"


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


def test_snapshot_returns_scheduler_state_matching_in_memory(tmp_path):
    clock = FakeClock(t=1_000_000.0)
    spawner = FakeSpawner(start_pid=7777)
    cfg = _make_cfg("j1")
    sched = _make_scheduler(current_dir=tmp_path, jobs=[cfg], clock=clock, spawner=spawner)

    sched.schedule_all()
    clock.advance(400)
    sched.fire_due()

    snap = sched.snapshot()
    assert isinstance(snap, SchedulerState)
    assert "j1" in snap.jobs
    assert snap.jobs["j1"].running_pid == 7777


# ---------------------------------------------------------------------------
# reload — Transition contract
# ---------------------------------------------------------------------------


def _find_transition(transitions: list[Transition], job_id: str) -> Transition:
    for t in transitions:
        if t.id == job_id:
            return t
    raise AssertionError(f"No Transition for job_id={job_id!r}")


def test_reload_unchanged_job_produces_unchanged_transition(tmp_path):
    clock = FakeClock(t=1_000_000.0)
    cfg = _make_cfg("j1")
    sched = _make_scheduler(current_dir=tmp_path, jobs=[cfg], clock=clock)
    sched.schedule_all()

    transitions = sched.reload([cfg])  # same config

    t = _find_transition(transitions, "j1")
    assert t.new == "unchanged"


def test_reload_changed_config_produces_updated_transition(tmp_path):
    clock = FakeClock(t=1_000_000.0)
    cfg_v1 = _make_cfg("j1", command=("dataset", "update"))
    sched = _make_scheduler(current_dir=tmp_path, jobs=[cfg_v1], clock=clock)
    sched.schedule_all()

    cfg_v2 = _make_cfg("j1", command=("dataset", "update", "--force"))
    transitions = sched.reload([cfg_v2])

    t = _find_transition(transitions, "j1")
    assert t.new == "updated"
    assert t.next_run_at is not None


def test_reload_disabled_job_produces_disabled_transition(tmp_path):
    clock = FakeClock(t=1_000_000.0)
    cfg = _make_cfg("j1", enabled=True)
    sched = _make_scheduler(current_dir=tmp_path, jobs=[cfg], clock=clock)
    sched.schedule_all()

    cfg_disabled = _make_cfg("j1", enabled=False)
    transitions = sched.reload([cfg_disabled])

    t = _find_transition(transitions, "j1")
    assert t.new == "disabled"
    assert t.next_run_at is None


def test_reload_removed_job_produces_unloaded_transition(tmp_path):
    clock = FakeClock(t=1_000_000.0)
    cfg = _make_cfg("j1")
    sched = _make_scheduler(current_dir=tmp_path, jobs=[cfg], clock=clock)
    sched.schedule_all()

    transitions = sched.reload([])  # j1 is gone

    t = _find_transition(transitions, "j1")
    assert t.new == "unloaded"


def test_reload_new_valid_job_produces_absent_updated_transition(tmp_path):
    clock = FakeClock(t=1_000_000.0)
    sched = _make_scheduler(current_dir=tmp_path, jobs=[], clock=clock)

    cfg_new = _make_cfg("j_new")
    transitions = sched.reload([cfg_new])

    t = _find_transition(transitions, "j_new")
    assert t.old == "absent"
    assert t.new == "updated"


def test_reload_invalid_existing_job_produces_outdated_transition(tmp_path):
    """
    A job that was previously scheduled but whose NEW config is invalid:
    -> new="outdated", error set, prior next_run_at preserved (not cleared).
    """
    clock = FakeClock(t=1_000_000.0)
    cfg = _make_cfg("j1")
    sched = _make_scheduler(current_dir=tmp_path, jobs=[cfg], clock=clock)
    sched.schedule_all()

    prior_next = sched.snapshot().jobs["j1"].next_run_at

    transitions = sched.reload([], invalid={"j1": "bad cron expression"})

    t = _find_transition(transitions, "j1")
    assert t.new == "outdated"
    assert t.error == "bad cron expression"

    # next_run_at must be preserved (not None / not cleared)
    state = sched.snapshot()
    assert state.jobs["j1"].next_run_at == pytest.approx(prior_next)


def test_reload_invalid_never_seen_job_produces_error_transition(tmp_path):
    """
    A job id in the invalid map that was never loaded:
    -> old="error" or new="error", error set.
    """
    clock = FakeClock(t=1_000_000.0)
    sched = _make_scheduler(current_dir=tmp_path, jobs=[], clock=clock)

    transitions = sched.reload([], invalid={"ghost": "missing field"})

    t = _find_transition(transitions, "ghost")
    assert t.new == "error"
    assert t.error is not None


def test_reload_running_job_handle_not_terminated(tmp_path):
    """
    A job that is currently running when reload is called:
    its handle must NOT be terminated (only next-run scheduling is updated).
    """
    clock = FakeClock(t=1_000_000.0)
    spawner = FakeSpawner()
    cfg = _make_cfg("j1")
    sched = _make_scheduler(
        current_dir=tmp_path, jobs=[cfg], clock=clock, spawner=spawner
    )

    sched.schedule_all()
    clock.advance(400)
    sched.fire_due()  # j1 is now running

    handle = spawner.last_handle
    assert handle.terminate_calls == 0

    # Reload with a slightly changed config (still same id, running)
    cfg_v2 = _make_cfg("j1", command=("dataset", "update", "--force"))
    sched.reload([cfg_v2])

    assert handle.terminate_calls == 0, (
        "reload must NOT terminate a currently running handle"
    )


def test_reload_populates_snapshot_last_reload(tmp_path):
    """After reload(), snapshot().last_reload reflects the transitions."""
    clock = FakeClock(t=1_000_000.0)
    cfg = _make_cfg("j1")
    sched = _make_scheduler(current_dir=tmp_path, jobs=[cfg], clock=clock)
    sched.schedule_all()

    assert sched.snapshot().last_reload is None

    cfg_v2 = _make_cfg("j1", command=("dataset", "update", "--force"))
    sched.reload([cfg_v2])

    last_reload = sched.snapshot().last_reload
    assert last_reload is not None
    serialized = list(last_reload)
    j1_entry = next(e for e in serialized if e["id"] == "j1")
    assert j1_entry["new"] == "updated"


def test_reload_old_label_idle_for_never_scheduled_job(tmp_path):
    """A loaded-but-never-scheduled job yields old='idle' on reload (not 'scheduled')."""
    clock = FakeClock(t=1_000_000.0)
    cfg = _make_cfg("j1")
    # Build the scheduler but do NOT call schedule_all(): next_run_at stays None.
    sched = _make_scheduler(current_dir=tmp_path, jobs=[cfg], clock=clock)

    cfg_v2 = _make_cfg("j1", command=("dataset", "update", "--force"))
    transitions = sched.reload([cfg_v2])

    t = _find_transition(transitions, "j1")
    assert t.old == "idle", f"never-scheduled job must report old='idle', got {t.old!r}"


def test_reload_old_label_scheduled_for_scheduled_job(tmp_path):
    """A genuinely scheduled job still reports old='scheduled' on reload."""
    clock = FakeClock(t=1_000_000.0)
    cfg = _make_cfg("j1")
    sched = _make_scheduler(current_dir=tmp_path, jobs=[cfg], clock=clock)
    sched.schedule_all()  # j1 now has next_run_at

    cfg_v2 = _make_cfg("j1", command=("dataset", "update", "--force"))
    transitions = sched.reload([cfg_v2])

    t = _find_transition(transitions, "j1")
    assert t.old == "scheduled"
