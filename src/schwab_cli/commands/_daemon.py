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
    cfg,
    logbook,
    notifier,
    *,
    full_auth=None,
):
    """One-shot full re-auth at daemon startup when the refresh token is
    already dead. Returns the fresh Session on success, ``None`` on
    failure.

    Runs :func:`schwab_cli.auth_flows.perform_full_auth` in-process (the
    same flow ``schwab auth --force`` drives) — we're still in the setup
    phase, before the server event loop and before the TokenManager
    threads exist, so a plain synchronous call is fine. Steady-state
    renewal afterwards belongs to the TokenManager's refresh track.

    ``full_auth`` is injectable for tests.
    """
    if full_auth is None:
        from schwab_cli.auth_flows import perform_full_auth
        full_auth = perform_full_auth

    def _notify(event: str, **fields) -> None:
        try:
            notifier.emit(event, **fields)
        except Exception:  # noqa: BLE001 — notification is best-effort
            pass

    try:
        fresh = full_auth(cfg)
    except Exception as e:  # noqa: BLE001 — surfaced via log + notification
        logbook.error(
            "daemon.startup_autologin_failed",
            error=f"{type(e).__name__}: {e}",
        )
        _notify("auth.auto_login.failed", trigger="startup")
        return None
    logbook.info("daemon.startup_autologin_succeeded")
    _notify("auth.auto_login.succeeded", trigger="startup")
    return fresh
