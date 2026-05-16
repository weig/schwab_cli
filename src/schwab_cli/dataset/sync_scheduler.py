"""Unified Schwab Data Sync Service.

One launchd job fires daily, this module dispatches three independent
subprocesses in parallel:

1. ``schwab dataset update --group volatility``  → market-data branch,
   sleeps until NY 17:00 ET then samples chains + writes OHLCV.
2. ``schwab dataset accounts snapshot``           → records today's NAV
   per account at NY 17:00 ET.
3. ``schwab dataset update --indices
   --max-age-days 6 --anchor-hour 18``           → spaced one hour
   later. Skips silently when the upstream member set was synced
   inside the freshness window — avoids burst-requesting the
   constituent provider every day.

Each child runs in its own OS process so a single failure (network
blip, upstream 5xx, parser change) doesn't cascade. After all three
exit, the scheduler emits a Telegram alert listing any job that
returned non-zero.

Before dispatch, the scheduler refreshes the access token if it's
close to expiry. Children read the persisted session from disk, so a
mid-run refresh isn't a coordination problem — the parent's refresh
just lands in the session file before the children start.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone


JOB_MARKET_DATA = "market-data"
JOB_ACCOUNTS    = "accounts"
JOB_INDICES     = "indices"


@dataclass
class JobResult:
    name: str
    returncode: int
    duration_s: float
    stdout_tail: str  # last few lines for the alert body


def run_daily_sync(
    *,
    notifier=None,
    skip_wait: bool = False,
    binary_path: str | None = None,
) -> int:
    """Execute the full daily sync. Returns the process exit code:
    ``0`` when every job succeeded, ``1`` otherwise (so launchd's
    StandardErrorPath captures the failing state)."""
    if notifier is None:
        from schwab_cli.notify import Notifier
        notifier = Notifier.from_file()

    _ensure_token_valid(notifier)

    binary = binary_path or _resolve_binary()

    jobs = _job_commands(binary, skip_wait=skip_wait)
    results = _dispatch_parallel(jobs)

    failed = [r for r in results if r.returncode != 0]
    if failed:
        notifier.emit(
            "scheduler.job_failed",
            failed=", ".join(r.name for r in failed),
            details="\n".join(
                f"{r.name} (exit {r.returncode}, {r.duration_s:.0f}s):"
                f"\n{r.stdout_tail}"
                for r in failed
            ),
        )
        return 1
    return 0


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
    except Exception:
        return

    cfg = config_module.load()
    if cfg is None:
        return
    try:
        session = load_session()
    except Exception:
        session = None
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
    binary: str, *, skip_wait: bool,
) -> list[tuple[str, list[str]]]:
    """Return ``[(job_name, argv), ...]`` for the three subprocesses."""
    common = ["--skip-wait"] if skip_wait else []
    return [
        (JOB_MARKET_DATA, [
            binary, "dataset", "update", "--group", "volatility", *common,
        ]),
        (JOB_ACCOUNTS, [
            binary, "dataset", "accounts", "snapshot", *common,
        ]),
        (JOB_INDICES, [
            binary, "dataset", "update", "--indices",
            "--max-age-days", "6",
            "--anchor-hour", "18",
            *common,
        ]),
    ]


def _dispatch_parallel(
    jobs: list[tuple[str, list[str]]],
) -> list[JobResult]:
    """Start every job concurrently and wait for all to finish.

    Each child has its stdout + stderr captured separately so a
    failing job's tail can be embedded in the Telegram alert body.
    We log full output to stderr as it streams so launchd's log file
    still has the operational trail.
    """
    children: list[tuple[str, subprocess.Popen, float]] = []
    for name, argv in jobs:
        print(f"[scheduler] starting {name}: {' '.join(argv)}",
              file=sys.stderr, flush=True)
        # Inherit env so the child sees the same SCHWAB_CONFIG_DIR,
        # PATH, etc. that the cron launcher set up.
        p = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=os.environ.copy(),
        )
        children.append((name, p, time.time()))

    results: list[JobResult] = []
    for name, p, started_at in children:
        stdout, _ = p.communicate()
        elapsed = time.time() - started_at
        tail = _tail_lines(stdout or "", n=10)
        print(
            f"[scheduler] {name} exited {p.returncode} after "
            f"{elapsed:.1f}s\n{stdout}",
            file=sys.stderr, flush=True,
        )
        results.append(JobResult(
            name=name, returncode=p.returncode,
            duration_s=elapsed, stdout_tail=tail,
        ))
    return results


def _tail_lines(text: str, *, n: int) -> str:
    lines = (text or "").rstrip("\n").splitlines()
    return "\n".join(lines[-n:])


def _resolve_binary() -> str:
    """Look up the ``schwab`` console-script. Falls back to the legacy
    ``schwab_cli`` name and finally to a literal ``schwab`` (relying on
    PATH at child-process spawn time)."""
    return shutil.which("schwab") or shutil.which("schwab_cli") or "schwab"
