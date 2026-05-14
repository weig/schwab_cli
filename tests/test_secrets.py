"""``resolve_secret`` is a pass-through after the auth refactor.

The ``op://`` 1Password branch was removed — credentials live as plain
strings in ``config.json``. The function survives as a single seam for
future re-introduction of a real secret resolver.
"""
from __future__ import annotations

from schwab_cli.secrets import resolve_secret


def test_literal_value_returned_verbatim():
    assert resolve_secret("plain-text-password") == "plain-text-password"


def test_empty_value_returned_verbatim():
    assert resolve_secret("") == ""


def test_op_prefix_passes_through_unchanged():
    """Belt-and-braces: stale ``op://`` strings in old configs must NOT
    cause a crash (no ``op`` shell-out). They flow through as literal
    strings and fail downstream at the actual consumer if used."""
    assert resolve_secret("op://Personal/Schwab/password") == \
        "op://Personal/Schwab/password"


def test_unicode_passes_through():
    assert resolve_secret("配置密码") == "配置密码"


def test_secret_error_no_longer_exported():
    """SecretError was removed when the 1Password branch was deleted.
    Documenting this explicitly so re-introduction is intentional."""
    import schwab_cli.secrets as mod
    assert not hasattr(mod, "SecretError")
