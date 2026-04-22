from datetime import datetime, timezone

import httpx
import respx

from schwab_cli.api.client import SchwabClient
from schwab_cli.api.history import get_history
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
    "empty": False,
    "previousClose": 142.30,
    "previousCloseDate": 1713312000000,
    "candles": [
        {
            "datetime": 1713398400000,
            "open": 142.50, "high": 144.10, "low": 141.90,
            "close": 143.20, "volume": 32450123,
        },
    ],
}


_START = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
_END = datetime(2024, 4, 22, 20, 0, 0, tzinfo=timezone.utc)


@respx.mock
def test_get_history_default_daily_params():
    route = respx.get("https://api.schwabapi.com/marketdata/v1/pricehistory").mock(
        return_value=httpx.Response(200, json=_SAMPLE),
    )
    result = get_history(
        _client(), "NVDA",
        frequency_type="daily", frequency=1,
        start=_START, end=_END,
    )
    assert result == _SAMPLE
    params = route.calls.last.request.url.params
    assert params["symbol"] == "NVDA"
    assert params["periodType"] == "year"
    assert params["frequencyType"] == "daily"
    assert params["frequency"] == "1"
    assert params["needPreviousClose"] == "true"
    assert params["needExtendedHoursData"] == "false"
    assert params["startDate"] == str(int(_START.timestamp() * 1000))
    assert params["endDate"] == str(int(_END.timestamp() * 1000))


@respx.mock
def test_get_history_minute_interval():
    route = respx.get("https://api.schwabapi.com/marketdata/v1/pricehistory").mock(
        return_value=httpx.Response(200, json=_SAMPLE),
    )
    get_history(
        _client(), "NVDA",
        frequency_type="minute", frequency=15,
        start=_START, end=_END,
    )
    params = route.calls.last.request.url.params
    assert params["periodType"] == "day"
    assert params["frequencyType"] == "minute"
    assert params["frequency"] == "15"


@respx.mock
def test_get_history_empty_passthrough():
    empty = {"symbol": "XYZZZ", "empty": True, "candles": []}
    respx.get("https://api.schwabapi.com/marketdata/v1/pricehistory").mock(
        return_value=httpx.Response(200, json=empty),
    )
    result = get_history(
        _client(), "XYZZZ",
        frequency_type="daily", frequency=1,
        start=_START, end=_END,
    )
    assert result == empty


@respx.mock
def test_get_history_honors_optional_flags():
    route = respx.get("https://api.schwabapi.com/marketdata/v1/pricehistory").mock(
        return_value=httpx.Response(200, json=_SAMPLE),
    )
    get_history(
        _client(), "NVDA",
        frequency_type="daily", frequency=1,
        start=_START, end=_END,
        need_previous_close=False,
        need_extended_hours=True,
    )
    params = route.calls.last.request.url.params
    assert params["needPreviousClose"] == "false"
    assert params["needExtendedHoursData"] == "true"


@respx.mock
def test_get_history_401_then_refresh_succeeds(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    respx.get("https://api.schwabapi.com/marketdata/v1/pricehistory").mock(
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
    result = get_history(
        _client(), "NVDA",
        frequency_type="daily", frequency=1,
        start=_START, end=_END,
    )
    assert result == _SAMPLE
