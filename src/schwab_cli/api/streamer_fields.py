"""Schwab streamer field-ID maps.

Every service-specific ``content`` frame comes back as numeric field
IDs. This module decodes the common ones into readable keys so the
rest of the code never has to remember "1 = bid price".

Only the most useful fields are mapped for MVP; the full Schwab
spec has 50+ per service and not all of them are populated in every
frame. Unknown IDs pass through untouched under their numeric key
so we don't lose data on a field we haven't catalogued yet.
"""

from __future__ import annotations

from typing import Any

# LEVELONE_EQUITIES — from Schwab streamer docs. Subset of the full
# 50+ fields; add more here as consumers need them.
LEVELONE_EQUITIES: dict[str, str] = {
    "0": "symbol",
    "1": "bid",
    "2": "ask",
    "3": "last",
    "4": "bid_size",
    "5": "ask_size",
    "8": "volume",
    "9": "last_size",
    "10": "high",
    "11": "low",
    "12": "close",
    "17": "open",
    "18": "net_change",
    "33": "mark",
    "34": "quote_time",
    "35": "trade_time",
    "42": "net_change_pct",
    "44": "mark_net_change",
    "45": "mark_net_change_pct",
}

# LEVELONE_OPTIONS — option chain streaming field IDs.
LEVELONE_OPTIONS: dict[str, str] = {
    "0": "symbol",
    "1": "description",
    "2": "bid",
    "3": "ask",
    "4": "last",
    "5": "high",
    "6": "low",
    "7": "close",
    "8": "volume",
    "9": "open_interest",
    "10": "volatility",
    "11": "intrinsic",
    "12": "expiry_year",
    "13": "multiplier",
    "14": "digits",
    "15": "open",
    "16": "bid_size",
    "17": "ask_size",
    "18": "last_size",
    "19": "net_change",
    "20": "strike",
    "21": "contract_type",
    "22": "underlying",
    "23": "expiry_month",
    "24": "expiry_day",
    "25": "days_to_expiry",
    "26": "time_value",
    "27": "delta",
    "28": "gamma",
    "29": "theta",
    "30": "vega",
    "31": "rho",
    "32": "security_status",
    "33": "theoretical_value",
    "34": "underlying_price",
    "35": "uv_expiry_type",
    "36": "mark",
    "37": "quote_time",
    "38": "trade_time",
    "39": "exchange_name",
    "40": "last_trading_day",
    "41": "settlement_type",
    "42": "net_change_pct",
}

# Service name → field map registry so decode() can dispatch.
_SERVICE_MAPS: dict[str, dict[str, str]] = {
    "LEVELONE_EQUITIES": LEVELONE_EQUITIES,
    "LEVELONE_OPTIONS": LEVELONE_OPTIONS,
}


def decode(service: str, content: dict[str, Any]) -> dict[str, Any]:
    """Rewrite a content dict's numeric field IDs to readable keys.

    ``key`` (the symbol/ticker) is always preserved. Numeric fields
    that aren't in the service's map pass through untouched — we'd
    rather show ``"47": 0.12`` in the output than silently drop it.
    """
    field_map = _SERVICE_MAPS.get(service, {})
    out: dict[str, Any] = {}
    # `key` is the symbol — always a string in every service.
    if "key" in content:
        out["symbol"] = content["key"]
    for field_id, value in content.items():
        if field_id == "key":
            continue
        readable = field_map.get(field_id)
        if readable is not None:
            out[readable] = value
        else:
            out[field_id] = value
    return out


def default_fields(service: str) -> str:
    """Comma-separated field-ID string to request by default.

    Trades off payload size vs. data richness. Default includes the
    fields a human watching a quote feed cares about: bid / ask /
    last / sizes / volume / times + net change.
    """
    if service == "LEVELONE_EQUITIES":
        return "0,1,2,3,4,5,8,9,33,34,35,42"
    if service == "LEVELONE_OPTIONS":
        return "0,2,3,4,8,9,16,17,18,20,21,27,28,29,30,36,37,42"
    # Unknown service — request only the symbol field.
    return "0"
