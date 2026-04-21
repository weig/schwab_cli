import httpx
import respx

from schwab_cli.api.client import SchwabClient
from schwab_cli.api.quotes import get_quotes
from schwab_cli.config import Config
from schwab_cli.session import Session


def _client() -> SchwabClient:
    cfg = Config(client_id="cid", client_secret="csec", redirect_uri="https://127.0.0.1:8443")
    s = Session(
        access_token="atok", refresh_token="rtok",
        expires_at=1_000_000, refresh_token_expires_at=2_000_000,
    )
    return SchwabClient(cfg, s)


@respx.mock
def test_get_quotes_single_symbol():
    route = respx.get("https://api.schwabapi.com/marketdata/v1/quotes").mock(
        return_value=httpx.Response(200, json={
            "AAPL": {"symbol": "AAPL", "quote": {"lastPrice": 232.14}},
        }),
    )
    result = get_quotes(_client(), ["AAPL"])
    assert result["AAPL"]["quote"]["lastPrice"] == 232.14
    assert route.calls.last.request.url.params["symbols"] == "AAPL"


@respx.mock
def test_get_quotes_multi_symbol_comma_joined():
    route = respx.get("https://api.schwabapi.com/marketdata/v1/quotes").mock(
        return_value=httpx.Response(200, json={
            "AAPL": {"symbol": "AAPL"},
            "MSFT": {"symbol": "MSFT"},
        }),
    )
    get_quotes(_client(), ["AAPL", "MSFT", "NVDA"])
    assert route.calls.last.request.url.params["symbols"] == "AAPL,MSFT,NVDA"


@respx.mock
def test_get_quotes_unknown_symbol_passthrough():
    respx.get("https://api.schwabapi.com/marketdata/v1/quotes").mock(
        return_value=httpx.Response(200, json={
            "AAPL": {"symbol": "AAPL"},
            "errors": {"invalidSymbols": ["ZZZZZZ"]},
        }),
    )
    result = get_quotes(_client(), ["AAPL", "ZZZZZZ"])
    assert "AAPL" in result
    assert "errors" in result


@respx.mock
def test_get_quotes_empty_list_noop():
    result = get_quotes(_client(), [])
    assert result == {}
