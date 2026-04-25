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
    assert s == PreviewSummary(None, None, None, None, (), ())


def test_none_preview_yields_all_none():
    s = summarise_preview(None)
    assert s == PreviewSummary(None, None, None, None, (), ())


def test_missing_commissionAndFee_block_yields_none_not_zero():
    """If Schwab omits the block entirely (vs returning zero values),
    we should report None (unknown) — not 0.0 (free)."""
    p = _deep_copy(_REAL_PREVIEW)
    del p["commissionAndFee"]
    s = summarise_preview(p)
    assert s.commission is None
    assert s.fees is None


# ---- BP-effect delta rendering (regression) -----------------------------
#
# The earlier guess (`orderValue` × side sign) was wrong for naked shorts:
# a $164 credit would tie up ~$12,700 of margin, but the panel showed
# +$164 instead of -$12,700. The renderer now computes the deltas from
# (projected after order) - (current account balance).


def test_render_confirmation_computes_bp_effect_deltas():
    from schwab_cli.output.orders import (
        OrderAnalytics, render_confirmation, summarise_preview,
    )
    preview = summarise_preview({
        "commissionAndFee": {
            "commission": {"commissionLegs": [
                {"commissionValues": [{"type": "COMMISSION", "value": 0.65}]},
            ]},
            "fee": {"feeLegs": [
                {"feeValues": [{"type": "SEC_FEE", "value": 0.01}]},
            ]},
        },
        "orderStrategy": {
            "orderBalance": {
                "orderValue": 164.0,
                "projectedBuyingPower": 27274.10,
                "projectedAvailableFund": 13637.05,
            },
            "orderLegs": [{"instruction": "SELL_TO_OPEN"}],
        },
    })
    body = {
        "orderType": "LIMIT", "duration": "DAY", "session": "NORMAL",
        "complexOrderStrategyType": "NONE",
        "orderLegCollection": [{
            "instruction": "SELL_TO_OPEN", "quantity": 1,
            "instrument": {"assetType": "OPTION", "symbol": "C   260501P00127000"},
        }],
    }
    out = render_confirmation(
        body=body,
        account_tail="0756",
        strategy_label="SELL 1 C PUT",
        is_naked_short=True,
        analytics=OrderAnalytics(
            max_profit=164.0, max_loss=-12536.0,
            breakevens=(125.36,), order_cost=-164.0,
        ),
        preview=preview,
        current_balances={
            "stockBuyingPower": 52674.10,
            "optionBuyingPower": 26337.05,
        },
    )
    # Single-line per BP bucket: current → effect → result.
    # Naked short ties up margin: effect = projected - current.
    assert "Buying Power (Stock)" in out
    assert "$52,674.10" in out and "-$25,400.00" in out and "$27,274.10" in out
    assert "Buying Power (Option)" in out
    assert "$26,337.05" in out and "-$12,700.00" in out and "$13,637.05" in out
    # Each row should contain two arrow separators.
    stock_line = next(ln for ln in out.splitlines() if "Buying Power (Stock)" in ln)
    option_line = next(ln for ln in out.splitlines() if "Buying Power (Option)" in ln)
    assert stock_line.count("→") == 2
    assert option_line.count("→") == 2


def test_render_confirmation_marks_bp_as_rejected_when_preview_rejects():
    """Schwab still returns ``projectedBuyingPower`` for a rejected
    preview, but the value is meaningless because the order won't fill.
    The panel should drop the projection and label both BP rows as
    rejected — matches TOS's "Illegal" treatment."""
    from schwab_cli.output.orders import render_confirmation, summarise_preview
    preview = summarise_preview({
        "orderStrategy": {
            "orderBalance": {
                "projectedBuyingPower": 47557.58,
                "projectedAvailableFund": 23778.79,
            },
        },
        "orderValidationResult": {
            "rejects": [{"activityMessage": "Account not approved."}],
        },
    })
    out = render_confirmation(
        body={"orderType": "LIMIT", "duration": "DAY", "session": "NORMAL",
              "orderLegCollection": []},
        account_tail="0756",
        strategy_label="t",
        is_naked_short=False,
        analytics=None,
        preview=preview,
        current_balances={
            "stockBuyingPower": 52674.10,
            "optionBuyingPower": 26337.05,
        },
    )
    # Misleading projected values must NOT appear.
    assert "47,557" not in out
    assert "23,778" not in out
    # Both rows show current BP + "rejected" suffix.
    assert "$52,674.10  (rejected" in out
    assert "$26,337.05  (rejected" in out


def test_render_confirmation_falls_back_to_n_a_without_balances():
    """When account-fetch is skipped (e.g. ``place --yes`` path), the
    delta lines collapse to ``n/a`` rather than guessing."""
    from schwab_cli.output.orders import render_confirmation, summarise_preview
    preview = summarise_preview({
        "orderStrategy": {
            "orderBalance": {
                "projectedBuyingPower": 14213.65,
                "projectedAvailableFund": 7106.83,
            },
        },
    })
    out = render_confirmation(
        body={"orderType": "LIMIT", "duration": "DAY", "session": "NORMAL",
              "orderLegCollection": []},
        account_tail="0756",
        strategy_label="t",
        is_naked_short=False,
        analytics=None,
        preview=preview,
        current_balances=None,
    )
    # No current balances: current/effect cells fall back to "n/a",
    # but the result column still shows projected values from preview.
    stock_line = next(ln for ln in out.splitlines() if "Buying Power (Stock)" in ln)
    option_line = next(ln for ln in out.splitlines() if "Buying Power (Option)" in ln)
    assert stock_line.count("n/a") == 2  # current + effect cells
    assert "$14,213.65" in stock_line     # result still rendered
    assert option_line.count("n/a") == 2
    assert "$7,106.83" in option_line


# ---- helpers ------------------------------------------------------------


def _deep_copy(d: dict) -> dict:
    import copy as _copy
    return _copy.deepcopy(d)
