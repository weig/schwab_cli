"""Tests for the 7 templates + custom in profile_new.templates.

Uses a stub Prompter that returns canned answers in queue order so
tests don't need a TTY.
"""

from __future__ import annotations

from collections import deque

import pytest

from schwab_cli.order_policy import parse_profile
from schwab_cli.order_policy.profile_new.templates import (
    TEMPLATES, by_key,
)


class _StubPrompter:
    """Returns canned answers in the order the template asks. Tests
    pass them as the ``answers`` constructor arg. Each ``text`` /
    ``select`` / etc call pops one item — failing the test if there
    are too few."""

    def __init__(self, answers: list) -> None:
        self._q = deque(answers)

    def _next(self):
        if not self._q:
            raise AssertionError("template asked more questions than were stubbed")
        return self._q.popleft()

    def text(self, label, *, default=""):
        v = self._next()
        return v if v is not None else default

    def select(self, label, choices, *, default=None):
        v = self._next()
        return v

    def integer(self, label, *, default=None, min_value=None):
        v = self._next()
        if v is None:
            return default
        return int(v)

    def number(self, label, *, default=None):
        v = self._next()
        if v is None:
            return default
        return float(v)

    def yes_no(self, label, *, default=False):
        v = self._next()
        return bool(v) if v is not None else default


# ---- registry -----------------------------------------------------------


def test_template_registry_has_8_entries():
    assert len(TEMPLATES) == 8
    keys = {t.key for t in TEMPLATES}
    assert keys == {
        "allow_equity_trade", "allow_short_put_open",
        "allow_covered_call_open", "allow_vertical_spread",
        "deny_underlying", "deny_loss_cooldown", "deny_fat_finger",
        "custom",
    }


def test_by_key_lookup():
    assert by_key("allow_equity_trade").key == "allow_equity_trade"
    assert by_key("does_not_exist") is None


def _build(key: str, answers: list) -> dict:
    template = by_key(key)
    assert template is not None
    return template.build(_StubPrompter(answers))


# ---- per-template happy-path -------------------------------------------


def test_allow_equity_trade():
    p = _build("allow_equity_trade", [
        "KO, PEP",                 # tickers
        "BUY",                     # side
        100,                        # qty cap
    ])
    assert p["effect"] == "allow"
    assert p["match"]["underlying"] == ["KO", "PEP"]
    assert p["match"]["asset_type"] == ["EQUITY"]
    assert p["match"]["instruction"] == ["BUY"]
    assert p["conditions"] == [{"quantity": {"lte": 100}}]


def test_allow_equity_trade_no_qty_cap():
    p = _build("allow_equity_trade", [
        "AAPL", "BUY", None,
    ])
    assert "conditions" not in p


def test_allow_short_put_open():
    p = _build("allow_short_put_open", [
        "KO",                       # tickers
        -0.30,                      # delta_lo
        -0.10,                      # delta_hi
        60,                         # dte_lo
        100,                        # dte_hi
        30.0,                       # iv max
        5.0,                        # bp_pct max
    ])
    assert p["effect"] == "allow"
    assert p["match"]["option_side"] == ["P"]
    assert p["match"]["instruction"] == ["SELL_TO_OPEN"]
    fields = [next(iter(c.keys())) for c in p["conditions"]]
    assert "delta" in fields
    assert "dte" in fields
    assert "iv" in fields
    assert "bp_required_pct" in fields


def test_allow_covered_call_open():
    p = _build("allow_covered_call_open", [
        "NVDA",
        21, 60, 5.0,
    ])
    assert p["match"]["option_side"] == ["C"]
    cond_fields = [next(iter(c.keys())) for c in p["conditions"]]
    assert "covered_by_equity" in cond_fields
    assert "strike_pct_above_spot" in cond_fields


def test_allow_vertical_spread_debit():
    p = _build("allow_vertical_spread", [
        "DEBIT",                    # net side
        "AMZN",                     # tickers
        2.50,                       # max debit
        5,                          # max qty
    ])
    assert p["match"]["order_type"] == ["NET_DEBIT"]
    assert p["match"]["complex_strategy_type"] == ["VERTICAL"]
    assert p["match"]["underlying"] == ["AMZN"]
    cond_fields = [next(iter(c.keys())) for c in p["conditions"]]
    assert "price" in cond_fields
    assert "quantity" in cond_fields


def test_allow_vertical_spread_credit_no_underlying_filter():
    p = _build("allow_vertical_spread", [
        "CREDIT",                   # net side
        "",                         # tickers blank
        1.00,                       # min credit
        None,                       # qty cap blank
    ])
    assert p["match"]["order_type"] == ["NET_CREDIT"]
    assert "underlying" not in p["match"]
    # Single condition: price >= 1.00 (credit lower bound).
    assert p["conditions"] == [{"price": {"gte": 1.00}}]


def test_deny_underlying():
    p = _build("deny_underlying", [
        "GME, AMC, BBBY",
        "meme blocklist",
    ])
    assert p["effect"] == "deny"
    assert p["match"]["underlying"] == ["GME", "AMC", "BBBY"]
    assert "meme blocklist" in p["reason"]


def test_deny_loss_cooldown():
    p = _build("deny_loss_cooldown", [3])
    assert p["effect"] == "deny"
    assert p["conditions"] == [
        {"consecutive_losing_closes_24h": {"gte": 3}},
    ]


def test_deny_fat_finger():
    p = _build("deny_fat_finger", [70.0, 130.0, 10])
    assert p["effect"] == "deny"
    or_clause = p["conditions"][0]["or"]
    fields = [next(iter(c.keys())) for c in or_clause]
    assert fields == ["price_pct_of_mid", "price_pct_of_mid", "quantity"]


def test_custom_with_valid_json():
    p = _build("custom", [
        "my_custom_rule",                    # name
        "deny",                              # effect
        '{"underlying": ["KO"]}',            # match (JSON)
        '[{"dte": {"gte": 21}}]',            # conditions (JSON)
    ])
    assert p["name"] == "my_custom_rule"
    assert p["effect"] == "deny"
    assert p["match"] == {"underlying": ["KO"]}
    assert p["conditions"] == [{"dte": {"gte": 21}}]


def test_custom_rejects_invalid_name():
    with pytest.raises(ValueError, match="invalid policy name"):
        _build("custom", [
            "Bad-Name!",  # uppercase + bang
            "allow",
            "*", "[]",
        ])


def test_custom_rejects_invalid_json_match():
    with pytest.raises(ValueError, match="not valid JSON"):
        _build("custom", [
            "x", "allow", "{not json}", "[]",
        ])


# ---- assembled profile validates against schema -----------------------


def test_assembled_profile_passes_schema():
    """An end-to-end check: build several policies and feed them
    through parse_profile to confirm everything we generate is
    valid input for the engine."""
    policies = [
        _build("allow_equity_trade", ["KO", "BUY", 100]),
        _build("deny_underlying", ["GME", "blocklist"]),
        _build("deny_fat_finger", [70.0, 130.0, 10]),
    ]
    profile_data = {
        "default_action": "deny",
        "notify_on_override": True,
        "policies": policies,
    }
    p = parse_profile(profile_data, name="t")
    assert len(p.policies) == 3
