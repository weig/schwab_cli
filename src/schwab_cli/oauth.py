from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from schwab_cli.config import Config

AUTH_URL = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"


class OAuthError(Exception):
    """Raised on OAuth protocol failures (bad responses, missing fields)."""


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
