"""Per-symbol volatility sampler.

Builds the per-day bundle described in spec §5.1 — interpolated ATM
IVs at 30/60/90 DTE, 25Δ wing pairs at the same tenors, HV(30d), and
the scope-A raw chain summary. No I/O: the caller passes a chain dict
and an underlying-close series; the function returns a dict ready to
hand to :func:`storage.vol_history.record_extended_snapshot`.
"""
from __future__ import annotations

from typing import Any

from schwab_cli.analytics.vol import (
    closest_dte_expiry,
    interp_iv_in_variance,
    pick_25d_wing,
    pick_atm_contract,
    pick_atm_curve,
    realized_vol,
)


_TARGET_TENORS = (30, 60, 90)


def sample_volatility(
    *,
    chain: dict,
    underlying_closes: list[float],
) -> dict[str, Any]:
    """Compute the per-symbol per-day metric bundle.

    Parameters
    ----------
    chain : dict
        Schwab chain response, expected to have ``underlying.last``
        and ``expiries: list[{expiry, dte, contracts}]``.
    underlying_closes : list[float]
        Trailing daily closes of the underlying (most recent last).

    Returns
    -------
    dict
        Keys: atm_iv, atm_iv_30d/60d/90d, iv_25d_put_30d/60d/90d,
        iv_25d_call_30d/60d/90d, hv_30d, raw_chain_summary,
        spot, atm_strike, atm_expiry, atm_dte. Any per-tenor value
        whose chain didn't bracket / lacked deltas is ``None``.
    """
    spot = float((chain.get("underlying") or {}).get("last") or 0.0)
    expiries = chain.get("expiries") or []

    # Existing near-term ATM (legacy column).
    atm = pick_atm_contract(expiries, spot)

    # Term-structure curve for interpolation.
    curve = pick_atm_curve(expiries, spot)

    bundle: dict[str, Any] = {
        "spot":              spot,
        "atm_iv":            atm["iv"] if atm else None,
        "atm_strike":        atm["strike"] if atm else None,
        "atm_expiry":        atm["expiry"] if atm else None,
        "atm_dte":           atm["dte"] if atm else None,
        "atm_iv_30d":        interp_iv_in_variance(curve, 30),
        "atm_iv_60d":        interp_iv_in_variance(curve, 60),
        "atm_iv_90d":        interp_iv_in_variance(curve, 90),
    }

    # 25Δ wings — picked at the closest-DTE expiry per tenor.
    wings: dict[int, dict[str, dict | None]] = {}
    for tenor in _TARGET_TENORS:
        exp = closest_dte_expiry(expiries, tenor)
        if exp is None:
            wings[tenor] = {"put": None, "call": None}
            bundle[f"iv_25d_put_{tenor}d"]  = None
            bundle[f"iv_25d_call_{tenor}d"] = None
            continue
        atm_iv_for_tenor = bundle.get(f"atm_iv_{tenor}d")
        put = pick_25d_wing(exp, side="P", target_delta=-0.25,
                            spot=spot, atm_iv=atm_iv_for_tenor)
        call = pick_25d_wing(exp, side="C", target_delta=+0.25,
                             spot=spot, atm_iv=atm_iv_for_tenor)
        wings[tenor] = {"put": put, "call": call}
        bundle[f"iv_25d_put_{tenor}d"]  = put["iv"] if put else None
        bundle[f"iv_25d_call_{tenor}d"] = call["iv"] if call else None

    # HV(30d) from underlying.
    bundle["hv_30d"] = realized_vol(underlying_closes, window=30)

    # Scope-A summary: only the contracts that fed the metrics.
    bundle["raw_chain_summary"] = _build_summary(
        spot=spot, atm=atm, curve=curve, wings=wings,
    )
    return bundle


def _build_summary(
    *,
    spot: float,
    atm: dict | None,
    curve: list[tuple[int, float]],
    wings: dict[int, dict[str, dict | None]],
) -> dict[str, Any]:
    """Build the scope-A raw chain summary dict (spec §5.3)."""
    atm_block: dict[str, dict | None] = {}
    for tenor in _TARGET_TENORS:
        if not curve:
            atm_block[f"{tenor}d"] = None
            continue
        atm_block[f"{tenor}d"] = {
            "tenor_dte":  tenor,
            "spot":       spot,
            "curve":      [{"dte": d, "iv": iv} for d, iv in curve],
        }
    wing_block = {
        f"{tenor}d": {
            "put":  wings[tenor]["put"],
            "call": wings[tenor]["call"],
        }
        for tenor in _TARGET_TENORS
    }
    return {
        "atm":   atm_block,
        "wings": wing_block,
        "spot":  spot,
        "atm_observed": atm,
    }
