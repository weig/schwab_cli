"""Credential resolution seam.

Per the auth refactor's "Credentials: plain-text only" decision,
:func:`resolve_secret` is currently a pass-through. The module stays
so any future caller has a stable seam to swap in a real resolver
(keychain, 1Password CLI, env-var indirection, …) without churning
call sites.

Today's behavior: return the input verbatim. Even ``op://...`` strings
pass through unchanged — a previously-configured 1Password reference
will land at downstream consumers as the literal string. That is
preferable to crashing on a missing ``op`` CLI.
"""
from __future__ import annotations


def resolve_secret(value: str) -> str:
    """Return ``value`` unchanged.

    Reserved as a future extension point. See module docstring.
    """
    return value
