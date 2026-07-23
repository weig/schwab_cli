"""Capture the OTM put band from a chain the dataset job already fetched.

This is the permanent raw-preservation layer. The dataset vol job owns the
single daily chain fetch per ticker; this module extracts the band of puts
the screener (and future variants) care about and persists it, so historical
option quotes are never discarded. No fetching here — the caller passes the
raw chain it already has.
"""
from __future__ import annotations

from schwab_cli.screener.config import ScreenerConfig
from schwab_cli.screener.locate import iter_puts, underlying_last
from schwab_cli.storage import screener as store


def extract_put_band(raw: dict, cfg: ScreenerConfig) -> list[dict]:
    """Puts within the configured DTE and |delta| band, from a raw chain."""
    out: list[dict] = []
    for p in iter_puts(raw):
        dte = p.get("dte")
        delta = p.get("delta")
        strike = p.get("strike")
        if strike is None or not isinstance(dte, int):
            continue
        if not (cfg.band_dte_lo <= dte <= cfg.band_dte_hi):
            continue
        if not isinstance(delta, (int, float)):
            continue
        if not (cfg.band_abs_delta_lo <= abs(delta) <= cfg.band_abs_delta_hi):
            continue
        out.append(p)
    return out


def extract_full_chain(raw: dict) -> list[dict]:
    """Every contract from both exp-date maps with quote + greeks fields.

    Unlike the put band this keeps calls, all deltas and all fetched
    expiries — the focus-tier permanent record for GEX / skew / term
    structure research.
    """
    import math

    out: list[dict] = []
    for side, map_key in (("C", "callExpDateMap"), ("P", "putExpDateMap")):
        for expiry_key, strike_map in (raw.get(map_key) or {}).items():
            expiry, _, dte_part = expiry_key.partition(":")
            try:
                dte = int(dte_part)
            except ValueError:
                continue
            for _strike, rows in (strike_map or {}).items():
                for row in rows or []:
                    iv_pct = row.get("volatility")
                    iv = (
                        iv_pct / 100.0
                        if isinstance(iv_pct, (int, float))
                        and math.isfinite(iv_pct) and iv_pct > 0
                        else None
                    )
                    out.append({
                        "expiry": expiry, "dte": dte, "side": side,
                        "strike": row.get("strikePrice"),
                        "bid": row.get("bid"), "ask": row.get("ask"),
                        "last": row.get("last"), "iv": iv,
                        "delta": row.get("delta"), "gamma": row.get("gamma"),
                        "theta": row.get("theta"), "vega": row.get("vega"),
                        "open_interest": row.get("openInterest"),
                        "volume": row.get("totalVolume"),
                    })
    return out


def capture_full_chain(
    conn,
    *,
    snapshot_date: str,
    symbol: str,
    raw: dict,
    now_ms: int,
) -> int:
    """Persist the full chain for one focus symbol/day. Returns row count."""
    contracts = extract_full_chain(raw)
    if not contracts:
        return 0
    store.record_chain_snapshot(
        conn, snapshot_date=snapshot_date, symbol=symbol, contracts=contracts,
        underlying_last=underlying_last(raw), now_ms=now_ms,
    )
    return len(contracts)


def capture_put_band(
    conn,
    *,
    snapshot_date: str,
    symbol: str,
    raw: dict,
    now_ms: int,
    cfg: ScreenerConfig | None = None,
) -> int:
    """Extract + persist the put band for one symbol/day. Returns row count.

    Best-effort by contract: raises nothing the caller must handle beyond
    normal DB errors; an empty/degenerate chain simply writes 0 rows.
    """
    cfg = cfg or ScreenerConfig()
    band = extract_put_band(raw, cfg)
    if not band:
        return 0
    store.record_put_band(
        conn, snapshot_date=snapshot_date, symbol=symbol, puts=band,
        underlying_last=underlying_last(raw), now_ms=now_ms,
    )
    return len(band)
