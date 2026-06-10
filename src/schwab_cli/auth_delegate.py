"""Delegate token refresh to the daemon — the single token owner.

Rule 1 of the auth design: CLI / REST / MCP / service code never runs an
OAuth exchange itself. When a caller finds the access token expired it
asks the daemon to refresh and re-reads ``session.json``; if the daemon
can't deliver, the caller surfaces an auth failure to the user instead
of self-healing.

Two paths, picked automatically:

* **In the daemon process** — :func:`set_local_refresher` is called at
  startup with ``TokenManager.force_exchange``, so every service-layer
  consumer inside the daemon resolves through the in-process manager.
  (An HTTP hop to ourselves could deadlock the event loop, and the
  bare/REST modes don't even mount the /auth routes.)
* **Everywhere else** (CLI, spawned job workers) — ``POST
  {daemon}/auth/refresh`` (single-flight server-side), then re-read the
  session file the daemon just wrote.

``on_unreachable`` fires only for transport failures (daemon down), not
for a daemon that answered but couldn't refresh — automated callers use
it to send a ``daemon.unreachable`` notification per the design rule.
"""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING, Callable

import httpx

import schwab_cli.session as session_module

if TYPE_CHECKING:
    from schwab_cli.session import Session

DEFAULT_DAEMON_URL = "http://127.0.0.1:7234"
DEFAULT_TIMEOUT_S = 15.0

# Set by the jobs scheduler on spawned workers — marks an unattended
# process, which should NOTIFY (not just print) when the daemon is gone.
_AUTOMATED_ENV = "SCHWAB_JOBS_SCHEDULED"

# Guarded by _refresher_lock: written once at daemon startup, cleared on
# shutdown, read from every track/HTTP-worker thread.
_refresher_lock = threading.Lock()
_local_refresher: Callable[[], "Session | None"] | None = None


def set_local_refresher(fn: Callable[[], "Session | None"] | None) -> None:
    """Install the in-process refresh path (daemon startup only).

    Pass ``None`` to clear (daemon shutdown / tests). When set,
    :func:`request_refresh` never makes an HTTP call.
    """
    global _local_refresher
    with _refresher_lock:
        _local_refresher = fn


def daemon_url() -> str:
    """Daemon base URL; ``SCHWAB_DAEMON_URL`` overrides the default."""
    return os.environ.get("SCHWAB_DAEMON_URL", DEFAULT_DAEMON_URL).rstrip("/")


def automated_unreachable_notifier() -> Callable[[str], None] | None:
    """daemon.unreachable hook for unattended processes, else ``None``.

    Interactive CLI users see the auth failure directly; a scheduled
    worker (``SCHWAB_JOBS_SCHEDULED=1``, set by the jobs spawner) has
    nobody at the terminal, so it notifies through the Notifier instead.
    """
    if os.environ.get(_AUTOMATED_ENV) != "1":
        return None

    def _notify(detail: str) -> None:
        try:
            from schwab_cli.notify import Notifier

            Notifier.from_file().emit("daemon.unreachable", detail=detail)
        except Exception:  # noqa: BLE001 — notification is best-effort
            pass

    return _notify


def request_refresh(
    *,
    base_url: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    on_unreachable: Callable[[str], None] | None = None,
) -> "Session | None":
    """Ask the token owner for a fresh access token.

    Returns the fresh :class:`Session` on success, ``None`` on any
    failure (daemon unreachable, refresh rejected, session unreadable).
    Never raises and never writes — the daemon is the single writer.
    """
    with _refresher_lock:
        local = _local_refresher
    if local is not None:
        try:
            return local()
        except Exception:  # noqa: BLE001 — refresh failure ≡ None for callers
            return None

    url = f"{base_url or daemon_url()}/auth/refresh"
    try:
        # RequestError covers every transport failure incl. all timeout
        # flavors. HTTPStatusError is deliberately NOT in scope: we never
        # call raise_for_status() — non-200 is handled by the status
        # check below, keeping the "never raises" contract honest.
        resp = httpx.post(url, timeout=timeout_s)
    except httpx.RequestError as e:
        if on_unreachable is not None:
            try:
                on_unreachable(f"{type(e).__name__}: {e}")
            except Exception:  # noqa: BLE001 — notification is best-effort
                pass
        return None
    if resp.status_code != 200:
        # Daemon reachable but couldn't refresh (e.g. dead refresh token,
        # recovery underway). Its own tracks alert the user; we just fail.
        return None
    try:
        return session_module.load()
    except session_module.SessionError:
        return None
