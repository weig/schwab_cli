"""Tests for ``summarise_preview`` against real Schwab response shapes.

The shape was captured live from the trader API on 2026-04-25 — the
parser regressed at one point because the legacy guesses didn't match
the real keys, dropping a real REJECT silently. These tests pin the
mapping so that can't happen again.
"""

from __future__ import annotations

from schwab_cli.output.orders import PreviewSummary, summarise_preview


# ---- captured Schwab response (NVDA equity SELL @ 190 LIMIT) -------------


_REAL_PREVIEW: dict = {
    "commissionAndFee": {
        "commission": {
            "commissionLegs": [
                {"commissionValues": [
                    {"type": "BASE_CHARGE", "value": 0.0},
                    {"type": "COMMISSION", "value": 0.0},
                ]},
            ],
        },
        "fee": {
            "feeLegs": [
                {"feeValues": [
                    {"type": "SEC_FEE", "value": 0.02},
                    {"type": "OPT_REG_FEE", "value": 0.0},
                    {"type": "TAF_FEE", "value": 0.01},
                ]},
            ],
        },
        "trueCommission": {"commissionLegs": []},
    },
    "orderId": 0,
    "orderStrategy": {
        "accountNumber": "57410756",
        "orderBalance": {
            "orderValue": 190.0,
            "projectedAvailableFund": 26337.05,
            "projectedBuyingPower": 52674.10,
            "projectedCommission": 0.0,
        },
        "orderLegs": [
            {
                "askPrice": 208.10,
                "assetType": "EQUITY",
                "bidPrice": 208.09,
                "finalSymbol": "NVDA",
                "instruction": "SELL",
                "instrument": {"assetType": "EQUITY", "symbol": "NVDA"},
                "lastPrice": 208.27,
                "legId": 1,
                "markPrice": 208.27,
                "positionEffect": "CLOSING",
                "projectedCommission": 0.0,
            },
        ],
        "orderStrategyType": "SINGLE",
        "orderType": "LIMIT",
        "orderValue": 190.0,
        "price": 190.0,
        "quantity": 1.0,
        "status": "REJECTED",
    },
    "orderValidationResult": {
        "rejects": [
            {
                "activityMessage": (
                    "Your limit is significantly higher or lower than the "
                    "last traded price. Confirm you are trading the correct "
                    "security."
                ),
                "originalSeverity": "REJECT",
            },
        ],
    },
}


def test_commission_sums_across_value_types():
    s = summarise_preview(_REAL_PREVIEW)
    # BASE_CHARGE 0 + COMMISSION 0 = 0 (so we should see 0.0, not None)
    assert s.commission == 0.0


def test_fees_sum_across_sec_optreg_taf():
    s = summarise_preview(_REAL_PREVIEW)
    assert s.fees == 0.03  # 0.02 + 0.0 + 0.01


def test_bp_after_stock_uses_projected_buying_power():
    s = summarise_preview(_REAL_PREVIEW)
    assert s.bp_after_stock == 52674.10


def test_bp_after_option_uses_projected_available_fund():
    s = summarise_preview(_REAL_PREVIEW)
    assert s.bp_after_option == 26337.05


def test_bp_effect_signed_positive_for_sell():
    s = summarise_preview(_REAL_PREVIEW)
    # SELL frees BP — so bp_effect should be positive orderValue.
    assert s.bp_effect == 190.0


def test_bp_effect_signed_negative_for_buy():
    """Same shape, BUY instead of SELL — BP effect should flip sign."""
    p = _deep_copy(_REAL_PREVIEW)
    p["orderStrategy"]["orderLegs"][0]["instruction"] = "BUY"
    s = summarise_preview(p)
    assert s.bp_effect == -190.0


def test_rejects_are_extracted_from_activityMessage():
    """Regression: parser used to look for 'message' but Schwab uses
    'activityMessage'. A real REJECT was being silently dropped."""
    s = summarise_preview(_REAL_PREVIEW)
    assert len(s.rejects) == 1
    assert "limit is significantly higher" in s.rejects[0]


def test_rejects_legacy_message_key_still_works():
    p = {
        "orderValidationResult": {
            "rejects": [{"message": "legacy reject text"}],
        },
    }
    s = summarise_preview(p)
    assert s.rejects == ("legacy reject text",)


def test_warnings_collected_from_warns_alerts_reviews():
    p = {
        "orderValidationResult": {
            "warns": [{"activityMessage": "warn-1"}],
            "alerts": [{"activityMessage": "alert-1"}],
            "reviews": [{"message": "review-1"}],
        },
    }
    s = summarise_preview(p)
    assert set(s.warnings) == {"warn-1", "alert-1", "review-1"}
    assert s.rejects == ()


def test_empty_preview_yields_all_none():
    s = summarise_preview({})
    assert s == PreviewSummary(None, None, None, None, None, (), ())


def test_none_preview_yields_all_none():
    s = summarise_preview(None)
    assert s == PreviewSummary(None, None, None, None, None, (), ())


def test_missing_commissionAndFee_block_yields_none_not_zero():
    """If Schwab omits the block entirely (vs returning zero values),
    we should report None (unknown) — not 0.0 (free)."""
    p = _deep_copy(_REAL_PREVIEW)
    del p["commissionAndFee"]
    s = summarise_preview(p)
    assert s.commission is None
    assert s.fees is None


def test_bp_effect_zero_legs_returns_none():
    """Ambiguous side mix (or no legs) → bp_effect is None, not 0.0."""
    p = _deep_copy(_REAL_PREVIEW)
    p["orderStrategy"]["orderLegs"] = []
    s = summarise_preview(p)
    assert s.bp_effect is None


def test_bp_effect_handles_options_buy_to_open():
    p = _deep_copy(_REAL_PREVIEW)
    p["orderStrategy"]["orderLegs"][0]["instruction"] = "BUY_TO_OPEN"
    s = summarise_preview(p)
    assert s.bp_effect == -190.0


def test_bp_effect_handles_options_sell_to_open():
    p = _deep_copy(_REAL_PREVIEW)
    p["orderStrategy"]["orderLegs"][0]["instruction"] = "SELL_TO_OPEN"
    s = summarise_preview(p)
    assert s.bp_effect == 190.0


# ---- helpers ------------------------------------------------------------


def _deep_copy(d: dict) -> dict:
    import copy as _copy
    return _copy.deepcopy(d)
