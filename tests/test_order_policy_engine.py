"""Engine tests — match clause + condition evaluator + 16 operators +
decision algorithm + field providers.

All in one file to keep the engine layer's tests close together.
"""

from __future__ import annotations

from datetime import date

import pytest

from schwab_cli.order_policy import evaluate, parse_profile
from schwab_cli.order_policy.conditions import (
    UnevaluatableField,
    evaluate_condition,
    evaluate_conditions,
)
from schwab_cli.order_policy.fields import FieldProvider, OrderContext
from schwab_cli.order_policy.match import matches
from schwab_cli.order_policy.schema import (
    AllOfMatch,
    AnyOfMatch,
    FieldMatch,
    Predicate,
    WildcardMatch,
)


# ---- match clause --------------------------------------------------------


def test_wildcard_always_matches():
    assert matches(WildcardMatch(), {"underlying": "NVDA"})


def test_field_match_passes_when_value_in_set():
    m = FieldMatch(fields={"underlying": ("NVDA", "AMZN")})
    assert matches(m, {"underlying": "NVDA"})
    assert not matches(m, {"underlying": "AAPL"})


def test_field_match_missing_field_fails():
    m = FieldMatch(fields={"underlying": ("NVDA",)})
    assert not matches(m, {})


def test_field_match_negation():
    m = FieldMatch(
        fields={"underlying": ("NVDA",)},
        negated_fields={"instruction": ("SELL_TO_CLOSE",)},
    )
    assert matches(m, {"underlying": "NVDA", "instruction": "BUY_TO_OPEN"})
    assert not matches(m, {"underlying": "NVDA", "instruction": "SELL_TO_CLOSE"})


def test_any_of():
    m = AnyOfMatch((
        FieldMatch(fields={"underlying": ("NVDA",)}),
        FieldMatch(fields={"underlying": ("AMD",)}),
    ))
    assert matches(m, {"underlying": "NVDA"})
    assert matches(m, {"underlying": "AMD"})
    assert not matches(m, {"underlying": "AAPL"})


def test_all_of():
    m = AllOfMatch((
        FieldMatch(fields={"underlying": ("KO",)}),
        FieldMatch(fields={"instruction": ("SELL_TO_OPEN",)}),
    ))
    assert matches(m, {"underlying": "KO", "instruction": "SELL_TO_OPEN"})
    assert not matches(m, {"underlying": "KO", "instruction": "BUY"})


# ---- conditions / operators ---------------------------------------------


def _ctx(values: dict):
    def fn(name):
        if name not in values:
            raise UnevaluatableField(f"missing: {name}")
        return values[name]
    return fn


def test_predicate_lte_passes():
    pred = Predicate("quantity", (("lte", 5),))
    r = evaluate_condition(pred, _ctx({"quantity": 3}))
    assert r.satisfied is True
    assert r.predicates[0].satisfied is True
    assert r.predicates[0].actual == 3


def test_predicate_lte_fails():
    pred = Predicate("quantity", (("lte", 5),))
    r = evaluate_condition(pred, _ctx({"quantity": 10}))
    assert r.satisfied is False
    assert r.predicates[0].satisfied is False


def test_predicate_multi_op_anded():
    pred = Predicate("delta", (("gte", -0.30), ("lte", 0.0)))
    assert evaluate_condition(pred, _ctx({"delta": -0.15})).satisfied is True
    assert evaluate_condition(pred, _ctx({"delta": -0.40})).satisfied is False
    assert evaluate_condition(pred, _ctx({"delta": 0.05})).satisfied is False


def test_between_inclusive():
    pred = Predicate("dte", (("between", [21, 90]),))
    assert evaluate_condition(pred, _ctx({"dte": 21})).satisfied
    assert evaluate_condition(pred, _ctx({"dte": 90})).satisfied
    assert not evaluate_condition(pred, _ctx({"dte": 20})).satisfied
    assert not evaluate_condition(pred, _ctx({"dte": 91})).satisfied


def test_in_and_not_in():
    a = Predicate("symbol", (("in", ["KO", "PEP"]),))
    assert evaluate_condition(a, _ctx({"symbol": "KO"})).satisfied
    assert not evaluate_condition(a, _ctx({"symbol": "AMZN"})).satisfied
    b = Predicate("symbol", (("not_in", ["GME", "AMC"]),))
    assert evaluate_condition(b, _ctx({"symbol": "AAPL"})).satisfied
    assert not evaluate_condition(b, _ctx({"symbol": "GME"})).satisfied


def test_string_ops():
    eq = Predicate("name", (("equals", "Bob"),))
    assert evaluate_condition(eq, _ctx({"name": "Bob"})).satisfied
    assert not evaluate_condition(eq, _ctx({"name": "bob"})).satisfied
    eqi = Predicate("name", (("equals_ci", "BOB"),))
    assert evaluate_condition(eqi, _ctx({"name": "bob"})).satisfied
    sw = Predicate("name", (("starts_with", "Mr"),))
    assert evaluate_condition(sw, _ctx({"name": "Mr X"})).satisfied
    ew = Predicate("name", (("ends_with", "X"),))
    assert evaluate_condition(ew, _ctx({"name": "Mr X"})).satisfied
    co = Predicate("name", (("contains", "r X"),))
    assert evaluate_condition(co, _ctx({"name": "Mr X"})).satisfied
    re_ = Predicate("name", (("matches", r"^M.*X$"),))
    assert evaluate_condition(re_, _ctx({"name": "MrX"})).satisfied


def test_unevaluatable_field_marks_predicate_unevaluatable():
    pred = Predicate("delta", (("gte", 0),))
    r = evaluate_condition(pred, _ctx({}))  # no delta in ctx
    assert r.satisfied is False
    p = r.predicates[0]
    assert p.unevaluatable is True
    assert p.actual is None


def test_implicit_and_at_policy_level():
    p1 = Predicate("a", (("eq", 1),))
    p2 = Predicate("b", (("eq", 2),))
    r = evaluate_conditions((p1, p2), _ctx({"a": 1, "b": 2}))
    assert r.satisfied is True
    r2 = evaluate_conditions((p1, p2), _ctx({"a": 1, "b": 3}))
    assert r2.satisfied is False


# ---- decision algorithm --------------------------------------------------


def _body(*, symbol="KO", instr="BUY", side_letter=None, strike=None,
          expiry=None, quantity=10, price="50.00", asset="EQUITY"):
    instrument: dict = {"assetType": asset, "symbol": symbol}
    if asset == "OPTION":
        # Build OSI-ish symbol when option testing.
        from schwab_cli.order_ticket import to_osi
        instrument["symbol"] = to_osi(symbol, expiry, "CALL" if side_letter == "C" else "PUT", strike)
    return {
        "session": "NORMAL", "duration": "DAY",
        "orderType": "LIMIT", "price": price, "quantity": quantity,
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [{
            "instruction": instr, "quantity": quantity,
            "instrument": instrument,
        }],
    }


def _ctx_for(body):
    return OrderContext(body=body, account_number="12345678",
                        today=date(2026, 4, 25))


def test_phase_a_deny_precedence_wins():
    prof = parse_profile({
        "default_action": "allow",
        "policies": [
            {"name": "allow_all", "effect": "allow", "match": "*"},
            {"name": "deny_ko", "effect": "deny",
             "match": {"underlying": ["KO"]}},
        ],
    }, name="t")
    d = evaluate(prof, _ctx_for(_body(symbol="KO")))
    assert d.decision == "reject"
    assert d.rule_phase == "A"
    assert d.rule_name == "deny_ko"


def test_phase_b_matched_allow_with_failing_condition_rejects():
    prof = parse_profile({
        "default_action": "allow",
        "policies": [{
            "name": "allow_small_only",
            "effect": "allow",
            "match": {"underlying": ["KO"]},
            "conditions": [{"quantity": {"lte": 5}}],
        }],
    }, name="t")
    d = evaluate(prof, _ctx_for(_body(symbol="KO", quantity=10)))
    assert d.decision == "reject"
    assert d.rule_phase == "B"
    assert d.rule_name == "allow_small_only"
    assert d.failing_predicate is not None
    assert d.failing_predicate.field == "quantity"


def test_phase_c_matched_allow_passing():
    prof = parse_profile({
        "default_action": "deny",
        "policies": [{
            "name": "ok",
            "effect": "allow",
            "match": {"underlying": ["KO"]},
            "conditions": [{"quantity": {"lte": 100}}],
        }],
    }, name="t")
    d = evaluate(prof, _ctx_for(_body(symbol="KO", quantity=10)))
    assert d.decision == "approve"
    assert d.rule_phase == "C"


def test_phase_d_default_action_kicks_in_when_nothing_matches():
    prof_allow = parse_profile({
        "default_action": "allow",
        "policies": [{
            "name": "wrong_match", "effect": "allow",
            "match": {"underlying": ["NOT_KO"]},
        }],
    }, name="a")
    prof_deny = parse_profile({
        "default_action": "deny",
        "policies": [{
            "name": "wrong_match", "effect": "allow",
            "match": {"underlying": ["NOT_KO"]},
        }],
    }, name="d")
    body = _body(symbol="KO")
    d_allow = evaluate(prof_allow, _ctx_for(body))
    d_deny = evaluate(prof_deny, _ctx_for(body))
    assert d_allow.decision == "approve"
    assert d_allow.rule_phase == "D"
    assert d_deny.decision == "reject"
    assert d_deny.rule_phase == "D"


def test_disabled_policy_is_skipped():
    prof = parse_profile({
        "default_action": "deny",
        "policies": [
            {"name": "off", "enabled": False, "effect": "allow", "match": "*"},
        ],
    }, name="t")
    d = evaluate(prof, _ctx_for(_body()))
    assert d.decision == "reject"  # default_action deny


def test_unevaluatable_field_in_allow_condition_rejects_phase_b():
    """Phase 2a: live-data fields aren't implemented yet; an allow
    policy that depends on `delta` should fail Phase B with an
    `unevaluatable` predicate."""
    prof = parse_profile({
        "default_action": "deny",
        "policies": [{
            "name": "needs_delta", "effect": "allow",
            "match": "*",
            "conditions": [{"delta": {"gte": -0.30}}],
        }],
    }, name="t")
    d = evaluate(prof, _ctx_for(_body()))
    assert d.decision == "reject"
    assert d.rule_phase == "B"
    fail = d.failing_predicate
    assert fail is not None and fail.unevaluatable is True


# ---- field provider — intrinsic + pricing -------------------------------


def test_field_provider_categorical_view_for_equity():
    ctx = _ctx_for(_body(symbol="KO", instr="BUY"))
    p = FieldProvider(ctx)
    assert p.get("underlying") == "KO"
    assert p.get("asset_type") == "EQUITY"
    assert p.get("instruction") == "BUY"
    assert p.get("quantity") == 10
    assert p.get("price") == 50.00
    assert p.get("order_value") == 50.00 * 10  # equity multiplier 1


def test_field_provider_pricing_for_option():
    body = _body(asset="OPTION", symbol="NVDA", instr="BUY_TO_OPEN",
                 side_letter="C",
                 strike=250, expiry=date(2026, 5, 15), quantity=2,
                 price="1.50")
    ctx = _ctx_for(body)
    p = FieldProvider(ctx)
    assert p.get("underlying") == "NVDA"
    assert p.get("asset_type") == "OPTION"
    assert p.get("option_side") == "C"
    assert p.get("strike") == 250.0
    assert p.get("expiry") == "2026-05-15"
    # dte is computed against today=2026-04-25.
    assert p.get("dte") == 20
    assert p.get("order_value") == 1.50 * 2 * 100


def test_field_provider_unevaluatable_for_phase2b_fields():
    ctx = _ctx_for(_body())
    p = FieldProvider(ctx)
    with pytest.raises(UnevaluatableField):
        p.get("delta")
    with pytest.raises(UnevaluatableField):
        p.get("bp_required_pct")
