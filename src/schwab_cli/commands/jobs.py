"""CLI surface for running scheduled jobs: ``schwab jobs run <id>``.

Resolves a job id to its promoted config under ``jobs/.current/<id>.json`` and
delegates execution to :func:`schwab_cli.server.jobs.runner.execute_job`,
propagating that job's exit code as the CLI's exit code.
"""
from __future__ import annotations

import re
from pathlib import Path

import typer

from schwab_cli import paths
from schwab_cli.server.jobs import config
from schwab_cli.server.jobs.config import JobConfig, JobConfigError
from schwab_cli.server.jobs.runner import execute_job

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
