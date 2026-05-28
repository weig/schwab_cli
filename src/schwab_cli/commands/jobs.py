"""CLI surface for running scheduled jobs: ``schwab jobs run <id>``.

Resolves a job id to its promoted config under ``jobs/.current/<id>.json`` and
delegates execution to :func:`schwab_cli.server.jobs.runner.execute_job`,
propagating that job's exit code as the CLI's exit code.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import typer

from schwab_cli import paths
from schwab_cli.dataset.launchd import uninstall_all_schwab_plists
from schwab_cli.server.jobs import config
from schwab_cli.server.jobs.config import JobConfig, JobConfigError
from schwab_cli.server.jobs.defaults import write_default_jobs
from schwab_cli.server.jobs.runner import execute_job
from schwab_cli.server.jobs.runtime import (
    current_dir,
    jobs_dir,
    read_pidfile,
)
from schwab_cli.server.jobs.state import load_state

app = typer.Typer(
    help="Run promoted scheduled jobs by id (used by the scheduler and manually).",
    no_args_is_help=True,
)

# A job id maps directly to a filename stem; constrain it to a safe character
# set so it can never contain path separators or traversal sequences.
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def resolve_job_config(job_id: str, *, config_dir: Path | None = None) -> JobConfig:
    """Load the promoted config for ``job_id`` from ``jobs/.current/<id>.json``.

    Raises :class:`typer.BadParameter` if ``job_id`` is malformed, escapes the
    ``.current`` directory, has no promoted file, or has an invalid config.
    """
    if not _JOB_ID_RE.match(job_id):
        raise typer.BadParameter(
            f"invalid job id: {job_id!r} "
            "(allowed: letters, digits, '_' and '-', 1-64 chars)"
        )

    base = config_dir or paths.config_dir()
    current_dir = (base / "jobs" / ".current").resolve()
    path = (current_dir / f"{job_id}.json").resolve()
    # Defence in depth: confine the resolved file to the .current directory even
    # if the id pattern is ever loosened.
    if path.parent != current_dir:
        raise typer.BadParameter(f"invalid job id: {job_id!r} (path escapes job dir)")

    if not path.exists():
        raise typer.BadParameter(f"no such job: {job_id} (expected {path})")

    try:
        return config.parse_job(path)
    except JobConfigError as exc:
        raise typer.BadParameter(
            f"job {job_id!r} has an invalid config: {exc.message}"
        ) from exc


@app.command("run")
def run(job_id: str) -> None:
    """Run a single promoted job by id and exit with its return code."""
    cfg = resolve_job_config(job_id)
    rc = execute_job(cfg)
    raise typer.Exit(rc)


# ---------------------------------------------------------------------------
# Process liveness
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
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


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _server_running(current: Path) -> bool:
    """True when a pidfile exists under ``current`` and names a live process."""
    info = read_pidfile(current)
    if not info:
        return False
    pid = info.get("pid")
    if not isinstance(pid, int):
        return False
    return _pid_alive(pid)


def _derive_state(*, enabled: bool, running_pid: int | None, next_run_at: float | None) -> str:
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

    jobs: list[dict] = []
    for cfg in valid:
        run_state = scheduler_state.jobs.get(cfg.id)
        running_pid = run_state.running_pid if run_state else None
        next_run_at = run_state.next_run_at if run_state else None
        last_run_at = run_state.last_run_at if run_state else None
        last_status = run_state.last_status if run_state else None
        last_exit_code = run_state.last_exit_code if run_state else None

        edit_error = staging_errors.get(cfg.id)
        outdated = edit_error is not None

        jobs.append(
            {
                "id": cfg.id,
                "name": cfg.name,
                "enabled": cfg.enabled,
                "cron": cfg.cron,
                "timezone": cfg.timezone,
                "state": _derive_state(
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

    return {"jobs": jobs, "server_running": _server_running(current)}


def _fmt_ts(ts: float | None) -> str:
    """Format an epoch timestamp as a readable local-time string."""
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def render_status(payload: dict[str, Any]) -> str:
    """Render the status payload as a per-job multi-line stanza string."""
    jobs = payload.get("jobs", [])
    if not jobs:
        return "No promoted jobs."

    width = max(len(str(job["id"])) for job in jobs)
    lines: list[str] = []
    for job in jobs:
        job_id = str(job["id"])
        state = job["state"]
        outdated_tag = " (outdated)" if job.get("outdated") else ""
        header = (
            f'{job_id}:{" " * (width - len(job_id))} {state}{outdated_tag}, '
            f'cron "{job["cron"]}" {job["timezone"]}'
        )
        lines.append(header)

        if state == "scheduled" and job.get("next_run_at") is not None:
            lines.append(f"  next run {_fmt_ts(job['next_run_at'])}")
        if job.get("running_pid") is not None:
            lines.append(f"  running now (pid {job['running_pid']})")
        if job.get("last_run_at") is not None:
            lines.append(
                f"  last run {_fmt_ts(job['last_run_at'])} ({job.get('last_status')})"
            )
        if job.get("outdated"):
            lines.append(
                f"  ⚠ staged edit invalid, not applied: {job.get('edit_error')}"
            )

    return "\n".join(lines)


def render_reload_report(transitions: list[dict]) -> str:
    """Render one line per reload transition."""
    if not transitions:
        return "No changes."

    # "outdated" is intentionally included: an outdated edit was rejected and the
    # last-good version keeps running, so a next_run is still meaningful. The
    # next_run shown for an "outdated" transition is the still-running last-good
    # version's schedule — NOT the rejected staged edit's.
    _SHOW_NEXT = {"updated", "unchanged", "outdated"}
    lines: list[str] = []
    for t in transitions:
        job_id = t.get("id")
        old = t.get("old")
        new = t.get("new")
        line = f"{job_id}: {old} → {new}"
        if new in _SHOW_NEXT and t.get("next_run_at") is not None:
            line += f", next run {_fmt_ts(t['next_run_at'])}"
        if t.get("error"):
            line += f" ({t['error']})"
        lines.append(line)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI: list
# ---------------------------------------------------------------------------


@app.command("list")
def list_jobs() -> None:
    """List staged job configs and any validation errors (read-only).

    Always exits 0: invalid staged files are reported as ``INVALID`` lines but
    do not change the exit code, since listing never mutates anything.
    """
    valid, errors = config.load_jobs(jobs_dir())

    if not valid and not errors:
        typer.echo("No staged jobs.")
        raise typer.Exit(0)

    for cfg in valid:
        typer.echo(
            f'{cfg.id}: {cfg.name} '
            f'[{"enabled" if cfg.enabled else "disabled"}] '
            f'cron "{cfg.cron}" {cfg.timezone} type={cfg.type}'
        )
    for job_id, message in sorted(errors.items()):
        typer.echo(f"{job_id}: INVALID - {message}")

    raise typer.Exit(0)


# ---------------------------------------------------------------------------
# CLI: status
# ---------------------------------------------------------------------------


@app.command("status")
def status(
    json_output: bool = typer.Option(
        False, "--json", "-j", help="Emit the status payload as JSON."
    ),
) -> None:
    """Show the status of promoted jobs."""
    payload = status_payload()
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(render_status(payload))
    raise typer.Exit(0)


# ---------------------------------------------------------------------------
# CLI: reload / sync
# ---------------------------------------------------------------------------


def _reload_impl() -> None:
    """Send SIGHUP to a running server to reload staged configs."""
    current = current_dir()
    valid, errs = config.load_jobs(jobs_dir())

    info = read_pidfile(current)
    alive = bool(info) and isinstance(info.get("pid"), int) and _pid_alive(info["pid"])

    if not alive:
        typer.echo("server not running; staged configs will apply on next start.")
        if errs:
            typer.echo("Staged config errors:")
            for job_id, message in sorted(errs.items()):
                typer.echo(f"  {job_id}: {message}")
        else:
            typer.echo(f"{len(valid)} staged config(s) valid.")
        raise typer.Exit(1 if errs else 0)

    pid = info["pid"]

    # Capture the state's updated_at BEFORE signalling so we can detect that the
    # server has written a *fresh* reload report rather than re-reading a stale
    # one from a previous reload (last_reload is already non-None after the very
    # first reload, so its mere presence proves nothing).
    baseline = load_state(current).updated_at

    # PID-reuse tradeoff: between reading the pidfile and signalling, the pid
    # could in principle be recycled. Accepted for a single-user localhost
    # daemon — mirrors the note in state.reconcile_orphans.
    os.kill(pid, signal.SIGHUP)

    # Poll (bounded ~3s) until the server records a *new* reload report, i.e.
    # updated_at has advanced past the baseline AND a report is present.
    deadline = time.time() + 3.0
    fresh = False
    state = load_state(current)
    while time.time() < deadline:
        state = load_state(current)
        if state.updated_at != baseline and state.last_reload is not None:
            fresh = True
            break
        time.sleep(0.1)

    if not fresh:
        # Best-effort: the server did not respond within the timeout. Do not
        # present a possibly-stale prior report as if it were fresh.
        typer.echo("no response from server (reload may still be in progress).")
        raise typer.Exit(1)

    transitions = [dict(t) for t in state.last_reload]
    typer.echo(render_reload_report(transitions))
    bad = any(t.get("new") in ("outdated", "error") for t in transitions)
    raise typer.Exit(1 if bad else 0)


@app.command("reload")
def reload() -> None:
    """Reload staged job configs into a running server (sends SIGHUP)."""
    _reload_impl()


@app.command("sync")
def sync() -> None:
    """Alias for ``reload``: apply staged configs to a running server."""
    _reload_impl()


# ---------------------------------------------------------------------------
# CLI: enable / disable
# ---------------------------------------------------------------------------


def _set_enabled(job_id: str, enabled: bool) -> None:
    """Validate ``job_id``, rewrite its staged config's ``enabled`` flag atomically."""
    if not _JOB_ID_RE.match(job_id):
        raise typer.BadParameter(
            f"invalid job id: {job_id!r} "
            "(allowed: letters, digits, '_' and '-', 1-64 chars)"
        )

    staging = jobs_dir().resolve()
    path = (staging / f"{job_id}.json").resolve()
    if path.parent != staging:
        raise typer.BadParameter(f"invalid job id: {job_id!r} (path escapes job dir)")

    if not path.exists():
        typer.echo(f"no such job: {job_id} (expected {path})", err=True)
        raise typer.Exit(1)

    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        typer.echo(f"cannot read job {job_id!r}: {exc}", err=True)
        raise typer.Exit(1) from exc

    if not isinstance(raw, dict):
        typer.echo(f"job {job_id!r} is not a JSON object", err=True)
        raise typer.Exit(1)

    updated = {**raw, "enabled": enabled}
    tmp = path.parent / f".{path.name}.tmp"
    # Atomic write: temp file + os.replace, both guarded so a failure on either
    # the write or the rename cleans up the temp and reports a friendly error.
    # Mirrors state.save_state's pattern.
    try:
        tmp.write_text(json.dumps(updated, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        typer.echo(f"cannot write job {job_id}: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(f"{job_id}: {'enabled' if enabled else 'disabled'}")

    # Nudge a running server to pick up the change. PID-reuse tradeoff (the pid
    # could be recycled between read and signal) is accepted for a single-user
    # localhost daemon — mirrors the note in state.reconcile_orphans.
    current = current_dir()
    info = read_pidfile(current)
    if info and isinstance(info.get("pid"), int) and _pid_alive(info["pid"]):
        os.kill(info["pid"], signal.SIGHUP)


# ---------------------------------------------------------------------------
# CLI: init / migrate
# ---------------------------------------------------------------------------


@app.command("init")
def init() -> None:
    """Seed the three default job configs into the staging jobs dir.

    Existing files are never overwritten. Prints ``<stem>: created|exists``
    for each default and exits 0.
    """
    results = write_default_jobs(jobs_dir())
    for stem in sorted(results):
        typer.echo(f"{stem}: {results[stem]}")
    raise typer.Exit(0)


@app.command("migrate")
def migrate() -> None:
    """Cut over from the legacy launchd scheduler to server-run jobs.

    Tears down the old launchd scheduler FIRST, then seeds the default job
    configs. If the teardown fails, no defaults are written.
    """
    try:
        uninstall_all_schwab_plists()
    except RuntimeError as exc:
        typer.secho(
            f"migrate aborted: could not remove the legacy scheduler: {exc}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1) from exc

    results = write_default_jobs(jobs_dir())
    for stem in sorted(results):
        typer.echo(f"{stem}: {results[stem]}")

    typer.echo("")
    typer.echo("(re)start `schwab server` to pick up the jobs.")
    typer.echo("Check job status anytime with `schwab jobs status`.")
    raise typer.Exit(0)


@app.command("enable")
def enable(job_id: str) -> None:
    """Enable a staged job by id."""
    _set_enabled(job_id, True)


@app.command("disable")
def disable(job_id: str) -> None:
    """Disable a staged job by id."""
    _set_enabled(job_id, False)
