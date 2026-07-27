"""BUG-2 (downgraded) — broker-IV drift telemetry, signal-only.

A single after-hours snapshot suggested Schwab's ``volatility`` field may be
off by ~2 vol points at some near-ATM strikes. That is not enough to re-base
the pipeline onto self-solved IV (which would make IV correctness our own
liability). Instead we OBSERVE: for tight-quote, near-ATM contracts, solve IV
locally from the mid price (reusing analytics.bs.implied_vol) and emit one
layered record per contract. The main pipeline is untouched — this only
appends telemetry for a later, data-driven decision.

Layered on purpose (strike / moneyness / dte / spot_used): the observed bias
was concentrated at specific strikes, so an aggregate would wash it out.
"""
from __future__ import annotations

from schwab_cli.analytics.bs import implied_vol
from schwab_cli.analytics.vol import is_valid_contract

# Sample only where the signal isn't drowned by microstructure noise.
_MAX_REL_SPREAD = 0.15      # (ask-bid)/mid must be tighter than this
_MAX_MONEYNESS = 0.10       # within ±10% of spot (near-ATM only)
_MAX_DTE = 120              # front/belly; LEAPS IV is its own animal
_WARN_DRIFT = 0.01          # |solved - broker| > 1 vol pt → caller may WARN


def sample_iv_drift(
    expiries: list[dict],
    *,
    spot: float,
    r: float,
    now_ms: int,
    symbol: str,
) -> list[dict]:
    """Return one drift record per eligible contract (may be empty).

    Each record: symbol, captured_at_ms, expiry, dte, strike, side,
    moneyness (K/S), bid, ask, mid, spot_used, iv_broker, iv_solved,
    drift (solved-broker), warn (bool). ``iv_solved`` may be ``None`` when
    the solver can't converge — the record is still emitted (a solve failure
    is itself signal).
    """
    if not spot or spot <= 0:
        return []
    out: list[dict] = []
    for exp in expiries:
        dte = int(exp.get("dte") or 0)
        if dte <= 0 or dte > _MAX_DTE:
            continue
        T = dte / 365.0
        for c in exp.get("contracts") or []:
            if not is_valid_contract(c):
                continue
            strike = c.get("strike")
            bid, ask = c.get("bid"), c.get("ask")
            if strike is None or bid is None or ask is None:
                continue
            if bid <= 0 or ask <= 0:
                continue
            mid = (bid + ask) / 2.0
            if mid <= 0 or (ask - bid) / mid > _MAX_REL_SPREAD:
                continue
            if abs(strike / spot - 1.0) > _MAX_MONEYNESS:
                continue
            iv_broker = c.get("iv")
            iv_solved = implied_vol(
                mid, spot, strike, T, r, is_call=(c.get("side") == "C"),
            )
            drift = (iv_solved - iv_broker) if (
                iv_solved is not None and iv_broker is not None) else None
            out.append({
                "symbol": symbol, "captured_at_ms": now_ms,
                "expiry": exp.get("expiry"), "dte": dte,
                "strike": strike, "side": c.get("side"),
                "moneyness": round(strike / spot, 4),
                "bid": bid, "ask": ask, "mid": round(mid, 4),
                "spot_used": spot,
                "iv_broker": iv_broker, "iv_solved": iv_solved,
                "drift": drift,
                "warn": bool(drift is not None and abs(drift) > _WARN_DRIFT),
            })
    return out
