import pytest

from schwab_cli.config import Config
from schwab_cli.oauth import OAuthError, TokenResponse, build_auth_url


def _cfg(**kwargs):
    base = dict(
        client_id="cid",
        client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
    )
    base.update(kwargs)
    return Config(**base)


def test_build_auth_url_includes_required_params():
    url = build_auth_url(_cfg())
    assert url.startswith("https://api.schwabapi.com/v1/oauth/authorize?")
    assert "response_type=code" in url
    assert "client_id=cid" in url
    # redirect_uri must be URL-encoded
    assert "redirect_uri=https%3A%2F%2F127.0.0.1%3A8443" in url


def test_token_response_parse_accepts_full_payload():
    tr = TokenResponse.parse({
        "access_token": "a",
        "refresh_token": "r",
        "expires_in": 1800,
        "scope": "ignored",
    })
    assert tr == TokenResponse(access_token="a", refresh_token="r", expires_in=1800)


def test_token_response_parse_coerces_expires_in_to_int():
    tr = TokenResponse.parse({
        "access_token": "a",
        "refresh_token": "r",
        "expires_in": "1800",
    })
    assert tr.expires_in == 1800


@pytest.mark.parametrize("missing", ["access_token", "refresh_token", "expires_in"])
def test_token_response_parse_raises_on_missing_field(missing):
    full = {"access_token": "a", "refresh_token": "r", "expires_in": 1800}
    full.pop(missing)
    with pytest.raises(OAuthError, match=missing):
        TokenResponse.parse(full)


def test_token_response_is_frozen():
    tr = TokenResponse(access_token="a", refresh_token="r", expires_in=1)
    with pytest.raises(Exception):
        tr.access_token = "x"  # type: ignore[misc]
