from __future__ import annotations

import httpx

_TRUTHY_DEBUG_VALUES = frozenset({"true", "yes", "1"})


def _is_debug_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.lower() in _TRUTHY_DEBUG_VALUES


def _summarize_error(e: BaseException) -> str:
    """One-line human-readable reason from common exception types."""
    if isinstance(e, httpx.HTTPStatusError):
        body = e.response.text or ""
        first_line = body.splitlines()[0] if body else ""
        return f"{e.response.status_code} {first_line}".strip()
    if isinstance(e, httpx.RequestError):
        return f"network: {type(e).__name__}"
    return str(e)
