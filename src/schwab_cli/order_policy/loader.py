"""Per-file profile loader for ``~/.config/schwab_cli/profiles/order/``.

Each profile lives in its own JSON file; the filename (sans ``.json``)
is the profile name. ``inherit: "<other>"`` references another file in
the same directory; cycles are detected.

Public:

* :func:`profiles_dir` — resolved path (env / default).
* :func:`list_profiles` — names available.
* :func:`load_profile` — by name → resolved :class:`Profile`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from schwab_cli.order_policy.schema import (
    Profile,
    SchemaError,
    is_valid_profile_name,
    parse_profile,
)

# Reserved profile names — always resolvable, fall back to bundled
# defaults if the user hasn't customised them.
RESERVED_PROFILES: frozenset[str] = frozenset({
    "default", "emergency_stop", "read_only", "dry_run",
})

_BUNDLED_DIR = Path(__file__).parent / "reserved"


class PolicyConfigError(Exception):
    """Raised on a config-time problem the user should see and fix.

    Includes missing files, bad JSON, schema errors, and inherit cycles.
    """


def profiles_dir() -> Path:
    """Resolve the directory holding profile files.

    Override priority:
    1. ``SCHWAB_CLI_POLICY_DIR`` env var (full path to the directory).
    2. ``~/.config/schwab_cli/profiles/order/``.
    """
    env = os.environ.get("SCHWAB_CLI_POLICY_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "schwab_cli" / "profiles" / "order"


def list_profiles(*, base_dir: Path | None = None) -> list[str]:
    """Return profile names (filename stems) sorted alphabetically.

    Always includes the reserved profile names even when no user
    files exist for them — they fall back to bundled defaults.
    """
    base = base_dir or profiles_dir()
    names: set[str] = set(RESERVED_PROFILES)
    if base.exists() and base.is_dir():
        for entry in base.iterdir():
            if entry.is_file() and entry.suffix == ".json":
                stem = entry.stem
                if is_valid_profile_name(stem):
                    names.add(stem)
    return sorted(names)


def load_profile(
    name: str, *, base_dir: Path | None = None,
) -> Profile:
    """Load and resolve a profile by name.

    Resolves the ``inherit`` chain (cycle-detected), applies
    ``overrides`` to top-level fields, then **appends** the child's
    ``policies`` to the inherited list (per spec §4 inheritance
    semantics). Returns a single, fully-resolved :class:`Profile`.

    Raises :class:`PolicyConfigError` on missing file, malformed JSON,
    schema validation failure, or inheritance cycle.
    """
    base = base_dir or profiles_dir()
    if not is_valid_profile_name(name):
        raise PolicyConfigError(
            f"invalid profile name {name!r} (allowed: alphanumerics, "
            "underscore, dot, hyphen; max 64 chars)"
        )
    chain: list[str] = []
    resolved = _resolve(name, base, chain)
    return resolved


def _resolve(
    name: str, base: Path, chain: list[str],
) -> Profile:
    if name in chain:
        cycle = " -> ".join([*chain, name])
        raise PolicyConfigError(f"inherit cycle detected: {cycle}")
    chain = [*chain, name]
    raw = _read_raw(name, base)

    parent_name = raw.get("inherit")
    if parent_name is not None:
        if not isinstance(parent_name, str):
            raise PolicyConfigError(
                f"profile {name!r}: `inherit` must be a string"
            )
        if not is_valid_profile_name(parent_name):
            raise PolicyConfigError(
                f"profile {name!r}: invalid inherit target {parent_name!r}"
            )
        parent = _resolve(parent_name, base, chain)
        merged = _apply_inheritance(raw, parent, name=name)
    else:
        merged = raw

    try:
        return parse_profile(merged, name=name)
    except SchemaError as e:
        raise PolicyConfigError(f"profile {name!r}: {e}") from e


def _read_raw(name: str, base: Path) -> dict[str, Any]:
    user_path = base / f"{name}.json"
    if user_path.exists():
        path = user_path
    elif name in RESERVED_PROFILES:
        # Fall back to the bundled reserved profile when the user
        # hasn't placed a custom file at this name.
        path = _BUNDLED_DIR / f"{name}.json"
        if not path.exists():
            raise PolicyConfigError(
                f"reserved profile {name!r} bundled file is missing — "
                "this is a packaging bug, please reinstall schwab_cli"
            )
    else:
        raise PolicyConfigError(
            f"profile file not found: {user_path}\n"
            f"available: {', '.join(list_profiles(base_dir=base)) or '(none)'}"
        )
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise PolicyConfigError(
            f"profile {name!r} ({path}) is not valid JSON: {e}"
        ) from e
    if not isinstance(data, dict):
        raise PolicyConfigError(
            f"profile {name!r}: top-level must be a JSON object"
        )
    return data


def _apply_inheritance(
    child_raw: dict[str, Any], parent: Profile, *, name: str,
) -> dict[str, Any]:
    """Build the post-inheritance dict that ``parse_profile`` will see.

    Resolution order (per spec §4):
    1. Start from the parent's serialised form.
    2. Apply ``overrides`` from the child to top-level fields
       (description / default_action / etc.).
    3. Append the child's ``policies`` to the inherited list (no
       de-duplication; deny rules can intentionally repeat).
    """
    base = _profile_to_dict(parent)
    overrides = child_raw.get("overrides", {})
    if overrides and not isinstance(overrides, dict):
        raise PolicyConfigError(
            f"profile {name!r}: `overrides` must be an object"
        )
    base.update(overrides)

    # Top-level fields the child can set directly (without going through
    # `overrides`). Any of these on the child wins.
    for key in (
        "description", "default_action", "allow_override",
        "override_confirmation", "override_max_per_day", "notify_on_override",
    ):
        if key in child_raw:
            base[key] = child_raw[key]

    # Append child policies to the inherited list.
    parent_policies = base.get("policies", [])
    child_policies = child_raw.get("policies", [])
    if not isinstance(child_policies, list):
        raise PolicyConfigError(
            f"profile {name!r}: `policies` must be a list"
        )
    base["policies"] = list(parent_policies) + list(child_policies)
    return base


def _profile_to_dict(p: Profile) -> dict[str, Any]:
    """Serialise a resolved Profile back to the JSON shape so it can be
    used as the starting point for an inherited child."""
    return {
        "description": p.description,
        "default_action": p.default_action,
        "allow_override": p.allow_override,
        "override_confirmation": p.override_confirmation,
        "override_max_per_day": p.override_max_per_day,
        "notify_on_override": p.notify_on_override,
        "policies": [_policy_to_dict(pp) for pp in p.policies],
    }


def _policy_to_dict(p) -> dict[str, Any]:
    return {
        "name": p.name,
        "description": p.description,
        "enabled": p.enabled,
        "match": _match_to_dict(p.match),
        "conditions": [_cond_to_dict(c) for c in p.conditions],
        "effect": p.effect,
        "reason": p.reason,
        "tags": list(p.tags),
    }


def _match_to_dict(m) -> Any:
    from schwab_cli.order_policy.schema import (
        AllOfMatch, AnyOfMatch, FieldMatch, WildcardMatch,
    )
    if isinstance(m, WildcardMatch):
        return "*"
    if isinstance(m, FieldMatch):
        out: dict[str, list[str]] = {}
        for k, v in m.fields.items():
            out[k] = list(v)
        for k, v in m.negated_fields.items():
            out[f"not_{k}"] = list(v)
        return out
    if isinstance(m, AnyOfMatch):
        return {"any_of": [_match_to_dict(c) for c in m.clauses]}
    if isinstance(m, AllOfMatch):
        return {"all_of": [_match_to_dict(c) for c in m.clauses]}
    raise PolicyConfigError(f"internal: unknown match type {type(m).__name__}")


def _cond_to_dict(c) -> Any:
    from schwab_cli.order_policy.schema import (
        AndCondition, NotCondition, OrCondition, Predicate,
    )
    if isinstance(c, Predicate):
        return {c.field_name: dict(c.op_values)}
    if isinstance(c, AndCondition):
        return {"and": [_cond_to_dict(x) for x in c.children]}
    if isinstance(c, OrCondition):
        return {"or": [_cond_to_dict(x) for x in c.children]}
    if isinstance(c, NotCondition):
        return {"not": [_cond_to_dict(x) for x in c.children]}
    raise PolicyConfigError(f"internal: unknown condition type {type(c).__name__}")


def select_profile_name(
    *, flag: str | None = None, env: str | None = None,
    default: str = "default",
) -> str:
    """Resolve the active profile name per the spec priority chain:

    1. ``--profile NAME`` flag.
    2. ``SCHWAB_CLI_PROFILE`` env var (passed in as ``env``).
    3. ``default`` (filename stem of the default profile).
    """
    if flag:
        return flag
    if env:
        return env
    return default
