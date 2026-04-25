"""Order policy engine — Phase 2.

White-list-by-default policy gate that evaluates every ``order place`` /
``order preview`` against an active **profile** before Schwab is touched.
Profiles live as one JSON file each under
``~/.config/schwab_cli/profiles/order/<name>.json``.

Public API (Phase 2a):

* :func:`schema.parse_profile` — JSON-shaped dict → :class:`Profile`.
* :func:`loader.load_profile` — by name (resolves ``inherit`` chain).
* :func:`loader.list_profiles` — names available in the profiles dir.
* :func:`decision.evaluate` — order body + profile → :class:`Decision`.
"""

from __future__ import annotations

from schwab_cli.order_policy.decision import Decision, evaluate
from schwab_cli.order_policy.loader import (
    PolicyConfigError,
    list_profiles,
    load_profile,
    profiles_dir,
)
from schwab_cli.order_policy.schema import (
    Condition,
    Effect,
    MatchClause,
    Operator,
    Policy,
    Profile,
    parse_profile,
)

__all__ = [
    "Condition",
    "Decision",
    "Effect",
    "MatchClause",
    "Operator",
    "Policy",
    "PolicyConfigError",
    "Profile",
    "evaluate",
    "list_profiles",
    "load_profile",
    "parse_profile",
    "profiles_dir",
]
