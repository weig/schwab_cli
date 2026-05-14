from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import httpx

from schwab_cli.config import Config

if TYPE_CHECKING:
    from schwab_cli.auth_handlers import AuthResult

AUTH_URL = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"


class OAuthError(Exception):
    """Raised on OAuth protocol failures (bad responses, missing fields)."""


class OAuthAuthorizationError(OAuthError):
    """The authorization step failed — Schwab returned an OAuth error
    response on the redirect, so we never got a ``code`` to exchange.

    Surfaced by :func:`resolve_auth_result` when it sees a ``kind="error"``
    :class:`AuthResult`. Carries the OAuth error code and (optional) human
    description so the caller can render a clear message to the user.
    """

    def __init__(self, error: str, description: str | None):
        self.error = error
        self.description = description
        msg = f"{error}: {description}" if description else error
        super().__init__(msg)


@dataclass(frozen=True)
class TokenResponse:
    access_token: str
    refresh_token: str
    expires_in: int

    @classmethod
    def parse(cls, data: dict) -> "TokenResponse":
        for field in ("access_token", "refresh_token", "expires_in"):
            if field not in data:
                raise OAuthError(f"token response missing '{field}'")
        return cls(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_in=int(data["expires_in"]),
        )


def build_auth_url(cfg: Config, *, state: str | None = None) -> str:
    params = {
        "response_type": "code",
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
    }
    if state:
        params["state"] = state
    return f"{AUTH_URL}?" + urlencode(params)


def exchange_code(cfg: Config, code: str) -> TokenResponse:
    resp = httpx.post(
        TOKEN_URL,
        auth=(cfg.client_id, cfg.client_secret),
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": cfg.redirect_uri,
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    return TokenResponse.parse(resp.json())


def refresh(cfg: Config, refresh_token: str) -> TokenResponse:
    resp = httpx.post(
        TOKEN_URL,
        auth=(cfg.client_id, cfg.client_secret),
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    return TokenResponse.parse(resp.json())


def resolve_auth_result(cfg: Config, result: "AuthResult") -> TokenResponse:
    """Turn an ``AuthResult`` into a :class:`TokenResponse`.

    The access-token layer: every variant of ``AuthResult`` funnels through
    here on its way to a saveable session.

    * ``kind="code"`` — exchange via Schwab's token endpoint.
    * ``kind="token"`` — already exchanged (future ``AuthServerHandler``);
      wrap and return WITHOUT an HTTP call.
    * ``kind="error"`` — raise :class:`OAuthAuthorizationError` carrying
      the OAuth error code and description.

    The caller (``commands/auth.run``) handles two kinds of exceptions:

    * :class:`OAuthAuthorizationError` from the error branch — surface the
      OAuth error to the user.
    * :class:`httpx.HTTPStatusError` / :class:`httpx.RequestError` /
      :class:`OAuthError` from the code branch — surface a transport error.
    """
    kind = result.get("kind")
    if kind == "code":
        return exchange_code(cfg, result["code"])
    if kind == "token":
        return TokenResponse(
            access_token=result["access_token"],
            refresh_token=result["refresh_token"],
            expires_in=int(result["expires_in"]),
        )
    if kind == "error":
        raise OAuthAuthorizationError(
            error=result["error"],
            description=result.get("error_description"),
        )
    raise OAuthError(f"unknown AuthResult kind: {kind!r}")
