"""Schwab Trader API — order endpoints.

All functions take a :class:`SchwabClient` and return either parsed
JSON or, for ``place_order`` / ``replace_order``, a tuple
``(order_id, response)`` where ``order_id`` is extracted from the
``Location`` response header (Schwab returns ``201`` with no body).

Tests for this module live in :file:`tests/test_api_orders.py` and
**must** mock every HTTP call via ``respx`` — never hit Schwab live.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import httpx

from schwab_cli.api.client import ApiError, SchwabClient


_LOCATION_RE = re.compile(r"accounts/(?P<hash>[^/]+)/orders/(?P<id>\d+)")


def _iso8601(dt: datetime) -> str:
    """Format a tz-aware datetime as Schwab's expected ISO-8601 (UTC, ms, Z)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse_order_id_from_location(resp: httpx.Response) -> str:
    """Extract the order id from a placeOrder / replaceOrder response.

    Schwab returns ``201 Created`` with an empty body and the order
    detail URL in the ``Location`` header, e.g.
    ``https://api.schwabapi.com/trader/v1/accounts/<hash>/orders/123456789``.

    Raises :class:`ApiError` if the header is missing or malformed —
    that's a contract violation we want to surface loudly rather than
    silently returning a sentinel.
    """
    loc = resp.headers.get("location") or resp.headers.get("Location")
    if not loc:
        raise ApiError(
            f"placeOrder response missing Location header (status "
            f"{resp.status_code})"
        )
    m = _LOCATION_RE.search(loc)
    if not m:
        raise ApiError(
            f"placeOrder Location header doesn't look like an order URL: {loc!r}"
        )
    return m.group("id")


def place_order(
    client: SchwabClient, account_hash: str, body: dict,
) -> tuple[str, httpx.Response]:
    """``POST /accounts/{hash}/orders``. Returns ``(order_id, response)``.

    The response is returned alongside the id so callers can inspect
    headers / status if they need to.
    """
    resp = client.post(
        f"{SchwabClient.TRADER_BASE}/accounts/{account_hash}/orders",
        json=body,
    )
    order_id = parse_order_id_from_location(resp)
    return order_id, resp


def preview_order(
    client: SchwabClient, account_hash: str, body: dict,
) -> dict:
    """``POST /accounts/{hash}/previewOrder``.

    Returns the parsed JSON response containing commission/fee
    estimates, BP impact, and ``orderValidationResult`` warnings.

    If Schwab gates the endpoint with 404/501, ``ApiError`` propagates;
    callers (e.g. the confirmation panel) decide whether to render
    "preview unavailable" and continue.
    """
    resp = client.post(
        f"{SchwabClient.TRADER_BASE}/accounts/{account_hash}/previewOrder",
        json=body,
    )
    return resp.json() if resp.text else {}


def get_order(
    client: SchwabClient, account_hash: str, order_id: str,
) -> dict:
    """``GET /accounts/{hash}/orders/{id}``. Returns parsed JSON."""
    result = client.get(
        f"{SchwabClient.TRADER_BASE}/accounts/{account_hash}/orders/{order_id}"
    )
    if not isinstance(result, dict):
        raise ApiError(f"unexpected get_order response shape: {type(result).__name__}")
    return result


def list_orders_for_account(
    client: SchwabClient,
    account_hash: str,
    *,
    start: datetime,
    end: datetime,
    status: str | None = None,
    max_results: int | None = None,
) -> list[dict]:
    """``GET /accounts/{hash}/orders`` with required date window.

    ``status`` is a single Schwab status enum value or ``None`` (no
    server-side filter). ``max_results`` maps to ``maxResults``.
    """
    params: dict[str, str | int] = {
        "fromEnteredTime": _iso8601(start),
        "toEnteredTime": _iso8601(end),
    }
    if status:
        params["status"] = status
    if max_results is not None:
        params["maxResults"] = max_results
    result = client.get(
        f"{SchwabClient.TRADER_BASE}/accounts/{account_hash}/orders",
        params=params,
    )
    return result if isinstance(result, list) else []


def list_orders_all_accounts(
    client: SchwabClient,
    *,
    start: datetime,
    end: datetime,
    status: str | None = None,
    max_results: int | None = None,
) -> list[dict]:
    """``GET /orders`` (cross-account). Same query params as the
    per-account variant; no account hash in the URL."""
    params: dict[str, str | int] = {
        "fromEnteredTime": _iso8601(start),
        "toEnteredTime": _iso8601(end),
    }
    if status:
        params["status"] = status
    if max_results is not None:
        params["maxResults"] = max_results
    result = client.get(
        f"{SchwabClient.TRADER_BASE}/orders",
        params=params,
    )
    return result if isinstance(result, list) else []


def cancel_order(
    client: SchwabClient, account_hash: str, order_id: str,
) -> httpx.Response:
    """``DELETE /accounts/{hash}/orders/{id}``. Returns the raw response.

    Schwab typically replies 200 or 204 with an empty body. Callers
    should treat any 2xx as success.
    """
    return client.delete(
        f"{SchwabClient.TRADER_BASE}/accounts/{account_hash}/orders/{order_id}"
    )
