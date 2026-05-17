"""Append-only audit log for the Data Sync Service.

One file, ``~/.config/schwab_cli/scheduler.log``, captures every
state transition the scheduler and its children make. The operator
can ``tail -f`` it during a run or grep it after the fact.

Why a dedicated file instead of riding ``logging`` defaults: launchd
already captures the scheduler's stderr into ``dataset.log``, but
that's a binary-truncated buffer with no rotation, and it interleaves
debug output with crash tracebacks. The audit log is the clean trail:
INFO-only, line-per-event, ISO-timestamped, rotated at 10 MB.

Each line follows the shape::

    2026-05-17T15:23:01Z [scheduler] start
    2026-05-17T15:23:02Z [indices]   last sync 6.7d ago …
    2026-05-17T15:36:18Z [scheduler] summary: 3 dispatched …

Multiple processes (parent + children) can write concurrently —
``RotatingFileHandler`` opens in append mode, which on Unix is
atomic per ``write()`` call; events from different children
interleave at line boundaries cleanly.
"""
from __future__ import annotations

import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path


_AUDIT_LOGGER_NAME = "schwab_cli.audit"
# Module-level state — once installed, the handler stays for the
# lifetime of the process. Subsequent ``setup()`` calls are no-ops.
_INSTALLED = False


def audit_log_path() -> Path:
    """Co-located with the config + ``last_run.json`` so a fresh
    operator browsing ``~/.config/schwab_cli/`` finds everything in
    one place."""
    try:
        from schwab_cli.dataset.config import config_path
        return config_path().parent / "scheduler.log"
    except Exception:
        return Path.home() / ".config" / "schwab_cli" / "scheduler.log"


def setup() -> logging.Logger:
    """Idempotent installer. Adds a rotating file handler on the
    audit logger; subsequent calls return the same logger without
    duplicating the handler."""
    global _INSTALLED
    log = logging.getLogger(_AUDIT_LOGGER_NAME)
    if _INSTALLED:
        return log
    log.setLevel(logging.INFO)
    # Don't bubble up to root — audit lines shouldn't double-print
    # into launchd's stderr capture.
    log.propagate = False
    path = audit_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Best-effort — if the config dir can't be created, fall
        # back to a stderr-only audit so we still see events during
        # development.
        handler: logging.Handler = logging.StreamHandler()
    else:
        handler = RotatingFileHandler(
            path, maxBytes=10 * 1024 * 1024, backupCount=3,
            delay=True,
        )
    fmt = logging.Formatter(
        "%(asctime)sZ %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    # logging's default uses localtime; we want UTC so timestamps
    # are unambiguous across machines and DST transitions.
    fmt.converter = time.gmtime
    handler.setFormatter(fmt)
    log.addHandler(handler)
    _INSTALLED = True
    return log


def scheduler_log() -> "TaggedLogger":
    """Convenience for the orchestrator — prefixes every line with
    ``[scheduler]`` so the file is easy to scan when multiple tasks
    are interleaving."""
    return TaggedLogger(setup(), "scheduler")


def task_log(task_name: str) -> "TaggedLogger":
    """Convenience for child commands. Prefix with ``[<task>]`` so
    the audit file can be split by task with a simple grep."""
    return TaggedLogger(setup(), task_name)


class TaggedLogger:
    """Thin wrapper that prepends a ``[tag]`` to every message.

    Kept as a tiny class (instead of using ``logging.LoggerAdapter``)
    because the adapter API is awkward for our 3-method need and we
    don't want subclass surprises in future audit calls.
    """

    def __init__(self, base: logging.Logger, tag: str) -> None:
        self._base = base
        self._tag = f"[{tag}]"

    def info(self, msg: str, *args: object) -> None:
        self._base.info(f"{self._tag} {msg}", *args)

    def warning(self, msg: str, *args: object) -> None:
        self._base.warning(f"{self._tag} {msg}", *args)

    def error(self, msg: str, *args: object) -> None:
        self._base.error(f"{self._tag} {msg}", *args)


def reset_for_tests() -> None:
    """Test hook only — clears the installed flag so a test using
    monkeypatched config_path can re-attach the handler to the new
    location. Do not call from production code."""
    global _INSTALLED
    log = logging.getLogger(_AUDIT_LOGGER_NAME)
    for h in list(log.handlers):
        log.removeHandler(h)
    _INSTALLED = False
