"""TDD red-phase tests for schwab_cli.server.jobs.state.

All imports are expected to fail (ModuleNotFoundError / ImportError)
until the module is implemented.  These tests define the exact contract
that state.py must fulfil.

Run with:
    uv run --frozen --extra dev python -m pytest tests/test_jobs_state.py -q
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from schwab_cli.server.jobs.state import (
    JobRunState,
    SchedulerState,
    drain_run_reports,
    load_state,
    read_run_reports,
    reconcile_orphans,
    save_state,
    state_path,
    status_for_exit_code,
    write_run_report,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_running_state(
    job_id: str,
    pid: int = 1001,
    pgid: int = 1001,
    started_at: float = 1_000_000.0,
    next_run_at: float | None = 2_000_000.0,
) -> JobRunState:
    return JobRunState(
        id=job_id,
        last_run_at=None,
        last_status=None,
        last_exit_code=None,
        last_log=None,
        next_run_at=next_run_at,
        running_pid=pid,
        running_pgid=pgid,
        started_at=started_at,
    )


def _make_finished_state(job_id: str) -> JobRunState:
    return JobRunState(
        id=job_id,
        last_run_at=999_999.0,
        last_status="ok",
        last_exit_code=0,
        last_log="done\n",
        next_run_at=1_200_000.0,
        running_pid=None,
        running_pgid=None,
        started_at=None,
    )


# ---------------------------------------------------------------------------
# state_path
# ---------------------------------------------------------------------------


def test_state_path_returns_state_json_under_current_dir(tmp_path):
    result = state_path(tmp_path)
    assert result == tmp_path / "state.json"


# ---------------------------------------------------------------------------
# load_state — missing file
# ---------------------------------------------------------------------------


def test_load_state_missing_file_returns_empty_scheduler_state(tmp_path):
    s = load_state(tmp_path)
    assert isinstance(s, SchedulerState)
    assert s.jobs == {}


def test_load_state_missing_file_has_none_updated_at(tmp_path):
    s = load_state(tmp_path)
    assert s.updated_at is None


# ---------------------------------------------------------------------------
# save_state / load_state round-trip
# ---------------------------------------------------------------------------


def test_round_trip_several_job_run_states(tmp_path):
    """save_state then load_state must reproduce the exact SchedulerState."""
    jobs = {
        "j1": _make_finished_state("j1"),
        "j2": _make_running_state("j2", pid=2222, pgid=2222, started_at=1_100_000.0),
        "j3": JobRunState(id="j3"),  # all-None fields
    }
    original = SchedulerState(jobs=jobs, updated_at=1_234_567.89)

    save_state(tmp_path, original)
    loaded = load_state(tmp_path)

    assert isinstance(loaded, SchedulerState)
    assert set(loaded.jobs.keys()) == {"j1", "j2", "j3"}

    j1 = loaded.jobs["j1"]
    assert j1.last_status == "ok"
    assert j1.last_exit_code == 0
    assert j1.last_log == "done\n"
    assert j1.next_run_at == pytest.approx(1_200_000.0)
    assert j1.running_pid is None

    j2 = loaded.jobs["j2"]
    assert j2.running_pid == 2222
    assert j2.running_pgid == 2222
    assert j2.started_at == pytest.approx(1_100_000.0)

    j3 = loaded.jobs["j3"]
    assert j3.last_run_at is None
    assert j3.last_status is None
    assert j3.running_pid is None


def test_round_trip_preserves_updated_at(tmp_path):
    ts = 9_876_543.21
    original = SchedulerState(jobs={}, updated_at=ts)
    save_state(tmp_path, original)
    loaded = load_state(tmp_path)
    assert loaded.updated_at == pytest.approx(ts)


def test_round_trip_none_updated_at(tmp_path):
    original = SchedulerState(jobs={}, updated_at=None)
    save_state(tmp_path, original)
    loaded = load_state(tmp_path)
    assert loaded.updated_at is None


# ---------------------------------------------------------------------------
# save_state atomicity
# ---------------------------------------------------------------------------


def test_save_state_no_leftover_temp_file(tmp_path):
    """After a successful save, no .tmp files remain in current_dir."""
    original = SchedulerState(jobs={"j1": _make_finished_state("j1")}, updated_at=1.0)
    save_state(tmp_path, original)

    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == [], f"Unexpected temp files: {tmp_files}"


def test_save_state_atomic_no_temp_on_replace_failure(tmp_path, monkeypatch):
    """If os.replace raises, the temp file must be cleaned up (not left behind)."""
    import schwab_cli.server.jobs.state as state_mod

    def _failing_replace(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(state_mod.os, "replace", _failing_replace)

    original = SchedulerState(jobs={}, updated_at=1.0)
    with pytest.raises(OSError, match="simulated replace failure"):
        save_state(tmp_path, original)

    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == [], f"Temp file left behind after failure: {tmp_files}"


def test_save_state_no_temp_on_non_oserror(tmp_path, monkeypatch):
    """A non-OSError during write must still clean up the temp file."""
    import schwab_cli.server.jobs.state as state_mod

    real_write_text = Path.write_text

    def _failing_write_text(self, *args, **kwargs):
        # Create the temp file first, then blow up with a non-OSError so we can
        # prove the cleanup path runs for arbitrary exceptions (BaseException).
        real_write_text(self, *args, **kwargs)
        raise RuntimeError("simulated non-OSError failure")

    monkeypatch.setattr(state_mod.Path, "write_text", _failing_write_text)

    original = SchedulerState(jobs={}, updated_at=1.0)
    with pytest.raises(RuntimeError, match="simulated non-OSError failure"):
        save_state(tmp_path, original)

    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == [], f"Temp file left behind after non-OSError: {tmp_files}"


def test_save_state_writes_valid_json(tmp_path):
    """The file written by save_state must be parseable JSON."""
    original = SchedulerState(jobs={"j1": _make_finished_state("j1")}, updated_at=1.0)
    save_state(tmp_path, original)

    raw = (tmp_path / "state.json").read_text(encoding="utf-8")
    parsed = json.loads(raw)  # must not raise
    assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# Run-report markers
# ---------------------------------------------------------------------------


def test_status_for_exit_code_mapping():
    assert status_for_exit_code(0) == "ok"
    assert status_for_exit_code(2) == "auth-failed"  # EXIT_AUTH_FAILED
    assert status_for_exit_code(1) == "failed"
    assert status_for_exit_code(127) == "failed"


def test_write_then_read_run_report_round_trip(tmp_path):
    write_run_report(
        tmp_path, "j1", last_run_at=123.5, last_status="ok", last_exit_code=0
    )
    reports = read_run_reports(tmp_path)
    assert reports == {
        "j1": {"last_run_at": 123.5, "last_status": "ok", "last_exit_code": 0}
    }


def test_write_run_report_creates_reports_dir(tmp_path):
    assert not (tmp_path / "reports").exists()
    write_run_report(
        tmp_path, "j1", last_run_at=1.0, last_status="ok", last_exit_code=0
    )
    assert (tmp_path / "reports" / "j1.json").exists()


def test_write_run_report_is_atomic_no_tmp_leftover(tmp_path):
    write_run_report(
        tmp_path, "j1", last_run_at=1.0, last_status="ok", last_exit_code=0
    )
    tmp_files = list((tmp_path / "reports").glob("*.tmp"))
    assert tmp_files == [], f"Unexpected temp files: {tmp_files}"


def test_read_run_reports_does_not_delete(tmp_path):
    write_run_report(
        tmp_path, "j1", last_run_at=1.0, last_status="ok", last_exit_code=0
    )
    read_run_reports(tmp_path)
    # File must still be present after a read-only overlay.
    assert (tmp_path / "reports" / "j1.json").exists()


def test_drain_run_reports_returns_and_deletes(tmp_path):
    write_run_report(
        tmp_path, "j1", last_run_at=2.0, last_status="auth-failed", last_exit_code=2
    )
    write_run_report(
        tmp_path, "j2", last_run_at=3.0, last_status="failed", last_exit_code=1
    )
    drained = drain_run_reports(tmp_path)
    assert set(drained) == {"j1", "j2"}
    assert drained["j1"]["last_exit_code"] == 2
    assert drained["j2"]["last_status"] == "failed"
    # Both marker files are consumed.
    assert list((tmp_path / "reports").glob("*.json")) == []


def test_drain_run_reports_missing_dir_returns_empty(tmp_path):
    assert drain_run_reports(tmp_path) == {}


def test_read_run_reports_missing_dir_returns_empty(tmp_path):
    assert read_run_reports(tmp_path) == {}


def test_read_run_reports_ignores_corrupt_marker(tmp_path):
    rdir = tmp_path / "reports"
    rdir.mkdir()
    (rdir / "bad.json").write_text("{not json", encoding="utf-8")
    write_run_report(
        tmp_path, "good", last_run_at=1.0, last_status="ok", last_exit_code=0
    )
    reports = read_run_reports(tmp_path)
    assert "good" in reports
    assert "bad" not in reports


# ---------------------------------------------------------------------------
# reconcile_orphans
# ---------------------------------------------------------------------------


def test_reconcile_alive_matching_proc_kills_and_marks_interrupted(tmp_path):
    """
    A running job whose pid is alive AND proc_start matches started_at:
    killpg must be called with its pgid and the job must be marked interrupted
    with running fields cleared.
    """
    started = 1_000_000.0
    rs = _make_running_state("j1", pid=5001, pgid=5001, started_at=started)
    state = SchedulerState(jobs={"j1": rs}, updated_at=started)

    killed_pgids: list[int] = []

    new_state = reconcile_orphans(
        state,
        alive=lambda pid: pid == 5001,
        proc_start=lambda pid: started if pid == 5001 else None,
        killpg=lambda pgid: killed_pgids.append(pgid),
    )

    assert killed_pgids == [5001], "killpg must be called with the job's pgid"

    j1 = new_state.jobs["j1"]
    assert j1.last_status == "interrupted"
    assert j1.running_pid is None
    assert j1.running_pgid is None
    assert j1.started_at is None
    assert j1.last_run_at == pytest.approx(started)
    # next_run_at must be preserved
    assert j1.next_run_at == pytest.approx(2_000_000.0)


def test_reconcile_dead_pid_marks_interrupted_no_kill():
    """
    A job whose pid is dead: mark interrupted, killpg NOT called.
    """
    started = 1_000_000.0
    rs = _make_running_state("j1", pid=5002, pgid=5002, started_at=started)
    state = SchedulerState(jobs={"j1": rs}, updated_at=started)

    killed_pgids: list[int] = []

    new_state = reconcile_orphans(
        state,
        alive=lambda pid: False,  # pid is dead
        proc_start=lambda pid: None,
        killpg=lambda pgid: killed_pgids.append(pgid),
    )

    assert killed_pgids == [], "killpg must NOT be called when pid is dead"

    j1 = new_state.jobs["j1"]
    assert j1.last_status == "interrupted"
    assert j1.running_pid is None


def test_reconcile_pid_reuse_no_kill():
    """
    A job whose pid is alive BUT proc_start mismatches started_at (PID reuse):
    mark interrupted, killpg NOT called (must not kill an unrelated process).
    """
    started = 1_000_000.0
    different_start = 2_000_000.0  # a different process reused the PID
    rs = _make_running_state("j1", pid=5003, pgid=5003, started_at=started)
    state = SchedulerState(jobs={"j1": rs}, updated_at=started)

    killed_pgids: list[int] = []

    new_state = reconcile_orphans(
        state,
        alive=lambda pid: True,  # pid is alive (but a different process)
        proc_start=lambda pid: different_start,  # mismatches started_at
        killpg=lambda pgid: killed_pgids.append(pgid),
    )

    assert killed_pgids == [], "killpg must NOT be called on a reused PID"

    j1 = new_state.jobs["j1"]
    assert j1.last_status == "interrupted"
    assert j1.running_pid is None


def test_reconcile_no_running_pid_unchanged():
    """A job with no running_pid is left completely unchanged."""
    rs = _make_finished_state("j1")
    state = SchedulerState(jobs={"j1": rs}, updated_at=1.0)

    killed_pgids: list[int] = []

    new_state = reconcile_orphans(
        state,
        alive=lambda pid: False,
        proc_start=lambda pid: None,
        killpg=lambda pgid: killed_pgids.append(pgid),
    )

    assert killed_pgids == []
    assert new_state.jobs["j1"] == rs  # unchanged


def test_reconcile_multiple_jobs_mixed(tmp_path):
    """
    Multiple jobs: alive+match -> kill+interrupt; dead -> interrupt no kill;
    not running -> unchanged.
    """
    started = 1_000_000.0

    alive_rs = _make_running_state("alive", pid=6001, pgid=6001, started_at=started)
    dead_rs = _make_running_state("dead", pid=6002, pgid=6002, started_at=started)
    no_pid_rs = _make_finished_state("nopid")

    state = SchedulerState(
        jobs={"alive": alive_rs, "dead": dead_rs, "nopid": no_pid_rs},
        updated_at=started,
    )

    killed: list[int] = []

    def _alive(pid: int) -> bool:
        return pid == 6001

    def _proc_start(pid: int) -> float | None:
        return started if pid == 6001 else None

    new_state = reconcile_orphans(
        state,
        alive=_alive,
        proc_start=_proc_start,
        killpg=lambda pgid: killed.append(pgid),
    )

    assert 6001 in killed
    assert 6002 not in killed

    assert new_state.jobs["alive"].last_status == "interrupted"
    assert new_state.jobs["dead"].last_status == "interrupted"
    assert new_state.jobs["nopid"] == no_pid_rs
