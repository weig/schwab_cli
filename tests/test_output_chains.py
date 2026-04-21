from schwab_cli.output.chains import render_chain, shape_envelope
from schwab_cli.output.format import Format


_RAW_MULTI_STRIKE = {
    "symbol": "NVDA",
    "status": "SUCCESS",
    "underlying": {"symbol": "NVDA", "last": 142.35, "change": 2.10, "percentChange": 1.50},
    "callExpDateMap": {
        "2027-01-15:632": {
            "135.0": [{
                "putCall": "CALL", "symbol": "NVDA  270115C00135000",
                "bid": 8.40, "ask": 8.50, "last": 8.45,
                "delta": 0.71, "gamma": 0.018, "theta": -0.04, "vega": 0.18, "rho": 0.052,
                "volatility": 35.0,
                "strikePrice": 135.0,
                "inTheMoney": True,
                "totalVolume": 123, "openInterest": 456,
                "mark": 8.45, "bidSize": 10, "askSize": 15, "lastSize": 1,
                "openPrice": 8.10, "highPrice": 8.60, "lowPrice": 8.05, "closePrice": 8.35,
                "timeValue": 8.45, "intrinsicValue": 7.35,
                "multiplier": 100, "settlementType": "P",
                "expirationDate": "2027-01-15", "daysToExpiration": 632,
            }],
            "140.0": [{
                "putCall": "CALL", "symbol": "NVDA  270115C00140000",
                "bid": 5.15, "ask": 5.25, "last": 5.20,
                "delta": 0.58, "strikePrice": 140.0, "inTheMoney": True,
                "volatility": 33.0, "multiplier": 100, "settlementType": "P",
                "expirationDate": "2027-01-15", "daysToExpiration": 632,
            }],
            "145.0": [{
                "putCall": "CALL", "symbol": "NVDA  270115C00145000",
                "bid": 1.70, "ask": 1.80, "last": 1.75,
                "delta": 0.41, "strikePrice": 145.0, "inTheMoney": False,
                "volatility": float("nan"),
                "multiplier": 100, "settlementType": "P",
                "expirationDate": "2027-01-15", "daysToExpiration": 632,
            }],
        },
    },
    "putExpDateMap": {
        "2027-01-15:632": {
            "135.0": [{
                "putCall": "PUT", "symbol": "NVDA  270115P00135000",
                "bid": 0.42, "ask": 0.45, "last": 0.43,
                "delta": -0.12, "strikePrice": 135.0, "inTheMoney": False,
                "volatility": 38.0, "multiplier": 100, "settlementType": "P",
                "expirationDate": "2027-01-15", "daysToExpiration": 632,
            }],
            "140.0": [{
                "putCall": "PUT", "symbol": "NVDA  270115P00140000",
                "bid": 1.15, "ask": 1.20, "last": 1.18,
                "delta": -0.23, "strikePrice": 140.0, "inTheMoney": False,
                "volatility": 36.0, "multiplier": 100, "settlementType": "P",
                "expirationDate": "2027-01-15", "daysToExpiration": 632,
            }],
            "145.0": [{
                "putCall": "PUT", "symbol": "NVDA  270115P00145000",
                "bid": 4.10, "ask": 4.15, "last": 4.12,
                "delta": -0.58, "strikePrice": 145.0, "inTheMoney": True,
                "volatility": 34.0, "multiplier": 100, "settlementType": "P",
                "expirationDate": "2027-01-15", "daysToExpiration": 632,
            }],
        },
    },
}


def test_shape_envelope_header():
    env = shape_envelope(_RAW_MULTI_STRIKE)
    assert env["symbol"] == "NVDA"
    assert env["expiry"] == "2027-01-15"
    assert env["dte"] == 632
    assert env["underlying"]["last"] == 142.35
    assert env["underlying"]["netChange"] == 2.10
    assert env["underlying"]["pctChange"] == 1.50


def test_shape_envelope_contracts_sorted_ascending_call_before_put():
    env = shape_envelope(_RAW_MULTI_STRIKE)
    rows = env["contracts"]
    assert len(rows) == 6
    # ordered: (135 C), (135 P), (140 C), (140 P), (145 C), (145 P)
    assert [(r["strike"], r["side"]) for r in rows] == [
        (135.0, "C"), (135.0, "P"),
        (140.0, "C"), (140.0, "P"),
        (145.0, "C"), (145.0, "P"),
    ]


def test_shape_envelope_normalizes_iv_from_percent_to_fraction():
    env = shape_envelope(_RAW_MULTI_STRIKE)
    call_135 = next(r for r in env["contracts"] if r["side"] == "C" and r["strike"] == 135.0)
    assert call_135["iv"] == 0.35  # 35.0 / 100


def test_shape_envelope_nan_iv_becomes_none():
    env = shape_envelope(_RAW_MULTI_STRIKE)
    call_145 = next(r for r in env["contracts"] if r["side"] == "C" and r["strike"] == 145.0)
    assert call_145["iv"] is None


def test_shape_envelope_option_symbol_internal_spaces_preserved():
    env = shape_envelope(_RAW_MULTI_STRIKE)
    call_135 = next(r for r in env["contracts"] if r["side"] == "C" and r["strike"] == 135.0)
    assert call_135["optionSymbol"] == "NVDA  270115C00135000"  # Schwab's internal double-space preserved


def test_shape_envelope_in_the_money_preserved():
    env = shape_envelope(_RAW_MULTI_STRIKE)
    call_135 = next(r for r in env["contracts"] if r["side"] == "C" and r["strike"] == 135.0)
    call_145 = next(r for r in env["contracts"] if r["side"] == "C" and r["strike"] == 145.0)
    assert call_135["inTheMoney"] is True
    assert call_145["inTheMoney"] is False


def test_shape_envelope_trim_to_strike_count_keeps_n_closest_to_atm():
    env = shape_envelope(_RAW_MULTI_STRIKE, strike_count=2)
    # spot 142.35 — 2 strikes closest are 140 and 145 (both closer than 135)
    strikes = sorted({r["strike"] for r in env["contracts"]})
    assert strikes == [140.0, 145.0]


def test_shape_envelope_failed_status_returns_empty_contracts():
    raw = {"symbol": "XYZZZ", "status": "FAILED",
           "callExpDateMap": {}, "putExpDateMap": {}}
    env = shape_envelope(raw)
    assert env["contracts"] == []
    assert env["expiry"] is None


def test_shape_envelope_settlement_type_preserved():
    env = shape_envelope(_RAW_MULTI_STRIKE)
    call_135 = next(r for r in env["contracts"] if r["side"] == "C" and r["strike"] == 135.0)
    assert call_135["settlementType"] == "P"


def test_shape_envelope_trim_tie_breaks_toward_lower_strike():
    raw = {
        "symbol": "TIE",
        "underlying": {"last": 100.0, "change": 0, "percentChange": 0},
        "callExpDateMap": {
            "2027-01-15:100": {
                "90.0": [{"putCall": "CALL", "symbol": "C90",
                          "strikePrice": 90.0, "inTheMoney": True,
                          "expirationDate": "2027-01-15", "daysToExpiration": 100}],
                "110.0": [{"putCall": "CALL", "symbol": "C110",
                           "strikePrice": 110.0, "inTheMoney": False,
                           "expirationDate": "2027-01-15", "daysToExpiration": 100}],
            },
        },
        "putExpDateMap": {},
    }
    env = shape_envelope(raw, strike_count=1)
    strikes = sorted({r["strike"] for r in env["contracts"]})
    # 90 and 110 are equidistant from spot 100 — tie-break picks the lower strike.
    assert strikes == [90.0]


def test_shape_envelope_trim_with_count_exceeding_available_keeps_all():
    env = shape_envelope(_RAW_MULTI_STRIKE, strike_count=99)
    # Only 3 strikes exist in fixture — keep all 3 (6 contracts: 3 calls + 3 puts).
    assert len(env["contracts"]) == 6


def _envelope():
    return shape_envelope(_RAW_MULTI_STRIKE)


def test_render_chain_human_a_has_header():
    out = render_chain(_envelope(), fmt=Format.HUMAN, detail=0,
                       requested_type="ALL", width=160)
    assert "NVDA" in out
    assert "2027-01-15" in out
    assert "632" in out  # DTE
    assert "142.35" in out  # spot


def test_render_chain_human_a_has_strike_column_centered():
    out = render_chain(_envelope(), fmt=Format.HUMAN, detail=0,
                       requested_type="ALL", width=160)
    assert "STRIKE" in out
    # Strikes present
    assert "135.00" in out
    assert "140.00" in out
    assert "145.00" in out


def test_render_chain_human_a_marks_atm_row():
    out = render_chain(_envelope(), fmt=Format.HUMAN, detail=0,
                       requested_type="ALL", width=160)
    # Spot is 142.35; closest strike is 140.00 (140 vs 145: |142.35-140|=2.35 < |142.35-145|=2.65)
    # The ATM marker `←` appears in the output.
    assert "←" in out


def test_render_chain_human_a_emits_ansi_color():
    out = render_chain(_envelope(), fmt=Format.HUMAN, detail=0,
                       requested_type="ALL", width=160)
    # Positive delta (call) shows green; negative (put) shows red.
    assert "\x1b[32m" in out  # green
    assert "\x1b[31m" in out  # red


def test_render_chain_human_a_bolds_itm_rows():
    out = render_chain(_envelope(), fmt=Format.HUMAN, detail=0,
                       requested_type="ALL", width=160)
    # At least one ITM row exists → bold ANSI present.
    assert "\x1b[1m" in out


def test_render_chain_human_a_non_itm_strike_row_not_bold():
    # Fixture contains strikes where BOTH sides are OTM (ITM is union false).
    raw = {
        "symbol": "NVDA",
        "underlying": {"last": 100.0, "change": 0, "percentChange": 0},
        "callExpDateMap": {
            "2027-01-15:100": {
                "120.0": [{
                    "putCall": "CALL", "symbol": "C120",
                    "strikePrice": 120.0, "inTheMoney": False,
                    "bid": 1.0, "ask": 1.1, "last": 1.05,
                    "delta": 0.2, "expirationDate": "2027-01-15",
                    "daysToExpiration": 100,
                }],
            },
        },
        "putExpDateMap": {
            "2027-01-15:100": {
                "120.0": [{
                    "putCall": "PUT", "symbol": "P120",
                    "strikePrice": 120.0, "inTheMoney": True,  # put is ITM at 120 when spot=100
                    "bid": 20.0, "ask": 20.2, "last": 20.1,
                    "delta": -0.8, "expirationDate": "2027-01-15",
                    "daysToExpiration": 100,
                }],
                "80.0": [{
                    "putCall": "PUT", "symbol": "P80",
                    "strikePrice": 80.0, "inTheMoney": False,  # both sides OTM at 80 when spot=100
                    "bid": 0.1, "ask": 0.15, "last": 0.12,
                    "delta": -0.05, "expirationDate": "2027-01-15",
                    "daysToExpiration": 100,
                }],
            },
        },
    }
    env = shape_envelope(raw)
    out = render_chain(env, fmt=Format.HUMAN, detail=0,
                       requested_type="ALL", width=160)
    # The 80.0 strike row has call=None (no call at 80) and put=OTM — no ITM.
    # Its strike label should not have the bold ANSI adjacent to it.
    # Easiest check: the row for strike 80.00 exists and does NOT have
    # the bold-strike-label `[bold]80.00[/]` → no `\x1b[1m80.00` substring.
    assert "80.00" in out
    assert "\x1b[1m80.00" not in out
