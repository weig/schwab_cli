"""Execute a single scheduled job in the current process.

The runner translates a validated :class:`~schwab_cli.server.jobs.config.JobConfig`
into either an ``os.execvp`` of the ``schwab`` console-script (command jobs) or a
direct in-process call of a dotted Python callable (python jobs). It maps known
failure modes to the stable exit-code contract in
:mod:`schwab_cli._exit_codes` so the scheduler can react without parsing output.
"""
from __future__ import annotations

import importlib
import logging
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from schwab_cli._exit_codes import EXIT_AUTH_FAILED
from schwab_cli.api.client import SessionExpired
from schwab_cli.server.jobs.config import JobConfig
from schwab_cli.service.auth import NotAuthenticated

log = logging.getLogger(__name__)

# Conventional "command not found / cannot execute" exit code.
_EXIT_CANNOT_EXEC = 127

# Top-level modules a python job's dotted runner may never resolve into. These
# expose arbitrary code execution / process / filesystem primitives, so a
# malicious or careless job config must not be able to reach them.
_BLOCKED_RUNNER_MODULES = frozenset(
    {
        "builtins",
        "subprocess",
        "os",
        "sys",
        "importlib",
        "shutil",
        "socket",
        "ctypes",
        "posix",
        "nt",
    }
)


def resolve_binary() -> str:
    """Return the path to the ``schwab`` console-script.

    Resolution order:
      1. The console script co-located with the running interpreter
         (``Path(sys.executable).parent``). This is the critical case for the
         launchd daemon: launchd gives it a minimal PATH WITHOUT ``~/.local/bin``,
         so :func:`shutil.which` can't find ``schwab`` — but the script lives
         right next to ``sys.executable`` in the same venv/tool bin dir.
      2. ``shutil.which`` on PATH (covers shell / dev invocations).
      3. The literal ``"schwab"`` fallback (exec then fails loudly).
    """
    bindir = Path(sys.executable).parent
    for name in ("schwab", "schwab_cli"):
        candidate = bindir / name
        if candidate.exists():
            return str(candidate)
    for name in ("schwab", "schwab_cli"):
        path = shutil.which(name)
        if path:
            return path
    return "schwab"


def command_argv(cfg: JobConfig, *, binary: str | None = None) -> list[str]:
    """Build the argv list for a command-type job.

    Raises :class:`ValueError` if ``cfg`` is not a command job or carries an
    empty command.
    """
    if cfg.type != "command":
        raise ValueError(f"command_argv requires a command job; got type {cfg.type!r}")
    if not cfg.command:
        raise ValueError(f"command job {cfg.id!r} has an empty command")
    return [binary or resolve_binary(), *cfg.command]


def import_runner(dotted: str) -> Callable[..., Any]:
    """Import a dotted path and return the referenced callable.

    Splits on the final ``.`` into module path and attribute name. Raises
    :class:`ValueError` when the module cannot be imported, the attribute is
    missing, or the resolved object is not callable.
    """
    module_path, _, attr = dotted.rpartition(".")
    if not module_path or not attr:
        raise ValueError(f"invalid runner path: {dotted!r} (expected 'pkg.mod.fn')")

    top_level = module_path.split(".")[0]
    if top_level in _BLOCKED_RUNNER_MODULES:
        raise ValueError(f"runner module {module_path!r} is not permitted")

    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise ValueError(f"cannot import module for runner {dotted!r}: {exc}") from exc

    try:
        obj = getattr(module, attr)
    except AttributeError as exc:
        raise ValueError(f"runner {dotted!r} has no attribute {attr!r}") from exc

    if not callable(obj):
        raise ValueError(f"runner {dotted!r} is not callable")
    return obj


def _execute_command(cfg: JobConfig) -> int:
    """Replace the current process with the command job's argv.

    On success ``os.execvp`` does not return — the scheduler keeps monitoring
    the same PID by design. Only a failed exec returns here.
    """
    argv = command_argv(cfg)
    try:
        os.execvp(argv[0], argv)
    except OSError:
        log.exception("failed to exec command job %s: %r", cfg.id, argv)
        return _EXIT_CANNOT_EXEC
    # Unreachable on a successful exec (os.execvp never returns); present for
    # type-completeness only.
    return _EXIT_CANNOT_EXEC  # pragma: no cover


def _execute_python(cfg: JobConfig) -> int:
    """Call the python job's dotted runner in-process and map outcomes.

    Success is defined as the callable returning without raising; its return
    value is ignored.
    """
    fn = import_runner(cfg.runner)
    try:
        fn(*cfg.args, **cfg.kwargs)
        return 0
    except (SessionExpired, NotAuthenticated):
        log.warning("python job %s failed due to auth; signalling auth failure", cfg.id)
        return EXIT_AUTH_FAILED
    except SystemExit as exc:
        if isinstance(exc.code, int):
            return exc.code
        return 0 if exc.code is None else 1
    except Exception:
        log.exception("python job %s raised an unexpected error", cfg.id)
        return 1


def execute_job(cfg: JobConfig) -> int:
    """Run ``cfg`` and return its process-exit code."""
    if cfg.type == "command":
        return _execute_command(cfg)
    if cfg.type == "python":
        return _execute_python(cfg)
    log.error("job %s has unknown type %r; cannot execute", cfg.id, cfg.type)
    return 1


def _run_command_blocking(cfg: JobConfig) -> int:
    """Run a command job as a child process and return its exit code.

    Unlike :func:`_execute_command` (which ``execvp``s), this keeps the current
    process alive so a manual ``schwab jobs run`` can record an outcome after
    the job finishes.
    """
    argv = command_argv(cfg)
    try:
        completed = subprocess.run(argv, check=False)  # noqa: S603
    except OSError:
        log.exception("failed to run command job %s: %r", cfg.id, argv)
        return _EXIT_CANNOT_EXEC
    return completed.returncode


def run_job_blocking(cfg: JobConfig) -> int:
    """Run ``cfg`` without replacing the current process; return its exit code.

    The manual-run counterpart to :func:`execute_job`: command jobs run via
    :func:`subprocess.run` (no ``execvp``) so the caller survives to record the
    outcome; python jobs behave exactly like the python branch of
    :func:`execute_job`.
    """
    if cfg.type == "command":
        return _run_command_blocking(cfg)
    if cfg.type == "python":
        return _execute_python(cfg)
    log.error("job %s has unknown type %r; cannot execute", cfg.id, cfg.type)
    return 1


# ---------------------------------------------------------------------------
# Worker spawning (used by the scheduler)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JobHandle:
    """A handle to a spawned worker process and its process group.

    The worker is started in its own session/process group so the whole tree
    can be signalled together — a command job's ``execvp`` child and any
    grandchildren go down with a single ``killpg``.
    """

    pid: int
    pgid: int
    poll: Callable[[], int | None]
    terminate: Callable[[], None]
    kill: Callable[[], None]


def spawn_worker(
    cfg: JobConfig,
    *,
    log_path: Path,
    binary: str | None = None,
) -> JobHandle:
    """Spawn ``schwab jobs run <id>`` as a detached worker process.

    Output (stdout + stderr merged) is appended to ``log_path``; its parent
    directory is created if needed. The child runs in a new session so its
    process group can be signalled independently of the daemon.
    """
    argv = [binary or resolve_binary(), "jobs", "run", cfg.id]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # The child inherits its own copy of this fd; the parent must close its
    # copy unconditionally once Popen has forked (success or failure) so the
    # daemon does not leak a file descriptor per spawned job.
    log_file = open(log_path, "ab")  # noqa: SIM115 — closed in finally below
    try:
        proc = subprocess.Popen(
            argv,
            start_new_session=True,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            # Mark the child as scheduler-driven so `jobs run` execvp's (the
            # scheduler records the outcome on reap) rather than writing a
            # manual run-report marker.
            env={**os.environ, "SCHWAB_JOBS_SCHEDULED": "1"},
        )
    finally:
        log_file.close()

    pgid = os.getpgid(proc.pid)
    return JobHandle(
        pid=proc.pid,
        pgid=pgid,
        poll=proc.poll,
        terminate=lambda: os.killpg(pgid, signal.SIGTERM),
        kill=lambda: os.killpg(pgid, signal.SIGKILL),
    )
