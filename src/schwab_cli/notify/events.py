"""Event catalog for the notify package.

Event names are stable identifiers consumers subscribe to in
``notification.json``. Levels are used for message formatting and
as a coarse severity filter; channels can opt into any subset.

Adding a new event: extend :data:`EVENTS` with a ``(level, summary)``
tuple. Templates render ``summary`` plus a pretty-printed tail of
the event's ``fields`` dict.
"""

from __future__ import annotations

from typing import Literal

Level = Literal["info", "warning", "error"]

# Event-name → (level, one-line summary).
# Keys are the canonical event-name tokens used by
# ``Notifier.emit()`` and by ``notification.json`` `events` lists.
EVENTS: dict[str, tuple[Level, str]] = {
    "auth.auto_login.succeeded": (
        "info",
        "Schwab auto-login rotated the refresh token.",
    ),
    "auth.auto_login.failed": (
        "error",
        "Schwab auto-login FAILED. Manual re-auth required.",
    ),
    "auth.refresh_expiring": (
        "warning",
        "Schwab refresh token expiring soon; auto-login hasn't rotated yet.",
    ),
    "auth.access_token_lapsed": (
        "error",
        "Schwab access token expired and the exchange keeps failing; "
        "retrying every minute.",
    ),
    "auth.renewal_attempt_failed": (
        "warning",
        "Schwab refresh-token renewal checkpoint failed; "
        "waiting for the next checkpoint.",
    ),
    "auth.refresh_token_critical": (
        "error",
        "Schwab refresh token expires in <8h; proactive renewal stopped. "
        "Run `schwab auth --force`.",
    ),
    "auth.recovery_failing": (
        "error",
        "Schwab refresh token dead (invalid_grant); full re-auth keeps "
        "failing, retrying on backoff.",
    ),
    "auth.recovery_succeeded": (
        "info",
        "Schwab full re-auth recovered after invalid_grant.",
    ),
    "auth.manual_auth_required": (
        "error",
        "Schwab refresh token dead and auto-login is not configured. "
        "Run `schwab auth --force`.",
    ),
    "streamer.crash": (
        "error",
        "Schwab streamer WebSocket crashed and could not reconnect.",
    ),
    "daemon.start": (
        "info",
        "schwab_cli MCP daemon started.",
    ),
    "daemon.unreachable": (
        "error",
        "schwab_cli daemon unreachable — token refresh delegated nowhere. "
        "Ensure `schwab server` is running.",
    ),
    "daemon.stop": (
        "info",
        "schwab_cli MCP daemon stopped.",
    ),
    "scheduler.job_failed": (
        "error",
        "Schwab data sync — one or more daily jobs failed.",
    ),
    "scheduler.token_refreshed": (
        "info",
        "Schwab data sync — access token refreshed before dispatch.",
    ),
    "scheduler.token_refresh_failed": (
        "error",
        "Schwab data sync — access token refresh failed before dispatch.",
    ),
    "scheduler.binary_not_found": (
        "error",
        "Schwab data sync — `schwab` binary missing from PATH.",
    ),
    "scheduler.updater_skipped": (
        "warning",
        "Schwab data sync — an updater's spawn_argv() raised; skipped.",
    ),
    "scheduler.crashed": (
        "error",
        "Schwab data sync — orchestrator crashed before completing the run.",
    ),
    "scheduler.proactive_auth_invoked": (
        "info",
        "Schwab data sync — refresh token within 24h; invoking auto-login.",
    ),
    "scheduler.proactive_auth_succeeded": (
        "info",
        "Schwab data sync — proactive auto-login succeeded.",
    ),
    "scheduler.proactive_auth_failed": (
        "error",
        "Schwab data sync — proactive auto-login failed (best-effort continue).",
    ),
    "scheduler.proactive_auth_skipped": (
        "warning",
        "Schwab data sync — refresh token marginal but no auto_login_command "
        "configured; best-effort continue with existing session.",
    ),
    "scheduler.reactive_auth_retry": (
        "warning",
        "Schwab data sync — re-authing and respawning auth-failed tasks.",
    ),
    "scheduler.auth_unrecoverable": (
        "error",
        "Schwab data sync — auth dead; cannot recover this run.",
    ),
    "test.hello": (
        "info",
        "Test notification from schwab_cli.",
    ),
}


def level_of(event: str) -> Level:
    """Resolve an event name to its declared level. Unknown events
    fall back to ``"info"`` so third-party emitters (eventually)
    don't need to register in this catalog first."""
    entry = EVENTS.get(event)
    return entry[0] if entry is not None else "info"


def summary_of(event: str) -> str:
    entry = EVENTS.get(event)
    return entry[1] if entry is not None else event
