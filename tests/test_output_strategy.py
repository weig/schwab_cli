"""Tests for the ``strategy`` renderer.

Feeds canned metrics dicts (the shape the command layer produces) to
each of the three format branches and checks the outputs carry the
anchors downstream tooling and users rely on.
"""

from __future__ import annotations

import json

from schwab_cli.output.format import Format
from schwab_cli.output.strategy import render_strategy


def _ic_metrics() -> dict:
    """Full metrics dict for a profitable IC — all fields present."""
    return {
        "symbol": "NVDA",
        "strategy": "Iron Condor",
        "ticket_name": "IRON CONDOR",
        "supported": True,
        "reason": None,
        "naked": False,
        "model": "lognormal_flat_iv",
        "spot": 200.12,
        "dte": 8,
        "legs": [
            {"qty": 1, "side": "P", "strike": 192.5, "expiry": "2026-05-01",
             "premium": 0.80, "iv_pct": 41.2, "delta": -0.08},
            {"qty": -1, "side": "P", "strike": 197.5, "expiry": "2026-05-01",
             "premium": 1.40, "iv_pct": 39.8, "delta": -0.18},
            {"qty": -1, "side": "C", "strike": 207.5, "expiry": "2026-05-01",
             "premium": 1.60, "iv_pct": 38.1, "delta": 0.22},
            {"qty": 1, "side": "C", "strike": 210, "expiry": "2026-05-01",
             "premium": 0.90, "iv_pct": 37.5, "delta": 0.15},
        ],
        "ticket": (
            "SELL -1 IRON CONDOR NVDA 100 (Weeklys) 1 MAY 26 "
            "210/207.5/197.5/192.5 CALL/PUT @1.30 LMT"
        ),
        "net_premium": 1.30,  # per share, signed: positive = credit.
        "net_credit": 1.30,
        "net_debit": 0.0,
        "pop": 0.683,
        "ev": 42.0,
        "max_profit": 130.0,
        "max_loss": -370.0,
        "breakevens": [196.20, 208.80],
        "prob_touch": [0.245, 0.231],
        "greeks": {"delta": -0.02, "gamma": -0.012, "theta": 6.20, "vega": -8.40},
        "warnings": ["prob_touch_approx"],
    }


def _unsupported_calendar() -> dict:
    """Metrics envelope for a Phase-2 shape — analytics are null,
    ticket and warnings are still populated."""
    return {
        "symbol": "AMZN",
        "strategy": "Calendar Spread",
        "ticket_name": "CALENDAR",
        "supported": False,
        "reason": "multi-expiry",
        "naked": False,
        "model": None,
        "spot": 255.36,
        "dte": None,
        "legs": [
            {"qty": -1, "side": "C", "strike": 300, "expiry": "2026-05-01",
             "premium": 5.00, "iv_pct": 35.0, "delta": 0.15},
            {"qty": 1, "side": "C", "strike": 300, "expiry": "2026-07-01",
             "premium": 8.00, "iv_pct": 38.0, "delta": 0.28},
        ],
        "ticket": "BUY +1 CALENDAR AMZN 100 1 MAY 26/1 JUL 26 300 CALL @3.00 LMT",
        "net_premium": -3.00,
        "net_credit": 0.0,
        "net_debit": 3.00,
        "pop": None,
        "ev": None,
        "max_profit": None,
        "max_loss": None,
        "breakevens": None,
        "prob_touch": None,
        "greeks": {"delta": None, "gamma": None, "theta": None, "vega": None},
        "warnings": ["analytics_not_supported_yet:multi-expiry"],
    }


# ---- HUMAN -------------------------------------------------------------


def test_render_human_carries_strategy_label_and_ticket():
    out = render_strategy(_ic_metrics(), fmt=Format.HUMAN)
    assert "Iron Condor" in out
    assert "Schwab order ticket" in out
    assert "SELL -1 IRON CONDOR" in out


def test_render_human_shows_pop_ev_max_profit_max_loss():
    out = render_strategy(_ic_metrics(), fmt=Format.HUMAN)
    assert "POP" in out
    assert "68" in out  # 68.3%
    assert "EV" in out
    # Max loss line should show the dollar value.
    assert "-$370.00" in out or "-370" in out


def test_render_human_shows_breakevens_with_distance():
    out = render_strategy(_ic_metrics(), fmt=Format.HUMAN)
    assert "196.20" in out
    assert "208.80" in out


def test_render_human_net_credit_text_label():
    out = render_strategy(_ic_metrics(), fmt=Format.HUMAN)
    # Uses text label, not raw signed net_premium.
    assert "Net Credit" in out


def test_render_human_net_debit_text_label():
    m = dict(_ic_metrics())
    m["net_premium"] = -2.00
    m["net_credit"] = 0.0
    m["net_debit"] = 2.00
    out = render_strategy(m, fmt=Format.HUMAN)
    assert "Net Debit" in out


def test_render_human_unlimited_loss_shows_word_unlimited():
    m = dict(_ic_metrics())
    m["max_loss"] = None
    m["warnings"] = ["unlimited_loss", "naked_short_call"]
    out = render_strategy(m, fmt=Format.HUMAN)
    assert "unlimited" in out.lower()


def test_render_human_shows_warnings_block():
    out = render_strategy(_ic_metrics(), fmt=Format.HUMAN)
    assert "Warnings" in out or "⚠" in out
    assert "prob_touch_approx" in out


def test_render_human_unsupported_shape_still_shows_ticket():
    out = render_strategy(_unsupported_calendar(), fmt=Format.HUMAN)
    # Ticket still rendered.
    assert "BUY +1 CALENDAR" in out
    # But analytics are marked as not supported.
    assert "not supported" in out.lower() or "Phase 2" in out
    # No fake numbers.
    assert "POP: —" in out or "POP" not in out or "—" in out


# ---- JSON --------------------------------------------------------------


def test_render_json_roundtrips_metrics():
    out = render_strategy(_ic_metrics(), fmt=Format.JSON)
    data = json.loads(out)
    assert data["strategy"] == "Iron Condor"
    assert data["pop"] == 0.683
    assert data["net_premium"] == 1.30
    assert data["ticket"].startswith("SELL -1 IRON CONDOR")
    assert data["warnings"] == ["prob_touch_approx"]


def test_render_json_unlimited_loss_becomes_string():
    m = dict(_ic_metrics())
    m["max_loss"] = None
    m["warnings"] = ["unlimited_loss"]
    out = render_strategy(m, fmt=Format.JSON)
    data = json.loads(out)
    # Convention: None → "unlimited" string for unlimited-loss positions.
    assert data["max_loss"] == "unlimited"


def test_render_json_unsupported_shape_preserves_nulls():
    out = render_strategy(_unsupported_calendar(), fmt=Format.JSON)
    data = json.loads(out)
    assert data["supported"] is False
    assert data["reason"] == "multi-expiry"
    assert data["pop"] is None
    assert data["ev"] is None
    assert data["ticket"].startswith("BUY +1 CALENDAR")


# ---- MD ----------------------------------------------------------------


def test_render_md_has_gfm_structure():
    out = render_strategy(_ic_metrics(), fmt=Format.MD)
    assert out.startswith("# NVDA Iron Condor")
    assert "## Legs" in out
    assert "| Qty | Side | Strike" in out
    assert "## Metrics" in out
    # Ticket in a code block.
    assert "```" in out
    assert "SELL -1 IRON CONDOR" in out


def test_render_md_shows_warnings():
    out = render_strategy(_ic_metrics(), fmt=Format.MD)
    assert "Warnings" in out
    assert "prob_touch_approx" in out
