"""SeleniumBase UC browser backend for the Schwab OAuth flow.

Public entry point: :func:`run_browser_auth`. The function opens a Chromium
session via SeleniumBase's Undetected-Chrome mode (required to get past
Schwab's Akamai bot-detection), optionally drives the login/consent/account
screens automatically, and returns the final OAuth redirect URL the browser
landed on. Parsing ``code`` / ``state`` out of the URL is the caller's job
(see :mod:`schwab_cli.auth_flows`).

Browser visibility follows the ``HEADLESS`` env var:
  * ``HEADLESS=1`` → run Chromium headless (no window).
  * anything else  → visible window.

``automate=True`` runs the saved-credential login; ``automate=False`` opens
the auth URL and waits for the user to complete login manually.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from schwab_cli.browser.selectors import (
    ACCOUNT_CHECKBOX_SELECTOR,
    ACCOUNT_CONTINUE_SELECTOR,
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
    """Raised on any failure in the browser-driven auth flow."""


_RECONNECT_SECONDS = 4
_DEFAULT_TIMEOUT = 15
_POST_LOGIN_TIMEOUT = 60
_MFA_TIMEOUT = 60
_AUTOMATION_REDIRECT_TIMEOUT = 15
_MANUAL_REDIRECT_TIMEOUT = 300  # 5 min budget for the user to finish login


def _user_data_dir() -> Path:
    """Persistent Chromium profile dir so the Trust Device cookie survives."""
    return config_path().parent / "chromium-uc"


def _switch_to_main_window(driver) -> None:
    """Point ``driver`` at the largest real tab, skipping chrome-internal popups.

    Under SB UC visible mode, ``driver.window_handles`` frequently includes
    an auxiliary context (Omnibox Popup, empty about:blank shell) that the
    driver defaults to — while the human-visible Chrome tab is a different
    handle. We pick the handle whose URL is http(s) when possible; otherwise
    we pick the last handle (Chrome's most-recently-focused tab, which is
    usually the one the user sees).
    """
    try:
        handles = list(driver.window_handles)
    except Exception:
        return
    _debug_log(f"window handles: {len(handles)} found")
    if not handles:
        return
    # Score each handle: prefer http(s) URLs, then non-chrome-internal.
    best = None
    best_score = -1
    for h in handles:
        try:
            driver.switch_to.window(h)
            url = driver.current_url or ""
        except Exception:
            continue
        if url.startswith(("http://", "https://")):
            score = 3
        elif url in ("about:blank", "data:,"):
            score = 2
        elif url.startswith("chrome://"):
            score = 1  # e.g. chrome://newtab — still a real visible tab
        else:
            score = 0  # chrome-internal popup, devtools, etc.
        _debug_log(f"  handle {h[:8]}… url={url[:60]!r} score={score}")
        if score > best_score:
            best_score = score
            best = h
    if best is not None:
        driver.switch_to.window(best)
        _debug_log(f"switched to window {best[:8]}… (url={driver.current_url[:60]!r})")


def _pick_free_port() -> int:
    """Return a port that is free right now on 127.0.0.1.

    SeleniumBase's UC mode defaults to remote-debugging-port=9222. Its own
    "is 9222 free?" probe treats any non-200 HTTP response as "free" — which
    is wrong when something else (e.g. chrome-devtools-mcp) is squatting on
    the port but returning 404. We sidestep the whole probe by picking an
    ephemeral port ourselves and passing it to SB via ``chromium_arg``.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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
    print(text, file=sys.stderr, flush=True)


def _find_text_or_css(driver, selector: str):
    """Find elements by CSS or Playwright-style ``text=`` selector.

    Selenium has no native text= selector; we synthesize one via XPath
    ``contains()`` for the handful of text-based selectors we inherit from the
    Playwright-era selector file.
    """
    from selenium.webdriver.common.by import By
    if selector.startswith("text="):
        text = selector[len("text="):]
        xp = f"//*[contains(normalize-space(.), {repr(text)})]"
        return driver.find_elements(By.XPATH, xp)
    return driver.find_elements(By.CSS_SELECTOR, selector)


def _wait_for_one_of_v2(driver, conditions: dict[str, str], timeout: int) -> str:
    """Poll for whichever (label → selector) entry is present in the DOM first.

    We deliberately do NOT gate on Selenium's ``is_displayed()`` — under
    SB UC mode (especially visible) it misfires for Angular-hydrated forms
    that are plainly on screen. Presence in the DOM is a safer signal;
    downstream ``send_keys`` / ``click`` will raise crisply if an element
    is truly interaction-blocked.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for name, selector in conditions.items():
            try:
                els = _find_text_or_css(driver, selector)
                if els:
                    return name
            except Exception:
                pass
        time.sleep(0.2)
    raise AuthError(
        "Auth step timed out — Schwab may have changed. Selectors live at "
        "src/schwab_cli/browser/selectors.py. Auth incomplete."
    )


def _wait_for_hydration(driver, timeout: int = 8) -> None:
    """Pause until Angular has hydrated enough to bind event handlers.

    Schwab's post-login pages ship as an Angular shell where the buttons
    exist in the DOM but handlers aren't wired up yet — JS-dispatched events
    get ignored until bootstrap finishes. The placeholder ``<title>cag</title>``
    is a reliable "not hydrated yet" signal; we wait for it to become the
    real page title. A fixed floor of 500ms gives handlers time to attach
    even after the title has already settled.
    """
    deadline = time.monotonic() + timeout
    start = time.monotonic()
    while time.monotonic() < deadline:
        try:
            title = driver.execute_script("return document.title || '';")
            if title and title != "cag":
                break
        except Exception:
            pass
        time.sleep(0.2)
    time.sleep(max(0.0, 0.5 - (time.monotonic() - start)))


def _wait_visible(driver, selector: str, timeout: int) -> None:
    """Wait until ``selector`` appears in the DOM (presence, not viewport).

    See :func:`_wait_for_one_of_v2` for the rationale on not using
    ``is_displayed()``.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            els = _find_text_or_css(driver, selector)
            if els:
                return
        except Exception:
            pass
        time.sleep(0.2)
    raise AuthError(
        f"Selector {selector!r} never appeared in the DOM — auth incomplete."
    )


def _set_checkbox(driver, selector: str, checked: bool = True) -> None:
    """Check/uncheck a checkbox or radio in a framework-aware way.

    Schwab uses Angular reactive forms over styled-label checkbox components;
    the native <input> is visibility-hidden and ``.click()`` hits nothing.
    Setting ``.checked`` and dispatching input/change/blur events is the
    pattern the framework's bindings actually listen for.
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
    els = _find_text_or_css(driver, selector)
    if not els:
        raise AuthError(f"Cannot click {selector!r}: no element found.")
    el = els[0]
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    except Exception:
        pass
    try:
        el.click()
    except Exception:
        # SB UC visible mode sometimes raises ElementNotInteractableException
        # even when the element is on screen; fall back to a JS click which
        # bypasses Selenium's viewport heuristics.
        driver.execute_script("arguments[0].click();", el)


def _set_input(driver, selector: str, value: str) -> None:
    """Set a text input's value via JS and fire the events Angular expects.

    Under SB UC visible mode, ``WebElement.send_keys`` frequently raises
    ``ElementNotInteractableException`` on Schwab's Angular login form —
    Selenium's "is interactable?" heuristic misfires for elements that are
    present in the DOM but haven't finished hydrating (or whose styled-label
    wrappers confuse the viewport check). Setting ``.value`` and dispatching
    input/change/blur mirrors what a user typing would look like to
    Angular reactive forms, and doesn't require the element to pass
    Selenium's interactability gate.
    """
    els = _find_text_or_css(driver, selector)
    if not els:
        raise AuthError(f"Input {selector!r} not found")
    el = els[0]
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    except Exception:
        pass
    driver.execute_script(
        """
        const el = arguments[0];
        const val = arguments[1];
        el.focus();
        el.value = val;
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        el.dispatchEvent(new Event('blur', { bubbles: true }));
        """,
        el,
        value,
    )


def _click_modal_accept(driver, selector: str) -> None:
    """Click the Informed Consent Accept button with escalating strategies.

    Observed in visible UC mode: a JS-dispatched MouseEvent on the sdps-button
    wrapper fires *some* handlers (enough to trigger Angular's SPA router to
    navigate to /accounts) but not all — notably, the POST that records
    consent on the backend never happens, so the subsequent accounts XHR
    hangs forever behind a spinner. Selenium's native ``.click()`` sends a
    real browser-level click that invokes all registered listeners,
    including those inside the web-component shadow root.
    """
    els = _find_text_or_css(driver, selector)
    if not els:
        raise AuthError(f"Modal button {selector!r} not found.")
    el = els[0]
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    except Exception:
        pass
    try:
        el.click()
        return
    except Exception:
        pass
    # Fallback 1: HTMLElement.click() — bypasses Selenium's interactability
    # heuristics but still invokes the native click handler.
    try:
        driver.execute_script("arguments[0].click();", el)
        return
    except Exception:
        pass
    # Fallback 2: synthesized MouseEvent sequence that composes through a
    # shadow DOM boundary. Last resort because Angular's ngClick binding
    # sometimes only partially responds to synthesized events.
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
    """Return the first URL the browser navigates to that starts with ``redirect_uri``.

    Chromium briefly navigates to the redirect URI and then shows a
    ``chrome-error://`` page when nothing's listening (client flow), so polling
    ``current_url`` on its own misses the window. Scanning the CDP performance
    log for ``Network.requestWillBeSent`` catches the request at the moment
    it's issued. For the code_relay flow the relay URL loads normally and
    ``current_url`` works, but the CDP scan still picks it up first.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            current = driver.current_url
            if current.startswith(redirect_uri):
                return current
        except Exception:
            pass
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
            pass
        time.sleep(0.1)
    raise AuthError("Redirect didn't happen — auth incomplete.")


def _automate_login(
    driver,
    cfg: Config,
    username: str,
    password: str,
    dump,
) -> None:
    """Drive the Schwab login → MFA → consent → accounts → Done screens.

    Leaves the browser on the page where Chromium is about to navigate to
    ``cfg.redirect_uri``; the caller should then invoke :func:`_capture_redirect`.
    """
    _debug_log("waiting for login page")
    _wait_visible(driver, LOGIN_USERNAME_SELECTOR, _DEFAULT_TIMEOUT)
    dump("login")

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
    _set_input(driver, LOGIN_USERNAME_SELECTOR, username)
    _set_input(driver, LOGIN_PASSWORD_SELECTOR, password)
    _click_first(driver, LOGIN_SUBMIT_SELECTOR)

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
    _wait_for_hydration(driver)
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
        post_login = "mfa_waiting"

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
        _wait_for_hydration(driver)
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
    _wait_for_hydration(driver)
    dump("consent-modal")
    _click_modal_accept(driver, CONSENT_MODAL_ACCEPT_SELECTOR)
    # Give the accept handler a beat to start the navigation before we begin
    # polling current_url for the accounts URL fragment.
    time.sleep(1.0)

    _debug_log("waiting for account selection page")
    deadline = time.monotonic() + _DEFAULT_TIMEOUT
    while time.monotonic() < deadline:
        if URL_FRAGMENT_ACCOUNTS in driver.current_url:
            break
        time.sleep(0.2)
    else:
        raise AuthError("Account selection page didn't load — auth incomplete.")
    _wait_for_hydration(driver)

    # The account list is fetched async after the route loads — checkboxes
    # don't exist in the DOM yet at the moment the URL changes. Poll for up
    # to `_POST_LOGIN_TIMEOUT` (60s) because Schwab's account-fetch XHR can
    # be slow. Emit progress logs every 5s so a stuck spinner is observable.
    from selenium.webdriver.common.by import By
    deadline = time.monotonic() + _POST_LOGIN_TIMEOUT
    checkboxes: list = []
    last_log = time.monotonic()
    while time.monotonic() < deadline:
        checkboxes = driver.find_elements(By.CSS_SELECTOR, ACCOUNT_CHECKBOX_SELECTOR)
        if checkboxes:
            break
        if time.monotonic() - last_log >= 5.0:
            spinner_present = bool(driver.find_elements(By.CSS_SELECTOR, "sdps-spinner"))
            _debug_log(
                f"still waiting for account checkboxes "
                f"(spinner_present={spinner_present})"
            )
            last_log = time.monotonic()
        time.sleep(0.3)
    dump("accounts")
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
    _wait_for_hydration(driver)

    # Wait for the Done button to render (same async-fetch pattern as the
    # accounts list). Without this, the following _click_first can see an
    # empty DOM shell.
    deadline = time.monotonic() + _DEFAULT_TIMEOUT
    while time.monotonic() < deadline:
        if _find_text_or_css(driver, DONE_SELECTOR):
            break
        time.sleep(0.3)
    dump("confirm")

    _debug_log("clicking Done")
    _click_first(driver, DONE_SELECTOR)


def run_browser_auth(
    cfg: Config,
    *,
    automate: bool,
    state: str | None = None,
) -> str:
    """Run the Schwab OAuth flow in a SeleniumBase UC browser.

    Returns the final redirect URL the browser navigated to (starts with
    ``cfg.redirect_uri``). Raises :class:`AuthError` on any failure.

    Browser visibility is controlled by the ``HEADLESS`` env var; automation
    vs. manual login is controlled by the ``automate`` keyword.
    """
    debug = _is_debug_truthy(os.environ.get("DEBUG"))
    headless = _is_debug_truthy(os.environ.get("HEADLESS"))

    username = ""
    password = ""
    if automate:
        _debug_log("resolving secrets")
        username = resolve_secret(cfg.username or "")
        password = resolve_secret(cfg.password or "")

    user_data_dir = _user_data_dir()
    user_data_dir.mkdir(parents=True, exist_ok=True)
    try:
        user_data_dir.chmod(0o700)
    except OSError:
        pass

    debug_port = _pick_free_port()
    _debug_log(
        f"launching seleniumbase UC chromium (headless={headless}, "
        f"profile={user_data_dir}, debug_port={debug_port})"
    )
    from seleniumbase import Driver
    try:
        driver = Driver(
            uc=True,
            headless=headless,
            user_data_dir=str(user_data_dir),
            log_cdp_events=True,  # enables performance log used by _capture_redirect
            chromium_arg=f"--remote-debugging-port={debug_port}",
        )
    except Exception as e:
        raise AuthError(f"Failed to launch SeleniumBase UC: {e}") from e
    _debug_log("driver ready (Chromium launched, UC patches applied)")

    # SB UC visible mode often leaves the automation attached to an auxiliary
    # window (Omnibox Popup, about:blank shell, etc.) while a separate
    # user-facing tab shows the stock new-tab page. Force the driver onto
    # the first "real" window so subsequent navigation actually lands there.
    _switch_to_main_window(driver)

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
        auth_url = build_auth_url(cfg, state=state)
        _debug_log(f"navigating to auth URL: {auth_url}")
        if headless:
            # Proven path for headless: open URL in a new tab, close the blank
            # one, then disconnect/reconnect the driver. `driver.close()` is
            # instant because there's no visible window to tear down.
            driver.uc_open_with_reconnect(auth_url, reconnect_time=_RECONNECT_SECONDS)
            _debug_log(
                f"reconnected after initial nav (reconnect_time={_RECONNECT_SECONDS}s)"
            )
        else:
            # Visible browser: Akamai doesn't block a real visible Chromium,
            # so we don't need UC's "detach while navigating" trick here.
            # NOTE: SB's UC mode replaces `driver.get` with a lambda that
            # runs the same `window.open("...","_blank") + driver.close()`
            # pattern as `uc_open_with_reconnect` — which hangs in visible
            # mode because Chrome can't close the only visible window in 20s.
            # `driver.default_get` is the original Selenium .get saved by SB
            # before UC patching; it navigates the current tab synchronously.
            driver.default_get(auth_url)
            _debug_log("navigated via driver.default_get (visible mode)")

        if automate:
            _automate_login(driver, cfg, username, password, dump)
            redirect_timeout = _AUTOMATION_REDIRECT_TIMEOUT
        else:
            _user_message(
                f"Complete the Schwab login in the browser. Waiting up to "
                f"{_MANUAL_REDIRECT_TIMEOUT // 60} min for the OAuth redirect..."
            )
            redirect_timeout = _MANUAL_REDIRECT_TIMEOUT

        _debug_log(f"waiting for redirect to {cfg.redirect_uri} (<= {redirect_timeout}s)")
        return _capture_redirect(driver, cfg.redirect_uri, redirect_timeout)
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
