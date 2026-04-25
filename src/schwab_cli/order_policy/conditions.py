"""Condition evaluator + operator implementations.

Returns a structured result so the audit log can capture the exact
field / operator / expected / actual / satisfied tuple for every
predicate that ran.

Field values are resolved via a ``ctx.get(field_name)`` callable
(provided by the field-provider layer). The ``UnevaluatableField``
sentinel, returned when a field isn't yet implemented, propagates
through operators as a non-satisfied result with a clear annotation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from schwab_cli.order_policy.schema import (
    AndCondition,
    Condition,
    NotCondition,
    OrCondition,
    Predicate,
)


class UnevaluatableField(Exception):
    """Raised by a field provider when the field is referenced but
    not yet implemented. The condition evaluator surfaces this as a
    non-satisfied result with ``unevaluatable=True``."""


@dataclass(frozen=True)
class PredicateResult:
    """One operator firing on one field. Multiple ops on the same
    field produce multiple PredicateResults (one per op)."""

    field: str
    op: str
    expected: Any
    actual: Any
    satisfied: bool
    unevaluatable: bool = False
    error: str = ""


@dataclass(frozen=True)
class ConditionResult:
    """Outcome of evaluating a top-level condition tree.

    ``predicates`` is the flat list of every predicate that ran (in
    evaluation order), useful for the audit log. ``satisfied`` is
    the boolean rolled up through the tree.
    """

    satisfied: bool
    predicates: tuple[PredicateResult, ...]


# Public entry — evaluates a single condition.
def evaluate_condition(
    cond: Condition, ctx: Callable[[str], Any],
) -> ConditionResult:
    bag: list[PredicateResult] = []
    sat = _walk(cond, ctx, bag)
    return ConditionResult(satisfied=sat, predicates=tuple(bag))


# Public entry — evaluates a flat list (implicit AND).
def evaluate_conditions(
    conds: tuple[Condition, ...], ctx: Callable[[str], Any],
) -> ConditionResult:
    if not conds:
        return ConditionResult(satisfied=True, predicates=())
    bag: list[PredicateResult] = []
    sat = True
    for c in conds:
        if not _walk(c, ctx, bag):
            sat = False
    return ConditionResult(satisfied=sat, predicates=tuple(bag))


def _walk(
    cond: Condition,
    ctx: Callable[[str], Any],
    bag: list[PredicateResult],
) -> bool:
    if isinstance(cond, Predicate):
        return _evaluate_predicate(cond, ctx, bag)
    if isinstance(cond, AndCondition):
        # AND — short-circuit on first false but still record predicates
        # we did evaluate.
        all_true = True
        for child in cond.children:
            if not _walk(child, ctx, bag):
                all_true = False
        return all_true
    if isinstance(cond, OrCondition):
        any_true = False
        for child in cond.children:
            # Evaluate every branch so the audit captures all actuals,
            # but short-circuit boolean.
            if _walk(child, ctx, bag):
                any_true = True
        return any_true
    if isinstance(cond, NotCondition):
        # `not` wraps a list; AND-join the children, then negate.
        all_true = True
        for child in cond.children:
            if not _walk(child, ctx, bag):
                all_true = False
        return not all_true
    raise TypeError(f"unknown condition type: {type(cond).__name__}")


def _evaluate_predicate(
    pred: Predicate,
    ctx: Callable[[str], Any],
    bag: list[PredicateResult],
) -> bool:
    """Evaluate one Predicate (which may carry several operators on
    the same field — those AND-join). Returns the overall pass/fail
    for this predicate; appends one PredicateResult per (op, value)."""
    try:
        actual = ctx(pred.field_name)
        unevaluatable = False
        err = ""
    except UnevaluatableField as e:
        actual = None
        unevaluatable = True
        err = str(e) or "field not available in current phase"

    overall = True
    for op, expected in pred.op_values:
        if unevaluatable:
            bag.append(PredicateResult(
                field=pred.field_name, op=op, expected=expected,
                actual=None, satisfied=False, unevaluatable=True, error=err,
            ))
            overall = False
            continue
        try:
            sat = _APPLY[op](actual, expected)
            bag.append(PredicateResult(
                field=pred.field_name, op=op,
                expected=expected, actual=actual, satisfied=sat,
            ))
            if not sat:
                overall = False
        except (TypeError, ValueError) as e:
            bag.append(PredicateResult(
                field=pred.field_name, op=op,
                expected=expected, actual=actual,
                satisfied=False, error=str(e),
            ))
            overall = False
    return overall


# ---- operators ------------------------------------------------------------


def _op_eq(a, b): return a == b
def _op_ne(a, b): return a != b


def _op_lt(a, b):
    _require_numeric(a, b, op="lt")
    return a < b


def _op_lte(a, b):
    _require_numeric(a, b, op="lte")
    return a <= b


def _op_gt(a, b):
    _require_numeric(a, b, op="gt")
    return a > b


def _op_gte(a, b):
    _require_numeric(a, b, op="gte")
    return a >= b


def _op_between(a, b):
    if not isinstance(b, (list, tuple)) or len(b) != 2:
        raise ValueError("between expects [low, high]")
    low, high = b
    _require_numeric(a, low, op="between")
    _require_numeric(a, high, op="between")
    return low <= a <= high


def _op_in(a, b):
    if not isinstance(b, (list, tuple)):
        raise ValueError("in expects a list")
    return a in b


def _op_not_in(a, b):
    if not isinstance(b, (list, tuple)):
        raise ValueError("not_in expects a list")
    return a not in b


def _op_equals(a, b):
    if not isinstance(b, str):
        raise ValueError("equals expects a string")
    return isinstance(a, str) and a == b


def _op_equals_ci(a, b):
    if not isinstance(b, str):
        raise ValueError("equals_ci expects a string")
    return isinstance(a, str) and a.lower() == b.lower()


def _op_starts_with(a, b):
    if not isinstance(a, str) or not isinstance(b, str):
        raise ValueError("starts_with expects strings on both sides")
    return a.startswith(b)


def _op_ends_with(a, b):
    if not isinstance(a, str) or not isinstance(b, str):
        raise ValueError("ends_with expects strings on both sides")
    return a.endswith(b)


def _op_contains(a, b):
    if not isinstance(a, str) or not isinstance(b, str):
        raise ValueError("contains expects strings on both sides")
    return b in a


def _op_matches(a, b):
    if not isinstance(a, str) or not isinstance(b, str):
        raise ValueError("matches expects strings on both sides")
    try:
        return re.search(b, a) is not None
    except re.error as e:
        raise ValueError(f"invalid regex: {e}") from e


_APPLY: dict[str, Callable[[Any, Any], bool]] = {
    "eq": _op_eq, "ne": _op_ne,
    "lt": _op_lt, "lte": _op_lte, "gt": _op_gt, "gte": _op_gte,
    "between": _op_between,
    "in": _op_in, "not_in": _op_not_in,
    "equals": _op_equals, "equals_ci": _op_equals_ci,
    "starts_with": _op_starts_with, "ends_with": _op_ends_with,
    "contains": _op_contains, "matches": _op_matches,
}


def _require_numeric(*vals, op: str) -> None:
    for v in vals:
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise TypeError(
                f"{op} requires numeric operands, got {type(v).__name__}"
            )
