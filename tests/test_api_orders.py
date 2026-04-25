"""Tests for the order endpoints in `schwab_cli.api.orders`.

**Safety**: every HTTP call is mocked via :mod:`respx`. The tests
must NEVER reach the real Schwab API. If a test passes without a
respx route, that's a respx setup bug — fix the test, don't loosen
the mock — because a missing route under the wrong dispatcher
config could fall through to the network.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import respx

from schwab_cli.api.client import ApiError, SchwabClient
from schwab_cli.api.orders import (
    cancel_order,
    get_order,
    list_orders_all_accounts,
    list_orders_for_account,
    parse_order_id_from_location,
    place_order,
    preview_order,
)
from schwab_cli.config import Config
from schwab_cli.session import Session


def _cfg() -> Config:
    return Config(
        client_id="cid", client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
    )


def _session() -> Session:
    return Session(
        access_token="atok", refresh_token="rtok",
        expires_at=1_000_000, refresh_token_expires_at=2_000_000,
    )


def _client() -> SchwabClient:
    return SchwabClient(_cfg(), _session())


_HASH = "ABC123HASH"
_ORDER_URL = f"https://api.schwabapi.com/trader/v1/accounts/{_HASH}/orders"


# ---- parse_order_id_from_location -----------------------------------------


def test_parse_order_id_from_location_full_url():
    resp = httpx.Response(
        201,
        headers={
            "Location": (
                "https://api.schwabapi.com/trader/v1/accounts/ABC123/orders/987654"
            )
        },
    )
    assert parse_order_id_from_location(resp) == "987654"


def test_parse_order_id_from_location_relative():
    resp = httpx.Response(
        201, headers={"Location": "/trader/v1/accounts/HASH/orders/42"},
    )
    assert parse_order_id_from_location(resp) == "42"


def test_parse_order_id_from_location_lowercase_header():
    resp = httpx.Response(
        201, headers={"location": "/accounts/H/orders/12345"},
    )
    assert parse_order_id_from_location(resp) == "12345"


def test_parse_order_id_missing_header_raises():
    resp = httpx.Response(201)
    with pytest.raises(ApiError, match="Location"):
        parse_order_id_from_location(resp)


def test_parse_order_id_malformed_header_raises():
    resp = httpx.Response(201, headers={"Location": "https://elsewhere.example/x"})
    with pytest.raises(ApiError, match="order URL"):
        parse_order_id_from_location(resp)


# ---- place_order ----------------------------------------------------------


@respx.mock
def test_place_order_returns_id_from_location_header():
    route = respx.post(_ORDER_URL).mock(
        return_value=httpx.Response(
            201,
            headers={"Location": f"{_ORDER_URL}/555"},
        ),
    )
    body = {"orderType": "LIMIT", "quantity": 1, "price": "1.00",
            "duration": "DAY", "session": "NORMAL",
            "orderStrategyType": "SINGLE", "orderLegCollection": []}
    order_id, resp = place_order(_client(), _HASH, body)
    assert order_id == "555"
    assert resp.status_code == 201
    assert route.called
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == "Bearer atok"
    # Body was JSON-serialised.
    import json as _json
    assert _json.loads(sent.content)["orderType"] == "LIMIT"


@respx.mock
def test_place_order_4xx_surfaces_apierror():
    respx.post(_ORDER_URL).mock(
        return_value=httpx.Response(
            400,
            text='{"errors":[{"message":"Insufficient buying power"}]}',
        ),
    )
    with pytest.raises(ApiError, match="400"):
        place_order(_client(), _HASH, {"x": "y"})


# ---- preview_order --------------------------------------------------------


@respx.mock
def test_preview_order_returns_parsed_json():
    preview_url = (
        f"https://api.schwabapi.com/trader/v1/accounts/{_HASH}/previewOrder"
    )
    payload = {
        "orderValueImpact": {"buyingPowerEffect": -86.35},
        "commission": 1.30,
    }
    respx.post(preview_url).mock(return_value=httpx.Response(200, json=payload))
    out = preview_order(_client(), _HASH, {"orderType": "LIMIT"})
    assert out == payload


@respx.mock
def test_preview_order_404_raises_apierror_for_caller_to_handle():
    preview_url = (
        f"https://api.schwabapi.com/trader/v1/accounts/{_HASH}/previewOrder"
    )
    respx.post(preview_url).mock(return_value=httpx.Response(404))
    with pytest.raises(ApiError, match="404"):
        preview_order(_client(), _HASH, {"x": "y"})


# ---- get_order ------------------------------------------------------------


@respx.mock
def test_get_order_returns_dict():
    url = f"{_ORDER_URL}/777"
    respx.get(url).mock(return_value=httpx.Response(
        200, json={"orderId": 777, "status": "WORKING"},
    ))
    order = get_order(_client(), _HASH, "777")
    assert order["orderId"] == 777
    assert order["status"] == "WORKING"


@respx.mock
def test_get_order_unexpected_list_raises():
    url = f"{_ORDER_URL}/777"
    respx.get(url).mock(return_value=httpx.Response(200, json=[]))
    with pytest.raises(ApiError, match="response shape"):
        get_order(_client(), _HASH, "777")


# ---- list_orders_for_account ----------------------------------------------


@respx.mock
def test_list_orders_for_account_sends_iso_window():
    route = respx.get(_ORDER_URL).mock(
        return_value=httpx.Response(200, json=[{"orderId": 1}]),
    )
    start = datetime(2026, 4, 18, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 4, 25, 0, 0, 0, tzinfo=timezone.utc)
    out = list_orders_for_account(_client(), _HASH, start=start, end=end)
    assert out == [{"orderId": 1}]
    sent = route.calls.last.request
    assert "fromEnteredTime=2026-04-18T00%3A00%3A00.000Z" in str(sent.url)
    assert "toEnteredTime=2026-04-25T00%3A00%3A00.000Z" in str(sent.url)
    assert "status=" not in str(sent.url)


@respx.mock
def test_list_orders_for_account_with_status_and_limit():
    route = respx.get(_ORDER_URL).mock(
        return_value=httpx.Response(200, json=[]),
    )
    start = datetime(2026, 4, 18, tzinfo=timezone.utc)
    end = datetime(2026, 4, 25, tzinfo=timezone.utc)
    list_orders_for_account(
        _client(), _HASH,
        start=start, end=end, status="FILLED", max_results=50,
    )
    sent = str(route.calls.last.request.url)
    assert "status=FILLED" in sent
    assert "maxResults=50" in sent


@respx.mock
def test_list_orders_for_account_handles_non_list_response():
    respx.get(_ORDER_URL).mock(return_value=httpx.Response(200, json={}))
    out = list_orders_for_account(
        _client(), _HASH,
        start=datetime(2026, 4, 18, tzinfo=timezone.utc),
        end=datetime(2026, 4, 25, tzinfo=timezone.utc),
    )
    assert out == []


# ---- list_orders_all_accounts ---------------------------------------------


@respx.mock
def test_list_orders_all_accounts_hits_cross_account_endpoint():
    url = "https://api.schwabapi.com/trader/v1/orders"
    respx.get(url).mock(return_value=httpx.Response(200, json=[{"orderId": 9}]))
    out = list_orders_all_accounts(
        _client(),
        start=datetime(2026, 4, 18, tzinfo=timezone.utc),
        end=datetime(2026, 4, 25, tzinfo=timezone.utc),
    )
    assert out == [{"orderId": 9}]


# ---- cancel_order ---------------------------------------------------------


@respx.mock
def test_cancel_order_sends_delete():
    url = f"{_ORDER_URL}/777"
    route = respx.delete(url).mock(return_value=httpx.Response(200))
    resp = cancel_order(_client(), _HASH, "777")
    assert resp.status_code == 200
    assert route.called
