"""Match clause evaluator.

Determines whether a policy applies to a given order. Match clauses
are restricted to **categorical** order fields (per spec §6.6) — no
numeric / computed fields are valid here.
"""

from __future__ import annotations

from typing import Any

from schwab_cli.order_policy.schema import (
    AllOfMatch,
    AnyOfMatch,
    FieldMatch,
    MatchClause,
    WildcardMatch,
)


def matches(clause: MatchClause, order_categories: dict[str, Any]) -> bool:
    """Return True if ``clause`` matches the order's categorical
    fields.

    ``order_categories`` is a flat dict like::

        {
          "account": "12345678",
          "underlying": "AMZN",
          "asset_type": "OPTION",
          "option_side": "C",
          "instruction": "BUY_TO_OPEN",
          "order_type": "NET_DEBIT",
          "duration": "DAY",
          "session": "NORMAL",
          "complex_strategy_type": "VERTICAL",
          "order_source": "manual",
        }

    Missing keys are treated as never-matching for that field; a
    ``FieldMatch`` whose ``fields`` dict requires a missing key
    returns False.
    """
    if isinstance(clause, WildcardMatch):
        return True
    if isinstance(clause, FieldMatch):
        return _match_field(clause, order_categories)
    if isinstance(clause, AnyOfMatch):
        return any(matches(c, order_categories) for c in clause.clauses)
    if isinstance(clause, AllOfMatch):
        return all(matches(c, order_categories) for c in clause.clauses)
    raise TypeError(f"unknown match clause type: {type(clause).__name__}")


def _match_field(clause: FieldMatch, order: dict[str, Any]) -> bool:
    # Positive constraints — every required field must hit its set.
    for field_name, allowed_values in clause.fields.items():
        actual = order.get(field_name)
        if actual is None or actual not in allowed_values:
            return False
    # Negative constraints — no excluded field may match its set.
    for field_name, excluded_values in clause.negated_fields.items():
        actual = order.get(field_name)
        if actual is not None and actual in excluded_values:
            return False
    return True
