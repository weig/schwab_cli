from __future__ import annotations

import os
import sys

import httpx

_TRUTHY_DEBUG_VALUES = frozenset({"true", "yes", "1"})


def _is_debug_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.lower() in _TRUTHY_DEBUG_VALUES


def _debug_log(message: str) -> None:
    """Write a `[debug] <message>` line to stderr when DEBUG is truthy.

    Never pass secrets, tokens, resolved credentials, or the auth code to this
    function — the DEBUG mode is for flow visibility, not credential dumping.
    """
    if _is_debug_truthy(os.environ.get("DEBUG")):
        print(f"[debug] {message}", file=sys.stderr, flush=True)


def _summarize_error(e: BaseException) -> str:
    """One-line human-readable reason from common exception types."""
    if isinstance(e, httpx.HTTPStatusError):
        body = e.response.text or ""
        first_line = body.splitlines()[0] if body else ""
        return f"{e.response.status_code} {first_line}".strip()
    if isinstance(e, httpx.RequestError):
        return f"network: {type(e).__name__}"
    return str(e)
