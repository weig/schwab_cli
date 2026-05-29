"""In-process job scheduler core.

The :class:`JobScheduler` owns the live scheduling loop: it computes each
enabled job's next cron fire time, spawns due jobs as detached worker
processes (subject to a concurrency cap), reaps finished children, enforces
per-job timeouts, and persists run state after every tick.

Every external dependency is injected as a callable seam — ``now``, ``spawn``,
``next_run``, ``renew``, ``notify`` — mirroring
:mod:`schwab_cli.server.maintenance`, so the whole loop is exercisable with
fakes and zero real time / processes / signals.

Auth-failure handling: a child that exits with
:data:`~schwab_cli._exit_codes.EXIT_AUTH_FAILED` triggers a one-shot
``renew()`` and a fast retry (``now + retry_delay_s``) as long as that job has
retries remaining; once exhausted the failure is terminal and ``notify`` fires.
Any other non-zero exit is a generic failure — never retried immediately, just
rescheduled to the normal next occurrence.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from schwab_cli._exit_codes import EXIT_AUTH_FAILED
from schwab_cli.server.jobs import schedule
from schwab_cli.server.jobs.config import JobConfig
from schwab_cli.server.jobs.runner import JobHandle
from schwab_cli.server.jobs.state import (
    JobRunState,
    SchedulerState,
    drain_run_reports,
    save_state,
)

log = logging.getLogger(__name__)

WATCHDOG_S = 10
MAX_CONCURRENT_JOBS = 4
LOG_RETENTION = 20
SHUTDOWN_GRACE_S = 10
# Poll interval while waiting for children to exit after SIGTERM during shutdown.
_TERM_POLL_S = 0.1

# next_run signature: (cron, timezone_name, after_dt) -> next_dt
NextRun = Callable[[str, str, datetime], datetime]


@dataclass(frozen=True)
class Transition:
    """A single id's state change produced by :meth:`JobScheduler.reload`."""

    id: str
    old: str | None
    new: str
    next_run_at: float | None = None
    error: str | None = None


def _epoch_to_utc(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


class JobScheduler:
    """Owns the in-memory scheduling state and drives the run loop."""

    def __init__(
        self,
        *,
        current_dir: Path,
        jobs: Iterable[JobConfig],
        now: Callable[[], float],
        spawn: Callable[[JobConfig, Path], JobHandle],
        next_run: NextRun = schedule.next_run_after,
        renew: Callable[[], None] | None = None,
        notify: Callable[..., None] | None = None,
        watchdog_s: float = WATCHDOG_S,
        max_concurrent: int = MAX_CONCURRENT_JOBS,
        log_retention: int = LOG_RETENTION,
    ) -> None:
        self._current_dir = current_dir
        self._now = now
        self._spawn = spawn
        self._next_run = next_run
        self._renew = renew
        self._notify = notify
        self._watchdog_s = watchdog_s
        self._max_concurrent = max_concurrent
        self._log_retention = log_retention

        self._configs: dict[str, JobConfig] = {cfg.id: cfg for cfg in jobs}
        self._states: dict[str, JobRunState] = {
            cfg.id: JobRunState(id=cfg.id) for cfg in self._configs.values()
        }
        self._handles: dict[str, JobHandle] = {}
        self._deadlines: dict[str, float] = {}
        # Per-job remaining auth retries; seeded lazily from cfg.retries.
        self._retries_left: dict[str, int] = {}
        # Ids that enforce_timeouts has terminated; reap classifies as "timeout".
        self._timed_out: set[str] = set()
        # Actual log file path per running job, stashed at fire time so reap can
        # record the real ".log" file (not just the per-job log directory).
        self._log_paths: dict[str, Path] = {}
        # Serialized transitions from the most recent reload(), surfaced via
        # snapshot().last_reload so the CLI can read the last reload report.
        self._last_reload: tuple | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_state(self, job_id: str) -> JobRunState:
        rs = self._states.get(job_id)
        if rs is None:
            rs = JobRunState(id=job_id)
            self._states[job_id] = rs
        return rs

    def _compute_next_run(self, cfg: JobConfig, after_epoch: float) -> float:
        after_dt = _epoch_to_utc(after_epoch)
        nxt = self._next_run(cfg.cron, cfg.timezone, after_dt)
        return nxt.timestamp()

    def _reschedule(self, cfg: JobConfig, after_epoch: float) -> None:
        rs = self._ensure_state(cfg.id)
        if cfg.enabled:
            nxt = self._compute_next_run(cfg, after_epoch)
        else:
            nxt = None
        self._states[cfg.id] = replace(rs, next_run_at=nxt)

    def _prune_logs(self, job_id: str) -> None:
        log_dir = self._current_dir / "logs" / job_id
        if not log_dir.is_dir():
            return
        logs = sorted(
            (p for p in log_dir.glob("*.log") if p.is_file()),
            key=lambda p: p.name,
        )
        excess = len(logs) - self._log_retention
        for old in logs[:excess] if excess > 0 else []:
            try:
                old.unlink()
            except OSError:  # noqa: PERF203 — best-effort cleanup
                log.debug("could not prune old log %s", old)

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    def schedule_all(self) -> None:
        """Compute ``next_run_at`` for every job (None for disabled jobs)."""
        now = self._now()
        for cfg in self._configs.values():
            self._reschedule(cfg, now)

    def fire_due(self) -> None:
        """Spawn every enabled, due, non-running job up to the concurrency cap."""
        now = self._now()
        for cfg in self._configs.values():
            if len(self._handles) >= self._max_concurrent:
                break
            if not cfg.enabled or cfg.id in self._handles:
                continue
            rs = self._ensure_state(cfg.id)
            if rs.next_run_at is None or rs.next_run_at > now:
                continue

            ts = int(now)
            log_path = self._current_dir / "logs" / cfg.id / f"{ts}.log"
            handle = self._spawn(cfg, log_path)
            self._handles[cfg.id] = handle
            self._deadlines[cfg.id] = now + cfg.timeout_s
            self._log_paths[cfg.id] = log_path
            self._states[cfg.id] = replace(
                rs,
                running_pid=handle.pid,
                running_pgid=handle.pgid,
                started_at=now,
            )
            self._prune_logs(cfg.id)

    def reap(self) -> None:
        """Collect finished children, classify outcomes, and reschedule."""
        now = self._now()
        for job_id, handle in list(self._handles.items()):
            code = handle.poll()
            if code is None:
                continue

            cfg = self._configs.get(job_id)
            rs = self._ensure_state(job_id)
            # Cron rescheduling is anchored to when this run STARTED, not when
            # we happened to reap it — otherwise reap latency would skew the
            # schedule forward by a whole tick.
            run_start = rs.started_at if rs.started_at is not None else now
            timed_out = job_id in self._timed_out
            self._timed_out.discard(job_id)
            log_path = self._log_paths.pop(job_id, None)
            del self._handles[job_id]
            self._deadlines.pop(job_id, None)

            if timed_out:
                status = "timeout"
            elif code == 0:
                status = "ok"
            elif code == EXIT_AUTH_FAILED:
                status = "auth-failed"
            else:
                status = "failed"

            rs = replace(
                rs,
                last_run_at=now,
                last_exit_code=code,
                last_status=status,
                last_log=str(log_path) if log_path is not None else None,
                running_pid=None,
                running_pgid=None,
                started_at=None,
            )
            self._states[job_id] = rs

            if cfg is None:
                continue

            self._handle_outcome(cfg, status, code, now, run_start)

    def _handle_outcome(
        self, cfg: JobConfig, status: str, code: int, now: float, run_start: float
    ) -> None:
        """Apply retry / reschedule / notify policy after a child terminates.

        Normal rescheduling is anchored to ``run_start`` (the cron occurrence
        that just fired); the fast auth retry is anchored to ``now``.
        """
        if status == "ok":
            self._retries_left[cfg.id] = cfg.retries
            self._reschedule(cfg, run_start)
            return

        if status == "auth-failed":
            remaining = self._retries_left.get(cfg.id, cfg.retries)
            if remaining > 0:
                if self._renew is not None:
                    self._renew()
                self._retries_left[cfg.id] = remaining - 1
                rs = self._ensure_state(cfg.id)
                self._states[cfg.id] = replace(
                    rs, next_run_at=now + cfg.retry_delay_s
                )
                return
            # Retries exhausted: terminal auth failure.
            self._retries_left[cfg.id] = cfg.retries
            self._reschedule(cfg, run_start)
            self._emit_failure(cfg, status, code)
            return

        # Generic failure / timeout: never retry immediately.
        self._retries_left[cfg.id] = cfg.retries
        self._reschedule(cfg, run_start)
        self._emit_failure(cfg, status, code)

    def _emit_failure(self, cfg: JobConfig, status: str, code: int) -> None:
        if self._notify is None:
            return
        try:
            self._notify(
                "scheduler.job_failed",
                job_id=cfg.id,
                status=status,
                exit_code=code,
            )
        except Exception:  # noqa: BLE001 — notify must never break a tick
            log.exception("notify failed for job %s", cfg.id)

    def enforce_timeouts(self) -> None:
        """Terminate any running job past its deadline; reap records 'timeout'."""
        now = self._now()
        for job_id, handle in list(self._handles.items()):
            deadline = self._deadlines.get(job_id)
            if deadline is not None and now >= deadline:
                self._timed_out.add(job_id)
                handle.terminate()

    def _ingest_run_reports(self) -> None:
        """Merge any pending manual run-report markers into ``_states``.

        Manual ``schwab jobs run`` invocations drop a marker under
        ``<current>/reports/``; the scheduler (sole authoritative writer of
        state.json) drains them here so a manual run's last_run is recorded
        and not clobbered by this tick's ``save_state``.
        """
        reports = drain_run_reports(self._current_dir)
        for job_id, report in reports.items():
            run_at = report.get("last_run_at")
            if not isinstance(run_at, (int, float)):
                # A marker with no usable run time can't record a run; skip it
                # rather than clobbering a valid last_run_at with None.
                continue
            rs = self._ensure_state(job_id)
            self._states[job_id] = replace(
                rs,
                last_run_at=run_at,
                last_status=report.get("last_status"),
                last_exit_code=report.get("last_exit_code"),
            )

    def tick(self) -> None:
        """One scheduling cycle: ingest reports, reap, enforce timeouts, fire, persist."""
        self._ingest_run_reports()
        self.reap()
        self.enforce_timeouts()
        self.fire_due()
        save_state(self._current_dir, self.snapshot())

    def now(self) -> float:
        """Return the scheduler's current time per its injected ``now`` seam."""
        return self._now()

    def next_wakeup(self) -> float:
        """Return the soonest moment the loop should wake: min(next_run, watchdog)."""
        now = self._now()
        watchdog = now + self._watchdog_s
        soonest = None
        for rs in self._states.values():
            if rs.next_run_at is not None:
                soonest = rs.next_run_at if soonest is None else min(soonest, rs.next_run_at)
        if soonest is None:
            return watchdog
        return min(soonest, watchdog)

    # ------------------------------------------------------------------
    # Reload
    # ------------------------------------------------------------------

    def reload(
        self,
        jobs: Iterable[JobConfig],
        *,
        invalid: dict[str, str] | None = None,
    ) -> list[Transition]:
        """Diff a fresh set of configs against current state.

        Running jobs are never terminated by a reload — only their scheduling
        is updated. Invalid-but-previously-loaded jobs keep their prior
        ``next_run_at`` (``outdated``); invalid never-seen jobs become
        ``error``. See module docstring / tests for the full matrix.
        """
        invalid = invalid or {}
        new_configs = {cfg.id: cfg for cfg in jobs}
        now = self._now()

        prior_ids = set(self._configs)
        all_ids = sorted(prior_ids | set(new_configs) | set(invalid))

        transitions: list[Transition] = []
        for job_id in all_ids:
            transitions.append(
                self._reload_one(job_id, new_configs, invalid, prior_ids, now)
            )

        # Stash a serialized copy so snapshot()/save_state can surface the last
        # reload report (transitions are frozen dataclasses -> plain dicts).
        self._last_reload = tuple(asdict(t) for t in transitions)

        return transitions

    def _current_old_label(self, job_id: str) -> str:
        """Label describing a job's current loaded state for a Transition.old."""
        if job_id in self._handles:
            return "running"
        rs = self._states.get(job_id)
        cfg = self._configs.get(job_id)
        if cfg is not None and not cfg.enabled:
            return "disabled"
        if rs is None:
            return "idle"
        if rs.next_run_at is not None:
            return "scheduled"
        # Loaded but never scheduled (e.g. no next_run_at yet) -> idle.
        return "idle"

    def _reload_one(
        self,
        job_id: str,
        new_configs: dict[str, JobConfig],
        invalid: dict[str, str],
        prior_ids: set[str],
        now: float,
    ) -> Transition:
        was_loaded = job_id in prior_ids

        if job_id in invalid:
            error = invalid[job_id]
            if was_loaded:
                # Keep the prior schedule + config + handle untouched.
                rs = self._states.get(job_id)
                return Transition(
                    id=job_id,
                    old=self._current_old_label(job_id),
                    new="outdated",
                    next_run_at=rs.next_run_at if rs else None,
                    error=error,
                )
            return Transition(id=job_id, old="error", new="error", error=error)

        new_cfg = new_configs.get(job_id)
        if new_cfg is None:
            # Previously loaded, gone from new set, not invalid -> unload.
            old = self._current_old_label(job_id)
            self._configs.pop(job_id, None)
            rs = self._states.get(job_id)
            if rs is not None:
                self._states[job_id] = replace(rs, next_run_at=None)
            # Running handle is intentionally NOT terminated here.
            return Transition(id=job_id, old=old, new="unloaded", next_run_at=None)

        # new_cfg is a valid config.
        if not was_loaded:
            self._configs[job_id] = new_cfg
            self._ensure_state(job_id)
            self._reschedule(new_cfg, now)
            return Transition(
                id=job_id,
                old="absent",
                new="updated",
                next_run_at=self._states[job_id].next_run_at,
            )

        old_cfg = self._configs[job_id]
        old_label = self._current_old_label(job_id)
        self._configs[job_id] = new_cfg

        if not new_cfg.enabled:
            rs = self._ensure_state(job_id)
            self._states[job_id] = replace(rs, next_run_at=None)
            return Transition(
                id=job_id, old=old_label, new="disabled", next_run_at=None
            )

        if old_cfg == new_cfg:
            rs = self._ensure_state(job_id)
            return Transition(
                id=job_id,
                old=old_label,
                new="unchanged",
                next_run_at=rs.next_run_at,
            )

        # Config changed: reschedule (never touch a running handle).
        self._reschedule(new_cfg, now)
        return Transition(
            id=job_id,
            old=old_label,
            new="updated",
            next_run_at=self._states[job_id].next_run_at,
        )

    # ------------------------------------------------------------------
    # Lifecycle / snapshot
    # ------------------------------------------------------------------

    def terminate_children(
        self,
        grace_s: float = SHUTDOWN_GRACE_S,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Gracefully stop every running child, then SIGKILL survivors.

        Shutdown path (runs on the main thread): SIGTERM every running child,
        poll up to ``grace_s`` wall-clock seconds for them to exit, then SIGKILL
        any that are still alive. Finally mark each terminated job's state
        ``interrupted`` (running fields cleared) and drop its handle/deadline so
        a subsequent snapshot()/save_state never persists a phantom running pid.

        ``sleep`` is injected so tests can drive the grace loop without real
        sleeping. Wall-clock deadlines use :func:`time.monotonic` rather than the
        injected ``now`` seam, since shutdown is real-time regardless of the
        scheduling clock.
        """
        terminated_ids = list(self._handles.keys())
        if not terminated_ids:
            return

        for handle in self._handles.values():
            handle.terminate()

        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline:
            if all(h.poll() is not None for h in self._handles.values()):
                break
            sleep(_TERM_POLL_S)

        for handle in self._handles.values():
            if handle.poll() is None:
                handle.kill()

        # Coherent shutdown state: clear running bookkeeping for every job we
        # signalled so the final snapshot reflects reality.
        now = self._now()
        for job_id in terminated_ids:
            rs = self._ensure_state(job_id)
            self._states[job_id] = replace(
                rs,
                last_status="interrupted",
                last_run_at=now,
                running_pid=None,
                running_pgid=None,
                started_at=None,
            )
        self._handles.clear()
        self._deadlines.clear()
        self._log_paths.clear()
        self._timed_out.clear()

    def snapshot(self) -> SchedulerState:
        """Return an immutable snapshot of the current run state."""
        return SchedulerState(
            jobs=dict(self._states),
            last_reload=self._last_reload,
            updated_at=self._now(),
        )
