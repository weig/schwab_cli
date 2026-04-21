"""SeleniumBase UC backend for the auth flow.

Used only when HEADLESS=1 is requested. SeleniumBase's `uc_open_with_reconnect`
trick gets past Schwab's Akamai WAF that blocks Playwright headless sessions.

Architecture mirrors `flow.py`'s `run_full_auth` step-for-step. The selectors
are imported from `selectors.py` so a single source of truth governs both
backends.

NOTE: this module imports SeleniumBase lazily inside `run_full_auth_selenium`
so the dependency is not required when running with the (default) Playwright
backend.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from schwab_cli.browser.selectors import (
    ACCOUNT_CHECKBOX_SELECTOR,
    ACCOUNT_CONTINUE_SELECTOR,
    CONFIRM_PAGE_SELECTOR,
    CONSENT_CHECKBOX_SELECTOR,
    CONSENT_CONTINUE_SELECTOR,
    CONSENT_MODAL_ACCEPT_SELECTOR,
    CONSENT_PAGE_SELECTOR,
    DONE_SELECTOR,
    LOGIN_PASSWORD_SELECTOR,
    LOGIN_SUBMIT_SELECTOR,
    LOGIN_USERNAME_SELECTOR,
    MFA_PICKER_PAGE_SELECTOR,
    MFA_WAITING_PAGE_SELECTOR,
    SCHWAB_APP_OPTION_SELECTOR,
    TRUST_CONTINUE_SELECTOR,
    TRUST_DEVICE_PAGE_SELECTOR,
    TRUST_YES_SELECTOR,
    URL_FRAGMENT_ACCOUNTS,
    URL_FRAGMENT_CONFIRMATION,
)
from schwab_cli.config import Config, config_path
from schwab_cli.oauth import build_auth_url
from schwab_cli.secrets import resolve_secret
from schwab_cli.utils import _debug_log, _is_debug_truthy


class AuthError(Exception):
    """Raised on any failure in the SeleniumBase auth flow.

    Imported by callers via `flow.AuthError` (re-exported there).
    """


_RECONNECT_SECONDS = 4
_DEFAULT_TIMEOUT = 15
_POST_LOGIN_TIMEOUT = 60
_MFA_TIMEOUT = 60


def _user_data_dir() -> Path:
    # Different dir from the Playwright one — incompatible profile formats.
    return config_path().parent / "chromium-uc"


def _make_dump_dir() -> Path:
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
    out = content
    for s in secrets:
        if s:
            out = out.replace(s, "[REDACTED]")
    return out


def _user_message(text: str) -> None:
    import sys
    print(text, file=sys.stderr, flush=True)


def _wait_for_one_of(driver, conditions: dict[str, str], timeout: int) -> str:
    """Poll for whichever CSS selector appears first.

    `conditions` maps a label → CSS selector. Returns the matched label.
    Raises AuthError on timeout.
    """
    from selenium.webdriver.common.by import By
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for name, selector in conditions.items():
            try:
                els = driver.find_elements(By.CSS_SELECTOR, selector)
                if els and any(e.is_displayed() for e in els):
                    return name
            except Exception:
                pass
        time.sleep(0.2)
    raise AuthError(
        "Auth step timed out — Schwab may have changed. Selectors live at "
        "src/schwab_cli/browser/selectors.py. Auth incomplete."
    )


def _selector_to_css(playwright_selector: str) -> str:
    """Convert simple Playwright text selectors to CSS where possible.

    Most of our selectors are already CSS (#id, input[type=...]). The two
    text-based ones (MFA waiting page, trust device page heading) need to
    be detected via element presence with text. Selenium has no direct
    text= selector, so we use XPath for those — see _wait_for_one_of which
    handles selectors prefixed with "text=" specially.
    """
    return playwright_selector


def _find_text_or_css(driver, selector: str):
    """Find element(s) by CSS or 'text=' Playwright-style selector.

    Selenium has no direct equivalent to Playwright's text=, so we synthesize
    via XPath for the few text-based selectors we use.
    """
    from selenium.webdriver.common.by import By
    if selector.startswith("text="):
        text = selector[len("text="):]
        # contains() handles substring match like Playwright text=
        xp = f"//*[contains(normalize-space(.), {repr(text)})]"
        return driver.find_elements(By.XPATH, xp)
    return driver.find_elements(By.CSS_SELECTOR, selector)


def _wait_for_one_of_v2(driver, conditions: dict[str, str], timeout: int) -> str:
    """Like _wait_for_one_of but supports Playwright-style 'text=...' selectors."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for name, selector in conditions.items():
            try:
                els = _find_text_or_css(driver, selector)
                if els and any(e.is_displayed() for e in els):
                    return name
            except Exception:
                pass
        time.sleep(0.2)
    raise AuthError(
        "Auth step timed out — Schwab may have changed. Selectors live at "
        "src/schwab_cli/browser/selectors.py. Auth incomplete."
    )


def _wait_visible(driver, selector: str, timeout: int) -> None:
    """Wait until a single selector resolves to a visible element."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            els = _find_text_or_css(driver, selector)
            if els and any(e.is_displayed() for e in els):
                return
        except Exception:
            pass
        time.sleep(0.2)
    raise AuthError(
        f"Selector {selector!r} never became visible — auth incomplete."
    )


def _set_checkbox(driver, selector: str, checked: bool = True) -> None:
    """Check/uncheck a checkbox or radio in a framework-aware way.

    Schwab's consent/trust-device controls use Angular reactive forms over
    styled-label checkbox components. The underlying <input> is often
    visibility-hidden with the visible affordance on a sibling label, so
    Selenium `.click()` hits nothing. Directly setting `.checked` and
    dispatching input+change+blur events is the pattern Angular's binding
    actually listens for, and bypasses the label-click indirection.
    """
    els = _find_text_or_css(driver, selector)
    if not els:
        raise AuthError(f"Checkbox {selector!r} not found")
    driver.execute_script(
        """
        const el = arguments[0];
        const target = Boolean(arguments[1]);
        if (el.checked !== target) {
            el.checked = target;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new Event('blur', { bubbles: true }));
        }
        """,
        els[0],
        checked,
    )


def _click_first(driver, selector: str) -> None:
    """Click the first matching element (CSS or text=).

    Regular Selenium `.click()` works for most Schwab buttons; ActionChains
    regresses them (observed on Continue buttons). Use native click.
    """
    els = _find_text_or_css(driver, selector)
    if not els:
        raise AuthError(f"Cannot click {selector!r}: no element found.")
    el = els[0]
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    except Exception:
        pass
    el.click()


def _click_modal_accept(driver, selector: str) -> None:
    """Click the Informed Consent modal's Accept button.

    This button is an `sdps-button` (Schwab Design System Web Component).
    Its click handler lives inside the shadow root; native Selenium
    `.click()` hits the host but the handler doesn't fire. Dispatching a
    full MouseEvent (not just `.click()`) propagates through the shadow
    boundary correctly.
    """
    els = _find_text_or_css(driver, selector)
    if not els:
        raise AuthError(f"Modal button {selector!r} not found.")
    el = els[0]
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    except Exception:
        pass
    driver.execute_script(
        """
        const el = arguments[0];
        const rect = el.getBoundingClientRect();
        const opts = {
            bubbles: true, cancelable: true, view: window, composed: true,
            clientX: rect.left + rect.width / 2,
            clientY: rect.top + rect.height / 2,
            button: 0,
        };
        el.dispatchEvent(new MouseEvent('mousedown', opts));
        el.dispatchEvent(new MouseEvent('mouseup', opts));
        el.dispatchEvent(new MouseEvent('click', opts));
        """,
        el,
    )


def _capture_redirect(driver, redirect_uri: str, timeout: int) -> str:
    """Capture the redirect URL via CDP performance log.

    Chrome navigates briefly to redirect_uri, then shows chrome-error://
    because nothing's listening. Polling current_url misses the window;
    scanning Network.requestWillBeSent CDP events catches the request that
    was issued before the failure.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # Cheap path: maybe current_url is briefly correct
        try:
            current = driver.current_url
            if current.startswith(redirect_uri):
                return current
        except Exception:
            pass
        # Robust path: scan performance log for navigation events
        try:
            for entry in driver.get_log("performance"):
                try:
                    msg = json.loads(entry["message"])
                    method = msg.get("message", {}).get("method", "")
                    if method == "Network.requestWillBeSent":
                        url = msg["message"]["params"]["request"]["url"]
                        if url.startswith(redirect_uri):
                            return url
                except (json.JSONDecodeError, KeyError):
                    continue
        except Exception:
            # Performance log may not be enabled in some configurations
            pass
        time.sleep(0.1)
    raise AuthError("Redirect didn't happen — auth incomplete.")


def run_full_auth_selenium(cfg: Config) -> str:
    """SeleniumBase UC implementation of the OAuth browser flow.

    Mirrors the Playwright `run_full_auth` step-for-step. Returns the
    captured authorization code or raises AuthError.
    """
    from seleniumbase import Driver

    debug = _is_debug_truthy(os.environ.get("DEBUG"))
    _debug_log("resolving secrets")
    username = resolve_secret(cfg.username or "")
    password = resolve_secret(cfg.password or "")

    user_data_dir = _user_data_dir()
    user_data_dir.mkdir(parents=True, exist_ok=True)
    try:
        user_data_dir.chmod(0o700)
    except OSError:
        pass

    _debug_log(f"launching seleniumbase UC chromium (headless=True, profile={user_data_dir})")
    try:
        driver = Driver(
            uc=True,
            headless=True,
            user_data_dir=str(user_data_dir),
            log_cdp_events=True,  # enables performance log used by _capture_redirect
        )
    except Exception as e:
        raise AuthError(f"Failed to launch SeleniumBase UC: {e}") from e

    run_dir = _make_dump_dir() if debug else None
    if run_dir is not None:
        _debug_log(f"page source dumps will be written to {run_dir}")
    step_idx = [0]
    secrets_to_redact = (username, password)

    def dump(label: str) -> None:
        if run_dir is None:
            return
        step_idx[0] += 1
        try:
            content = driver.page_source
        except Exception as e:
            content = f"<!-- failed to capture: {e} -->"
        content = _sanitize(content, secrets_to_redact)
        path = run_dir / f"{step_idx[0]:02d}-{label}.html"
        try:
            path.write_text(content)
            path.chmod(0o600)
        except OSError as e:
            _debug_log(f"failed to write dump: {e}")
            return
        _debug_log(f"dumped page source → {path}")

    try:
        auth_url = build_auth_url(cfg)
        _debug_log(f"navigating to auth URL: {auth_url}")
        driver.uc_open_with_reconnect(auth_url, reconnect_time=_RECONNECT_SECONDS)

        _debug_log("waiting for login page")
        _wait_visible(driver, LOGIN_USERNAME_SELECTOR, _DEFAULT_TIMEOUT)
        dump("login")

        # Quick sanity check: did Akamai still block us despite UC mode?
        try:
            if "Access Denied" in driver.title:
                raise AuthError(
                    "SeleniumBase UC was blocked by Akamai (Access Denied). "
                    "The bypass may need re-tuning."
                )
        except AuthError:
            raise
        except Exception:
            pass

        _debug_log("filling credentials and submitting login")
        driver.find_element("css selector", LOGIN_USERNAME_SELECTOR).send_keys(username)
        driver.find_element("css selector", LOGIN_PASSWORD_SELECTOR).send_keys(password)
        driver.find_element("css selector", LOGIN_SUBMIT_SELECTOR).click()

        _debug_log("waiting for MFA picker / waiting / consent (whichever appears first)")
        post_login = _wait_for_one_of_v2(
            driver,
            conditions={
                "consent": CONSENT_PAGE_SELECTOR,
                "mfa_waiting": MFA_WAITING_PAGE_SELECTOR,
                "mfa_picker": MFA_PICKER_PAGE_SELECTOR,
            },
            timeout=_POST_LOGIN_TIMEOUT,
        )
        dump(f"post-login-{post_login}")

        if post_login == "mfa_picker":
            _debug_log("MFA picker page; clicking Schwab App option")
            els = _find_text_or_css(driver, SCHWAB_APP_OPTION_SELECTOR)
            if not els:
                dump("mfa-no-schwab-app")
                raise AuthError(
                    "Schwab App MFA option not found. Inspect the dumped page "
                    "source under ~/.config/schwab_cli/auth-debug/."
                )
            els[0].click()
            post_login = "mfa_waiting"  # fall through

        if post_login == "mfa_waiting":
            _user_message("Schwab App MFA: check your phone to approve (up to 60s)...")
            _debug_log("waiting for trust-device or consent page (60s)")
            after = _wait_for_one_of_v2(
                driver,
                conditions={
                    "trust": TRUST_DEVICE_PAGE_SELECTOR,
                    "consent": CONSENT_PAGE_SELECTOR,
                },
                timeout=_MFA_TIMEOUT,
            )
            dump(f"post-mfa-{after}")
            if after == "trust":
                _debug_log("trust-device page: selecting yes and clicking continue")
                _set_checkbox(driver, TRUST_YES_SELECTOR, True)
                _click_first(driver, TRUST_CONTINUE_SELECTOR)
                dump("after-trust-continue")
                _wait_visible(driver, CONSENT_PAGE_SELECTOR, _DEFAULT_TIMEOUT)

        _debug_log("checking consent agreement and clicking continue")
        dump("consent")
        _set_checkbox(driver, CONSENT_CHECKBOX_SELECTOR, True)
        _click_first(driver, CONSENT_CONTINUE_SELECTOR)

        _debug_log("waiting for Informed Consent modal Accept")
        _wait_visible(driver, CONSENT_MODAL_ACCEPT_SELECTOR, _DEFAULT_TIMEOUT)
        dump("consent-modal")
        _click_modal_accept(driver, CONSENT_MODAL_ACCEPT_SELECTOR)

        _debug_log("waiting for account selection page")
        deadline = time.monotonic() + _DEFAULT_TIMEOUT
        while time.monotonic() < deadline:
            if URL_FRAGMENT_ACCOUNTS in driver.current_url:
                break
            time.sleep(0.2)
        else:
            raise AuthError("Account selection page didn't load — auth incomplete.")
        dump("accounts")

        from selenium.webdriver.common.by import By
        checkboxes = driver.find_elements(By.CSS_SELECTOR, ACCOUNT_CHECKBOX_SELECTOR)
        _debug_log(f"found {len(checkboxes)} account checkbox(es)")
        if not checkboxes:
            raise AuthError("No accounts available on this login.")
        for cb in checkboxes:
            try:
                if not cb.is_selected():
                    driver.execute_script(
                        "const e=arguments[0]; e.checked=true; "
                        "e.dispatchEvent(new Event('input',{bubbles:true})); "
                        "e.dispatchEvent(new Event('change',{bubbles:true})); "
                        "e.dispatchEvent(new Event('blur',{bubbles:true}));",
                        cb,
                    )
            except Exception:
                pass
        _debug_log("all accounts selected; clicking continue")
        _click_first(driver, ACCOUNT_CONTINUE_SELECTOR)

        _debug_log("waiting for confirmation page")
        deadline = time.monotonic() + _DEFAULT_TIMEOUT
        while time.monotonic() < deadline:
            if URL_FRAGMENT_CONFIRMATION in driver.current_url:
                break
            time.sleep(0.2)
        else:
            raise AuthError("Confirmation page didn't load — auth incomplete.")
        dump("confirm")

        # Click Done and capture redirect URL via CDP performance log.
        _debug_log(f"clicking Done and capturing redirect to {cfg.redirect_uri}")
        _click_first(driver, DONE_SELECTOR)
        redirect_url = _capture_redirect(driver, cfg.redirect_uri, _DEFAULT_TIMEOUT)

        parsed = urlparse(redirect_url)
        code = parse_qs(parsed.query).get("code", [None])[0]
        if not code:
            raise AuthError("Redirect reached but no `code` param present.")
        _debug_log("authorization code captured (closing driver to exchange before TTL)")
        return code
    except Exception:
        if run_dir is not None:
            try:
                dump("failure")
            except Exception:
                pass
        raise
    finally:
        try:
            driver.quit()
        except Exception:
            pass
