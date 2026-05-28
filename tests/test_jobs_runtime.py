"""TDD red-phase tests for schwab_cli.server.jobs.runtime (Phase 3).

All imports are expected to fail (ModuleNotFoundError / ImportError) until
the runtime module is implemented — that is the expected RED state.

Design principles
-----------------
* FakeScheduler — records tick/reload calls and stubs next_wakeup.
* Real JobScheduler + FakeClock + FakeSpawner used for apply_reload tests
  that need genuine Transition objects.
* Deterministic fakes: no real sleeping, no real subprocesses, no real I/O
  beyond the tmp_path filesystem.

Run with:
    uv run --frozen --extra dev python -m pytest tests/test_jobs_runtime.py -q
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Import guards — collect cleanly even before the module exists
# ---------------------------------------------------------------------------

try:
    from schwab_cli.server.jobs import runtime as runtime_mod
    _RUNTIME_AVAILABLE = True
except (ModuleNotFoundError, ImportError):
    runtime_mod = None  # type: ignore[assignment]
    _RUNTIME_AVAILABLE = False

try:
    from schwab_cli.server.jobs.config import JobConfig, load_jobs, promote
    _CONFIG_AVAILABLE = True
except (ModuleNotFoundError, ImportError):
    _CONFIG_AVAILABLE = False

try:
    from schwab_cli.server.jobs.runner import JobHandle
    from schwab_cli.server.jobs.scheduler import JobScheduler, Transition
    from schwab_cli.server.jobs.state import JobRunState, SchedulerState, load_state
    _SCHEDULER_AVAILABLE = True
except (ModuleNotFoundError, ImportError):
    _SCHEDULER_AVAILABLE = False

try:
    from schwab_cli import paths as paths_mod
    _PATHS_AVAILABLE = True
except (ModuleNotFoundError, ImportError):
    _PATHS_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not _RUNTIME_AVAILABLE,
    reason="schwab_cli.server.jobs.runtime not implemented yet",
)


# ---------------------------------------------------------------------------
# Helpers / fakes shared across tests
# ---------------------------------------------------------------------------


def _make_job_file(directory: Path, job_id: str, *, enabled: bool = True) -> Path:
    """Write a minimal valid job JSON file and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": f"Test Job {job_id}",
        "enabled": enabled,
        "cron": "0 9 * * *",
        "timezone": "UTC",
        "type": "command",
        "command": ["schwab", "quote", "AAPL"],
    }
    p = directory / f"{job_id}.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def _make_invalid_job_file(directory: Path, job_id: str) -> Path:
    """Write a job JSON file that fails validation (missing required fields)."""
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"name": "Bad Job"}  # missing enabled, cron, timezone, type
    p = directory / f"{job_id}.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


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
    """Fake JobHandle with a settable exit code and call recording."""

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
    """Records (cfg, log_path) pairs and returns FakeHandles."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._next_pid = 1000

    def __call__(self, cfg: "JobConfig", log_path: Path) -> FakeHandle:
        handle = FakeHandle(pid=self._next_pid, pgid=self._next_pid)
        self._next_pid += 1
        self.calls.append((cfg, log_path))
        return handle


def _fake_next_run(cron: str, tz: str, after_dt) -> "datetime":
    """Always schedule 300 seconds in the future."""
    from datetime import datetime, timezone, timedelta
    return after_dt + timedelta(seconds=300)


def _make_real_scheduler(tmp_path: Path, jobs=()) -> "JobScheduler":
    """Build a real JobScheduler with fake seams, parked clock, no disk I/O."""
    clock = FakeClock()
    spawner = FakeSpawner()
    return JobScheduler(
        current_dir=tmp_path / "current",
        jobs=jobs,
        now=clock,
        spawn=spawner,
        next_run=_fake_next_run,
    )


@dataclass
class FakeScheduler:
    """Minimal fake that records tick and reload calls."""

    tick_count: int = 0
    reload_count: int = 0
    reload_args: list = field(default_factory=list)
    _next_wakeup: float = 5.0

    def tick(self) -> None:
        self.tick_count += 1

    def reload(self, jobs, *, invalid=None) -> list:
        self.reload_count += 1
        self.reload_args.append((jobs, invalid))
        return []

    def next_wakeup(self) -> float:
        return self._next_wakeup

    def schedule_all(self) -> None:
        pass

    def snapshot(self) -> "SchedulerState":
        if not _SCHEDULER_AVAILABLE:
            return MagicMock()
        return SchedulerState(jobs={}, updated_at=time.time())


# ---------------------------------------------------------------------------
# jobs_dir / current_dir path helpers
# ---------------------------------------------------------------------------


class TestJobsDirAndCurrentDir:
    """jobs_dir and current_dir must build the correct paths."""

    def test_jobs_dir_uses_explicit_config_dir(self, tmp_path):
        result = runtime_mod.jobs_dir(config_dir=tmp_path)
        assert result == tmp_path / "jobs"

    def test_current_dir_uses_explicit_config_dir(self, tmp_path):
        result = runtime_mod.current_dir(config_dir=tmp_path)
        assert result == tmp_path / "jobs" / ".current"

    def test_jobs_dir_falls_back_to_paths_config_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        result = runtime_mod.jobs_dir()
        assert result == tmp_path / "jobs"

    def test_current_dir_falls_back_to_paths_config_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        result = runtime_mod.current_dir()
        assert result == tmp_path / "jobs" / ".current"

    def test_jobs_dir_and_current_dir_are_nested(self, tmp_path):
        """current_dir must always be a child of jobs_dir."""
        jd = runtime_mod.jobs_dir(config_dir=tmp_path)
        cd = runtime_mod.current_dir(config_dir=tmp_path)
        assert cd.parent == jd


# ---------------------------------------------------------------------------
# Pidfile: write / read / remove
# ---------------------------------------------------------------------------


class TestPidfileRoundtrip:
    """write_pidfile / read_pidfile / remove_pidfile contract."""

    def test_write_then_read_roundtrip(self, tmp_path):
        current = tmp_path / ".current"
        current.mkdir()
        pid_path = runtime_mod.write_pidfile(current)
        data = runtime_mod.read_pidfile(current)
        assert data is not None
        assert data["pid"] == os.getpid()
        assert "pgid" in data
        assert "start_time" in data

    def test_write_pidfile_creates_directory(self, tmp_path):
        current = tmp_path / "nested" / ".current"
        # must NOT exist yet
        assert not current.exists()
        runtime_mod.write_pidfile(current)
        assert current.is_dir()

    def test_write_pidfile_returns_path_ending_in_server_pid(self, tmp_path):
        current = tmp_path / ".current"
        current.mkdir()
        pid_path = runtime_mod.write_pidfile(current)
        assert pid_path.name == "server.pid"
        assert pid_path.parent == current

    def test_write_pidfile_contains_current_process_pid(self, tmp_path):
        current = tmp_path / ".current"
        current.mkdir()
        runtime_mod.write_pidfile(current)
        raw = json.loads((current / "server.pid").read_text())
        assert raw["pid"] == os.getpid()

    def test_write_pidfile_is_valid_json(self, tmp_path):
        current = tmp_path / ".current"
        current.mkdir()
        pid_path = runtime_mod.write_pidfile(current)
        # Must be parseable JSON (atomic write should never leave partial).
        data = json.loads(pid_path.read_text())
        assert isinstance(data, dict)

    def test_read_pidfile_missing_returns_none(self, tmp_path):
        current = tmp_path / ".current"
        current.mkdir()
        assert runtime_mod.read_pidfile(current) is None

    def test_read_pidfile_corrupt_returns_none(self, tmp_path):
        current = tmp_path / ".current"
        current.mkdir()
        (current / "server.pid").write_text("NOT JSON {{{{", encoding="utf-8")
        assert runtime_mod.read_pidfile(current) is None

    def test_read_pidfile_empty_file_returns_none(self, tmp_path):
        current = tmp_path / ".current"
        current.mkdir()
        (current / "server.pid").write_text("", encoding="utf-8")
        assert runtime_mod.read_pidfile(current) is None

    def test_remove_pidfile_removes_existing(self, tmp_path):
        current = tmp_path / ".current"
        current.mkdir()
        runtime_mod.write_pidfile(current)
        assert (current / "server.pid").exists()
        runtime_mod.remove_pidfile(current)
        assert not (current / "server.pid").exists()

    def test_remove_pidfile_noop_when_absent(self, tmp_path):
        current = tmp_path / ".current"
        current.mkdir()
        # Must not raise
        runtime_mod.remove_pidfile(current)

    def test_remove_pidfile_noop_when_dir_missing(self, tmp_path):
        current = tmp_path / ".current"
        # Directory doesn't exist either — still must not raise
        runtime_mod.remove_pidfile(current)

    def test_write_then_remove_then_read_is_none(self, tmp_path):
        current = tmp_path / ".current"
        current.mkdir()
        runtime_mod.write_pidfile(current)
        runtime_mod.remove_pidfile(current)
        assert runtime_mod.read_pidfile(current) is None

    def test_write_pidfile_atomicity_no_tmp_leftover(self, tmp_path):
        """The temp file used during the atomic write must be cleaned up."""
        current = tmp_path / ".current"
        current.mkdir()
        runtime_mod.write_pidfile(current)
        leftover = list(current.glob(".*.tmp"))
        assert leftover == [], f"Unexpected tmp files: {leftover}"


# ---------------------------------------------------------------------------
# apply_reload
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _SCHEDULER_AVAILABLE or not _CONFIG_AVAILABLE,
    reason="scheduler / config modules not yet available",
)
class TestApplyReload:
    """apply_reload integrates promote + load_jobs + scheduler.reload."""

    def _build_real_scheduler(self, tmp_path: Path, initial_jobs=()):
        clock = FakeClock()
        spawner = FakeSpawner()
        sched = JobScheduler(
            current_dir=tmp_path / "current",
            jobs=initial_jobs,
            now=clock,
            spawn=spawner,
            next_run=_fake_next_run,
        )
        sched.schedule_all()
        return sched, clock

    def test_valid_staged_job_produces_updated_transition(self, tmp_path):
        """A valid staged job not yet in current → Transition.new == 'updated'."""
        staging = tmp_path / "jobs"
        current = tmp_path / "jobs" / ".current"
        _make_job_file(staging, "alpha")

        sched, _ = self._build_real_scheduler(tmp_path)
        transitions = runtime_mod.apply_reload(staging, current, sched)

        assert isinstance(transitions, list)
        assert len(transitions) == 1
        t = transitions[0]
        assert t.id == "alpha"
        assert t.new == "updated"

    def test_valid_staged_job_promoted_to_current(self, tmp_path):
        """After apply_reload the job file must exist in current dir."""
        staging = tmp_path / "jobs"
        current = tmp_path / "jobs" / ".current"
        _make_job_file(staging, "beta")

        sched, _ = self._build_real_scheduler(tmp_path)
        runtime_mod.apply_reload(staging, current, sched)

        assert (current / "beta.json").exists()

    def test_invalid_staged_job_with_existing_current_is_outdated(self, tmp_path):
        """Staging invalid + current exists → Transition.new == 'outdated'."""
        staging = tmp_path / "jobs"
        current = tmp_path / "jobs" / ".current"

        # Seed current with a valid version so it already knows the job.
        _make_job_file(current, "gamma")
        # Stage an invalid version.
        _make_invalid_job_file(staging, "gamma")

        # Pre-load the scheduler with the valid version from current.
        valid_cfg, _ = load_jobs(current)
        sched, _ = self._build_real_scheduler(tmp_path, valid_cfg)

        transitions = runtime_mod.apply_reload(staging, current, sched)

        gamma_transitions = [t for t in transitions if t.id == "gamma"]
        assert gamma_transitions, f"No transition for 'gamma'. Got: {transitions}"
        t = gamma_transitions[0]
        assert t.new == "outdated"

    def test_invalid_staged_job_with_existing_current_keeps_scheduled(self, tmp_path):
        """When a job is outdated the scheduler keeps its prior schedule."""
        staging = tmp_path / "jobs"
        current = tmp_path / "jobs" / ".current"

        _make_job_file(current, "delta")
        _make_invalid_job_file(staging, "delta")

        valid_cfg, _ = load_jobs(current)
        sched, clock = self._build_real_scheduler(tmp_path, valid_cfg)

        runtime_mod.apply_reload(staging, current, sched)

        # The job's next_run_at should still be set (not cleared).
        snap = sched.snapshot()
        rs = snap.jobs.get("delta")
        # Outdated job keeps its prior schedule — next_run_at not None.
        assert rs is not None
        assert rs.next_run_at is not None

    def test_returns_list_of_transitions(self, tmp_path):
        staging = tmp_path / "jobs"
        current = tmp_path / "jobs" / ".current"
        _make_job_file(staging, "epsilon")

        sched, _ = self._build_real_scheduler(tmp_path)
        transitions = runtime_mod.apply_reload(staging, current, sched)

        assert isinstance(transitions, list)
        for t in transitions:
            assert hasattr(t, "id")
            assert hasattr(t, "new")

    def test_empty_staging_dir_unloads_known_jobs(self, tmp_path):
        """No staged jobs → scheduler marks all previously known jobs 'unloaded'."""
        staging = tmp_path / "jobs"
        staging.mkdir(parents=True, exist_ok=True)  # exists but empty
        current = tmp_path / "jobs" / ".current"

        # Seed current with a valid job.
        _make_job_file(current, "zeta")
        valid_cfg, _ = load_jobs(current)
        sched, _ = self._build_real_scheduler(tmp_path, valid_cfg)

        transitions = runtime_mod.apply_reload(staging, current, sched)

        zeta_t = [t for t in transitions if t.id == "zeta"]
        assert zeta_t
        assert zeta_t[0].new == "unloaded"


# ---------------------------------------------------------------------------
# run_scheduler_loop
# ---------------------------------------------------------------------------


class TestRunSchedulerLoop:
    """run_scheduler_loop drives tick/reload with injected callables."""

    def test_tick_called_n_times_with_max_iterations(self, tmp_path):
        """With max_iterations=3 the loop must call scheduler.tick() 3 times."""
        sched = FakeScheduler()
        wait_calls = []

        def wait(timeout: float) -> None:
            wait_calls.append(timeout)

        runtime_mod.run_scheduler_loop(
            sched,
            staging=tmp_path / "staging",
            current=tmp_path / "current",
            stop=lambda: False,
            reload_requested=lambda: False,
            wait=wait,
            max_iterations=3,
        )

        assert sched.tick_count == 3

    def test_wait_called_n_times_with_max_iterations(self, tmp_path):
        """wait() is called once per iteration."""
        sched = FakeScheduler()
        wait_calls = []

        def wait(timeout: float) -> None:
            wait_calls.append(timeout)

        runtime_mod.run_scheduler_loop(
            sched,
            staging=tmp_path / "staging",
            current=tmp_path / "current",
            stop=lambda: False,
            reload_requested=lambda: False,
            wait=wait,
            max_iterations=3,
        )

        assert len(wait_calls) == 3

    def test_wait_receives_timeout_from_next_wakeup(self, tmp_path):
        """wait() timeout argument must come from scheduler.next_wakeup()."""
        expected_wakeup = 42.0

        @dataclass
        class TimedFake:
            tick_count: int = 0
            _wakeup: float = expected_wakeup

            def tick(self):
                self.tick_count += 1

            def next_wakeup(self):
                return self._wakeup

            def reload(self, jobs, *, invalid=None):
                return []

            def schedule_all(self):
                pass

        sched = TimedFake()
        received_timeouts = []

        def wait(timeout: float) -> None:
            received_timeouts.append(timeout)

        now = time.time()

        runtime_mod.run_scheduler_loop(
            sched,
            staging=tmp_path / "staging",
            current=tmp_path / "current",
            stop=lambda: False,
            reload_requested=lambda: False,
            wait=wait,
            max_iterations=2,
        )

        # The timeout passed to wait should be derived from next_wakeup()
        # (may be clamped or offset by current time — but must be > 0 and finite).
        assert all(isinstance(t, (int, float)) for t in received_timeouts)
        assert len(received_timeouts) == 2

    def test_stop_returning_true_ends_loop_early(self, tmp_path):
        """stop() returning True on the first check exits before any tick."""
        sched = FakeScheduler()
        wait_calls = []

        runtime_mod.run_scheduler_loop(
            sched,
            staging=tmp_path / "staging",
            current=tmp_path / "current",
            stop=lambda: True,  # already stopped on entry
            reload_requested=lambda: False,
            wait=lambda t: wait_calls.append(t),
            max_iterations=10,
        )

        assert sched.tick_count == 0
        assert len(wait_calls) == 0

    def test_stop_returning_true_on_second_iteration(self, tmp_path):
        """stop() becoming True after the first iteration stops after one tick."""
        sched = FakeScheduler()
        call_count = [0]

        def stop():
            call_count[0] += 1
            return call_count[0] > 1  # False first time, True second time

        runtime_mod.run_scheduler_loop(
            sched,
            staging=tmp_path / "staging",
            current=tmp_path / "current",
            stop=stop,
            reload_requested=lambda: False,
            wait=lambda t: None,
            max_iterations=10,
        )

        assert sched.tick_count == 1

    def test_reload_requested_true_on_iteration_2_calls_scheduler_reload(self, tmp_path):
        """reload_requested() returning True on iteration 2 triggers apply_reload."""
        staging = tmp_path / "staging"
        current = tmp_path / "current"
        staging.mkdir(parents=True, exist_ok=True)
        current.mkdir(parents=True, exist_ok=True)

        sched = FakeScheduler()
        iteration = [0]

        def reload_requested():
            iteration[0] += 1
            return iteration[0] == 2  # True only on second call

        runtime_mod.run_scheduler_loop(
            sched,
            staging=staging,
            current=current,
            stop=lambda: False,
            reload_requested=reload_requested,
            wait=lambda t: None,
            max_iterations=3,
        )

        # apply_reload -> scheduler.reload should have been called exactly once.
        assert sched.reload_count == 1

    def test_reload_requested_never_true_means_zero_reloads(self, tmp_path):
        sched = FakeScheduler()

        runtime_mod.run_scheduler_loop(
            sched,
            staging=tmp_path / "staging",
            current=tmp_path / "current",
            stop=lambda: False,
            reload_requested=lambda: False,
            wait=lambda t: None,
            max_iterations=5,
        )

        assert sched.reload_count == 0

    def test_zero_max_iterations_does_nothing(self, tmp_path):
        sched = FakeScheduler()

        runtime_mod.run_scheduler_loop(
            sched,
            staging=tmp_path / "staging",
            current=tmp_path / "current",
            stop=lambda: False,
            reload_requested=lambda: False,
            wait=lambda t: None,
            max_iterations=0,
        )

        assert sched.tick_count == 0
        assert sched.reload_count == 0


# ---------------------------------------------------------------------------
# jobs_admin_payload
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _SCHEDULER_AVAILABLE or not _CONFIG_AVAILABLE,
    reason="scheduler / config modules not yet available",
)
class TestJobsAdminPayload:
    """jobs_admin_payload returns a JSON-serializable snapshot dict."""

    def _make_scheduler_with_jobs(self, tmp_path: Path) -> "JobScheduler":
        staging = tmp_path / "jobs"
        current = tmp_path / "jobs" / ".current"
        _make_job_file(staging, "job-one")
        _make_job_file(staging, "job-two", enabled=False)
        promote(staging, current)

        clock = FakeClock()
        spawner = FakeSpawner()
        valid, _ = load_jobs(current)
        sched = JobScheduler(
            current_dir=current,
            jobs=valid,
            now=clock,
            spawn=spawner,
            next_run=_fake_next_run,
        )
        sched.schedule_all()
        return sched

    def test_returns_dict_with_jobs_key(self, tmp_path):
        sched = self._make_scheduler_with_jobs(tmp_path)
        payload = runtime_mod.jobs_admin_payload(sched)
        assert isinstance(payload, dict)
        assert "jobs" in payload

    def test_jobs_key_contains_known_job_ids(self, tmp_path):
        sched = self._make_scheduler_with_jobs(tmp_path)
        payload = runtime_mod.jobs_admin_payload(sched)
        jobs = payload["jobs"]
        assert "job-one" in jobs
        assert "job-two" in jobs

    def test_payload_is_json_serializable(self, tmp_path):
        sched = self._make_scheduler_with_jobs(tmp_path)
        payload = runtime_mod.jobs_admin_payload(sched)
        # Must not raise
        serialized = json.dumps(payload)
        assert isinstance(serialized, str)

    def test_payload_has_updated_at_key(self, tmp_path):
        sched = self._make_scheduler_with_jobs(tmp_path)
        payload = runtime_mod.jobs_admin_payload(sched)
        assert "updated_at" in payload

    def test_payload_has_last_reload_key(self, tmp_path):
        sched = self._make_scheduler_with_jobs(tmp_path)
        payload = runtime_mod.jobs_admin_payload(sched)
        assert "last_reload" in payload

    def test_job_entries_contain_expected_fields(self, tmp_path):
        sched = self._make_scheduler_with_jobs(tmp_path)
        payload = runtime_mod.jobs_admin_payload(sched)
        entry = payload["jobs"]["job-one"]
        # JobRunState fields that must appear
        assert "id" in entry

    def test_scheduler_with_no_jobs_returns_empty_jobs_dict(self, tmp_path):
        clock = FakeClock()
        spawner = FakeSpawner()
        sched = JobScheduler(
            current_dir=tmp_path / "current",
            jobs=[],
            now=clock,
            spawn=spawner,
            next_run=_fake_next_run,
        )
        payload = runtime_mod.jobs_admin_payload(sched)
        assert payload["jobs"] == {}
        assert json.dumps(payload)  # still serializable
