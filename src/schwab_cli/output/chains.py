from __future__ import annotations

import math
from typing import Any


def _finite(v: Any) -> float | None:
    if v is None:
        return None
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(fv):
        return None
    return fv


def _shape_contract(raw: dict, side: str) -> dict:
    iv_pct = _finite(raw.get("volatility"))
    return {
        "optionSymbol": (raw.get("symbol") or ""),
        "side": side,
        "strike": _finite(raw.get("strikePrice")),
        "bid": _finite(raw.get("bid")),
        "ask": _finite(raw.get("ask")),
        "last": _finite(raw.get("last")),
        "delta": _finite(raw.get("delta")),
        "iv": (iv_pct / 100.0) if iv_pct is not None else None,
        "gamma": _finite(raw.get("gamma")),
        "theta": _finite(raw.get("theta")),
        "vega": _finite(raw.get("vega")),
        "volume": raw.get("totalVolume"),
        "openInterest": raw.get("openInterest"),
        "mark": _finite(raw.get("mark")),
        "bidSize": raw.get("bidSize"),
        "askSize": raw.get("askSize"),
        "lastSize": raw.get("lastSize"),
        "open": _finite(raw.get("openPrice")),
        "high": _finite(raw.get("highPrice")),
        "low": _finite(raw.get("lowPrice")),
        "close": _finite(raw.get("closePrice")),
        "rho": _finite(raw.get("rho")),
        "timeValue": _finite(raw.get("timeValue")),
        "intrinsic": _finite(raw.get("intrinsicValue")),
        "inTheMoney": bool(raw.get("inTheMoney")),
        "multiplier": raw.get("multiplier"),
        "settlementType": raw.get("settlementType"),
    }


def shape_envelope(raw: dict, *, strike_count: int | None = None) -> dict:
    """Flatten a Schwab /chains response into our display envelope.

    If `strike_count` is given, keeps only the N strikes whose prices are
    closest to the underlying spot — both the call and the put at each kept
    strike survive the trim.
    """
    underlying_raw = (raw or {}).get("underlying") or {}
    underlying = {
        "last": _finite(underlying_raw.get("last")),
        "netChange": _finite(underlying_raw.get("change")),
        "pctChange": _finite(underlying_raw.get("percentChange")),
    }

    contracts: list[dict] = []
    expiry: str | None = None
    dte: int | None = None

    for source_key, side in (("callExpDateMap", "C"), ("putExpDateMap", "P")):
        date_map = (raw or {}).get(source_key) or {}
        for exp_key, strike_map in date_map.items():
            for _strike_str, contract_list in (strike_map or {}).items():
                for c in (contract_list or []):
                    if expiry is None:
                        expiry = c.get("expirationDate") or exp_key.split(":")[0]
                        dte = c.get("daysToExpiration")
                    contracts.append(_shape_contract(c, side))

    if strike_count is not None and contracts:
        spot = underlying["last"]
        if spot is not None:
            strikes = sorted({c["strike"] for c in contracts if c["strike"] is not None})
            strikes.sort(key=lambda s: (abs(s - spot), s))
            keep = set(strikes[:strike_count])
            contracts = [c for c in contracts if c["strike"] in keep]

    contracts.sort(key=lambda r: (r["strike"] if r["strike"] is not None else 0.0,
                                  0 if r["side"] == "C" else 1))

    return {
        "symbol": (raw or {}).get("symbol", ""),
        "expiry": expiry,
        "dte": dte,
        "underlying": underlying,
        "contracts": contracts,
    }
