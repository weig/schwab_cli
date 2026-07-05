"""Tests for the target-put locator."""
from __future__ import annotations

from schwab_cli.screener import locate


def _chain(puts_by_expiry: dict, underlying: float = 540.0) -> dict:
    """Build a raw-Schwab-shaped chain from {(\"expiry\", dte): [rows]}."""
    put_map: dict = {}
    for (expiry, dte), rows in puts_by_expiry.items():
        strike_map = {str(r["strikePrice"]): [r] for r in rows}
        put_map[f"{expiry}:{dte}"] = strike_map
    return {"underlying": {"last": underlying}, "putExpDateMap": put_map}


def _put(strike, delta, bid, ask, oi=1000, vol=200):
    return {
        "strikePrice": strike, "delta": delta, "bid": bid, "ask": ask,
        "openInterest": oi, "totalVolume": vol,
    }


def test_is_third_friday():
    assert locate.is_third_friday("2026-08-21")  # 3rd Friday of Aug 2026
    assert not locate.is_third_friday("2026-08-07")  # a weekly Friday
    assert not locate.is_third_friday("2026-08-20")  # Thursday


def test_prefers_monthly_nearest_30dte():
    chain = _chain({
        ("2026-08-07", 27): [_put(500, -0.25, 4.0, 4.2)],   # weekly, closer to 30
        ("2026-08-21", 41): [_put(495, -0.25, 5.0, 5.2)],   # monthly, out of window
        ("2026-08-14", 34): [_put(498, -0.25, 4.5, 4.7)],   # weekly in window
    })
    # No monthly in [25,35] window → nearest in-window weekly (27) wins.
    tp, reason = locate.locate_target_put(chain)
    assert reason is None
    assert tp.expiry == "2026-08-07" and tp.dte == 27


def test_monthly_wins_when_in_window():
    chain = _chain({
        ("2026-08-21", 33): [_put(495, -0.25, 5.0, 5.2)],   # monthly, in window
        ("2026-08-14", 26): [_put(498, -0.30, 4.5, 4.7)],   # weekly, in window
    })
    tp, reason = locate.locate_target_put(chain)
    assert reason is None
    assert tp.expiry == "2026-08-21"  # monthly preferred over closer weekly


def test_picks_delta_closest_to_target():
    chain = _chain({
        ("2026-08-21", 31): [
            _put(520, -0.40, 8.0, 8.2),
            _put(500, -0.26, 4.0, 4.2),   # closest to -0.25
            _put(480, -0.12, 1.0, 1.2),
        ],
    })
    tp, _ = locate.locate_target_put(chain)
    assert tp.strike == 500 and tp.delta == -0.26


def test_computes_mid_and_spread():
    chain = _chain({("2026-08-21", 31): [_put(500, -0.25, 4.0, 4.4)]})
    tp, _ = locate.locate_target_put(chain)
    assert tp.mid == 4.2
    assert abs(tp.spread_pct - (0.4 / 4.2)) < 1e-9


def test_spread_none_when_mid_zero():
    chain = _chain({("2026-08-21", 31): [_put(500, -0.25, 0.0, 0.0)]})
    tp, _ = locate.locate_target_put(chain)
    assert tp.spread_pct is None


def test_no_expiry_in_window():
    chain = _chain({("2026-09-18", 74): [_put(500, -0.25, 4.0, 4.2)]})
    tp, reason = locate.locate_target_put(chain)
    assert tp is None and reason == "no_expiry_in_window"


def test_no_puts():
    tp, reason = locate.locate_target_put({"underlying": {"last": 540.0}})
    assert tp is None and reason == "no_puts"


def test_underlying_last():
    assert locate.underlying_last({"underlying": {"last": 123.4}}) == 123.4
    assert locate.underlying_last({}) is None
