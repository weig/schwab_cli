from __future__ import annotations

import time
from typing import Protocol


class AuthError(Exception):
    """Raised on any failure during the browser-driven auth flow."""


class _PageLike(Protocol):
    def content(self) -> str: ...
    def wait_for_selector(self, selector: str, *, timeout: int): ...


_POLL_INTERVAL_SECONDS = 0.2

_UI_CHANGED_MESSAGE = (
    "Auth step timed out — Schwab may have changed. "
    "Selectors live at src/schwab_cli/browser/selectors.py. "
    "Auth incomplete."
)


def wait_any(
    page: _PageLike,
    *,
    expected: str,
    known_errors: dict[str, str],
    timeout_ms: int = 15_000,
) -> None:
    """Wait for the expected selector or a known error marker, whichever comes first.

    On a known error marker → raise AuthError with the mapped user-facing message.
    On the expected selector appearing → return None.
    On timeout with neither matched → raise AuthError("...Schwab may have changed...").
    """
    deadline = time.monotonic() + timeout_ms / 1000.0
    while True:
        # Check known error markers FIRST so a definite failure is not masked by
        # a coincidentally-present expected element.
        try:
            content = page.content()
        except Exception:
            content = ""
        for marker, user_message in known_errors.items():
            if marker in content:
                raise AuthError(user_message)

        # Try the expected selector with a short per-iteration timeout.
        try:
            page.wait_for_selector(expected, timeout=int(_POLL_INTERVAL_SECONDS * 1000))
            return
        except Exception:
            pass

        if time.monotonic() >= deadline:
            raise AuthError(_UI_CHANGED_MESSAGE)
