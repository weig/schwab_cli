from datetime import date

import httpx
import pytest
import respx

from schwab_cli.api.chains import get_chain
from schwab_cli.api.client import SchwabClient
from schwab_cli.config import Config
from schwab_cli.session import Session


def _client() -> SchwabClient:
    cfg = Config(
        client_id="cid", client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
    )
    s = Session(
        access_token="atok", refresh_token="rtok",
        expires_at=1_000_000, refresh_token_expires_at=2_000_000,
    )
    return SchwabClient(cfg, s)


_SAMPLE = {
    "symbol": "NVDA",
    "status": "SUCCESS",
    "underlying": {"symbol": "NVDA", "last": 142.35},
    "callExpDateMap": {},
    "putExpDateMap": {},
}


@respx.mock
def test_get_chain_default_params():
    route = respx.get("https://api.schwabapi.com/marketdata/v1/chains").mock(
        return_value=httpx.Response(200, json=_SAMPLE),
    )
    get_chain(_client(), "NVDA", from_date=date(2027, 1, 15), to_date=date(2027, 1, 15))
    params = route.calls.last.request.url.params
    assert params["symbol"] == "NVDA"
    assert params["contractType"] == "ALL"
    assert params["strategy"] == "SINGLE"
    assert params["includeUnderlyingQuote"] == "true"
    # default strike_count=10 → Schwab strikeCount=5 (per-side)
    assert params["strikeCount"] == "5"
    assert params["fromDate"] == "2027-01-15"
    assert params["toDate"] == "2027-01-15"
    assert "strike" not in params


@respx.mock
def test_get_chain_strike_count_rounds_up_for_odd():
    route = respx.get("https://api.schwabapi.com/marketdata/v1/chains").mock(
        return_value=httpx.Response(200, json=_SAMPLE),
    )
    get_chain(_client(), "NVDA", strike_count=5)
    # ceil(5/2) = 3
    assert route.calls.last.request.url.params["strikeCount"] == "3"


@respx.mock
def test_get_chain_with_explicit_strike():
    route = respx.get("https://api.schwabapi.com/marketdata/v1/chains").mock(
        return_value=httpx.Response(200, json=_SAMPLE),
    )
    get_chain(_client(), "NVDA", strike=250.0)
    assert route.calls.last.request.url.params["strike"] == "250.0"


def test_get_chain_rejects_zero_or_negative_strike_count():
    with pytest.raises(ValueError):
        get_chain(_client(), "NVDA", strike_count=0)
    with pytest.raises(ValueError):
        get_chain(_client(), "NVDA", strike_count=-5)


@respx.mock
def test_get_chain_contract_type_forwarded():
    route = respx.get("https://api.schwabapi.com/marketdata/v1/chains").mock(
        return_value=httpx.Response(200, json=_SAMPLE),
    )
    get_chain(_client(), "NVDA", contract_type="PUT")
    assert route.calls.last.request.url.params["contractType"] == "PUT"


@respx.mock
def test_get_chain_returns_response_dict():
    respx.get("https://api.schwabapi.com/marketdata/v1/chains").mock(
        return_value=httpx.Response(200, json=_SAMPLE),
    )
    result = get_chain(_client(), "NVDA")
    assert result == _SAMPLE


@respx.mock
def test_get_chain_empty_response_passthrough():
    empty = {"symbol": "XYZZZ", "status": "FAILED",
             "callExpDateMap": {}, "putExpDateMap": {}}
    respx.get("https://api.schwabapi.com/marketdata/v1/chains").mock(
        return_value=httpx.Response(200, json=empty),
    )
    result = get_chain(_client(), "XYZZZ")
    assert result["status"] == "FAILED"


@respx.mock
def test_get_chain_401_then_refresh_succeeds(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    respx.get("https://api.schwabapi.com/marketdata/v1/chains").mock(
        side_effect=[
            httpx.Response(401, json={}),
            httpx.Response(200, json=_SAMPLE),
        ],
    )
    respx.post("https://api.schwabapi.com/v1/oauth/token").mock(
        return_value=httpx.Response(200, json={
            "access_token": "new_at", "refresh_token": "new_rt",
            "expires_in": 1800,
        }),
    )
    result = get_chain(_client(), "NVDA")
    assert result == _SAMPLE
