"""Scheduler run-state: persistence + crash recovery.

The scheduler keeps a small, JSON-serialisable record of every job's last run
and any in-flight child process. ``state.json`` lives in the ``current``
directory next to the promoted job configs. Writes are atomic (temp file +
``os.replace``) so a crash mid-write never corrupts the file — mirroring
:func:`schwab_cli.server.jobs.config._atomic_write`.

On daemon restart :func:`reconcile_orphans` inspects every job that was marked
running. A child that is still alive *and* whose process start time matches the
recorded ``started_at`` is genuinely orphaned: its process group is killed and
the job is marked ``interrupted``. A dead PID — or one that has been reused by
an unrelated process (start-time mismatch) — is marked ``interrupted`` WITHOUT
killing anything.
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Optional

from schwab_cli._exit_codes import EXIT_AUTH_FAILED

log = logging.getLogger(__name__)

# Tolerance (seconds) when matching a recorded started_at against the OS-reported
# process start time. Clocks and bookkeeping rarely agree to the microsecond.
_PROC_START_TOLERANCE_S = 2.0


@dataclass(frozen=True)
class JobRunState:
    """Immutable record of a single job's last/active run."""

    id: str
    last_run_at: Optional[float] = None
    last_status: Optional[str] = None
    last_exit_code: Optional[int] = None
    last_log: Optional[str] = None
    next_run_at: Optional[float] = None
    running_pid: Optional[int] = None
    running_pgid: Optional[int] = None
    started_at: Optional[float] = None


@dataclass(frozen=True)
class SchedulerState:
    """Snapshot of every job's run state plus scheduler bookkeeping."""

    jobs: Mapping[str, JobRunState] = field(default_factory=dict)
    last_reload: tuple | None = None
    updated_at: float | None = None


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def state_path(current_dir: Path) -> Path:
    """Return the ``state.json`` path under ``current_dir``."""
    return current_dir / "state.json"


# ---------------------------------------------------------------------------
# (De)serialisation
# ---------------------------------------------------------------------------


def _job_from_dict(job_id: str, raw: Mapping[str, Any]) -> JobRunState:
    """Build a JobRunState from a (possibly partial) mapping."""
    return JobRunState(
        id=raw.get("id", job_id),
        last_run_at=raw.get("last_run_at"),
        last_status=raw.get("last_status"),
        last_exit_code=raw.get("last_exit_code"),
        last_log=raw.get("last_log"),
        next_run_at=raw.get("next_run_at"),
        running_pid=raw.get("running_pid"),
        running_pgid=raw.get("running_pgid"),
        started_at=raw.get("started_at"),
    )


def _state_to_dict(state: SchedulerState) -> dict[str, Any]:
    return {
        "jobs": {jid: dataclasses.asdict(rs) for jid, rs in state.jobs.items()},
        "last_reload": list(state.last_reload) if state.last_reload is not None else None,
        "updated_at": state.updated_at,
    }


def load_state(current_dir: Path) -> SchedulerState:
    """Load ``state.json`` from ``current_dir``.

    A missing file yields an empty :class:`SchedulerState`. The loader is
    robust to absent fields — anything missing defaults to ``None``.
    """
    path = state_path(current_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return SchedulerState(jobs={})
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not read scheduler state %s: %s; starting empty", path, exc)
        return SchedulerState(jobs={})

    if not isinstance(raw, dict):
        log.warning("scheduler state %s is not a JSON object; starting empty", path)
        return SchedulerState(jobs={})

    jobs_raw = raw.get("jobs") or {}
    jobs: dict[str, JobRunState] = {}
    if isinstance(jobs_raw, dict):
        for jid, jraw in jobs_raw.items():
            if isinstance(jraw, dict):
                jobs[jid] = _job_from_dict(jid, jraw)

    last_reload = raw.get("last_reload")
    if isinstance(last_reload, list):
        last_reload = tuple(last_reload)

    return SchedulerState(
        jobs=jobs,
        last_reload=last_reload,
        updated_at=raw.get("updated_at"),
    )


def save_state(current_dir: Path, state: SchedulerState) -> None:
    """Atomically persist ``state`` to ``current_dir/state.json``.

    Writes a temp file in the same directory then ``os.replace`` over the
    target. On *any* failure (not just ``OSError``) the temp file is removed
    and the original error re-raised, so no stray ``.tmp`` is ever left behind.
    """
    current_dir.mkdir(parents=True, exist_ok=True)
    dest = state_path(current_dir)
    tmp = current_dir / f".{dest.name}.tmp"
    payload = json.dumps(_state_to_dict(state), indent=2)
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, dest)
    except BaseException:
        # os.replace removes tmp on success; on any failure path it may still
        # exist. suppress FileNotFoundError so cleanup never masks the real error.
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise


# ---------------------------------------------------------------------------
# Run-report markers (manual `jobs run` -> scheduler ingest)
# ---------------------------------------------------------------------------
#
# The scheduler is the sole authoritative writer of state.json (its tick
# rewrites the file from in-memory state, clobbering any external edit). A
# manual `schwab jobs run <id>` therefore hands its outcome to the scheduler
# via a small JSON marker under ``<current>/reports/<job_id>.json``; the next
# tick drains the markers and merges them into the authoritative state. A
# read-only overlay (read_run_reports) lets the status view surface a manual
# run immediately, whether or not the daemon is up.


def reports_dir(current: Path) -> Path:
    """Return the run-report inbox directory under ``current``."""
    return current / "reports"


def status_for_exit_code(exit_code: int) -> str:
    """Map a process exit code to a status string (matches the scheduler).

    ``0 -> "ok"``, ``EXIT_AUTH_FAILED (2) -> "auth-failed"``, else ``"failed"``.
    """
    if exit_code == 0:
        return "ok"
    if exit_code == EXIT_AUTH_FAILED:
        return "auth-failed"
    return "failed"


def write_run_report(
    current: Path,
    job_id: str,
    *,
    last_run_at: float,
    last_status: str,
    last_exit_code: int,
) -> None:
    """Atomically write a manual run-report marker for ``job_id``.

    Creates ``<current>/reports/`` if needed and writes
    ``<job_id>.json`` via temp-file + :func:`os.replace` (mirrors
    :func:`save_state`).
    """
    rdir = reports_dir(current)
    rdir.mkdir(parents=True, exist_ok=True)
    dest = rdir / f"{job_id}.json"
    tmp = rdir / f".{dest.name}.tmp"
    payload = json.dumps(
        {
            "last_run_at": last_run_at,
            "last_status": last_status,
            "last_exit_code": last_exit_code,
        },
        indent=2,
    )
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, dest)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise


def _read_report_file(path: Path) -> dict | None:
    """Parse one report marker; ``None`` when missing/corrupt/not-an-object."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not read run report %s: %s; ignoring", path, exc)
        return None
    if not isinstance(raw, dict):
        return None
    return {
        "last_run_at": raw.get("last_run_at"),
        "last_status": raw.get("last_status"),
        "last_exit_code": raw.get("last_exit_code"),
    }


def read_run_reports(current: Path) -> dict[str, dict]:
    """Return all pending run reports keyed by job id (does NOT delete).

    Read-only overlay for status display. A missing reports dir yields ``{}``.
    """
    rdir = reports_dir(current)
    if not rdir.is_dir():
        return {}
    out: dict[str, dict] = {}
    for path in sorted(rdir.glob("*.json")):
        report = _read_report_file(path)
        if report is not None:
            out[path.stem] = report
    return out


def drain_run_reports(current: Path) -> dict[str, dict]:
    """Return all pending run reports keyed by job id and DELETE the files.

    The authoritative ingest path: the scheduler calls this at the start of a
    tick so the markers are consumed exactly once. A missing reports dir yields
    ``{}``.
    """
    rdir = reports_dir(current)
    if not rdir.is_dir():
        return {}
    out: dict[str, dict] = {}
    for path in sorted(rdir.glob("*.json")):
        report = _read_report_file(path)
        if report is not None:
            out[path.stem] = report
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
    return out


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------


def _mark_interrupted(rs: JobRunState) -> JobRunState:
    """Return a copy of ``rs`` cleared of running fields and marked interrupted."""
    return replace(
        rs,
        last_status="interrupted",
        last_run_at=rs.started_at,
        running_pid=None,
        running_pgid=None,
        started_at=None,
    )


def reconcile_orphans(
    state: SchedulerState,
    *,
    alive: Callable[[int], bool],
    proc_start: Callable[[int], float | None],
    killpg: Callable[[int], None],
) -> SchedulerState:
    """Recover jobs left marked-running by a previous daemon instance.

    For each job with ``running_pid`` set:

    * alive AND ``proc_start`` within tolerance of ``started_at`` -> the child
      is a genuine orphan: ``killpg(running_pgid)`` then mark interrupted.
    * dead PID, or alive with a mismatched start time (PID reuse) -> mark
      interrupted WITHOUT killing.

    Jobs without ``running_pid`` are returned unchanged. The function is pure:
    a new :class:`SchedulerState` is returned.
    """
    new_jobs: dict[str, JobRunState] = {}
    for jid, rs in state.jobs.items():
        if rs.running_pid is None:
            new_jobs[jid] = rs
            continue

        pid = rs.running_pid
        started = rs.started_at
        ps = proc_start(pid)
        is_match = (
            started is not None
            and ps is not None
            and abs(ps - started) <= _PROC_START_TOLERANCE_S
        )
        if alive(pid) and is_match:
            if rs.running_pgid is not None:
                killpg(rs.running_pgid)
        new_jobs[jid] = _mark_interrupted(rs)

    return replace(state, jobs=new_jobs)
