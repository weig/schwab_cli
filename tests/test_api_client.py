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


def _fresh_session(access="fresh_atok") -> Session:
    return Session(
        access_token=access,
        refresh_token="rtok",
        expires_at=1_500_000,
        refresh_token_expires_at=2_000_000,
    )


@respx.mock
def test_401_triggers_delegated_refresh_and_retry(monkeypatch):
    """A 401 delegates to the token owner (auth_delegate / refresh hook)
    — the client NEVER runs an OAuth exchange itself anymore."""
    respx.get("https://api.schwabapi.com/trader/v1/accounts").mock(
        side_effect=[
            httpx.Response(401, json={"error": "invalid_token"}),
            httpx.Response(200, json=[{"accountNumber": "123"}]),
        ]
    )
    monkeypatch.setattr(
        "schwab_cli.auth_delegate.request_refresh",
        lambda **kw: _fresh_session(),
    )

    client = SchwabClient(_cfg(), _session(access="old_atok"))
    body = client.get("https://api.schwabapi.com/trader/v1/accounts")
    assert body == [{"accountNumber": "123"}]
    last_call = respx.routes[0].calls.last
    assert last_call.request.headers["Authorization"] == "Bearer fresh_atok"


@respx.mock
def test_401_uses_injected_refresh_hook_over_delegate(monkeypatch):
    """The daemon wires its TokenManager via refresh_hook — it must win
    over the default delegate path."""
    respx.get("https://api.schwabapi.com/trader/v1/accounts").mock(
        side_effect=[
            httpx.Response(401),
            httpx.Response(200, json=[{"accountNumber": "123"}]),
        ]
    )

    def _no_delegate(**kw):
        raise AssertionError("default delegate must not be used")

    monkeypatch.setattr(
        "schwab_cli.auth_delegate.request_refresh", _no_delegate,
    )
    client = SchwabClient(
        _cfg(), _session(access="old_atok"),
        refresh_hook=lambda: _fresh_session("hook_atok"),
    )
    body = client.get("https://api.schwabapi.com/trader/v1/accounts")
    assert body == [{"accountNumber": "123"}]
    last_call = respx.routes[0].calls.last
    assert last_call.request.headers["Authorization"] == "Bearer hook_atok"


@respx.mock
def test_401_then_refresh_fails_raises_session_expired(monkeypatch):
    respx.get("https://api.schwabapi.com/trader/v1/accounts").mock(
        return_value=httpx.Response(401, json={"error": "invalid_token"}),
    )
    monkeypatch.setattr(
        "schwab_cli.auth_delegate.request_refresh", lambda **kw: None,
    )

    client = SchwabClient(_cfg(), _session())
    with pytest.raises(SessionExpired, match="Session expired"):
        client.get("https://api.schwabapi.com/trader/v1/accounts")


@respx.mock
def test_401_twice_after_refresh_raises_session_expired(monkeypatch):
    respx.get("https://api.schwabapi.com/trader/v1/accounts").mock(
        side_effect=[
            httpx.Response(401),
            httpx.Response(401),
        ]
    )
    monkeypatch.setattr(
        "schwab_cli.auth_delegate.request_refresh",
        lambda **kw: _fresh_session(),
    )

    client = SchwabClient(_cfg(), _session())
    with pytest.raises(SessionExpired):
        client.get("https://api.schwabapi.com/trader/v1/accounts")


@respx.mock
def test_resolve_account_exact_match():
    respx.get("https://api.schwabapi.com/trader/v1/accounts/accountNumbers").mock(
        return_value=httpx.Response(200, json=[
            {"accountNumber": "12345678", "hashValue": "HASH_A"},
            {"accountNumber": "87654321", "hashValue": "HASH_B"},
        ]),
    )
    client = SchwabClient(_cfg(), _session())
    ids = client.resolve_account("12345678")
    assert ids.account_number == "12345678"
    assert ids.hash_value == "HASH_A"


@respx.mock
def test_resolve_account_by_suffix():
    respx.get("https://api.schwabapi.com/trader/v1/accounts/accountNumbers").mock(
        return_value=httpx.Response(200, json=[
            {"accountNumber": "12345678", "hashValue": "HASH_A"},
            {"accountNumber": "87654321", "hashValue": "HASH_B"},
        ]),
    )
    client = SchwabClient(_cfg(), _session())
    ids = client.resolve_account("5678")
    assert ids.account_number == "12345678"


@respx.mock
def test_resolve_account_ambiguous_raises():
    respx.get("https://api.schwabapi.com/trader/v1/accounts/accountNumbers").mock(
        return_value=httpx.Response(200, json=[
            {"accountNumber": "11115678", "hashValue": "HASH_A"},
            {"accountNumber": "22225678", "hashValue": "HASH_B"},
        ]),
    )
    client = SchwabClient(_cfg(), _session())
    with pytest.raises(ApiError, match="Multiple accounts match"):
        client.resolve_account("5678")


@respx.mock
def test_resolve_account_unknown_raises():
    respx.get("https://api.schwabapi.com/trader/v1/accounts/accountNumbers").mock(
        return_value=httpx.Response(200, json=[
            {"accountNumber": "12345678", "hashValue": "HASH_A"},
        ]),
    )
    client = SchwabClient(_cfg(), _session())
    with pytest.raises(ApiError, match="not found"):
        client.resolve_account("99999999")


@respx.mock
def test_resolve_account_caches_result():
    route = respx.get("https://api.schwabapi.com/trader/v1/accounts/accountNumbers").mock(
        return_value=httpx.Response(200, json=[
            {"accountNumber": "12345678", "hashValue": "HASH_A"},
        ]),
    )
    client = SchwabClient(_cfg(), _session())
    client.resolve_account("12345678")
    client.resolve_account("5678")
    assert route.call_count == 1  # second call used the cache
