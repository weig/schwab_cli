from __future__ import annotations

import math
from datetime import date
from typing import Literal

from schwab_cli.api.client import SchwabClient
from schwab_cli.ticker import to_schwab_form


def get_chain(
    client: SchwabClient,
    symbol: str,
    *,
    contract_type: Literal["CALL", "PUT", "ALL"] = "ALL",
    strike: float | None = None,
    strike_count: int | None = 10,
    from_date: date | None = None,
    to_date: date | None = None,
) -> dict:
    """Fetch the option chain for `symbol` at the given expiry window.

    `strike_count` is our *total* desired strikes around ATM; Schwab's
    `strikeCount` param is per-side, so we pass `ceil(strike_count / 2)`.
    The output layer trims further as needed.

    Pass ``strike_count=None`` to **omit** the ``strikeCount`` param from
    the request. This matters when `strike` is given for an exact-strike
    lookup: Schwab's chain endpoint silently prefers the ATM-window
    resolution from ``strikeCount`` over the explicit ``strike``, so
    including both returns strikes around ATM rather than the one we
    asked for. The only reliable way to pin a specific strike is to
    send ``strike`` alone.
    """
    if strike_count is not None and strike_count < 1:
        raise ValueError(f"strike_count must be >= 1 or None, got {strike_count}")
    params: dict[str, str | int] = {
        "symbol": to_schwab_form(symbol),
        "contractType": contract_type,
        "strategy": "SINGLE",
        "includeUnderlyingQuote": "true",
    }
    if strike_count is not None:
        params["strikeCount"] = math.ceil(strike_count / 2)
    if strike is not None:
        params["strike"] = str(strike)
    if from_date is not None:
        params["fromDate"] = from_date.isoformat()
    if to_date is not None:
        params["toDate"] = to_date.isoformat()
    return client.get(f"{SchwabClient.MARKET_BASE}/chains", params=params)


def flatten_chain(raw: dict) -> tuple[list[dict], list[dict]]:
    """Flatten Schwab's ``callExpDateMap`` / ``putExpDateMap`` into the
    ``[{expiry, dte, contracts}, ...]`` shape downstream analytics expect.

    Schwab returns options as nested ``map[expiryKey][strike] → list[row]``
    dicts; analytics functions (:func:`pick_atm_contract`,
    :func:`pick_atm_curve`, :func:`sample_volatility`) operate on a
    flat per-expiry contract list. This helper does the conversion in
    one place so callers don't reach into the raw shape.

    Returns ``(expiries, flat_contracts)`` where ``flat_contracts`` is
    the same set of contracts unrolled out of expiry buckets — useful
    for cross-chain aggregations like put/call volume ratios.

    IV is normalized from Schwab's percent form (e.g. ``32.5``) to the
    decimal form analytics use (``0.325``).
    """
    per_expiry: dict[tuple[str, int], list[dict]] = {}
    flat: list[dict] = []
    for side, map_key in [("C", "callExpDateMap"), ("P", "putExpDateMap")]:
        for expiry_key, strike_map in (raw.get(map_key) or {}).items():
            expiry, _, dte_part = expiry_key.partition(":")
            try:
                dte = int(dte_part)
            except ValueError:
                dte = 0
            bucket = per_expiry.setdefault((expiry, dte), [])
            for _strike, rows in (strike_map or {}).items():
                for row in rows or []:
                    iv_pct = row.get("volatility")
                    # Schwab returns -999.0 in `volatility` as an "IV
                    # unavailable" sentinel (holidays / illiquid snapshots).
                    # Treat any non-positive value as missing so downstream
                    # `iv is not None` guards skip it instead of storing -9.99.
                    iv = (
                        iv_pct / 100.0
                        if isinstance(iv_pct, (int, float))
                        and math.isfinite(iv_pct)
                        and iv_pct > 0
                        else None
                    )
                    contract = {
                        "side":         side,
                        "strike":       row.get("strikePrice"),
                        "iv":           iv,
                        "delta":        row.get("delta"),
                        "volume":       row.get("totalVolume"),
                        "openInterest": row.get("openInterest"),
                        "expiry":       expiry,
                        "dte":          dte,
                    }
                    bucket.append(contract)
                    flat.append(contract)
    expiries = [
        {"expiry": exp, "dte": dte, "contracts": contracts}
        for (exp, dte), contracts in per_expiry.items()
    ]
    expiries.sort(key=lambda e: e["dte"])
    return expiries, flat
