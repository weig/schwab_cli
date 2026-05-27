"""Shared daemon helpers used by ``commands/server.py``.

These two helpers were originally defined in the now-removed
``commands/mcp.py``. They live here so the ``server`` daemon (the only
daemon now) can reuse them:

* :func:`_resolve_log_file` — resolve the structured-log destination
  from the ``--log-file`` / ``--no-log-file`` flags.
* :func:`_attempt_startup_autologin` — one-shot OAuth rotation at daemon
  startup when the refresh token is already dead.

The default structured-log path is also exported here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path


DEFAULT_LOG_FILE = Path.home() / ".config" / "schwab_cli" / "mcp.log"


def resolve_log_file(log_file: str | None, no_log_file: bool) -> Path | None:
    """Resolve the structured-log destination.

    ``--no-log-file`` wins (events still go to stderr); an explicit
    ``--log-file`` is expanded; otherwise the default path is used.
    """
    if no_log_file:
        return None
    if log_file:
        return Path(log_file).expanduser()
    return DEFAULT_LOG_FILE


def attempt_startup_autologin(
    logbook,
    notifier,
    *,
    monitor_cls=None,
    session_loader=None,
):
    """One-shot rotation at daemon startup when the refresh token is
    already dead. Returns the freshly-loaded Session on success,
    ``None`` on failure.

    Reuses ``AuthMonitor.run_once`` so the subprocess, env, anti-thrash,
    and notification code is identical to the steady-state rotation path.
    Runs synchronously via ``asyncio.run`` because we're still in the
    setup phase — the server event loop hasn't started yet.

    Accepts ``monitor_cls`` / ``session_loader`` overrides for tests —
    the defaults import the real AuthMonitor and session loader.
    """
    if monitor_cls is None:
        from schwab_cli.mcp_server.auth_monitor import AuthMonitor
        monitor_cls = AuthMonitor
    if session_loader is None:
        from schwab_cli.session import load as load_session
        session_loader = load_session

    monitor = monitor_cls(logbook, notifier)
    result = asyncio.run(monitor.run_once(reason="startup"))
    if not result.ok:
        return None
    return session_loader()


# Backwards-compatible aliases — the old ``commands/mcp.py`` exposed
# these underscore-prefixed names; keep them so any lingering importer
# (and the migrated server code) can use either spelling.
_resolve_log_file = resolve_log_file
_attempt_startup_autologin = attempt_startup_autologin
