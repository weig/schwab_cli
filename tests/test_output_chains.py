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
    # Positive delta → green (ANSI 32); negative → red (ANSI 31). Colors
    # may merge with bold into a compound SGR like \x1b[1;32m on ITM rows.
    assert "32m" in out
    assert "31m" in out


def test_render_chain_human_a_bolds_itm_rows():
    out = render_chain(_envelope(), fmt=Format.HUMAN, detail=0,
                       requested_type="ALL", width=160)
    # ITM row → bold ANSI present. Bold may appear standalone as \x1b[1m
    # or merged with color as \x1b[1;32m — "1m" appears in both.
    assert "1m" in out


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
    # Non-ITM strike row should not wrap the strike label in bold.
    # With nested markup, bold wraps around the strike cell producing
    # "\x1b[1m80.00" on ITM rows; OTM rows render the plain cell.
    assert "80.00" in out
    assert "\x1b[1m80.00" not in out


def test_render_chain_human_b_columns_present():
    out = render_chain(_envelope(), fmt=Format.HUMAN, detail=1,
                       requested_type="ALL", width=160)
    # Symbol, Side, Strike, Bid, Ask, Last are required columns.
    assert "Symbol" in out
    assert "Side" in out
    assert "Strike" in out
    # Greeks columns
    assert "IV" in out
    assert "Δ" in out
    assert "Γ" in out
    assert "Θ" in out
    assert "Vol" in out
    assert "OI" in out


def test_render_chain_human_b_one_row_per_contract():
    out = render_chain(_envelope(), fmt=Format.HUMAN, detail=1,
                       requested_type="ALL", width=160)
    # 3 calls + 3 puts = 6 contract rows
    assert out.count("270115C00135000") == 1
    assert out.count("270115P00135000") == 1
    assert out.count("270115C00140000") == 1
    assert out.count("270115P00140000") == 1


def test_render_chain_human_b_sorted_ascending_call_before_put():
    out = render_chain(_envelope(), fmt=Format.HUMAN, detail=1,
                       requested_type="ALL", width=160)
    # Call 135 appears before Put 135; Put 135 before Call 140.
    idx_c135 = out.index("270115C00135000")
    idx_p135 = out.index("270115P00135000")
    idx_c140 = out.index("270115C00140000")
    assert idx_c135 < idx_p135 < idx_c140


def test_render_chain_human_b_bolds_itm_rows():
    out = render_chain(_envelope(), fmt=Format.HUMAN, detail=1,
                       requested_type="ALL", width=160)
    assert "1m" in out  # bold ANSI — may be standalone or merged with color


def test_render_chain_human_b_emits_color_on_delta():
    out = render_chain(_envelope(), fmt=Format.HUMAN, detail=1,
                       requested_type="ALL", width=160)
    assert "32m" in out  # green (may merge with bold)
    assert "31m" in out  # red (may merge with bold)


def test_render_chain_human_a_falls_back_to_b_when_puts_only(capsys):
    # Envelope with only put contracts
    puts_only_raw = {**_RAW_MULTI_STRIKE, "callExpDateMap": {}}
    env = shape_envelope(puts_only_raw)
    out = render_chain(env, fmt=Format.HUMAN, detail=0,
                       requested_type="PUT", width=160)
    # Layout B markers (per-contract rows) rather than Layout A (STRIKE centered).
    assert "Side" in out
    assert "Symbol" in out
    # Stderr note about fallback.
    err = capsys.readouterr().err
    assert "one-sided" in err
    assert "--detail=1" in err


def test_render_chain_human_a_no_fallback_when_both_sides(capsys):
    render_chain(_envelope(), fmt=Format.HUMAN, detail=0,
                 requested_type="ALL", width=160)
    err = capsys.readouterr().err
    assert "one-sided" not in err


def test_render_chain_human_detail2_has_main_row_plus_continuation():
    out = render_chain(_envelope(), fmt=Format.HUMAN, detail=2,
                       requested_type="ALL", width=180)
    # Main row present
    assert "270115C00135000" in out
    # Continuation lines with Mark / DTE / B.Sz etc.
    assert "Mark:" in out
    assert "B.Sz:" in out
    assert "A.Sz:" in out
    assert "L.Sz:" in out
    assert "DTE:" in out
    assert "Time Val:" in out
    assert "Intrinsic:" in out


def test_render_chain_human_detail2_settlement_suffix_in_symbol():
    out = render_chain(_envelope(), fmt=Format.HUMAN, detail=2,
                       requested_type="ALL", width=180)
    # settlementType="P" maps to (PM)
    assert "(PM)" in out


def test_render_chain_human_detail2_no_multiplier_or_itm_columns():
    out = render_chain(_envelope(), fmt=Format.HUMAN, detail=2,
                       requested_type="ALL", width=180)
    assert "Mult:" not in out
    assert "ITM:" not in out


def test_render_chain_human_a_drops_rightmost_columns_at_narrow_width(capsys):
    # At 60 cols the Δ, OI, Vol pairs cannot fit — drop from the right.
    out = render_chain(_envelope(), fmt=Format.HUMAN, detail=0,
                       requested_type="ALL", width=60)
    err = capsys.readouterr().err
    assert "terminal too narrow" in err
    assert "--detail=1" in err
    # STRIKE column and B/A/L always kept.
    assert "STRIKE" in out


def test_render_chain_human_b_drops_rightmost_greeks_at_narrow_width(capsys):
    out = render_chain(_envelope(), fmt=Format.HUMAN, detail=1,
                       requested_type="ALL", width=70)
    err = capsys.readouterr().err
    assert "terminal too narrow" in err
    # Symbol, Side, Strike, Bid, Ask, Last always kept.
    assert "Symbol" in out
    assert "Strike" in out
    assert "Bid" in out


def test_render_chain_wide_width_keeps_all_columns(capsys):
    render_chain(_envelope(), fmt=Format.HUMAN, detail=1,
                 requested_type="ALL", width=200)
    err = capsys.readouterr().err
    assert "too narrow" not in err


import json as _json_test


def test_render_chain_json_detail0_fields():
    out = render_chain(_envelope(), fmt=Format.JSON, detail=0,
                       requested_type="ALL")
    data = _json_test.loads(out)
    assert data["symbol"] == "NVDA"
    assert data["expiry"] == "2027-01-15"
    row = data["contracts"][0]
    assert set(["optionSymbol", "side", "strike", "bid", "ask", "last", "delta"]).issubset(row)
    # detail=0 excludes greeks beyond delta and vol/OI/etc.
    assert "iv" not in row
    assert "gamma" not in row
    assert "volume" not in row


def test_render_chain_json_detail1_adds_greeks_and_vol():
    out = render_chain(_envelope(), fmt=Format.JSON, detail=1,
                       requested_type="ALL")
    data = _json_test.loads(out)
    row = data["contracts"][0]
    for key in ["iv", "gamma", "theta", "vega", "volume", "openInterest"]:
        assert key in row
    # detail=2-only fields absent
    assert "mark" not in row
    assert "rho" not in row


def test_render_chain_json_detail2_has_all_fields():
    out = render_chain(_envelope(), fmt=Format.JSON, detail=2,
                       requested_type="ALL")
    data = _json_test.loads(out)
    row = data["contracts"][0]
    for key in [
        "mark", "bidSize", "askSize", "lastSize",
        "open", "high", "low", "close",
        "rho", "timeValue", "intrinsic",
        "inTheMoney", "multiplier", "settlementType",
    ]:
        assert key in row


def test_render_chain_json_no_ansi_codes():
    for d in (0, 1, 2):
        out = render_chain(_envelope(), fmt=Format.JSON, detail=d,
                           requested_type="ALL")
        assert "\x1b[" not in out


def test_render_chain_json_nan_iv_serialized_as_null():
    out = render_chain(_envelope(), fmt=Format.JSON, detail=1,
                       requested_type="ALL")
    data = _json_test.loads(out)
    call_145 = next(r for r in data["contracts"]
                    if r["side"] == "C" and r["strike"] == 145.0)
    assert call_145["iv"] is None


def test_render_chain_md_detail0_has_header_and_table():
    out = render_chain(_envelope(), fmt=Format.MD, detail=0,
                       requested_type="ALL")
    lines = out.splitlines()
    assert lines[0].startswith("# NVDA")
    assert "2027-01-15" in lines[0]
    assert "**Spot:**" in out
    # Table header row
    assert "| Symbol | Side | Strike |" in out


def test_render_chain_md_detail0_itm_symbol_and_strike_bolded():
    out = render_chain(_envelope(), fmt=Format.MD, detail=0,
                       requested_type="ALL")
    # ITM call at strike 135 → both cells bolded
    assert "**NVDA  270115C00135000**" in out
    assert "| **135.00** |" in out


def test_render_chain_md_detail2_includes_details_subtable():
    out = render_chain(_envelope(), fmt=Format.MD, detail=2,
                       requested_type="ALL")
    # Blockquoted details heading with Settle suffix
    assert "> **Details — NVDA  270115C00135000** (Settle: PM)" in out
    # Sub-table header
    assert "| Mark | L.Sz | B.Sz | A.Sz |" in out


def test_render_chain_md_detail1_adds_greeks_columns():
    out = render_chain(_envelope(), fmt=Format.MD, detail=1,
                       requested_type="ALL")
    header_line = next(ln for ln in out.splitlines() if ln.startswith("| Symbol"))
    assert "IV" in header_line
    assert "Δ" in header_line
    assert "Γ" in header_line


def test_render_chain_md_no_ansi_codes():
    for d in (0, 1, 2):
        out = render_chain(_envelope(), fmt=Format.MD, detail=d,
                           requested_type="ALL")
        assert "\x1b[" not in out
