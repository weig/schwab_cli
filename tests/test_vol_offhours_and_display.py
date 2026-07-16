"""Tests for the vol off-hours write gate, quote-time header, and tiered
IVR/IVP display (the four fixes to the near-expiry/pollution issues)."""
from __future__ import annotations

from datetime import datetime, timezone

from schwab_cli.output.vol import _fmt_quote_time, render_vol
from schwab_cli.output.format import Format


def _ms(y, mo, d, h, mi) -> int:
    # Interpret the given wall-clock as ET, return epoch ms.
    from zoneinfo import ZoneInfo
    dt = datetime(y, mo, d, h, mi, tzinfo=ZoneInfo("America/New_York"))
    return int(dt.timestamp() * 1000)


def test_fmt_quote_time_in_hours_has_no_offhours_tag():
    s = _fmt_quote_time(_ms(2026, 7, 15, 11, 30))  # Wed 11:30 ET
    assert s == "2026-07-15 11:30 ET"


def test_fmt_quote_time_after_hours_flagged():
    s = _fmt_quote_time(_ms(2026, 7, 15, 19, 59))  # Wed 19:59 ET
    assert s.endswith("· off-hours") and "19:59" in s


def test_fmt_quote_time_weekend_flagged():
    s = _fmt_quote_time(_ms(2026, 7, 18, 11, 30))  # Saturday
    assert s.endswith("· off-hours")


def test_fmt_quote_time_none():
    assert _fmt_quote_time(None) is None
    assert _fmt_quote_time(0) is None


def _env(**over) -> dict:
    base = {
        "symbol": "AMZN", "spot": 254.96,
        "quote_time": _ms(2026, 7, 15, 19, 59),
        "iv": {"value": 0.333, "expiry": "2026-07-22", "dte": 7, "strike": 255.0},
        "iv_ref": None,
        "hv": {"window": 30, "value": 0.33},
        "hvp": {"lookback": 252, "value": 0.72, "sample_size": 178},
        "pc": {"volume_ratio": 0.4, "oi_ratio": 0.51},
        "ivp": {"value": 0.56, "sample_size": 90, "synthetic": 24, "observed": 66},
        "ivr_ivp": {"ivr": 88.2, "ivp": 90.9, "source": "atm_iv_30d",
                    "n_days": 45, "low_confidence": True},
    }
    base.update(over)
    return base


def test_human_prefers_tiered_ivr_over_legacy_ivp():
    out = render_vol(_env(), fmt=Format.HUMAN)
    assert "IVR" in out
    assert "88%" in out and "91%" in out            # tiered ivr/ivp, not 56
    assert "atm_iv_30d" in out and "low-conf" in out
    assert "· off-hours" in out                      # header timestamp tag
    assert "7 DTE" in out                            # near-expiry contract avoided


def test_human_falls_back_to_legacy_when_tiered_absent():
    out = render_vol(_env(ivr_ivp={"ivr": None, "ivp": None}), fmt=Format.HUMAN)
    # No tiered result → the legacy IVP row is shown, no IVR row.
    assert "IVP" in out
