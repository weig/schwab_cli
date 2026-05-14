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


# ---------------------------------------------------------------------
# resolve_auth_result — the access-token layer (D14)
# ---------------------------------------------------------------------

from unittest.mock import patch
from schwab_cli.oauth import OAuthAuthorizationError, resolve_auth_result


@respx.mock
def test_resolve_code_result_calls_exchange_code():
    """kind="code" → POSTs to the token endpoint and returns the TokenResponse."""
    respx.post(TOKEN_URL).respond(
        200, json={"access_token": "A", "refresh_token": "R", "expires_in": 1800},
    )
    result = {"kind": "code", "code": "C0.xyz", "state": "S"}
    tr = resolve_auth_result(_cfg(), result)
    assert tr == TokenResponse(access_token="A", refresh_token="R", expires_in=1800)
    # And it actually hit the token endpoint with the right body.
    call = respx.calls.last
    assert b"code=C0.xyz" in call.request.content
    assert b"grant_type=authorization_code" in call.request.content


def test_resolve_token_result_returns_directly_without_http_call():
    """kind="token" must NOT touch the network — the upstream did the
    exchange for us. Future ``AuthServerHandler`` lives here."""
    result = {
        "kind": "token",
        "access_token": "AT",
        "refresh_token": "RT",
        "expires_in": 3600,
    }
    with patch("schwab_cli.oauth.httpx.post") as posted:
        tr = resolve_auth_result(_cfg(), result)
    posted.assert_not_called()
    assert tr == TokenResponse(access_token="AT", refresh_token="RT", expires_in=3600)


def test_resolve_error_result_raises_oauth_authorization_error():
    """kind="error" → OAuthAuthorizationError with code + description."""
    result = {
        "kind": "error",
        "error": "access_denied",
        "error_description": "user rejected consent",
        "state": "S",
    }
    with pytest.raises(OAuthAuthorizationError) as exc:
        resolve_auth_result(_cfg(), result)
    assert exc.value.error == "access_denied"
    assert exc.value.description == "user rejected consent"
    assert str(exc.value) == "access_denied: user rejected consent"


def test_resolve_error_result_without_description():
    result = {
        "kind": "error",
        "error": "server_error",
        "error_description": None,
        "state": "S",
    }
    with pytest.raises(OAuthAuthorizationError) as exc:
        resolve_auth_result(_cfg(), result)
    assert exc.value.description is None
    assert str(exc.value) == "server_error"


def test_resolve_unknown_kind_raises_oauth_error():
    """Defensive — should be unreachable given the type union is
    exhaustive, but guards future variants added without updating this
    layer."""
    result = {"kind": "future_variant", "stuff": "..."}
    with pytest.raises(OAuthError, match="unknown AuthResult kind"):
        resolve_auth_result(_cfg(), result)


@respx.mock
def test_resolve_code_result_propagates_http_errors():
    """Transport-level errors from the code branch propagate unchanged —
    caller handles them separately from OAuthAuthorizationError."""
    respx.post(TOKEN_URL).respond(400, json={"error": "invalid_grant"})
    result = {"kind": "code", "code": "C0.xyz", "state": "S"}
    with pytest.raises(httpx.HTTPStatusError):
        resolve_auth_result(_cfg(), result)


def test_oauth_authorization_error_subclasses_oauth_error():
    """Callers that catch OAuthError get OAuthAuthorizationError too;
    they can choose to handle the auth-vs-transport distinction separately
    if needed."""
    e = OAuthAuthorizationError("access_denied", "rejected")
    assert isinstance(e, OAuthError)
