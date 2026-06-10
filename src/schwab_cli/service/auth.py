from __future__ import annotations

import time

from schwab_cli import auth_delegate
# Re-export the Layer-1 auth exceptions through the service package so
# Layer-3 interfaces (commands/MCP/REST) import them from the service,
# never from `api.client` directly — keeping the layer boundary clean.
from schwab_cli.api.client import ApiError as ApiError
from schwab_cli.api.client import SessionExpired as SessionExpired
from schwab_cli.config import Config
from schwab_cli.service import ServiceError
from schwab_cli.session import Session
from schwab_cli.session import load as load_session

__all__ = [
    "ApiError",
    "SessionExpired",
    "NotConfigured",
    "NotAuthenticated",
    "get_session",
]

# Ask the daemon for a refresh when the access token is within this many
# seconds of expiry.
_EXPIRY_SKEW_SECONDS = 60


class NotConfigured(ServiceError):
    """Raised when no config exists on disk."""


class NotAuthenticated(ServiceError):
    """Raised when no session file exists on disk."""


def get_session(cfg: Config) -> Session:
    """Return a Session with a usable access token — READ-ONLY.

    The daemon's TokenManager is the single owner of token writes; this
    function never runs an OAuth exchange and never writes session.json.

    - Load ``session.json``; if missing -> raise :class:`NotAuthenticated`.
    - If the access token is comfortably valid, return it as-is.
    - Otherwise ask the daemon to refresh (in-process TokenManager when
      running inside the daemon; ``POST /auth/refresh`` from CLI/worker
      processes) and return the re-read session.
    - If the daemon can't deliver (down, or the refresh token is dead)
      -> raise :class:`SessionExpired` for the user to handle.

    NEVER spawns webauto / a browser, and NEVER writes the session file.
    """
    session = load_session()
    if session is None:
        raise NotAuthenticated

    now = int(time.time())
    if session.expires_at - _EXPIRY_SKEW_SECONDS > now:
        # Access token still good.
        return session

    if session.refresh_token_expires_at <= now:
        raise SessionExpired("Session expired. Run `schwab_cli auth --force`.")

    fresh = auth_delegate.request_refresh(
        on_unreachable=auth_delegate.automated_unreachable_notifier(),
    )
    if fresh is not None and fresh.expires_at - _EXPIRY_SKEW_SECONDS > now:
        return fresh

    raise SessionExpired(
        "Access token expired and the daemon could not refresh it. "
        "Ensure `schwab server` is running, or run `schwab_cli auth`."
    )
