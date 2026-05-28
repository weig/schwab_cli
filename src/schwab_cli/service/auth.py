from __future__ import annotations

import time

import httpx

from schwab_cli import oauth
# Re-export the Layer-1 auth exceptions through the service package so
# Layer-3 interfaces (commands/MCP/REST) import them from the service,
# never from `api.client` directly — keeping the layer boundary clean.
from schwab_cli.api.client import ApiError as ApiError
from schwab_cli.api.client import SessionExpired as SessionExpired
from schwab_cli.config import Config
from schwab_cli.service import ServiceError
from schwab_cli.session import Session
from schwab_cli.session import load as load_session
from schwab_cli.session import save as save_session

__all__ = [
    "ApiError",
    "SessionExpired",
    "NotConfigured",
    "NotAuthenticated",
    "get_session",
]

# Refresh the access token when it is within this many seconds of expiry.
_EXPIRY_SKEW_SECONDS = 60


class NotConfigured(ServiceError):
    """Raised when no config exists on disk."""


class NotAuthenticated(ServiceError):
    """Raised when no session file exists on disk."""


def get_session(cfg: Config) -> Session:
    """Return a Session with a usable access token.

    - Load ``session.json``; if missing -> raise :class:`NotAuthenticated`.
    - If the access token is within a small skew of expiry AND the refresh
      token is still valid, mint a fresh access token via
      ``oauth.refresh`` (pure HTTP), persist it, and return the new Session.
    - If the refresh token is dead (expired, or ``oauth.refresh`` raises
      ``oauth.OAuthError`` / httpx errors) -> raise :class:`SessionExpired`.

    NEVER spawns webauto / a browser. Pure HTTP token mint only.
    """
    session = load_session()
    if session is None:
        raise NotAuthenticated

    now = int(time.time())
    if session.expires_at - _EXPIRY_SKEW_SECONDS > now:
        # Access token still good — no mint needed.
        return session

    # Access token is at/near expiry. Refresh token must still be valid
    # to mint a new one without an interactive re-auth.
    if session.refresh_token_expires_at <= now:
        raise SessionExpired("Session expired. Run `schwab_cli auth --force`.")

    try:
        tr = oauth.refresh(cfg, session.refresh_token)
    except (httpx.HTTPStatusError, httpx.RequestError, oauth.OAuthError) as e:
        raise SessionExpired(
            "Session expired. Run `schwab_cli auth --force`."
        ) from e

    fresh = Session.from_token_response(tr, now=now)
    save_session(fresh)
    return fresh
