from __future__ import annotations

import os
import time
from typing import Protocol
from urllib.parse import parse_qs, urlparse

from schwab_cli.browser.selectors import (
    ACCEPT_SELECTOR,
    ACCOUNT_CHECKBOX_SELECTOR,
    ACCOUNT_SELECTION_SELECTOR,
    CONFIRM_PAGE_SELECTOR,
    CONSENT_PAGE_SELECTOR,
    CONTINUE_SELECTOR,
    DONE_SELECTOR,
    INVALID_CLIENT_MARKERS,
    INVALID_CREDENTIALS_TEXT,
    LOGIN_PASSWORD_SELECTOR,
    LOGIN_SUBMIT_SELECTOR,
    LOGIN_USERNAME_SELECTOR,
    REDIRECT_URI_MISMATCH_TEXT,
    _is_debug_truthy,
)
from schwab_cli.config import Config
from schwab_cli.oauth import build_auth_url
from schwab_cli.secrets import resolve_secret


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


def _launch_browser(headless: bool):  # pragma: no cover
    """Real Playwright launch. Pulled out so tests can monkeypatch this single seam."""
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=headless)
    # Stash the playwright handle on the browser so we can stop it on close.
    browser._pw = pw  # type: ignore[attr-defined]

    original_close = browser.close

    def close_with_pw():
        try:
            original_close()
        finally:
            pw.stop()

    browser.close = close_with_pw  # type: ignore[method-assign]
    return browser


def run_full_auth(cfg: Config) -> str:
    """Drive the OAuth browser flow end-to-end.

    Returns the authorization `code` extracted from the redirect URI.
    Raises AuthError on any documented failure; the browser is always closed
    before raising.
    """
    username = resolve_secret(cfg.username or "")
    password = resolve_secret(cfg.password or "")
    headless = not _is_debug_truthy(os.environ.get("DEBUG"))

    try:
        browser = _launch_browser(headless)
    except Exception as e:
        msg = str(e)
        if "Executable doesn't exist" in msg or "playwright install" in msg.lower():
            raise AuthError(
                "Chromium not found. Run: `uv run playwright install chromium`"
            ) from e
        raise AuthError(f"Failed to launch browser: {msg}") from e

    try:
        page = browser.new_page()
        page.goto(build_auth_url(cfg))

        wait_any(
            page,
            expected=LOGIN_USERNAME_SELECTOR,
            known_errors={
                marker: "Schwab rejected client_id/secret — verify setup."
                for marker in INVALID_CLIENT_MARKERS
            },
        )

        page.fill(LOGIN_USERNAME_SELECTOR, username)
        page.fill(LOGIN_PASSWORD_SELECTOR, password)
        page.click(LOGIN_SUBMIT_SELECTOR)

        wait_any(
            page,
            expected=CONSENT_PAGE_SELECTOR,
            known_errors={
                INVALID_CREDENTIALS_TEXT: "Login failed — incorrect username/password.",
                REDIRECT_URI_MISMATCH_TEXT: "Redirect URI mismatch — re-check setup.",
            },
        )

        # Scroll to bottom so the Accept button is in view.
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.click(ACCEPT_SELECTOR)

        wait_any(
            page,
            expected=ACCOUNT_SELECTION_SELECTOR,
            known_errors={},
        )

        checkboxes = page.query_selector_all(ACCOUNT_CHECKBOX_SELECTOR)
        if not checkboxes:
            raise AuthError("No accounts available on this login.")
        for cb in checkboxes:
            if not cb.is_checked():
                cb.check()
        page.click(CONTINUE_SELECTOR)

        wait_any(
            page,
            expected=CONFIRM_PAGE_SELECTOR,
            known_errors={},
        )

        page.click(DONE_SELECTOR)

        try:
            page.wait_for_url(
                lambda u: u.startswith(cfg.redirect_uri),
                timeout=15_000,
            )
        except Exception as e:
            raise AuthError(
                "Redirect didn't happen — auth incomplete."
            ) from e

        parsed = urlparse(page.url)
        code = parse_qs(parsed.query).get("code", [None])[0]
        if not code:
            raise AuthError("Redirect reached but no `code` param present.")
        return code
    finally:
        try:
            browser.close()
        except Exception:  # pragma: no cover
            pass
