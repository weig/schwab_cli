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
    # Proactive auth: if the refresh token is within 24h of expiry,
    # do a full re-auth (via configured webauto-cli) BEFORE we burn
    # the 1-hour sleep_until_ny. This is the primary protection; the
    # reactive retry below is the safety net.
    _ensure_refresh_token_lifetime(notifier, audit)
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

    # Reactive auth retry: any child that exited with EXIT_AUTH_FAILED
    # (rc=2) signals that proactive auth didn't save us — re-auth
    # synchronously and re-dispatch JUST those children with
    # skip_wait=True (the timing wait already happened). One retry
    # budget; if the retry's auth fails or the re-spawned children
    # still come back rc=2, we accept it as unrecoverable.
    results = _maybe_retry_auth_failed(
        results=results,
        binary=binary,
        all_jobs=jobs,
        child_timeout_s=child_timeout_s,
        notifier=notifier,
        audit=audit,
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


# Minimum refresh-token lifetime required at scheduler-start. Schwab's
# refresh token has a 7-day TTL; the daily run can take ~13h (sleep
# until NY 17:00 ET + the actual work). Requiring 24h headroom means
# the run can complete even if the refresh token would have expired
# mid-day, AND we trigger proactive re-auth one day before the cliff
# so the user gets a healthy buffer rather than a midnight retry storm.
_PROACTIVE_REFRESH_MIN_LIFETIME_S = 24 * 3600


def _ensure_refresh_token_lifetime(notifier, audit) -> None:
    """Proactive auth: if the saved refresh token will expire inside
    the next ``_PROACTIVE_REFRESH_MIN_LIFETIME_S`` seconds, invoke
    ``auto_login_command`` *before* spawning any children so the run
    has guaranteed token coverage.

    No-op when the refresh token has plenty of life left. When auth
    runs and fails, we emit ``scheduler.proactive_auth_failed`` and
    return — the existing ``_ensure_token_valid`` and child paths
    still get a chance with whatever's in session.json.

    When ``auto_login_command`` is not configured but the refresh
    token is marginal, we emit ``scheduler.proactive_auth_skipped``
    (best-effort: the children may still succeed if the access token
    happens to be fresh enough).
    """
    try:
        from schwab_cli import config as config_module
        from schwab_cli.session import load as load_session
    except ImportError as e:
        audit.error(f"proactive auth: ImportError: {e}")
        return

    cfg = config_module.load()
    if cfg is None:
        audit.error("proactive auth: no config")
        return
    try:
        session = load_session()
    except Exception as e:
        audit.error(f"proactive auth: session unreadable: {e}")
        return
    if session is None:
        audit.error("proactive auth: no session")
        return

    now = int(time.time())
    ttl_s = session.refresh_token_expires_at - now
    if ttl_s >= _PROACTIVE_REFRESH_MIN_LIFETIME_S:
        audit.info(
            f"proactive auth check: refresh token TTL {ttl_s // 3600}h "
            f"(>= {_PROACTIVE_REFRESH_MIN_LIFETIME_S // 3600}h threshold); skip"
        )
        return

    audit.info(
        f"proactive auth check: refresh token TTL {ttl_s // 3600}h "
        f"(< {_PROACTIVE_REFRESH_MIN_LIFETIME_S // 3600}h threshold)"
    )
    if cfg.auto_login_command is None:
        audit.warning(
            "proactive auth: no auto_login_command configured; "
            "best-effort continue with existing session"
        )
        try:
            notifier.emit(
                "scheduler.proactive_auth_skipped",
                reason="no auto_login_command configured",
                refresh_ttl_hours=ttl_s // 3600,
            )
        except Exception as alert_err:
            audit.error(f"alert dispatch failed: {alert_err}")
        return

    audit.info("proactive auth: invoking auto_login_command")
    try:
        notifier.emit(
            "scheduler.proactive_auth_invoked",
            refresh_ttl_hours=ttl_s // 3600,
        )
    except Exception:
        pass

    try:
        from schwab_cli.auth_flows import perform_full_auth

        new_session = perform_full_auth(cfg)
        new_ttl_h = (new_session.refresh_token_expires_at - int(time.time())) // 3600
        audit.info(
            f"proactive auth: success; new refresh token TTL {new_ttl_h}h"
        )
        try:
            notifier.emit(
                "scheduler.proactive_auth_succeeded",
                new_refresh_ttl_hours=new_ttl_h,
            )
        except Exception as alert_err:
            audit.error(f"alert dispatch failed: {alert_err}")
    except Exception as e:
        audit.error(f"proactive auth: failed: {type(e).__name__}: {e}")
        try:
            notifier.emit(
                "scheduler.proactive_auth_failed",
                error=f"{type(e).__name__}: {e}",
            )
        except Exception as alert_err:
            audit.error(f"alert dispatch failed: {alert_err}")
        # Best-effort continue: children might still succeed using the
        # existing access token if it has any life left. The reactive
        # retry path catches the rest.


def _maybe_retry_auth_failed(
    *,
    results,
    binary: str,
    all_jobs,
    child_timeout_s: float,
    notifier,
    audit,
):
    """If any first-pass child exited with ``EXIT_AUTH_FAILED`` (rc=2),
    re-auth via ``perform_full_auth`` and re-dispatch just those
    children with ``--skip-wait``. Returns the merged result list.

    Tasks are required to be idempotent — re-running a successful job
    is fine (and the indices ``--max-age-days`` guard makes it a no-op
    anyway). We deliberately retry only the failed children to keep
    the second pass tight.
    """
    from schwab_cli._exit_codes import EXIT_AUTH_FAILED

    auth_failed = [r for r in results if r.returncode == EXIT_AUTH_FAILED]
    if not auth_failed:
        return results

    failed_names = [r.name for r in auth_failed]
    audit.warning(
        f"reactive auth retry: {len(auth_failed)} task(s) hit EXIT_AUTH_FAILED "
        f"({', '.join(failed_names)}); attempting re-auth"
    )
    try:
        notifier.emit(
            "scheduler.reactive_auth_retry",
            failed=", ".join(failed_names),
        )
    except Exception:
        pass

    # Re-auth.
    try:
        from schwab_cli import config as config_module
        from schwab_cli.auth_flows import perform_full_auth

        cfg = config_module.load()
        if cfg is None or cfg.auto_login_command is None:
            audit.error(
                "reactive auth retry: no auto_login_command configured; "
                "giving up"
            )
            try:
                notifier.emit(
                    "scheduler.auth_unrecoverable",
                    reason="no auto_login_command configured",
                )
            except Exception:
                pass
            return results

        perform_full_auth(cfg)
        audit.info("reactive auth retry: re-auth succeeded")
    except Exception as e:
        audit.error(
            f"reactive auth retry: re-auth failed: {type(e).__name__}: {e}"
        )
        try:
            notifier.emit(
                "scheduler.auth_unrecoverable",
                error=f"{type(e).__name__}: {e}",
            )
        except Exception:
            pass
        return results  # original results stand

    # Re-dispatch just the auth-failed jobs with skip_wait=True. We
    # rebuild their argv with --skip-wait appended; the dispatch loop
    # doesn't carry per-job customisation beyond that.
    retry_jobs = []
    for name, argv in all_jobs:
        if name in failed_names:
            retry_jobs.append((name, [*argv, "--skip-wait"]))

    audit.info(
        f"reactive auth retry: respawning {len(retry_jobs)} task(s) "
        f"with --skip-wait"
    )
    retry_results = _dispatch_parallel(
        retry_jobs, child_timeout_s=child_timeout_s, audit=audit,
    )

    # Merge: replace first-pass auth-failed entries with retry results,
    # preserving the original first-pass results for everything else.
    by_name = {r.name: r for r in results}
    for r in retry_results:
        by_name[r.name] = r
    return list(by_name.values())


def _ensure_token_valid(notifier) -> None:
    """Ask the daemon to refresh the access token if it's near expiry.

    Failure here is alerted but not fatal — the child processes will
    each surface their own auth errors more specifically. We freshen
    proactively so the children share one fresh token across the full
    run rather than each racing to refresh on first request.

    Delegates to the daemon's TokenManager (the single token writer)
    via :mod:`schwab_cli.auth_delegate` — this orchestrator never runs
    an OAuth exchange or writes session.json itself.
    """
    try:
        from schwab_cli import auth_delegate
        from schwab_cli.session import load as load_session
    except ImportError as e:
        # Narrowly scoped — only catch actual import problems, not
        # whatever runtime exception happens to occur below.
        try:
            notifier.emit(
                "scheduler.token_refresh_failed",
                error=f"ImportError: {e}",
            )
        except Exception:  # noqa: BLE001 — alerting must stay non-fatal
            pass
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

    def _unreachable(detail: str) -> None:
        notifier.emit("daemon.unreachable", detail=detail)

    fresh = auth_delegate.request_refresh(on_unreachable=_unreachable)
    if fresh is not None:
        notifier.emit(
            "scheduler.token_refreshed",
            expires_at=datetime.fromtimestamp(
                fresh.expires_at, tz=timezone.utc
            ).isoformat(),
        )
    else:
        notifier.emit(
            "scheduler.token_refresh_failed",
            reason="daemon could not refresh — ensure `schwab server` is "
                   "running, or run `schwab auth`",
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
