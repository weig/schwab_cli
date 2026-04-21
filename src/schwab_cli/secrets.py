from __future__ import annotations

import subprocess


class SecretError(Exception):
    """Raised when a secret reference cannot be resolved."""


def resolve_secret(value: str) -> str:
    """Resolve a secret reference.

    `op://...` values are passed to the 1Password CLI (`op read <ref>`); anything
    else is returned verbatim. The resolved value is never logged.
    """
    if not value.startswith("op://"):
        return value
    try:
        result = subprocess.run(
            ["op", "read", value],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as e:
        raise SecretError("1Password CLI (`op`) not found on PATH.") from e
    except subprocess.CalledProcessError as e:
        msg = (e.stderr or "").strip() or "unknown error"
        raise SecretError(f"op read failed: {msg}") from e
    return result.stdout.rstrip("\n")
