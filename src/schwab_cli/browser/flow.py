from __future__ import annotations

import os
import time
from typing import Protocol
from urllib.parse import parse_qs, urlparse

import sys
from datetime import datetime, timezone
from pathlib import Path

from schwab_cli.browser.selectors import (
    ACCOUNT_CHECKBOX_SELECTOR,
    ACCOUNT_CONTINUE_SELECTOR,
    CONFIRM_PAGE_SELECTOR,
    CONSENT_CHECKBOX_SELECTOR,
    CONSENT_CONTINUE_SELECTOR,
    CONSENT_MODAL_ACCEPT_SELECTOR,
    CONSENT_PAGE_SELECTOR,
    DONE_SELECTOR,
    INVALID_CLIENT_MARKERS,
    INVALID_CREDENTIALS_TEXT,
    LOGIN_PASSWORD_SELECTOR,
    LOGIN_SUBMIT_SELECTOR,
    LOGIN_USERNAME_SELECTOR,
    MFA_PAGE_SELECTOR,
    REDIRECT_URI_MISMATCH_TEXT,
    SCHWAB_APP_OPTION_SELECTOR,
    TRUST_CONTINUE_SELECTOR,
    TRUST_DEVICE_PAGE_SELECTOR,
    TRUST_YES_SELECTOR,
    URL_FRAGMENT_ACCOUNTS,
    URL_FRAGMENT_CONFIRMATION,
)
from schwab_cli.config import Config, config_path
from schwab_cli.utils import _debug_log, _is_debug_truthy
from schwab_cli.oauth import build_auth_url
from schwab_cli.secrets import resolve_secret

_DEBUG_SLOW_MO_MS = 1000
_DEBUG_HOLD_OPEN_SECONDS = 60

# Minimal stealth: hide the most obvious Playwright fingerprints Schwab
# (and most anti-bot stacks) check for.
#
# Note on user_agent: we DON'T override the UA. Spoofing Chrome/<old-version>
# leaves Sec-CH-UA client-hint headers reporting the real bundled version,
# which is a trivial mismatch for bot detection to spot. Playwright's
# default UA already drops 'HeadlessChrome' when not headless, so the
# default is more believable than our spoof.
_STEALTH_LAUNCH_ARGS = ("--disable-blink-features=AutomationControlled",)
_STEALTH_INIT_SCRIPT = (
    "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
)


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
        # Check expected selector FIRST. If the page has advanced, trust that
        # even if stale error text still lives in the DOM (hidden) — Schwab
        # pages keep error elements in the HTML and toggle visibility.
        try:
            page.wait_for_selector(expected, timeout=int(_POLL_INTERVAL_SECONDS * 1000))
            return
        except Exception:
            pass

        # Expected not there yet — check whether a known error explains why.
        try:
            content = page.content()
        except Exception:
            content = ""
        for marker, user_message in known_errors.items():
            if marker in content:
                raise AuthError(user_message)

        if time.monotonic() >= deadline:
            raise AuthError(_UI_CHANGED_MESSAGE)


def wait_for_first_present(
    page,
    *,
    candidates: dict[str, str],
    known_errors: dict[str, str],
    timeout_ms: int = 15_000,
) -> str:
    """Wait for any of the candidate selectors to appear.

    Returns the key of the first selector that matches. Same error/timeout
    semantics as `wait_any`. Selectors are polled in dict-iteration order each
    cycle so with Python 3.7+ insertion order is respected — put the more
    common path first to minimize wait on the slow path.
    """
    deadline = time.monotonic() + timeout_ms / 1000.0
    per_iter_timeout = int(_POLL_INTERVAL_SECONDS * 1000)
    while True:
        for name, selector in candidates.items():
            try:
                page.wait_for_selector(selector, timeout=per_iter_timeout)
                return name
            except Exception:
                pass

        try:
            content = page.content()
        except Exception:
            content = ""
        for marker, user_message in known_errors.items():
            if marker in content:
                raise AuthError(user_message)

        if time.monotonic() >= deadline:
            raise AuthError(_UI_CHANGED_MESSAGE)


def _user_message(text: str) -> None:
    """Print a user-facing status line to stderr (always, not just DEBUG)."""
    print(text, file=sys.stderr, flush=True)


def _make_dump_dir() -> Path:
    """Create (and lock down) a fresh debug dump directory for this run.

    Returned path looks like: ~/.config/schwab_cli/auth-debug/<ISO-timestamp>/
    """
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = config_path().parent / "auth-debug" / stamp
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
        root.parent.chmod(0o700)
    except OSError:
        pass
    return root


def _sanitize(content: str, secrets: tuple[str, ...]) -> str:
    """Redact any occurrences of the given secrets from the page source."""
    sanitized = content
    for s in secrets:
        if s:
            sanitized = sanitized.replace(s, "[REDACTED]")
    return sanitized


def _dump_page(
    page,
    *,
    label: str,
    run_dir: Path | None,
    step_idx: list[int],
    secrets: tuple[str, ...],
) -> None:
    """Save `page.content()` to disk at `<run_dir>/<NN>-<label>.html`.

    No-op if DEBUG is unset or `run_dir` is None. Sanitizes known secrets
    before writing. `step_idx` is a 1-element list used as a mutable counter
    so callers don't have to thread an index themselves.
    """
    if run_dir is None:
        return
    step_idx[0] += 1
    try:
        content = page.content()
    except Exception as e:
        content = f"<!-- failed to capture: {e} -->"
    content = _sanitize(content, secrets)
    filename = f"{step_idx[0]:02d}-{label}.html"
    path = run_dir / filename
    try:
        path.write_text(content)
        path.chmod(0o600)
    except OSError as e:
        _debug_log(f"failed to write dump {filename}: {e}")
        return
    _debug_log(f"dumped page source → {path}")


def _user_data_dir() -> Path:
    """Persistent Chromium profile dir.

    Survives across runs so Schwab's Trust Device cookie persists, removing
    the need for MFA approval on every auth invocation.
    """
    return config_path().parent / "chromium"


def _launch_browser(headless: bool, slow_mo_ms: int = 0):  # pragma: no cover
    """Launch Chromium with a persistent context.

    Returns a `BrowserContext` (not a `Browser`); call sites use `.new_page()`
    / `.add_init_script()` / `.close()` exactly the same way. Persistent state
    is saved at `~/.config/schwab_cli/chromium/` so cookies, local storage and
    the Trust Device cookie survive across runs.
    """
    from playwright.sync_api import sync_playwright

    user_data_dir = _user_data_dir()
    user_data_dir.mkdir(parents=True, exist_ok=True)
    try:
        user_data_dir.chmod(0o700)
    except OSError:
        pass

    pw = sync_playwright().start()
    context = pw.chromium.launch_persistent_context(
        user_data_dir=str(user_data_dir),
        headless=headless,
        slow_mo=slow_mo_ms,
        args=list(_STEALTH_LAUNCH_ARGS),
    )

    original_close = context.close

    def close_with_pw():
        try:
            original_close()
        finally:
            pw.stop()

    context.close = close_with_pw  # type: ignore[method-assign]
    return context


def run_full_auth(cfg: Config) -> str:
    """Drive the OAuth browser flow end-to-end.

    Returns the authorization `code` extracted from the redirect URI.
    Raises AuthError on any documented failure; the browser is always closed
    before raising.
    """
    _debug_log("resolving secrets")
    username = resolve_secret(cfg.username or "")
    password = resolve_secret(cfg.password or "")
    debug = _is_debug_truthy(os.environ.get("DEBUG"))
    headless = not debug
    slow_mo_ms = _DEBUG_SLOW_MO_MS if debug else 0

    _debug_log(
        f"launching chromium (headless={headless}, slow_mo={slow_mo_ms}ms, "
        f"profile={_user_data_dir()})"
    )
    try:
        context = _launch_browser(headless, slow_mo_ms=slow_mo_ms)
    except Exception as e:
        msg = str(e)
        if "Executable doesn't exist" in msg or "playwright install" in msg.lower():
            raise AuthError(
                "Chromium not found. Run: `uv run playwright install chromium`"
            ) from e
        raise AuthError(f"Failed to launch browser: {msg}") from e

    run_dir = _make_dump_dir() if debug else None
    if run_dir is not None:
        _debug_log(f"page source dumps will be written to {run_dir}")
    step_idx = [0]
    secrets_to_redact = (username, password)

    def dump(label: str) -> None:
        _dump_page(
            page,
            label=label,
            run_dir=run_dir,
            step_idx=step_idx,
            secrets=secrets_to_redact,
        )

    page = None
    try:
        context.add_init_script(_STEALTH_INIT_SCRIPT)
        page = context.new_page()
        auth_url = build_auth_url(cfg)
        _debug_log(f"navigating to auth URL: {auth_url}")
        page.goto(auth_url)

        _debug_log("waiting for login page")
        wait_any(
            page,
            expected=LOGIN_USERNAME_SELECTOR,
            known_errors={
                marker: "Schwab rejected client_id/secret — verify setup."
                for marker in INVALID_CLIENT_MARKERS
            },
        )
        dump("login")

        _debug_log("filling credentials and submitting login")
        # press_sequentially fires real keystroke events (keydown/keypress/input
        # /keyup); page.fill only sets value + dispatches a single 'input'
        # event, which Angular's reactive forms sometimes don't pick up cleanly
        # on Schwab's login. Real keystrokes ensure ng-dirty/ng-touched + the
        # form's own listeners fire as the user would.
        page.locator(LOGIN_USERNAME_SELECTOR).press_sequentially(username, delay=20)
        page.locator(LOGIN_PASSWORD_SELECTOR).press_sequentially(password, delay=20)
        # Tab off the password to fire blur (some forms only validate on blur).
        page.keyboard.press("Tab")
        page.click(LOGIN_SUBMIT_SELECTOR)

        _debug_log("waiting for MFA / consent page (whichever appears first)")
        post_login = wait_for_first_present(
            page,
            candidates={
                "consent": CONSENT_PAGE_SELECTOR,
                "mfa": MFA_PAGE_SELECTOR,
            },
            known_errors={
                INVALID_CREDENTIALS_TEXT: "Login failed — incorrect username/password.",
                REDIRECT_URI_MISMATCH_TEXT: "Redirect URI mismatch — re-check setup.",
            },
        )
        dump(f"post-login-{post_login}")

        if post_login == "mfa":
            _debug_log("MFA page detected; looking for Schwab App option")
            schwab_app = page.locator(SCHWAB_APP_OPTION_SELECTOR).first
            if schwab_app.count() == 0:
                dump("mfa-no-schwab-app")
                raise AuthError(
                    "Schwab App MFA option not found. Re-run with DEBUG=1 and "
                    "inspect the dumped page source to see what options "
                    "Schwab is offering."
                )
            schwab_app.click()
            _user_message("Schwab App MFA: check your phone to approve (up to 30s)...")
            _debug_log("waiting for trust-device or consent page (30s)")

            after_approval = wait_for_first_present(
                page,
                candidates={
                    "trust": TRUST_DEVICE_PAGE_SELECTOR,
                    "consent": CONSENT_PAGE_SELECTOR,
                },
                known_errors={},
                timeout_ms=30_000,
            )
            dump(f"post-mfa-{after_approval}")

            if after_approval == "trust":
                _debug_log("trust-device page: selecting yes and clicking continue")
                page.locator(TRUST_YES_SELECTOR).first.click()
                page.click(TRUST_CONTINUE_SELECTOR)
                dump("after-trust-continue")
                _debug_log("waiting for consent page after trust step")
                wait_any(
                    page,
                    expected=CONSENT_PAGE_SELECTOR,
                    known_errors={},
                )

        _debug_log("checking consent agreement and clicking continue")
        dump("consent")
        page.locator(CONSENT_CHECKBOX_SELECTOR).first.click()
        page.click(CONSENT_CONTINUE_SELECTOR)

        _debug_log("waiting for Informed Consent modal Accept")
        wait_any(
            page,
            expected=CONSENT_MODAL_ACCEPT_SELECTOR,
            known_errors={},
        )
        dump("consent-modal")
        page.click(CONSENT_MODAL_ACCEPT_SELECTOR)

        _debug_log("waiting for account selection page")
        try:
            page.wait_for_url(
                lambda u: URL_FRAGMENT_ACCOUNTS in u,
                timeout=15_000,
            )
        except Exception as e:
            raise AuthError("Account selection page didn't load — auth incomplete.") from e
        dump("accounts")

        checkboxes = page.query_selector_all(ACCOUNT_CHECKBOX_SELECTOR)
        _debug_log(f"found {len(checkboxes)} account checkbox(es)")
        if not checkboxes:
            raise AuthError("No accounts available on this login.")
        for cb in checkboxes:
            if not cb.is_checked():
                cb.check()
        _debug_log("all accounts selected; clicking continue")
        page.click(ACCOUNT_CONTINUE_SELECTOR)

        _debug_log("waiting for confirmation page")
        try:
            page.wait_for_url(
                lambda u: URL_FRAGMENT_CONFIRMATION in u,
                timeout=15_000,
            )
        except Exception as e:
            raise AuthError("Confirmation page didn't load — auth incomplete.") from e
        dump("confirm")

        # Capture the redirect URL via request interception. Chrome cannot
        # actually load the redirect_uri (no server is listening) so by the
        # time `page.url` updates it's already the chrome-error page —
        # `wait_for_url` would never match. `expect_request` catches the URL
        # at the moment Chrome issues the navigation request, before failure.
        _debug_log(f"clicking Done and capturing redirect to {cfg.redirect_uri}")
        try:
            with page.expect_request(
                lambda req: req.url.startswith(cfg.redirect_uri),
                timeout=15_000,
            ) as req_info:
                page.click(DONE_SELECTOR)
            redirect_url = req_info.value.url
        except Exception as e:
            raise AuthError("Redirect didn't happen — auth incomplete.") from e

        parsed = urlparse(redirect_url)
        code = parse_qs(parsed.query).get("code", [None])[0]
        if not code:
            raise AuthError("Redirect reached but no `code` param present.")
        _debug_log("authorization code captured")
        return code
    except Exception:
        # Always capture the current page so the user can see exactly what
        # Schwab was showing when we failed — selectors can be tuned from it.
        if page is not None:
            dump("failure")
        raise
    finally:
        if debug:
            _debug_log(
                f"holding browser open for {_DEBUG_HOLD_OPEN_SECONDS}s "
                "(Ctrl+C to close now)"
            )
            try:
                time.sleep(_DEBUG_HOLD_OPEN_SECONDS)
            except KeyboardInterrupt:
                _debug_log("hold interrupted; closing browser")
        try:
            context.close()
        except Exception:  # pragma: no cover
            pass
