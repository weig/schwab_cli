"""Scope checking for the REST resource server.

Flat string scopes with one hierarchy: ``order:<profile>``. Holding
``order:*`` satisfies any ``order:<profile>`` requirement. ``streaming``
is a transport modifier handled at the route layer (it never satisfies a
data-scope requirement here).
"""

from __future__ import annotations

from collections.abc import Set


def scope_satisfied(granted: Set[str], required: str) -> bool:
    """True when ``granted`` covers the single ``required`` scope."""
    if required in granted:
        return True
    if ":" in required:
        prefix = required.split(":", 1)[0]
        if f"{prefix}:*" in granted:
            return True
    return False
