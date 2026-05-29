"""Runtime glue between the daemon process and the in-process job scheduler.

This module is the thin control-plane layer that wires the Phase 0-2 building
blocks (:mod:`config`, :mod:`schedule`, :mod:`state`, :mod:`scheduler`,
:mod:`runner`) into the long-lived ``schwab server`` daemon:

* path helpers (:func:`jobs_dir`, :func:`current_dir`),
* an atomic server pidfile (:func:`write_pidfile` / :func:`read_pidfile` /
  :func:`remove_pidfile`),
* config promotion + reload (:func:`apply_reload`),
* the scheduler poll loop (:func:`run_scheduler_loop`), and
* a JSON-safe admin snapshot (:func:`jobs_admin_payload`).

Everything I/O- or time-related is injected as a callable seam so the whole
module is exercisable with fakes and zero real time / processes / signals.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

from schwab_cli import paths
from schwab_cli.server.jobs import config
from schwab_cli.server.jobs.config import load_jobs, promote
from schwab_cli.server.jobs.scheduler import JobScheduler, Transition
from schwab_cli.server.jobs.state import load_state, read_run_reports

log = logging.getLogger(__name__)

_PIDFILE_NAME = "server.pid"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def jobs_dir(config_dir: Path | None = None) -> Path:
    """Return the staging jobs directory (``<config_dir>/jobs``)."""
    base = config_dir if config_dir is not None else paths.config_dir()
    return Path(base) / "jobs"


def current_dir(config_dir: Path | None = None) -> Path:
    """Return the promoted ``.current`` directory (``<jobs_dir>/.current``)."""
    return jobs_dir(config_dir) / ".current"


# ---------------------------------------------------------------------------
# Pidfile
# ---------------------------------------------------------------------------


def _proc_start_time() -> float:
    """Best-effort process start time; falls back to ``time.time()``.

    Kept psutil-free: there is no portable stdlib way to read a process's
    start time, so we record the current wall clock at pidfile-write time.
    """
    return time.time()


def write_pidfile(current: Path) -> Path:
    """Atomically write the server pidfile under ``current`` and return its path.

    The payload records ``pid`` / ``pgid`` / ``start_time``. The directory tree
    is created if missing; the write is temp-file + :func:`os.replace` so a
    reader never observes a partial file.
    """
    current.mkdir(parents=True, exist_ok=True)
    dest = current / _PIDFILE_NAME
    payload = {
        "pid": os.getpid(),
        "pgid": os.getpgid(0),
        "start_time": _proc_start_time(),
    }
    tmp = current / f".{_PIDFILE_NAME}.tmp"
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, dest)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
    return dest


def read_pidfile(current: Path) -> dict | None:
    """Read and parse the server pidfile; ``None`` on missing/corrupt/empty."""
    path = current / _PIDFILE_NAME
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def remove_pidfile(current: Path) -> None:
    """Remove the server pidfile if present; a missing file is a no-op."""
    path = current / _PIDFILE_NAME
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:  # noqa: BLE001 — best-effort cleanup
        log.debug("could not remove pidfile %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Process liveness + status gathering
# ---------------------------------------------------------------------------


def pid_alive(pid: int) -> bool:
    """Return True if ``pid`` is a live process.

    ``os.kill(pid, 0)`` raises ``ProcessLookupError`` for a dead pid and
    ``PermissionError`` for a live process we may not signal (still alive).
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def server_running(current: Path) -> bool:
    """True when a pidfile exists under ``current`` and names a live process."""
    info = read_pidfile(current)
    if not info:
        return False
    pid = info.get("pid")
    if not isinstance(pid, int):
        return False
    return pid_alive(pid)


def derive_job_state(
    *, enabled: bool, running_pid: int | None, next_run_at: float | None
) -> str:
    """Map raw state fields to a coarse display state."""
    if running_pid is not None:
        return "running"
    if not enabled:
        return "disabled"
    if next_run_at is not None:
        return "scheduled"
    return "idle"


def status_payload(*, config_dir: Path | None = None) -> dict[str, Any]:
    """Merge active configs, persisted state and staging errors into a status view.

    Returns a JSON-serialisable dict of the shape::

        {
            "jobs": [
                {
                    "id": str, "name": str, "enabled": bool, "cron": str,
                    "timezone": str, "state": str, "next_run_at": float | None,
                    "last_run_at": float | None, "last_status": str | None,
                    "last_exit_code": int | None, "running_pid": int | None,
                    "outdated": bool, "edit_error": str | None,
                },
                ...
            ],
            "server_running": bool,
        }
    """
    current = current_dir(config_dir)
    valid, _current_errors = config.load_jobs(current)
    _staging_valid, staging_errors = config.load_jobs(jobs_dir(config_dir))
    scheduler_state = load_state(current)
    # Pending manual run-report markers (read-only; not drained). Overlaying
    # them lets a manual `jobs run` surface immediately — before/without the
    # daemon ingesting them — and consistently whether the daemon is up or down.
    pending_reports = read_run_reports(current)

    jobs: list[dict] = []
    for cfg in valid:
        run_state = scheduler_state.jobs.get(cfg.id)
        running_pid = run_state.running_pid if run_state else None
        next_run_at = run_state.next_run_at if run_state else None
        last_run_at = run_state.last_run_at if run_state else None
        last_status = run_state.last_status if run_state else None
        last_exit_code = run_state.last_exit_code if run_state else None

        report = pending_reports.get(cfg.id)
        if report is not None:
            report_run_at = report.get("last_run_at")
            # Prefer the pending report only when it is strictly newer than the
            # persisted state (a stale marker must never override fresh state).
            if report_run_at is not None and (
                last_run_at is None or report_run_at > last_run_at
            ):
                last_run_at = report_run_at
                last_status = report.get("last_status")
                last_exit_code = report.get("last_exit_code")

        edit_error = staging_errors.get(cfg.id)
        outdated = edit_error is not None

        jobs.append(
            {
                "id": cfg.id,
                "name": cfg.name,
                "enabled": cfg.enabled,
                "cron": cfg.cron,
                "timezone": cfg.timezone,
                "state": derive_job_state(
                    enabled=cfg.enabled,
                    running_pid=running_pid,
                    next_run_at=next_run_at,
                ),
                "next_run_at": next_run_at,
                "last_run_at": last_run_at,
                "last_status": last_status,
                "last_exit_code": last_exit_code,
                "running_pid": running_pid,
                "outdated": outdated,
                "edit_error": edit_error,
            }
        )

    return {"jobs": jobs, "server_running": server_running(current)}


# ---------------------------------------------------------------------------
# Reload
# ---------------------------------------------------------------------------


def apply_reload(
    staging: Path, current: Path, scheduler: JobScheduler
) -> list[Transition]:
    """Promote ``staging`` into ``current`` and reload the scheduler.

    Promotion classifies each id; ``outdated`` / ``error`` ids are forwarded to
    the scheduler as ``invalid`` (with their error message) so a bad staged
    version never clobbers a good running schedule.
    """
    results = promote(staging, current)
    valid, _ = load_jobs(current)
    invalid = {
        r.id: (r.error or "invalid")
        for r in results
        if r.outcome in ("outdated", "error")
    }
    return scheduler.reload(valid, invalid=invalid)


# ---------------------------------------------------------------------------
# Scheduler loop
# ---------------------------------------------------------------------------


def run_scheduler_loop(
    scheduler: JobScheduler,
    staging: Path,
    current: Path,
    *,
    stop: Callable[[], bool],
    reload_requested: Callable[[], bool],
    wait: Callable[[float], None],
    max_iterations: int | None = None,
) -> None:
    """Drive the scheduler until ``stop()`` returns True.

    Each iteration: tick, honour a pending reload, then sleep until the next
    wakeup via the injected ``wait``. ``max_iterations`` bounds the loop for
    deterministic tests.
    """
    count = 0
    while not stop():
        if max_iterations is not None and count >= max_iterations:
            break
        scheduler.tick()
        if reload_requested():
            apply_reload(staging, current, scheduler)
        # next_wakeup() is an absolute epoch; subtract the scheduler's clock to
        # get a relative timeout. Fall back to wall-clock when a (test) fake
        # scheduler exposes no public now() seam.
        now_fn = getattr(scheduler, "now", time.time)
        timeout = max(0.0, scheduler.next_wakeup() - now_fn())
        wait(timeout)
        count += 1


# ---------------------------------------------------------------------------
# Admin snapshot
# ---------------------------------------------------------------------------


def jobs_admin_payload(scheduler: JobScheduler) -> dict:
    """Return a JSON-serializable snapshot of the scheduler's run state."""
    snap = scheduler.snapshot()
    jobs = {
        job_id: dataclasses.asdict(run_state)
        for job_id, run_state in snap.jobs.items()
    }
    # snap.last_reload stores pre-serialized dicts (asdict'd Transitions),
    # not Transition objects — so just copy the list rather than re-dict each.
    last_reload = (
        list(snap.last_reload) if snap.last_reload is not None else None
    )
    return {
        "jobs": jobs,
        "updated_at": snap.updated_at,
        "last_reload": last_reload,
    }
