from __future__ import annotations

import math
from datetime import date
from typing import Literal

from schwab_cli.api.client import SchwabClient


def get_chain(
    client: SchwabClient,
    symbol: str,
    *,
    contract_type: Literal["CALL", "PUT", "ALL"] = "ALL",
    strike: float | None = None,
    strike_count: int = 10,
    from_date: date | None = None,
    to_date: date | None = None,
) -> dict:
    """Fetch the option chain for `symbol` at the given expiry window.

    `strike_count` is our *total* desired strikes around ATM; Schwab's
    `strikeCount` param is per-side, so we pass `ceil(strike_count / 2)`.
    The output layer trims further as needed.
    """
    if strike_count < 1:
        raise ValueError(f"strike_count must be >= 1, got {strike_count}")
    params: dict[str, str | int] = {
        "symbol": symbol,
        "contractType": contract_type,
        "strategy": "SINGLE",
        "includeUnderlyingQuote": "true",
        "strikeCount": math.ceil(strike_count / 2),
    }
    if strike is not None:
        params["strike"] = str(strike)
    if from_date is not None:
        params["fromDate"] = from_date.isoformat()
    if to_date is not None:
        params["toDate"] = to_date.isoformat()
    return client.get(f"{SchwabClient.MARKET_BASE}/chains", params=params)
