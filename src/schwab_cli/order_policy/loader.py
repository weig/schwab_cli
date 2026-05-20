"""Per-file profile loader for ``~/.config/schwab_cli/profiles/order/``.

Phase 2f: profiles are flat — no inheritance, no bundled reserved
fallbacks. The filename stem is the profile name; ``default.json``
is the no-flag/no-env fallback target.

Public:

* :func:`profiles_dir` — resolved path (env / default).
* :func:`list_profiles` — names available on disk.
* :func:`load_profile` — by name → :class:`Profile`.
* :func:`select_profile_name` — flag/env/default chain.
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


class PolicyConfigError(Exception):
    """Raised on a config-time problem the user should see and fix.

    Includes missing files, bad JSON, and schema errors.
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

    Returns an empty list if the directory doesn't exist yet —
    callers treat that as "no profiles configured" and point the
    user at ``profile new --type=order``.
    """
    base = base_dir or profiles_dir()
    if not base.exists() or not base.is_dir():
        return []
    names: list[str] = []
    for entry in base.iterdir():
        if entry.is_file() and entry.suffix == ".json":
            stem = entry.stem
            if is_valid_profile_name(stem):
                names.append(stem)
    return sorted(names)


def load_profile(
    name: str, *, base_dir: Path | None = None,
) -> Profile:
    """Load and parse a profile by name.

    Raises :class:`PolicyConfigError` on missing file, malformed JSON,
    or schema validation failure.
    """
    base = base_dir or profiles_dir()
    if not is_valid_profile_name(name):
        raise PolicyConfigError(
            f"invalid profile name {name!r} (allowed: alphanumerics, "
            "underscore, dot, hyphen; max 64 chars)"
        )
    raw = _read_raw(name, base)
    try:
        return parse_profile(raw, name=name)
    except SchemaError as e:
        raise PolicyConfigError(f"profile {name!r}: {e}") from e


def _read_raw(name: str, base: Path) -> dict[str, Any]:
    path = base / f"{name}.json"
    if not path.exists():
        listed = list_profiles(base_dir=base)
        if name == "default" and not listed:
            raise PolicyConfigError(
                "no profile resolved.\n"
                "hint: run `schwab_cli profile new --type=order` to "
                "create one,\n"
                "      or pass --profile NAME / set SCHWAB_CLI_PROFILE."
            )
        raise PolicyConfigError(
            f"profile file not found: {path}\n"
            f"available: {', '.join(listed) or '(none)'}"
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


def select_profile_name(
    *, flag: str | None = None, env: str | None = None,
    default: str = "default",
) -> str:
    """Resolve the active profile name per the priority chain:

    1. ``--profile NAME`` flag.
    2. ``SCHWAB_CLI_PROFILE`` env var (passed in as ``env``).
    3. ``default`` (filename stem of the default profile).
    """
    if flag:
        return flag
    if env:
        return env
    return default
