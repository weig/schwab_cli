import httpx
import pytest
import respx

from schwab_cli.api.client import ApiError, SchwabClient, SessionExpired
from schwab_cli.config import Config
from schwab_cli.session import Session


def _cfg() -> Config:
    return Config(
        client_id="cid",
        client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
    )


def _session(access="atok", refresh="rtok") -> Session:
    return Session(
        access_token=access,
        refresh_token=refresh,
        expires_at=1_000_000,
        refresh_token_expires_at=2_000_000,
    )


@respx.mock
def test_get_sends_bearer_auth_and_returns_json():
    route = respx.get("https://api.schwabapi.com/trader/v1/accounts").mock(
        return_value=httpx.Response(200, json=[{"accountNumber": "123"}]),
    )
    client = SchwabClient(_cfg(), _session(access="atok"))
    body = client.get("https://api.schwabapi.com/trader/v1/accounts")
    assert body == [{"accountNumber": "123"}]
    assert route.calls.last.request.headers["Authorization"] == "Bearer atok"


@respx.mock
def test_get_with_params_encodes_query():
    route = respx.get("https://api.schwabapi.com/marketdata/v1/quotes").mock(
        return_value=httpx.Response(200, json={"AAPL": {"symbol": "AAPL"}}),
    )
    client = SchwabClient(_cfg(), _session())
    client.get(
        "https://api.schwabapi.com/marketdata/v1/quotes",
        params={"symbols": "AAPL,MSFT"},
    )
    assert "symbols=AAPL%2CMSFT" in str(route.calls.last.request.url)


@respx.mock
def test_500_raises_api_error():
    respx.get("https://api.schwabapi.com/trader/v1/accounts").mock(
        return_value=httpx.Response(500, text="internal server error"),
    )
    client = SchwabClient(_cfg(), _session())
    with pytest.raises(ApiError, match="500"):
        client.get("https://api.schwabapi.com/trader/v1/accounts")


@respx.mock
def test_network_error_raises_api_error():
    respx.get("https://api.schwabapi.com/trader/v1/accounts").mock(
        side_effect=httpx.ConnectError("dns failed"),
    )
    client = SchwabClient(_cfg(), _session())
    with pytest.raises(ApiError, match="network"):
        client.get("https://api.schwabapi.com/trader/v1/accounts")


@respx.mock
def test_401_triggers_refresh_and_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    respx.get("https://api.schwabapi.com/trader/v1/accounts").mock(
        side_effect=[
            httpx.Response(401, json={"error": "invalid_token"}),
            httpx.Response(200, json=[{"accountNumber": "123"}]),
        ]
    )
    respx.post("https://api.schwabapi.com/v1/oauth/token").mock(
        return_value=httpx.Response(200, json={
            "access_token": "fresh_atok",
            "refresh_token": "fresh_rtok",
            "expires_in": 1800,
        }),
    )

    client = SchwabClient(_cfg(), _session(access="old_atok"))
    body = client.get("https://api.schwabapi.com/trader/v1/accounts")
    assert body == [{"accountNumber": "123"}]
    last_call = respx.routes[0].calls.last
    assert last_call.request.headers["Authorization"] == "Bearer fresh_atok"


@respx.mock
def test_401_then_refresh_fails_raises_session_expired(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    respx.get("https://api.schwabapi.com/trader/v1/accounts").mock(
        return_value=httpx.Response(401, json={"error": "invalid_token"}),
    )
    respx.post("https://api.schwabapi.com/v1/oauth/token").mock(
        return_value=httpx.Response(401, json={"error": "invalid_grant"}),
    )

    client = SchwabClient(_cfg(), _session())
    with pytest.raises(SessionExpired, match="Session expired"):
        client.get("https://api.schwabapi.com/trader/v1/accounts")


@respx.mock
def test_401_twice_after_refresh_raises_session_expired(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    respx.get("https://api.schwabapi.com/trader/v1/accounts").mock(
        side_effect=[
            httpx.Response(401),
            httpx.Response(401),
        ]
    )
    respx.post("https://api.schwabapi.com/v1/oauth/token").mock(
        return_value=httpx.Response(200, json={
            "access_token": "fresh_atok",
            "refresh_token": "fresh_rtok",
            "expires_in": 1800,
        }),
    )

    client = SchwabClient(_cfg(), _session())
    with pytest.raises(SessionExpired):
        client.get("https://api.schwabapi.com/trader/v1/accounts")
