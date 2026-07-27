"""BUG-2 broker-IV drift telemetry (signal-only)."""
from __future__ import annotations

import math

from schwab_cli.analytics.bs import bs_price
from schwab_cli.analytics.iv_drift import sample_iv_drift


def _contract(side, strike, iv, bid, ask, dte=26):
    return {"side": side, "strike": strike, "iv": iv, "delta": 0.5,
            "bid": bid, "ask": ask, "dte": dte}


def _priced_contract(side, strike, true_iv, spot, dte, r, broker_iv,
                     spread=0.05):
    """A contract whose mid is BS-consistent with true_iv, but whose broker
    iv field is deliberately broker_iv (to simulate a broker mismatch)."""
    T = dte / 365.0
    mid = bs_price(spot, strike, T, r, true_iv, is_call=(side == "C"))
    return {"side": side, "strike": strike, "iv": broker_iv, "delta": 0.5,
            "bid": round(mid - spread / 2, 4), "ask": round(mid + spread / 2, 4),
            "dte": dte}


def test_recovers_true_iv_from_mid_and_measures_drift():
    spot, dte, r = 319.09, 26, 0.04
    # Broker reports 28.93% but the mid is priced at the true 31.39%.
    c = _priced_contract("C", 320.0, 0.3139, spot, dte, r, broker_iv=0.2893)
    recs = sample_iv_drift([{"expiry": "2026-08-21", "dte": dte,
                             "contracts": [c]}],
                           spot=spot, r=r, now_ms=1, symbol="GOOG")
    assert len(recs) == 1
    rec = recs[0]
    assert rec["iv_solved"] == recs[0]["iv_solved"]
    assert abs(rec["iv_solved"] - 0.3139) < 0.005      # recovered true IV
    assert abs(rec["drift"] - (0.3139 - 0.2893)) < 0.005
    assert rec["warn"] is True                          # 2.5 pt > 1 pt
    assert rec["moneyness"] == round(320.0 / spot, 4)   # layered field present


def test_excludes_wide_spreads():
    spot, dte, r = 100.0, 30, 0.04
    c = _priced_contract("C", 100.0, 0.30, spot, dte, r, broker_iv=0.30,
                         spread=5.0)   # huge spread
    recs = sample_iv_drift([{"expiry": "x", "dte": dte, "contracts": [c]}],
                           spot=spot, r=r, now_ms=1, symbol="X")
    assert recs == []


def test_excludes_far_otm_and_sentinels():
    spot, dte, r = 100.0, 30, 0.04
    far = _priced_contract("C", 130.0, 0.30, spot, dte, r, broker_iv=0.30)
    sentinel = _contract("P", 100.0, -9.99, 1.0, 1.2)
    recs = sample_iv_drift([{"expiry": "x", "dte": dte,
                             "contracts": [far, sentinel]}],
                           spot=spot, r=r, now_ms=1, symbol="X")
    assert recs == []


def test_excludes_leaps():
    spot, dte, r = 100.0, 400, 0.04
    c = _priced_contract("C", 100.0, 0.30, spot, dte, r, broker_iv=0.30)
    recs = sample_iv_drift([{"expiry": "x", "dte": dte, "contracts": [c]}],
                           spot=spot, r=r, now_ms=1, symbol="X")
    assert recs == []


def test_matching_broker_iv_does_not_warn():
    spot, dte, r = 100.0, 30, 0.04
    c = _priced_contract("C", 100.0, 0.30, spot, dte, r, broker_iv=0.30)
    recs = sample_iv_drift([{"expiry": "x", "dte": dte, "contracts": [c]}],
                           spot=spot, r=r, now_ms=1, symbol="X")
    assert len(recs) == 1
    assert recs[0]["warn"] is False
    assert abs(recs[0]["drift"]) < 0.005
