"""Screener thresholds & parameters — all configurable (plan §4).

Defaults start loose per the spec ("跑两周观察幸存数量后收紧"). Values are
read from the shared ``dataset.json`` under an optional ``"screener"`` key,
so operators tune them without a schema migration; anything absent falls
back to the defaults here.
"""
from __future__ import annotations

from dataclasses import dataclass, fields


@dataclass(frozen=True)
class ScreenerConfig:
    # Hard filters (§4)
    earnings_window_days: int = 10      # drop if days_to_earnings <= this
    require_earnings_date: bool = True  # fail-closed: drop names w/ unknown earnings
    iv_lo: float = 0.05                 # drop if atm_iv_30d outside (lo, hi)
    iv_hi: float = 3.00
    spread_pct_max: float = 0.10        # drop if spread_pct > this
    put_oi_min: int = 500               # drop if put_oi < this
    put_volume_min: int = 100           # drop if put_volume < this
    underlying_min: float = 40.0        # drop if underlying_last < this
    bid_min: float = 0.05               # drop if put_bid <= this
    # Ranking (§5)
    rf_rate: float = 0.045              # 3-month T-bill proxy for BSM fair value
    ivr_low_conf_days: int = 120        # IVR flagged low-confidence under this
    # Validation (§7)
    cohort_size: int = 10               # top-N / bottom-N virtual positions/day


def load_screener_config() -> ScreenerConfig:
    """Load overrides from ``dataset.json['screener']`` atop the defaults."""
    try:
        from schwab_cli.dataset.config import load_config_or_default

        raw = (load_config_or_default() or {}).get("screener") or {}
    except Exception:
        raw = {}
    known = {f.name for f in fields(ScreenerConfig)}
    overrides = {k: v for k, v in raw.items() if k in known}
    return ScreenerConfig(**overrides)
