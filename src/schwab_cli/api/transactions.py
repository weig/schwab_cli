from __future__ import annotations

from datetime import datetime, timezone

from schwab_cli.api.client import SchwabClient


def _iso8601(dt: datetime) -> str:
    """Format a tz-aware datetime as Schwab's expected ISO-8601 (UTC, ms, Z)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def get_transactions(
    client: SchwabClient,
    account_hash: str,
    *,
    start: datetime,
    end: datetime,
    types: str | None = None,
    symbol: str | None = None,
) -> list[dict]:
    """Fetch the raw transaction list for one account over [start, end]."""
    params: dict[str, str] = {
        "startDate": _iso8601(start),
        "endDate": _iso8601(end),
    }
    if types:
        params["types"] = types
    if symbol:
        params["symbol"] = symbol
    result = client.get(
        f"{SchwabClient.TRADER_BASE}/accounts/{account_hash}/transactions",
        params=params,
    )
    return result if isinstance(result, list) else []


def get_all_transactions(
    client: SchwabClient,
    account_number: str | None,
    *,
    start: datetime,
    end: datetime,
    types: str | None = None,
    symbol: str | None = None,
) -> list[dict]:
    """Fetch transactions across all accounts (or just one).

    Each returned transaction is tagged with a synthetic `_account` key
    carrying the owning account number.

    `types="ALL"` is a CLI sentinel meaning "no type filter" — it is
    translated to an omitted `types` param on the wire.
    """
    if account_number is None:
        ids = client.account_ids()
    else:
        ids = [client.resolve_account(account_number)]

    api_types = None if (types is None or types == "ALL") else types

    out: list[dict] = []
    for acct in ids:
        raw = get_transactions(
            client, acct.hash_value,
            start=start, end=end, types=api_types, symbol=symbol,
        )
        for txn in raw:
            tagged = dict(txn)
            tagged["_account"] = acct.account_number
            out.append(tagged)
    return out
