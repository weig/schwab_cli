import httpx
import respx

from schwab_cli.api.accounts import get_account, get_positions, list_accounts
from schwab_cli.api.client import SchwabClient
from schwab_cli.config import Config
from schwab_cli.session import Session


def _client() -> SchwabClient:
    cfg = Config(client_id="cid", client_secret="csec", redirect_uri="https://127.0.0.1:8443")
    session = Session(
        access_token="atok", refresh_token="rtok",
        expires_at=1_000_000, refresh_token_expires_at=2_000_000,
    )
    return SchwabClient(cfg, session)


@respx.mock
def test_list_accounts_returns_all_with_positions():
    route = respx.get("https://api.schwabapi.com/trader/v1/accounts").mock(
        return_value=httpx.Response(200, json=[
            {"securitiesAccount": {"accountNumber": "12345678", "type": "MARGIN"}},
            {"securitiesAccount": {"accountNumber": "87654321", "type": "CASH"}},
        ]),
    )
    got = list_accounts(_client())
    assert len(got) == 2
    assert got[0]["securitiesAccount"]["accountNumber"] == "12345678"
    assert route.calls.last.request.url.params["fields"] == "positions"


@respx.mock
def test_get_account_resolves_and_fetches_one():
    respx.get("https://api.schwabapi.com/trader/v1/accounts/accountNumbers").mock(
        return_value=httpx.Response(200, json=[
            {"accountNumber": "12345678", "hashValue": "HASH_A"},
        ]),
    )
    detail = {"securitiesAccount": {"accountNumber": "12345678", "type": "MARGIN"}}
    route = respx.get("https://api.schwabapi.com/trader/v1/accounts/HASH_A").mock(
        return_value=httpx.Response(200, json=detail),
    )
    got = get_account(_client(), "12345678")
    assert got == detail
    assert route.calls.last.request.url.params["fields"] == "positions"


@respx.mock
def test_get_positions_all_accounts_aggregates():
    respx.get("https://api.schwabapi.com/trader/v1/accounts").mock(
        return_value=httpx.Response(200, json=[
            {"securitiesAccount": {
                "accountNumber": "12345678",
                "positions": [{"instrument": {"symbol": "AAPL"}, "longQuantity": 10}],
            }},
            {"securitiesAccount": {
                "accountNumber": "87654321",
                "positions": [{"instrument": {"symbol": "MSFT"}, "longQuantity": 5}],
            }},
        ]),
    )
    rows = get_positions(_client(), None)
    assert len(rows) == 2
    symbols = {(r["_account"], r["instrument"]["symbol"]) for r in rows}
    assert symbols == {("12345678", "AAPL"), ("87654321", "MSFT")}


@respx.mock
def test_get_positions_filtered_by_account():
    respx.get("https://api.schwabapi.com/trader/v1/accounts/accountNumbers").mock(
        return_value=httpx.Response(200, json=[
            {"accountNumber": "12345678", "hashValue": "HASH_A"},
        ]),
    )
    respx.get("https://api.schwabapi.com/trader/v1/accounts/HASH_A").mock(
        return_value=httpx.Response(200, json={"securitiesAccount": {
            "accountNumber": "12345678",
            "positions": [{"instrument": {"symbol": "AAPL"}, "longQuantity": 10}],
        }}),
    )
    rows = get_positions(_client(), "12345678")
    assert len(rows) == 1
    assert rows[0]["instrument"]["symbol"] == "AAPL"
    assert rows[0]["_account"] == "12345678"


@respx.mock
def test_get_positions_handles_account_without_positions_key():
    respx.get("https://api.schwabapi.com/trader/v1/accounts").mock(
        return_value=httpx.Response(200, json=[
            {"securitiesAccount": {"accountNumber": "12345678"}},  # no "positions"
        ]),
    )
    rows = get_positions(_client(), None)
    assert rows == []
