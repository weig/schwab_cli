import httpx
import pytest
import respx

from schwab_cli.config import Config
from schwab_cli.oauth import OAuthError, TOKEN_URL, TokenResponse, build_auth_url, exchange_code, refresh


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


@respx.mock
def test_exchange_code_posts_basic_auth_and_form_body():
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={
            "access_token": "atok", "refresh_token": "rtok", "expires_in": 1800
        })
    )
    tr = exchange_code(_cfg(), code="ABC123")
    assert tr == TokenResponse(access_token="atok", refresh_token="rtok", expires_in=1800)
    req = route.calls.last.request
    # Basic auth header: base64("cid:csec")
    assert req.headers["Authorization"].startswith("Basic ")
    body = dict(httpx.QueryParams(req.content.decode()))
    assert body == {
        "grant_type": "authorization_code",
        "code": "ABC123",
        "redirect_uri": "https://127.0.0.1:8443",
    }


@respx.mock
def test_refresh_posts_refresh_token_grant():
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={
            "access_token": "new_a", "refresh_token": "new_r", "expires_in": 1800
        })
    )
    tr = refresh(_cfg(), refresh_token="old_r")
    assert tr.access_token == "new_a"
    body = dict(httpx.QueryParams(route.calls.last.request.content.decode()))
    assert body == {"grant_type": "refresh_token", "refresh_token": "old_r"}


@respx.mock
def test_exchange_code_raises_on_4xx():
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )
    with pytest.raises(httpx.HTTPStatusError):
        exchange_code(_cfg(), code="bad")


@respx.mock
def test_refresh_raises_on_4xx():
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(401, json={"error": "invalid_client"})
    )
    with pytest.raises(httpx.HTTPStatusError):
        refresh(_cfg(), refresh_token="r")


@respx.mock
def test_exchange_code_raises_oauth_error_on_missing_field_in_200_response():
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "a", "refresh_token": "r"})
    )
    with pytest.raises(OAuthError, match="expires_in"):
        exchange_code(_cfg(), code="ABC")


@respx.mock
def test_refresh_raises_on_network_error():
    respx.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("nope"))
    with pytest.raises(httpx.RequestError):
        refresh(_cfg(), refresh_token="r")
