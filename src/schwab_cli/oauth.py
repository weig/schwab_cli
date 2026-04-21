from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

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


def build_auth_url(cfg: Config) -> str:
    return f"{AUTH_URL}?" + urlencode({
        "response_type": "code",
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
    })
