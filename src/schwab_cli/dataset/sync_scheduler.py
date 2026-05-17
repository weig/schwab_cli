"""Unified Schwab Data Sync Service.

One launchd job fires daily, this module dispatches three independent
subprocesses in parallel:

1. ``schwab dataset update --group volatility`` — market-data branch,
   sleeps until NY 17:00 ET then samples chains + writes OHLCV.
2. ``schwab dataset accounts snapshot`` — records today's NAV per
   account at NY 17:00 ET.
3. ``schwab dataset update --indices --max-age-days 6 --anchor-hour 18``
   — fires at the same moment as the other two but anchors its actual
   work to NY 18:00 ET, so the *outbound* HTTP request to the
   constituent provider is spaced an hour from the market-data hit.
   The child also short-circuits when the local ``subscriptions``
   table was last touched within the freshness window, so a daily
   dispatch still means a weekly (or whenever-membership-changes)
   provider hit. Note: that freshness check measures local writes,
   not upstream contact — stable membership means ``subscribed_at``
   doesn't move on a re-sync.

Each child runs in its own OS process so a single failure (network
blip, upstream 5xx, parser change) doesn't cascade. After all three
exit, the scheduler emits a Telegram alert listing any job that
returned non-zero and also writes a structured ``last_run.json`` so
``schwab doctor`` (or a future health check) can surface the failure
even when Telegram is unreachable.

Before dispatch, the scheduler refreshes the access token if it's
close to expiry. Children read the persisted session from disk; the
parent's atomic ``save_session`` lands before any child starts.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from schwab_cli.dataset.audit_log import scheduler_log
from schwab_cli.dataset.updaters import UPDATERS


_log = logging.getLogger(__name__)


# Re-exports — name tokens consumed by tests and Telegram alert
# formatting. Sourced from the updater registry so the constants
# can't drift from the canonical names used at dispatch time.
JOB_MARKET_DATA = "market-data"
JOB_ACCOUNTS    = "accounts"
JOB_INDICES     = "indices"


# Per-child wall-clock timeout. Each child internally sleep_until_ny
# to 17:00 (or 18:00 for indices). Worst case schedule: fire at NY
# 04:00 local → sleep ~13h to 17:00 → market-data work up to an hour →
# call it 16h. A timeout slightly above that protects launchd from a
# wedged child blocking the entire next-day run.
_DEFAULT_CHILD_TIMEOUT_S = 16 * 3600


@dataclass(frozen=True)
class JobResult:
    """One child process's outcome. Frozen so callers can't mutate
    historical state once collected."""
    name: str
    returncode: int
    duration_s: float
    stdout_tail: str       # last few lines for the alert body
    timed_out: bool = False

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0 and not self.timed_out


@dataclass
class RunSummary:
    """Aggregate of a single scheduler tick. Written to disk so an
    out-of-band health check can see whether the last run succeeded
    even if the Telegram channel was unreachable."""
    started_at: str         # ISO 8601 UTC
    finished_at: str        # ISO 8601 UTC
    overall_succeeded: bool
    jobs: list[dict[str, Any]] = field(default_factory=list)


def run_daily_sync(
    *,
    notifier=None,
    skip_wait: bool = False,
    binary_path: str | None = None,
    child_timeout_s: float = _DEFAULT_CHILD_TIMEOUT_S,
) -> int:
    """Execute the full daily sync. Returns ``0`` when every job
    succeeded, ``1`` otherwise.

    The return value is what launchd reports as the job's exit code;
    a non-zero exit alone does not capture *which* job failed, so we
    also (a) emit a structured Telegram alert and (b) write
    ``last_run.json`` next to the config file so subsequent health
    checks have an offline failure marker.

    Wraps the entire body in a top-level except so any unexpected
    crash (PATH issue, import failure, OS error) still fires a
    ``scheduler.crashed`` alert and writes a failure marker — the
    prior code would let the exception propagate, leaving the
    operator with no signal that the cron silently broke.
    """
    started_at = datetime.now(tz=timezone.utc)
    audit = scheduler_log()
    audit.info("start")

    if notifier is None:
        from schwab_cli.notify import Notifier
        notifier = Notifier.from_file()

    try:
        return _run_daily_sync_inner(
            notifier=notifier,
            skip_wait=skip_wait,
            binary_path=binary_path,
            child_timeout_s=child_timeout_s,
            started_at=started_at,
            audit=audit,
        )
    except Exception as e:
        import traceback as _tb
        tb = _tb.format_exc()
        _log.exception("scheduler crashed before completing run")
        audit.error(f"crashed: {type(e).__name__}: {e}")
        try:
            notifier.emit(
                "scheduler.crashed",
                error=f"{type(e).__name__}: {e}",
                traceback=tb,
            )
            audit.info("alert dispatched: scheduler.crashed")
        except Exception as alert_err:
            audit.error(f"alert dispatch failed: {alert_err}")
        try:
            _write_last_run(RunSummary(
                started_at=started_at.isoformat(timespec="seconds"),
                finished_at=datetime.now(tz=timezone.utc).isoformat(
                    timespec="seconds",
                ),
                overall_succeeded=False,
                jobs=[{
                    "name": "scheduler",
                    "returncode": -1,
                    "duration_s": (
                        datetime.now(tz=timezone.utc) - started_at
                    ).total_seconds(),
                    "timed_out": False,
                    "stdout_tail": tb,
                }],
            ))
        except Exception:
            pass
        return 1


def _run_daily_sync_inner(
    *,
    notifier,
    skip_wait: bool,
    binary_path: str | None,
    child_timeout_s: float,
    started_at: datetime,
    audit,
) -> int:
    _ensure_token_valid(notifier)

    binary = binary_path or _resolve_binary(notifier)
    audit.info(f"binary resolved: {binary}")

    jobs = _job_commands(binary, skip_wait=skip_wait, notifier=notifier)
    audit.info(
        f"scheduled {len(jobs)} task(s): "
        f"{', '.join(name for name, _ in jobs)}"
    )
    results = _dispatch_parallel(
        jobs, child_timeout_s=child_timeout_s, audit=audit,
    )

    failed = [r for r in results if not r.succeeded]
    succeeded = [r for r in results if r.succeeded]
    finished_at = datetime.now(tz=timezone.utc)
    elapsed = (finished_at - started_at).total_seconds()
    audit.info(
        f"summary: {len(results)} dispatched, "
        f"{len(succeeded)} succeeded, {len(failed)} failed, "
        f"{sum(1 for r in results if r.timed_out)} timed out "
        f"(elapsed {elapsed:.1f}s)"
    )

    if failed:
        try:
            notifier.emit(
                "scheduler.job_failed",
                failed=", ".join(r.name for r in failed),
                details="\n".join(
                    f"{r.name} ({_outcome_label(r)}, "
                    f"{r.duration_s:.0f}s):\n{r.stdout_tail}"
                    for r in failed
                ),
            )
            audit.info(
                f"alert dispatched: scheduler.job_failed "
                f"({', '.join(r.name for r in failed)})"
            )
        except Exception as alert_err:
            audit.error(f"alert dispatch failed: {alert_err}")

    summary = RunSummary(
        started_at=started_at.isoformat(timespec="seconds"),
        finished_at=finished_at.isoformat(timespec="seconds"),
        overall_succeeded=not failed,
        jobs=[
            {
                "name": r.name,
                "returncode": r.returncode,
                "duration_s": round(r.duration_s, 1),
                "timed_out": r.timed_out,
                "stdout_tail": r.stdout_tail,
            }
            for r in results
        ],
    )
    _write_last_run(summary)
    audit.info(
        f"finished (rc={0 if not failed else 1}, elapsed {elapsed:.1f}s)"
    )

    return 0 if not failed else 1


def _outcome_label(r: JobResult) -> str:
    if r.timed_out:
        return "timeout"
    return f"exit {r.returncode}"


# ---- token refresh ----------------------------------------------------


def _ensure_token_valid(notifier) -> None:
    """Refresh the access token if it's within 30 minutes of expiry.

    Failure here is alerted but not fatal — the child processes will
    each surface their own auth errors more specifically. We try to
    rotate proactively so the children share one fresh token across
    the full run rather than each racing to refresh on first request.
    """
    try:
        from schwab_cli import config as config_module
        from schwab_cli import oauth
        from schwab_cli.session import (
            Session, load as load_session, save as save_session,
        )
    except ImportError as e:
        # Narrowly scoped — only catch actual import problems, not
        # whatever runtime exception happens to occur below.
        notifier.emit(
            "scheduler.token_refresh_failed",
            error=f"ImportError: {e}",
        )
        return

    cfg = config_module.load()
    if cfg is None:
        notifier.emit(
            "scheduler.token_refresh_failed",
            reason="no config — run `schwab setup`",
        )
        return
    try:
        session = load_session()
    except Exception as e:
        notifier.emit(
            "scheduler.token_refresh_failed",
            error=f"session unreadable: {type(e).__name__}: {e}",
        )
        return
    if session is None:
        notifier.emit(
            "scheduler.token_refresh_failed",
            reason="no session — run `schwab auth`",
        )
        return

    now = int(time.time())
    refresh_window_s = 30 * 60
    if session.expires_at - now > refresh_window_s:
        return  # access token still has > 30 min left

    try:
        tr = oauth.refresh(cfg, session.refresh_token)
        new_session = Session.from_token_response(tr, now=now)
        save_session(new_session)
        notifier.emit(
            "scheduler.token_refreshed",
            expires_at=datetime.fromtimestamp(
                new_session.expires_at, tz=timezone.utc
            ).isoformat(),
        )
    except Exception as e:
        notifier.emit(
            "scheduler.token_refresh_failed",
            error=f"{type(e).__name__}: {e}",
        )


# ---- subprocess dispatch ---------------------------------------------


def _job_commands(
    binary: str, *, skip_wait: bool, notifier=None,
) -> list[tuple[str, list[str]]]:
    """Build ``[(name, argv), ...]`` from the pluggable
    :data:`schwab_cli.dataset.updaters.UPDATERS` registry. Adding a
    new daily task means appending one entry to that list — no edits
    to this file.

    A misbehaving updater whose ``spawn_argv`` raises is *skipped*
    rather than crashing the whole sync — the scheduler's job is to
    isolate failures, not amplify them. Skipped updaters surface as
    a notifier event so the operator can see which plugin broke.
    """
    out: list[tuple[str, list[str]]] = []
    for u in UPDATERS:
        try:
            out.append((u.name, u.spawn_argv(
                binary=binary, skip_wait=skip_wait,
            )))
        except Exception as e:
            _log.exception("updater %s spawn_argv failed", u.name)
            if notifier is not None:
                notifier.emit(
                    "scheduler.updater_skipped",
                    updater=u.name,
                    error=f"{type(e).__name__}: {e}",
                )
    return out


def _dispatch_parallel(
    jobs: list[tuple[str, list[str]]],
    *,
    child_timeout_s: float,
    audit=None,
) -> list[JobResult]:
    """Start every job concurrently and wait for all to finish.

    Per-child stdout + stderr is routed to its own temp file (not
    piped through the parent) so a chatty child can't fill the OS
    pipe buffer and deadlock its peers — that risk is real for the
    market-data job which streams chain progress for several minutes.

    Each child gets a hard wall-clock timeout; on timeout we kill the
    process group, mark the result as timed_out=True, and continue
    collecting peers.
    """
    children: list[tuple[str, subprocess.Popen, Path, float]] = []
    # Synthetic results for jobs whose Popen *itself* failed (e.g.
    # FileNotFoundError when the binary isn't on PATH). The old code
    # let that exception propagate out of the for-loop, taking the
    # whole sync with it. We now record a synthetic failure and keep
    # trying the remaining children — "one failure doesn't cascade".
    spawn_failures: list[JobResult] = []
    tmp_dir = Path(tempfile.mkdtemp(prefix="schwab-sync-"))
    for name, argv in jobs:
        _log.info("starting %s: %s", name, " ".join(argv))
        log_path = tmp_dir / f"{name}.log"
        try:
            log_fh = log_path.open("w")
            # Inherit env so the child sees the same SCHWAB_CONFIG_DIR /
            # PATH the cron launcher set up. start_new_session so we
            # can killpg without affecting the orchestrator itself.
            p = subprocess.Popen(
                argv,
                stdout=log_fh, stderr=subprocess.STDOUT,
                text=True, env=os.environ.copy(),
                start_new_session=True,
            )
            # Close our handle in the parent — the child kept its
            # own via fd inheritance. Prevents the file from staying
            # open if the parent crashes before .wait().
            log_fh.close()
            children.append((name, p, log_path, time.time()))
            if audit is not None:
                audit.info(f"task {name} started (pid={p.pid})")
        except (OSError, ValueError) as e:
            _log.exception("failed to spawn %s", name)
            if audit is not None:
                audit.error(
                    f"task {name} spawn failed: "
                    f"{type(e).__name__}: {e}"
                )
            spawn_failures.append(JobResult(
                name=name,
                returncode=-1,
                duration_s=0.0,
                stdout_tail=f"spawn failed: {type(e).__name__}: {e}",
                timed_out=False,
            ))

    results: list[JobResult] = list(spawn_failures)
    for name, p, log_path, started_at in children:
        timed_out = False
        try:
            p.wait(timeout=child_timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _log.error("%s exceeded %ss — killing process group",
                       name, child_timeout_s)
            try:
                os.killpg(os.getpgid(p.pid), 15)  # SIGTERM
            except (ProcessLookupError, PermissionError):
                pass
            # Brief grace period for cleanup before SIGKILL.
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(p.pid), 9)  # SIGKILL
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass

        elapsed = time.time() - started_at
        output = _read_log_safe(log_path)
        tail = _tail_lines(output, n=15)
        rc_eff = p.returncode if p.returncode is not None else -1
        _log.info("%s exited rc=%s after %.1fs (timed_out=%s)\n%s",
                  name, rc_eff, elapsed, timed_out, output)
        if audit is not None:
            outcome = "timed out" if timed_out else f"exit {rc_eff}"
            audit.info(
                f"task {name} finished, {outcome}, "
                f"elapsed {elapsed:.1f}s"
            )
        results.append(JobResult(
            name=name,
            returncode=rc_eff,
            duration_s=elapsed,
            stdout_tail=tail,
            timed_out=timed_out,
        ))

    # Best-effort cleanup. We deliberately keep the tempdir on error
    # paths handled above (logs already drained into JobResult.tail);
    # this branch only fires on the happy path.
    try:
        for _, _, log_path, _ in children:
            log_path.unlink(missing_ok=True)
        tmp_dir.rmdir()
    except OSError:
        pass

    return results


def _read_log_safe(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def _tail_lines(text: str, *, n: int) -> str:
    lines = (text or "").rstrip("\n").splitlines()
    return "\n".join(lines[-n:])


def _resolve_binary(notifier=None) -> str:
    """Look up the ``schwab`` console-script. Falls back to the
    legacy ``schwab_cli`` name. When neither is on PATH we emit a
    distinct ``scheduler.binary_not_found`` event and return the
    literal ``"schwab"`` — Popen will then fail loudly with
    FileNotFoundError, which the parent's outer handling treats as
    a fatal scheduler error."""
    for name in ("schwab", "schwab_cli"):
        path = shutil.which(name)
        if path:
            return path
    if notifier is not None:
        notifier.emit(
            "scheduler.binary_not_found",
            reason="neither `schwab` nor `schwab_cli` found on PATH",
        )
    return "schwab"


# ---- last-run marker --------------------------------------------------


def _last_run_path() -> Path:
    """Where ``last_run.json`` lives. Co-located with config so the
    health check can find it without extra discovery logic."""
    try:
        from schwab_cli.dataset.config import config_path
        return config_path().parent / "last_run.json"
    except Exception:
        return Path.home() / ".config" / "schwab_cli" / "last_run.json"


def _write_last_run(summary: RunSummary) -> None:
    """Persist the run summary atomically. Failure to write is logged
    but doesn't change the orchestrator's exit code — the alert path
    is the primary signal; this is the offline backup."""
    path = _last_run_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({
            "started_at":         summary.started_at,
            "finished_at":        summary.finished_at,
            "overall_succeeded":  summary.overall_succeeded,
            "jobs":               summary.jobs,
        }, indent=2))
        os.replace(tmp, path)
    except OSError as e:
        _log.warning("failed to write last_run.json: %s", e)
