"""Profile / Policy / MatchClause / Condition data model.

Pure parsing — no I/O, no side effects. JSON dict in, frozen dataclass
out, with full validation. Errors raise :class:`SchemaError` carrying
a JSON-pointer-ish ``path`` so the user can see exactly where the
problem is.

The shape mirrors ``docs/plan/order.md`` Phase 2a; see that doc and
``~/claude_channel/order_policy_spec.md`` for the rationale behind
each field.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal


class SchemaError(ValueError):
    """Raised on a malformed profile JSON. Carries a path pointer so
    error messages name the offending field."""

    def __init__(self, message: str, *, path: str = "<root>") -> None:
        super().__init__(f"{path}: {message}")
        self.path = path


Effect = Literal["allow", "deny"]
DefaultAction = Literal["allow", "deny"]


# ---- match clauses --------------------------------------------------------


@dataclass(frozen=True)
class FieldMatch:
    """Field-AND, value-OR match. ``not_<field>`` keys are stored under
    ``negated_fields`` (the ``not_`` prefix is stripped on parse)."""

    fields: dict[str, tuple[str, ...]] = field(default_factory=dict)
    negated_fields: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class WildcardMatch:
    """``"*"`` or ``{}`` — matches any order."""


@dataclass(frozen=True)
class AnyOfMatch:
    """OR across sub-clauses."""

    clauses: tuple["MatchClause", ...]


@dataclass(frozen=True)
class AllOfMatch:
    """Explicit AND across sub-clauses."""

    clauses: tuple["MatchClause", ...]


MatchClause = WildcardMatch | FieldMatch | AnyOfMatch | AllOfMatch


# Categorical fields valid in `match` (per spec §6.6). Numeric/computed
# fields belong in `conditions`, not `match`.
MATCH_FIELDS = frozenset({
    "account", "underlying", "asset_type", "option_side",
    "instruction", "order_type", "duration", "session",
    "complex_strategy_type", "order_source",
})


# ---- conditions -----------------------------------------------------------


Operator = Literal[
    "eq", "ne", "lt", "lte", "gt", "gte", "between",
    "in", "not_in",
    "equals", "equals_ci", "starts_with", "ends_with", "contains", "matches",
]

OPERATORS: frozenset[str] = frozenset({
    "eq", "ne", "lt", "lte", "gt", "gte", "between",
    "in", "not_in",
    "equals", "equals_ci", "starts_with", "ends_with", "contains", "matches",
})


@dataclass(frozen=True)
class Predicate:
    """``{<field>: {<op>: <value>, ...}}`` — multiple ops on the same
    field AND-joined."""

    field_name: str
    op_values: tuple[tuple[str, Any], ...]


@dataclass(frozen=True)
class AndCondition:
    """``{"and": [Condition, ...]}``."""

    children: tuple["Condition", ...]


@dataclass(frozen=True)
class OrCondition:
    """``{"or": [Condition, ...]}``."""

    children: tuple["Condition", ...]


@dataclass(frozen=True)
class NotCondition:
    """``{"not": [Condition]}``. Sub-list is wrapped in implicit AND
    if it has more than one entry, mirroring the policy-level list."""

    children: tuple["Condition", ...]


Condition = Predicate | AndCondition | OrCondition | NotCondition


# ---- policy + profile -----------------------------------------------------


@dataclass(frozen=True)
class Policy:
    name: str
    description: str = ""
    enabled: bool = True
    match: MatchClause = field(default_factory=WildcardMatch)
    conditions: tuple[Condition, ...] = ()
    effect: Effect = "allow"
    reason: str = ""
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Profile:
    """A profile is a flat JSON file. Phase 2f dropped the per-profile
    inheritance + override-gating fields — there are no resolved-vs-raw
    forms anymore. What you see in :class:`Profile` is what's on disk
    plus the filename (``name``)."""

    name: str
    description: str = ""
    default_action: DefaultAction = "deny"
    policies: tuple[Policy, ...] = ()
    notify_on_override: bool = True


# ---- parsing --------------------------------------------------------------


_PROFILE_KEYS: frozenset[str] = frozenset({
    "description", "default_action", "policies", "notify_on_override",
})


def parse_profile(data: dict[str, Any], *, name: str) -> Profile:
    """Validate + parse a single profile dict.

    ``name`` is the profile's filename stem (e.g. ``"default"``) — used
    purely for error messages.

    Phase 2f rejects any unknown top-level key so legacy profiles
    that carry ``inherit`` / ``overrides`` / ``allow_override`` /
    ``override_confirmation`` / ``override_max_per_day`` fail loud
    with a pointer at the dropped feature.
    """
    if not isinstance(data, dict):
        raise SchemaError(f"profile must be a JSON object, got {type(data).__name__}",
                          path=name)

    _reject_unknown_keys(data, _PROFILE_KEYS, where="profile", path=name)

    description = _opt_str(data, "description", "", path=name)
    default_action = _enum(data, "default_action", ("allow", "deny"),
                           required=True, path=name)
    notify_on_override = _opt_bool(data, "notify_on_override", True, path=name)

    policies_raw = data.get("policies", [])
    if not isinstance(policies_raw, list):
        raise SchemaError(f"`policies` must be a list, got {type(policies_raw).__name__}",
                          path=f"{name}.policies")
    seen_names: set[str] = set()
    policies: list[Policy] = []
    for i, p in enumerate(policies_raw):
        policy = _parse_policy(p, path=f"{name}.policies[{i}]")
        if policy.name in seen_names:
            raise SchemaError(
                f"duplicate policy name {policy.name!r} within profile {name!r}",
                path=f"{name}.policies[{i}].name",
            )
        seen_names.add(policy.name)
        policies.append(policy)

    return Profile(
        name=name,
        description=description,
        default_action=default_action,  # type: ignore[arg-type]
        policies=tuple(policies),
        notify_on_override=notify_on_override,
    )


# Dropped fields — listed so the unknown-key rejector can produce a
# pointed migration message instead of the generic "unknown key".
_PROFILE_DROPPED: dict[str, str] = {
    "inherit": "profile inheritance was dropped in Phase 2f — flatten the file",
    "overrides": "the `overrides` companion to `inherit` was dropped",
    "allow_override": "per-profile override gating was dropped — use the CLI ceremony",
    "override_confirmation": "the override-tier enum was dropped — single CLI ceremony for all",
    "override_max_per_day": "the per-profile override cap was dropped",
}


def _reject_unknown_keys(
    data: dict[str, Any], allowed: frozenset[str], *,
    where: str, path: str,
) -> None:
    """Raise :class:`SchemaError` if ``data`` contains a key outside
    ``allowed``. Dropped Phase 2e fields get a tailored message."""
    for key in data:
        if key in allowed:
            continue
        if where == "profile" and key in _PROFILE_DROPPED:
            raise SchemaError(
                f"unknown {where} field {key!r}: {_PROFILE_DROPPED[key]}",
                path=path,
            )
        raise SchemaError(
            f"unknown {where} field {key!r}; allowed: "
            f"{', '.join(sorted(allowed))}",
            path=path,
        )


_POLICY_KEYS: frozenset[str] = frozenset({
    "name", "description", "enabled", "match", "conditions",
    "effect", "reason", "tags",
})


def _parse_policy(d: Any, *, path: str) -> Policy:
    if not isinstance(d, dict):
        raise SchemaError(f"policy must be an object, got {type(d).__name__}",
                          path=path)
    _reject_unknown_keys(d, _POLICY_KEYS, where="policy", path=path)
    name = _req_str(d, "name", path=path)
    description = _opt_str(d, "description", "", path=path)
    enabled = _opt_bool(d, "enabled", True, path=path)
    match = _parse_match(d.get("match", "*"), path=f"{path}.match")
    conditions_raw = d.get("conditions", [])
    if not isinstance(conditions_raw, list):
        raise SchemaError(
            f"`conditions` must be a list, got {type(conditions_raw).__name__}",
            path=f"{path}.conditions",
        )
    conditions = tuple(
        _parse_condition(c, path=f"{path}.conditions[{i}]")
        for i, c in enumerate(conditions_raw)
    )
    effect = _enum(d, "effect", ("allow", "deny"), required=True, path=path)
    reason = _opt_str(d, "reason", "", path=path)
    tags_raw = d.get("tags", [])
    if not isinstance(tags_raw, list) or not all(isinstance(t, str) for t in tags_raw):
        raise SchemaError("`tags` must be a list of strings",
                          path=f"{path}.tags")
    return Policy(
        name=name,
        description=description,
        enabled=enabled,
        match=match,
        conditions=conditions,
        effect=effect,  # type: ignore[arg-type]
        reason=reason,
        tags=tuple(tags_raw),
    )


def _parse_match(d: Any, *, path: str) -> MatchClause:
    # Wildcard.
    if d == "*" or d == {}:
        return WildcardMatch()
    if not isinstance(d, dict):
        raise SchemaError(
            "match must be \"*\", an object, or have any_of/all_of",
            path=path,
        )
    # any_of / all_of (mutually exclusive with field keys).
    if "any_of" in d or "all_of" in d:
        for k in d:
            if k not in ("any_of", "all_of"):
                raise SchemaError(
                    f"match cannot mix `{k}` with `any_of`/`all_of`",
                    path=path,
                )
        if "any_of" in d and "all_of" in d:
            raise SchemaError(
                "match cannot have both `any_of` and `all_of`", path=path,
            )
        key = "any_of" if "any_of" in d else "all_of"
        sub = d[key]
        if not isinstance(sub, list):
            raise SchemaError(f"`{key}` must be a list", path=f"{path}.{key}")
        clauses = tuple(
            _parse_match(s, path=f"{path}.{key}[{i}]")
            for i, s in enumerate(sub)
        )
        return AnyOfMatch(clauses) if key == "any_of" else AllOfMatch(clauses)

    # Field match (with optional `not_<field>` negation).
    fields_map: dict[str, tuple[str, ...]] = {}
    negated: dict[str, tuple[str, ...]] = {}
    for k, v in d.items():
        is_neg = k.startswith("not_")
        bare = k[4:] if is_neg else k
        if bare not in MATCH_FIELDS:
            raise SchemaError(
                f"unknown match field {k!r}; allowed: "
                f"{', '.join(sorted(MATCH_FIELDS))}",
                path=path,
            )
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise SchemaError(
                f"match value for {k!r} must be a list of strings",
                path=f"{path}.{k}",
            )
        if is_neg:
            negated[bare] = tuple(v)
        else:
            fields_map[bare] = tuple(v)
    return FieldMatch(fields=fields_map, negated_fields=negated)


def _parse_condition(d: Any, *, path: str) -> Condition:
    if not isinstance(d, dict):
        raise SchemaError(
            f"condition must be an object, got {type(d).__name__}", path=path,
        )
    if len(d) != 1:
        raise SchemaError(
            "condition must have exactly one top-level key "
            "(field name OR and/or/not)", path=path,
        )
    (key, value), = d.items()
    if key in ("and", "or", "not"):
        if not isinstance(value, list):
            raise SchemaError(
                f"`{key}` must be a list of conditions",
                path=f"{path}.{key}",
            )
        children = tuple(
            _parse_condition(c, path=f"{path}.{key}[{i}]")
            for i, c in enumerate(value)
        )
        if key == "and":
            return AndCondition(children)
        if key == "or":
            return OrCondition(children)
        return NotCondition(children)
    # Predicate: {<field>: {<op>: <value>, ...}}
    if not isinstance(value, dict):
        raise SchemaError(
            f"predicate value for {key!r} must be an op-map "
            "(e.g. {\"lte\": 100})",
            path=path,
        )
    if not value:
        raise SchemaError(
            f"predicate for {key!r} cannot be empty", path=path,
        )
    op_pairs: list[tuple[str, Any]] = []
    for op, v in value.items():
        if op not in OPERATORS:
            raise SchemaError(
                f"unknown operator {op!r}; allowed: "
                f"{', '.join(sorted(OPERATORS))}",
                path=f"{path}.{key}",
            )
        op_pairs.append((op, v))
    return Predicate(field_name=key, op_values=tuple(op_pairs))


# ---- small helpers --------------------------------------------------------


def _req_str(d: dict, key: str, *, path: str) -> str:
    v = d.get(key)
    if not isinstance(v, str) or not v:
        raise SchemaError(
            f"`{key}` must be a non-empty string", path=path,
        )
    return v


def _opt_str(d: dict, key: str, default: str, *, path: str) -> str:
    if key not in d:
        return default
    v = d[key]
    if not isinstance(v, str):
        raise SchemaError(f"`{key}` must be a string", path=path)
    return v


def _opt_bool(d: dict, key: str, default: bool, *, path: str) -> bool:
    if key not in d:
        return default
    v = d[key]
    if not isinstance(v, bool):
        raise SchemaError(f"`{key}` must be a boolean", path=path)
    return v


def _opt_int_or_null(d: dict, key: str, *, path: str) -> int | None:
    if key not in d or d[key] is None:
        return None
    v = d[key]
    if not isinstance(v, int) or isinstance(v, bool):
        raise SchemaError(f"`{key}` must be an integer or null", path=path)
    if v < 0:
        raise SchemaError(f"`{key}` must be non-negative", path=path)
    return v


def _enum(
    d: dict, key: str, allowed: tuple[str, ...], *,
    required: bool = False, default: str | None = None, path: str,
) -> str:
    if key not in d:
        if required:
            raise SchemaError(
                f"`{key}` is required (one of {allowed})", path=path,
            )
        return default if default is not None else allowed[0]
    v = d[key]
    if v not in allowed:
        raise SchemaError(
            f"`{key}` must be one of {allowed}, got {v!r}", path=path,
        )
    return v


# Identifier validation for profile/policy names — keep them filesystem-
# and shell-safe.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def is_valid_profile_name(name: str) -> bool:
    return bool(_NAME_RE.match(name))
