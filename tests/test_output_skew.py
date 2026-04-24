"""Tests for the ``skew`` renderers.

The analytics layer is exercised elsewhere — these tests feed canned
metrics dicts through the three mode renderers × three formats and
check the outputs carry the anchors the user (and downstream tooling)
rely on.
"""

from __future__ import annotations

import json

from schwab_cli.output.format import Format
from schwab_cli.output.skew import render_cross, render_skew, render_term


# ---- canned metrics ----------------------------------------------------


def _l1_metrics() -> dict:
    """Full L1 metrics dict for a put-skewed chain."""
    return {
        "symbol": "AMZN",
        "expiry": "2026-05-01",
        "dte": 8,
        "spot": 255.36,
        "atm": {
            "strike": 257.5, "iv_pct": 61.62,
            "put_strike": 257.5, "put_iv_pct": 61.58,
        },
        "d25": {
            "put":  {"strike": 240.0, "delta": -0.25, "iv_pct": 62.80},
            "call": {"strike": 272.5, "delta":  0.26, "iv_pct": 59.51},
            "rr": 3.29, "bf": -0.46,
        },
        "d10": {
            "put":  {"strike": 232.5, "delta": -0.16, "iv_pct": 63.80},
            "call": {"strike": 280.0, "delta":  0.17, "iv_pct": 60.02},
            "rr": 3.78, "bf": 0.29,
        },
        "atm_slope_per_dollar": -0.0371,
        "iv_range": {"min_pct": 59.51, "max_pct": 63.80, "spread_pct": 4.29},
    }


def _l1_empty() -> dict:
    """What compute_skew emits on an empty chain — every metric is None."""
    return {
        "symbol": "X",
        "expiry": "2026-05-01",
        "dte": 8,
        "spot": 100.0,
        "atm": {"strike": None, "iv_pct": None,
                "put_strike": None, "put_iv_pct": None},
        "d25": {"put": None, "call": None, "rr": None, "bf": None},
        "d10": {"put": None, "call": None, "rr": None, "bf": None},
        "atm_slope_per_dollar": None,
        "iv_range": {"min_pct": None, "max_pct": None, "spread_pct": None},
    }


# ---- L1: HUMAN ---------------------------------------------------------


def test_render_skew_human_carries_core_anchors():
    out = render_skew(_l1_metrics(), fmt=Format.HUMAN)
    # Header with symbol, expiry, DTE.
    assert "AMZN Skew" in out
    assert "2026-05-01" in out
    assert "DTE 8" in out
    # ATM line and skew section labels.
    assert "ATM" in out
    assert "25Δ Skew" in out
    assert "10Δ Skew" in out
    # Interpretation tags render for non-null values.
    assert "put premium" in out
    # Exact metric values survive formatting.
    assert "+3.29" in out
    assert "+3.78" in out
    assert "-0.46" in out
    assert "-0.0371" in out
    # IV range appears.
    assert "IV Range" in out


def test_render_skew_human_empty_omits_missing_sections():
    out = render_skew(_l1_empty(), fmt=Format.HUMAN)
    # Header still present.
    assert "X Skew" in out
    # But no "25Δ Skew" section block since put/call are None.
    assert "25Δ Skew" not in out
    assert "ATM Slope" not in out


# ---- L1: JSON ----------------------------------------------------------


def test_render_skew_json_roundtrips_the_metrics():
    out = render_skew(_l1_metrics(), fmt=Format.JSON)
    data = json.loads(out)
    assert data["symbol"] == "AMZN"
    assert data["d25"]["rr"] == 3.29
    assert data["d10"]["call"]["strike"] == 280.0
    assert data["atm_slope_per_dollar"] == -0.0371
    assert data["iv_range"]["spread_pct"] == 4.29


# ---- L1: MD ------------------------------------------------------------


def test_render_skew_md_has_gfm_structure():
    out = render_skew(_l1_metrics(), fmt=Format.MD)
    assert out.startswith("# AMZN Skew")
    assert "## Skew Legs" in out
    assert "## Derived Metrics" in out
    # Table header rows present.
    assert "| Leg | Strike | Δ | IV |" in out
    # Metric values land in cells.
    assert "+3.29" in out
    assert "-0.0371" in out


# ---- L2: term structure ------------------------------------------------


def _term_metrics() -> list[dict]:
    """Three expiries of the same symbol — renderers must preserve order
    (analytics sorts by DTE before the renderer sees the list)."""
    m = _l1_metrics
    m8 = m(); m22 = m(); m267 = m()
    m22["dte"] = 22; m22["expiry"] = "2026-05-15"
    m22["atm"]["iv_pct"] = 45.4; m22["d25"]["rr"] = 1.16
    m22["d25"]["bf"] = -0.44; m22["atm_slope_per_dollar"] = -0.0181
    m267["dte"] = 267; m267["expiry"] = "2027-01-15"
    m267["atm"]["iv_pct"] = 38.4; m267["d25"]["rr"] = -3.11
    m267["d25"]["bf"] = -2.73; m267["atm_slope_per_dollar"] = 0.2340
    return [m8, m22, m267]


def test_render_term_human_has_header_and_rows():
    out = render_term(_term_metrics(), fmt=Format.HUMAN, symbol="AMZN")
    assert "AMZN Term Structure" in out
    # Column headers.
    assert "Expiry" in out and "DTE" in out and "25Δ RR" in out
    # Each expiry's DTE appears.
    assert "8" in out and "22" in out and "267" in out


def test_render_term_json_is_the_list_itself():
    out = render_term(_term_metrics(), fmt=Format.JSON, symbol="AMZN")
    data = json.loads(out)
    assert isinstance(data, list)
    assert [m["dte"] for m in data] == [8, 22, 267]


def test_render_term_md_has_pipe_table():
    out = render_term(_term_metrics(), fmt=Format.MD, symbol="AMZN")
    assert out.startswith("# AMZN Term Structure")
    assert "| Expiry | DTE | ATM IV | 25Δ RR | 25Δ BF | Slope/$ |" in out
    # Code-ticked expiry.
    assert "`2026-05-01`" in out


def test_render_term_empty_is_graceful():
    assert "No data" in render_term([], fmt=Format.HUMAN, symbol="X")
    assert json.loads(render_term([], fmt=Format.JSON, symbol="X")) == []


# ---- L3: cross-ticker --------------------------------------------------


def _cross_metrics() -> list[dict]:
    """Three symbols at DTE 8, already sorted by 25Δ RR descending (the
    renderer preserves caller order)."""
    nvda = _l1_metrics(); nvda["symbol"] = "NVDA"
    nvda["d25"]["rr"] = 4.40; nvda["d10"]["rr"] = 10.84
    nvda["atm"]["iv_pct"] = 36.6; nvda["atm_slope_per_dollar"] = -0.3097
    amzn = _l1_metrics()  # symbol AMZN, rr 3.29 from _l1_metrics
    msft = _l1_metrics(); msft["symbol"] = "MSFT"
    msft["d25"]["rr"] = 0.88; msft["d10"]["rr"] = 0.88
    msft["atm"]["iv_pct"] = 54.1; msft["atm_slope_per_dollar"] = 0.0030
    return [nvda, amzn, msft]


def test_render_cross_human_has_header_and_rows():
    out = render_cross(_cross_metrics(), fmt=Format.HUMAN)
    assert "Cross-Ticker Skew" in out
    assert "NVDA" in out and "AMZN" in out and "MSFT" in out
    assert "+4.40" in out and "+3.29" in out


def test_render_cross_json_is_the_list():
    out = render_cross(_cross_metrics(), fmt=Format.JSON)
    data = json.loads(out)
    assert [m["symbol"] for m in data] == ["NVDA", "AMZN", "MSFT"]


def test_render_cross_md_has_pipe_table():
    out = render_cross(_cross_metrics(), fmt=Format.MD)
    assert out.startswith("# Cross-Ticker Skew")
    assert "| Ticker | DTE | ATM IV | 25Δ RR | 10Δ Wing | 25Δ BF | Slope/$ |" in out


def test_render_cross_empty_is_graceful():
    assert "No data" in render_cross([], fmt=Format.HUMAN)
    assert json.loads(render_cross([], fmt=Format.JSON)) == []
