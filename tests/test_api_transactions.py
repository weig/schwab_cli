from datetime import datetime, timezone

import httpx
import respx

from schwab_cli.api.client import SchwabClient
from schwab_cli.api.transactions import get_all_transactions, get_transactions
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


_ACCT_NUMS = [
    {"accountNumber": "12340756", "hashValue": "hash0756"},
    {"accountNumber": "98765432", "hashValue": "hash5432"},
]


_TXN_TRADE = {
    "activityId": 1000001,
    "time": "2026-04-18T14:32:11+0000",
    "type": "TRADE",
    "status": "VALID",
    "subAccount": "CASH",
    "tradeDate": "2026-04-18T14:32:11+0000",
    "netAmount": -1055.30,
    "activityType": "EXECUTION",
    "transferItems": [
        {
            "instrument": {"assetType": "EQUITY", "symbol": "AMZN", "cusip": "023135106"},
            "amount": 5.0, "cost": -1055.30, "price": 211.06,
            "positionEffect": "OPENING",
        },
    ],
}


_TXN_DIV = {
    "activityId": 1000002,
    "time": "2026-04-15T00:00:00+0000",
    "type": "DIVIDEND_OR_INTEREST",
    "status": "VALID",
    "subAccount": "CASH",
    "netAmount": 12.43,
    "activityType": "ACTIVITY_CORRECTION",
    "transferItems": [
        {"instrument": {"assetType": "EQUITY", "symbol": "KO"}, "cost": 12.43},
    ],
}


_START = datetime(2026, 4, 15, 0, 0, 0, tzinfo=timezone.utc)
_END = datetime(2026, 4, 22, 20, 0, 0, tzinfo=timezone.utc)


@respx.mock
def test_get_transactions_basic_params():
    url = "https://api.schwabapi.com/trader/v1/accounts/hash0756/transactions"
    route = respx.get(url).mock(return_value=httpx.Response(200, json=[_TXN_TRADE]))
    result = get_transactions(
        _client(), "hash0756",
        start=_START, end=_END, types="TRADE",
    )
    assert result == [_TXN_TRADE]
    params = route.calls.last.request.url.params
    assert params["types"] == "TRADE"
    # ISO 8601 with milliseconds + Z suffix
    assert params["startDate"] == "2026-04-15T00:00:00.000Z"
    assert params["endDate"] == "2026-04-22T20:00:00.000Z"


@respx.mock
def test_get_transactions_no_types_omits_param():
    url = "https://api.schwabapi.com/trader/v1/accounts/hash0756/transactions"
    route = respx.get(url).mock(return_value=httpx.Response(200, json=[]))
    get_transactions(
        _client(), "hash0756",
        start=_START, end=_END, types=None,
    )
    params = route.calls.last.request.url.params
    assert "types" not in params


@respx.mock
def test_get_transactions_with_symbol():
    url = "https://api.schwabapi.com/trader/v1/accounts/hash0756/transactions"
    route = respx.get(url).mock(return_value=httpx.Response(200, json=[]))
    get_transactions(
        _client(), "hash0756",
        start=_START, end=_END, symbol="AMZN",
    )
    assert route.calls.last.request.url.params["symbol"] == "AMZN"


@respx.mock
def test_get_all_transactions_iterates_all_accounts():
    respx.get("https://api.schwabapi.com/trader/v1/accounts/accountNumbers").mock(
        return_value=httpx.Response(200, json=_ACCT_NUMS),
    )
    respx.get(
        "https://api.schwabapi.com/trader/v1/accounts/hash0756/transactions"
    ).mock(return_value=httpx.Response(200, json=[_TXN_TRADE]))
    respx.get(
        "https://api.schwabapi.com/trader/v1/accounts/hash5432/transactions"
    ).mock(return_value=httpx.Response(200, json=[_TXN_DIV]))

    result = get_all_transactions(
        _client(), None,
        start=_START, end=_END, types="TRADE",
    )
    assert len(result) == 2
    # Each txn should be tagged with its owning account number.
    acct_tags = sorted(t["_account"] for t in result)
    assert acct_tags == ["12340756", "98765432"]


@respx.mock
def test_get_all_transactions_single_account():
    respx.get("https://api.schwabapi.com/trader/v1/accounts/accountNumbers").mock(
        return_value=httpx.Response(200, json=_ACCT_NUMS),
    )
    route = respx.get(
        "https://api.schwabapi.com/trader/v1/accounts/hash0756/transactions"
    ).mock(return_value=httpx.Response(200, json=[_TXN_TRADE]))

    result = get_all_transactions(
        _client(), "0756",
        start=_START, end=_END, types="TRADE",
    )
    assert len(result) == 1
    assert result[0]["_account"] == "12340756"
    # Only the selected account should be called; hash5432 must not be hit.
    assert route.call_count == 1


@respx.mock
def test_get_all_transactions_empty_response():
    respx.get("https://api.schwabapi.com/trader/v1/accounts/accountNumbers").mock(
        return_value=httpx.Response(200, json=_ACCT_NUMS),
    )
    respx.get(
        "https://api.schwabapi.com/trader/v1/accounts/hash0756/transactions"
    ).mock(return_value=httpx.Response(200, json=[]))
    respx.get(
        "https://api.schwabapi.com/trader/v1/accounts/hash5432/transactions"
    ).mock(return_value=httpx.Response(200, json=[]))

    result = get_all_transactions(
        _client(), None,
        start=_START, end=_END,
    )
    assert result == []


@respx.mock
def test_get_all_transactions_types_all_passes_no_filter():
    respx.get("https://api.schwabapi.com/trader/v1/accounts/accountNumbers").mock(
        return_value=httpx.Response(200, json=_ACCT_NUMS[:1]),
    )
    route = respx.get(
        "https://api.schwabapi.com/trader/v1/accounts/hash0756/transactions"
    ).mock(return_value=httpx.Response(200, json=[]))
    get_all_transactions(
        _client(), None,
        start=_START, end=_END, types="ALL",
    )
    # "ALL" is a CLI sentinel → API call omits the types param
    assert "types" not in route.calls.last.request.url.params
