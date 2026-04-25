"""Schema parser tests — pure, no I/O."""

from __future__ import annotations

import pytest

from schwab_cli.order_policy.schema import (
    AllOfMatch,
    AndCondition,
    AnyOfMatch,
    FieldMatch,
    NotCondition,
    OrCondition,
    Predicate,
    SchemaError,
    WildcardMatch,
    parse_profile,
)


def test_minimal_profile_with_one_allow_policy():
    p = parse_profile({
        "default_action": "deny",
        "policies": [
            {
                "name": "allow_ko_buy",
                "match": {"underlying": ["KO"], "instruction": ["BUY"]},
                "effect": "allow",
            },
        ],
    }, name="minimal")
    assert p.name == "minimal"
    assert p.default_action == "deny"
    assert len(p.policies) == 1
    pol = p.policies[0]
    assert pol.name == "allow_ko_buy"
    assert pol.effect == "allow"
    assert isinstance(pol.match, FieldMatch)
    assert pol.match.fields == {"underlying": ("KO",), "instruction": ("BUY",)}


def test_wildcard_match_str_form():
    p = parse_profile({
        "default_action": "allow",
        "policies": [
            {"name": "global", "match": "*", "effect": "deny"},
        ],
    }, name="x")
    assert isinstance(p.policies[0].match, WildcardMatch)


def test_wildcard_match_empty_obj():
    p = parse_profile({
        "default_action": "allow",
        "policies": [{"name": "g", "match": {}, "effect": "deny"}],
    }, name="x")
    assert isinstance(p.policies[0].match, WildcardMatch)


def test_negated_field_match():
    p = parse_profile({
        "default_action": "deny",
        "policies": [{
            "name": "p", "effect": "allow",
            "match": {"underlying": ["KO"], "not_instruction": ["SELL_TO_CLOSE"]},
        }],
    }, name="x")
    m = p.policies[0].match
    assert isinstance(m, FieldMatch)
    assert m.fields == {"underlying": ("KO",)}
    assert m.negated_fields == {"instruction": ("SELL_TO_CLOSE",)}


def test_any_of_match():
    p = parse_profile({
        "default_action": "deny",
        "policies": [{
            "name": "p", "effect": "allow",
            "match": {"any_of": [
                {"underlying": ["NVDA"]},
                {"underlying": ["AMD"]},
            ]},
        }],
    }, name="x")
    m = p.policies[0].match
    assert isinstance(m, AnyOfMatch)
    assert len(m.clauses) == 2


def test_all_of_match_explicit():
    p = parse_profile({
        "default_action": "deny",
        "policies": [{
            "name": "p", "effect": "allow",
            "match": {"all_of": [
                {"underlying": ["KO"]},
                {"instruction": ["SELL_TO_OPEN"]},
            ]},
        }],
    }, name="x")
    m = p.policies[0].match
    assert isinstance(m, AllOfMatch)
    assert len(m.clauses) == 2


def test_predicate_with_multi_op_on_same_field():
    p = parse_profile({
        "default_action": "deny",
        "policies": [{
            "name": "p", "effect": "allow", "match": "*",
            "conditions": [{"delta": {"gte": -0.30, "lte": 0}}],
        }],
    }, name="x")
    pred = p.policies[0].conditions[0]
    assert isinstance(pred, Predicate)
    assert pred.field_name == "delta"
    ops = dict(pred.op_values)
    assert ops == {"gte": -0.30, "lte": 0}


def test_combinator_and_or_not():
    p = parse_profile({
        "default_action": "deny",
        "policies": [{
            "name": "p", "effect": "deny", "match": "*",
            "conditions": [
                {"and": [{"x": {"lte": 5}}, {"y": {"gte": 1}}]},
                {"or": [{"x": {"eq": 0}}, {"y": {"eq": 0}}]},
                {"not": [{"x": {"eq": 0}}]},
            ],
        }],
    }, name="x")
    cs = p.policies[0].conditions
    assert isinstance(cs[0], AndCondition)
    assert isinstance(cs[1], OrCondition)
    assert isinstance(cs[2], NotCondition)


# ---- error cases ----------------------------------------------------------


def test_missing_default_action_rejected():
    with pytest.raises(SchemaError, match="default_action"):
        parse_profile({"policies": []}, name="x")


def test_unknown_match_field_rejected():
    with pytest.raises(SchemaError, match="unknown match field"):
        parse_profile({
            "default_action": "deny",
            "policies": [{
                "name": "p", "effect": "allow",
                "match": {"price": ["100"]},
            }],
        }, name="x")


def test_unknown_operator_rejected():
    with pytest.raises(SchemaError, match="unknown operator"):
        parse_profile({
            "default_action": "deny",
            "policies": [{
                "name": "p", "effect": "allow", "match": "*",
                "conditions": [{"x": {"approximately": 5}}],
            }],
        }, name="x")


def test_duplicate_policy_name_rejected():
    with pytest.raises(SchemaError, match="duplicate policy name"):
        parse_profile({
            "default_action": "deny",
            "policies": [
                {"name": "dup", "effect": "allow"},
                {"name": "dup", "effect": "allow"},
            ],
        }, name="x")


def test_invalid_effect_rejected():
    with pytest.raises(SchemaError, match="effect"):
        parse_profile({
            "default_action": "deny",
            "policies": [{"name": "p", "effect": "maybe"}],
        }, name="x")


def test_match_cannot_mix_field_and_any_of():
    with pytest.raises(SchemaError, match="cannot mix"):
        parse_profile({
            "default_action": "deny",
            "policies": [{
                "name": "p", "effect": "allow",
                "match": {"underlying": ["KO"], "any_of": [{"underlying": ["KO"]}]},
            }],
        }, name="x")


def test_phase_2f_dropped_fields_rejected():
    """Phase 2f dropped per-profile override gating + inheritance."""
    for dropped, snippet in [
        ("inherit", "base"),
        ("overrides", {"default_action": "allow"}),
        ("allow_override", True),
        ("override_confirmation", "cli"),
        ("override_max_per_day", 3),
    ]:
        with pytest.raises(SchemaError, match=f"unknown profile field {dropped!r}"):
            parse_profile({
                "default_action": "allow",
                dropped: snippet,
                "policies": [],
            }, name="x")


def test_unknown_top_level_key_rejected():
    with pytest.raises(SchemaError, match="unknown profile field 'spurious'"):
        parse_profile({
            "default_action": "deny",
            "spurious": True,
            "policies": [],
        }, name="x")


def test_unknown_policy_field_rejected():
    with pytest.raises(SchemaError, match="unknown policy field"):
        parse_profile({
            "default_action": "deny",
            "policies": [{
                "name": "p", "effect": "allow",
                "spurious_policy_field": True,
            }],
        }, name="x")


def test_notify_on_override_kept():
    p = parse_profile({
        "default_action": "deny",
        "notify_on_override": False,
        "policies": [],
    }, name="x")
    assert p.notify_on_override is False
