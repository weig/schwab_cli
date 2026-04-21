"""Tests for browser/flow.py.

`FakePage` is a minimal Playwright Page stand-in that scripts page.content()
and selector responses so we can drive the flow logic without a real browser.
"""

from __future__ import annotations

import pytest

from schwab_cli.browser.flow import AuthError, wait_any


class FakeLocator:
    def __init__(self, present: bool):
        self._present = present

    def wait_for(self, *, timeout: int) -> None:
        if not self._present:
            raise TimeoutError("not present")


class FakePage:
    """Scriptable Page double for flow tests.

    Configure:
        - content_sequence: list of strings; .content() returns each in order
          (last is repeated if calls exceed the list).
        - selector_present: dict[str, bool] — which selectors return present
          when wait_for_selector is called.
    """

    def __init__(self, *, content_sequence: list[str] | None = None,
                 selectors_present: dict[str, bool] | None = None):
        self._content = content_sequence or [""]
        self._content_idx = 0
        self._selectors = selectors_present or {}
        self.url = ""

    def content(self) -> str:
        c = self._content[min(self._content_idx, len(self._content) - 1)]
        self._content_idx += 1
        return c

    def wait_for_selector(self, selector: str, *, timeout: int) -> FakeLocator:
        if self._selectors.get(selector, False):
            return FakeLocator(True)
        raise TimeoutError(f"selector {selector!r} not found")


def test_wait_any_returns_when_expected_selector_appears():
    page = FakePage(selectors_present={"#ready": True})
    # Should not raise; returns None on success.
    wait_any(page, expected="#ready", known_errors={}, timeout_ms=200)


def test_wait_any_raises_friendly_error_on_known_marker():
    page = FakePage(content_sequence=["...Invalid login ID or password...."],
                    selectors_present={"#ready": False})
    with pytest.raises(AuthError, match="incorrect username/password"):
        wait_any(
            page,
            expected="#ready",
            known_errors={"Invalid login ID or password.": "Login failed — incorrect username/password."},
            timeout_ms=200,
        )


def test_wait_any_times_out_with_ui_change_message():
    page = FakePage(content_sequence=[""], selectors_present={"#ready": False})
    with pytest.raises(AuthError, match="Schwab may have changed"):
        wait_any(page, expected="#ready", known_errors={}, timeout_ms=200)


def test_wait_any_expected_selector_takes_precedence_over_stale_marker():
    # Schwab pages keep error elements in the DOM and toggle visibility; once
    # the page has advanced, the expected selector is the source of truth and
    # stale error text must not trigger a false positive.
    page = FakePage(
        content_sequence=["Invalid login ID or password."],
        selectors_present={"#ready": True},  # expected element is present
    )
    # No exception: expected selector found, error marker ignored.
    wait_any(
        page,
        expected="#ready",
        known_errors={"Invalid login ID or password.": "Login failed — incorrect username/password."},
        timeout_ms=200,
    )


from urllib.parse import urlparse, parse_qs

from schwab_cli.config import Config
from schwab_cli.browser.flow import run_full_auth


def _cfg():
    return Config(
        client_id="cid",
        client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
        username="user@example.com",
        password="op://X/Y/Z",
    )


class FakeCheckbox:
    def __init__(self, checked: bool = False):
        self._checked = checked

    def is_checked(self) -> bool:
        return self._checked

    def check(self) -> None:
        self._checked = True


class FullFakePage(FakePage):
    """FakePage extended for run_full_auth: scripts goto, fill, click, navigation, checkboxes."""

    def __init__(self, **kwargs):
        final_redirect_url = kwargs.pop("final_redirect_url", None)
        checkboxes = kwargs.pop("checkboxes", [FakeCheckbox()])
        super().__init__(**kwargs)
        self.fills: list[tuple[str, str]] = []
        self.clicks: list[str] = []
        self.gotos: list[str] = []
        self._final_redirect_url = final_redirect_url
        self._checkboxes = checkboxes
        self._closed = False

    def goto(self, url: str) -> None:
        self.gotos.append(url)

    def fill(self, selector: str, value: str) -> None:
        self.fills.append((selector, value))

    def click(self, selector: str) -> None:
        self.clicks.append(selector)

    def query_selector_all(self, selector: str):
        return list(self._checkboxes)

    def evaluate(self, script: str) -> None:
        # Used to scroll to bottom on consent page; no-op for tests.
        pass

    def wait_for_url(self, predicate, *, timeout: int) -> None:
        if self._final_redirect_url and predicate(self._final_redirect_url):
            self.url = self._final_redirect_url
            return
        raise TimeoutError("redirect did not happen")

    def add_init_script(self, script: str) -> None:
        # Capture for assertion in stealth tests.
        self.init_scripts = getattr(self, "init_scripts", [])
        self.init_scripts.append(script)


class FakeBrowser:
    def __init__(self, page):
        self._page = page
        self.closed = False
        self.new_page_kwargs: list[dict] = []

    def new_page(self, **kwargs):
        self.new_page_kwargs.append(kwargs)
        return self._page

    def close(self):
        self.closed = True


def _happy_browser(page):
    return FakeBrowser(page)


def test_run_full_auth_happy_path(monkeypatch):
    page = FullFakePage(
        selectors_present={
            "input#loginIdInput": True,
            "text=Terms of Use": True,
            "text=Select accounts": True,
            "text=You will now be redirected": True,
        },
        final_redirect_url="https://127.0.0.1:8443/?code=AUTH_CODE_123&session=abc",
        checkboxes=[FakeCheckbox(False), FakeCheckbox(True)],
    )
    browser = _happy_browser(page)

    monkeypatch.setattr(
        "schwab_cli.browser.flow._launch_browser",
        lambda headless, **_: browser,
    )
    monkeypatch.setattr(
        "schwab_cli.browser.flow.resolve_secret",
        lambda v: f"resolved({v})",
    )

    code = run_full_auth(_cfg())

    assert code == "AUTH_CODE_123"
    assert browser.closed is True
    assert page.gotos[0].startswith("https://api.schwabapi.com/v1/oauth/authorize?")
    # Both username and password fields filled, with resolved (op://) values.
    assert ("input#loginIdInput", "resolved(user@example.com)") in page.fills
    assert ("input#passwordInput", "resolved(op://X/Y/Z)") in page.fills
    # Both checkboxes ended up checked (one was already, one we toggled).
    assert all(cb.is_checked() for cb in page._checkboxes)


def test_run_full_auth_invalid_client_marker(monkeypatch):
    page = FullFakePage(
        content_sequence=['{"error": "invalid_client"}'],
        selectors_present={"input#loginIdInput": False},
    )
    browser = _happy_browser(page)
    monkeypatch.setattr("schwab_cli.browser.flow._launch_browser", lambda headless, **_: browser)
    monkeypatch.setattr("schwab_cli.browser.flow.resolve_secret", lambda v: v)

    with pytest.raises(AuthError, match="rejected client_id/secret"):
        run_full_auth(_cfg())
    assert browser.closed is True


def test_run_full_auth_bad_credentials(monkeypatch):
    page = FullFakePage(
        # First content() check (after goto) returns empty, allowing login page.
        # Second content() check (after click login) finds the credentials error.
        content_sequence=["", "Invalid login ID or password."],
        selectors_present={
            "input#loginIdInput": True,
            "text=Terms of Use": False,
        },
    )
    browser = _happy_browser(page)
    monkeypatch.setattr("schwab_cli.browser.flow._launch_browser", lambda headless, **_: browser)
    monkeypatch.setattr("schwab_cli.browser.flow.resolve_secret", lambda v: v)

    with pytest.raises(AuthError, match="incorrect username/password"):
        run_full_auth(_cfg())
    assert browser.closed is True


def test_run_full_auth_redirect_uri_mismatch(monkeypatch):
    page = FullFakePage(
        content_sequence=["", "We are unable to complete your request."],
        selectors_present={
            "input#loginIdInput": True,
            "text=Terms of Use": False,
        },
    )
    browser = _happy_browser(page)
    monkeypatch.setattr("schwab_cli.browser.flow._launch_browser", lambda headless, **_: browser)
    monkeypatch.setattr("schwab_cli.browser.flow.resolve_secret", lambda v: v)

    with pytest.raises(AuthError, match="Redirect URI mismatch"):
        run_full_auth(_cfg())


def test_run_full_auth_no_accounts(monkeypatch):
    page = FullFakePage(
        selectors_present={
            "input#loginIdInput": True,
            "text=Terms of Use": True,
            "text=Select accounts": True,
        },
        checkboxes=[],  # no accounts shown
    )
    browser = _happy_browser(page)
    monkeypatch.setattr("schwab_cli.browser.flow._launch_browser", lambda headless, **_: browser)
    monkeypatch.setattr("schwab_cli.browser.flow.resolve_secret", lambda v: v)

    with pytest.raises(AuthError, match="No accounts available"):
        run_full_auth(_cfg())
    assert browser.closed is True


def test_run_full_auth_redirect_without_code(monkeypatch):
    page = FullFakePage(
        selectors_present={
            "input#loginIdInput": True,
            "text=Terms of Use": True,
            "text=Select accounts": True,
            "text=You will now be redirected": True,
        },
        final_redirect_url="https://127.0.0.1:8443/?session=abc",  # no code
        checkboxes=[FakeCheckbox()],
    )
    browser = _happy_browser(page)
    monkeypatch.setattr("schwab_cli.browser.flow._launch_browser", lambda headless, **_: browser)
    monkeypatch.setattr("schwab_cli.browser.flow.resolve_secret", lambda v: v)

    with pytest.raises(AuthError, match="no `code` param"):
        run_full_auth(_cfg())


def test_run_full_auth_chromium_missing_message(monkeypatch):
    def boom(headless, **_):
        raise RuntimeError("Executable doesn't exist at .../chromium")

    monkeypatch.setattr("schwab_cli.browser.flow._launch_browser", boom)
    monkeypatch.setattr("schwab_cli.browser.flow.resolve_secret", lambda v: v)

    with pytest.raises(AuthError, match="playwright install chromium"):
        run_full_auth(_cfg())


def test_run_full_auth_redirect_timeout(monkeypatch):
    """wait_for_url raises TimeoutError → AuthError('Redirect didn't happen')."""
    page = FullFakePage(
        selectors_present={
            "input#loginIdInput": True,
            "text=Terms of Use": True,
            "text=Select accounts": True,
            "text=You will now be redirected": True,
        },
        final_redirect_url=None,  # wait_for_url will raise TimeoutError
        checkboxes=[FakeCheckbox()],
    )
    browser = _happy_browser(page)
    monkeypatch.setattr("schwab_cli.browser.flow._launch_browser", lambda headless, **_: browser)
    monkeypatch.setattr("schwab_cli.browser.flow.resolve_secret", lambda v: v)

    with pytest.raises(AuthError, match="Redirect didn't happen"):
        run_full_auth(_cfg())
    assert browser.closed is True


def test_run_full_auth_generic_launch_failure(monkeypatch):
    """_launch_browser raises with a non-Chromium-specific message → generic AuthError."""

    def boom(headless, **_):
        raise RuntimeError("some unexpected failure")

    monkeypatch.setattr("schwab_cli.browser.flow._launch_browser", boom)
    monkeypatch.setattr("schwab_cli.browser.flow.resolve_secret", lambda v: v)

    with pytest.raises(AuthError, match="Failed to launch browser"):
        run_full_auth(_cfg())


def _happy_page_and_browser():
    page = FullFakePage(
        selectors_present={
            "input#loginIdInput": True,
            "text=Terms of Use": True,
            "text=Select accounts": True,
            "text=You will now be redirected": True,
        },
        final_redirect_url="https://127.0.0.1:8443/?code=CODE",
        checkboxes=[FakeCheckbox()],
    )
    return page, _happy_browser(page)


def test_run_full_auth_passes_slow_mo_when_debug_set(monkeypatch):
    monkeypatch.setenv("DEBUG", "1")
    monkeypatch.setattr("schwab_cli.browser.flow.time.sleep", lambda _s: None)
    _, browser = _happy_page_and_browser()
    captured = {}

    def capture_launch(headless, **kw):
        captured["headless"] = headless
        captured["slow_mo_ms"] = kw.get("slow_mo_ms", 0)
        return browser

    monkeypatch.setattr("schwab_cli.browser.flow._launch_browser", capture_launch)
    monkeypatch.setattr("schwab_cli.browser.flow.resolve_secret", lambda v: v)

    run_full_auth(_cfg())
    assert captured["headless"] is False
    assert captured["slow_mo_ms"] == 1000


def test_run_full_auth_no_slow_mo_when_debug_unset(monkeypatch):
    monkeypatch.delenv("DEBUG", raising=False)
    _, browser = _happy_page_and_browser()
    captured = {}

    def capture_launch(headless, **kw):
        captured["headless"] = headless
        captured["slow_mo_ms"] = kw.get("slow_mo_ms", 0)
        return browser

    monkeypatch.setattr("schwab_cli.browser.flow._launch_browser", capture_launch)
    monkeypatch.setattr("schwab_cli.browser.flow.resolve_secret", lambda v: v)

    run_full_auth(_cfg())
    assert captured["headless"] is True
    assert captured["slow_mo_ms"] == 0


def test_run_full_auth_emits_debug_logs_when_debug_set(monkeypatch, capsys):
    monkeypatch.setenv("DEBUG", "1")
    monkeypatch.setattr("schwab_cli.browser.flow.time.sleep", lambda _s: None)
    _, browser = _happy_page_and_browser()
    monkeypatch.setattr("schwab_cli.browser.flow._launch_browser", lambda headless, **_: browser)
    monkeypatch.setattr("schwab_cli.browser.flow.resolve_secret", lambda v: v)

    run_full_auth(_cfg())
    err = capsys.readouterr().err
    # Spot-check a few expected phases — exact wording is already locked by unit tests
    # of _debug_log; here we just confirm phases fire in order.
    assert "[debug] resolving secrets" in err
    assert "[debug] launching chromium" in err
    assert "[debug] navigating to auth URL" in err
    assert "[debug] authorization code captured" in err
    assert "[debug] holding browser open" in err
    # Credentials must never appear in the log.
    assert "user@example.com" not in err
    assert "op://X/Y/Z" not in err
    assert "CODE" not in err  # neither the auth code value


def test_run_full_auth_quiet_when_debug_unset(monkeypatch, capsys):
    monkeypatch.delenv("DEBUG", raising=False)
    _, browser = _happy_page_and_browser()
    monkeypatch.setattr("schwab_cli.browser.flow._launch_browser", lambda headless, **_: browser)
    monkeypatch.setattr("schwab_cli.browser.flow.resolve_secret", lambda v: v)

    run_full_auth(_cfg())
    err = capsys.readouterr().err
    assert "[debug]" not in err


def test_run_full_auth_holds_open_before_close_when_debug_set(monkeypatch):
    monkeypatch.setenv("DEBUG", "1")
    sleeps: list[float] = []
    monkeypatch.setattr("schwab_cli.browser.flow.time.sleep", lambda s: sleeps.append(s))
    _, browser = _happy_page_and_browser()
    monkeypatch.setattr("schwab_cli.browser.flow._launch_browser", lambda headless, **_: browser)
    monkeypatch.setattr("schwab_cli.browser.flow.resolve_secret", lambda v: v)

    run_full_auth(_cfg())
    assert sleeps == [60]          # _DEBUG_HOLD_OPEN_SECONDS
    assert browser.closed is True  # still closed after the hold


def test_run_full_auth_does_not_hold_open_when_debug_unset(monkeypatch):
    monkeypatch.delenv("DEBUG", raising=False)
    sleeps: list[float] = []
    monkeypatch.setattr("schwab_cli.browser.flow.time.sleep", lambda s: sleeps.append(s))
    _, browser = _happy_page_and_browser()
    monkeypatch.setattr("schwab_cli.browser.flow._launch_browser", lambda headless, **_: browser)
    monkeypatch.setattr("schwab_cli.browser.flow.resolve_secret", lambda v: v)

    run_full_auth(_cfg())
    assert sleeps == []
    assert browser.closed is True


def test_run_full_auth_holds_open_on_failure_then_closes(monkeypatch):
    monkeypatch.setenv("DEBUG", "1")
    sleeps: list[float] = []
    monkeypatch.setattr("schwab_cli.browser.flow.time.sleep", lambda s: sleeps.append(s))

    # Fail at the consent step so we exercise the failure → hold → close path.
    page = FullFakePage(
        content_sequence=["", "Invalid login ID or password."],
        selectors_present={
            "input#loginIdInput": True,
            "text=Terms of Use": False,
        },
    )
    browser = _happy_browser(page)
    monkeypatch.setattr("schwab_cli.browser.flow._launch_browser", lambda headless, **_: browser)
    monkeypatch.setattr("schwab_cli.browser.flow.resolve_secret", lambda v: v)

    with pytest.raises(AuthError):
        run_full_auth(_cfg())
    assert sleeps == [60]
    assert browser.closed is True


def test_run_full_auth_applies_stealth_measures(monkeypatch):
    """Browser is launched with realistic UA, and navigator.webdriver init script
    is injected before navigation — hides the three most obvious Playwright tells."""
    monkeypatch.delenv("DEBUG", raising=False)
    page, browser = _happy_page_and_browser()
    monkeypatch.setattr("schwab_cli.browser.flow._launch_browser", lambda headless, **_: browser)
    monkeypatch.setattr("schwab_cli.browser.flow.resolve_secret", lambda v: v)

    run_full_auth(_cfg())

    # new_page called with a non-headless UA string (no "HeadlessChrome")
    assert len(browser.new_page_kwargs) == 1
    ua = browser.new_page_kwargs[0].get("user_agent", "")
    assert "Chrome/" in ua
    assert "HeadlessChrome" not in ua
    # Init script injected that removes navigator.webdriver
    assert page.init_scripts and any("webdriver" in s for s in page.init_scripts)


def test_run_full_auth_hold_open_interrupted_by_ctrl_c(monkeypatch):
    monkeypatch.setenv("DEBUG", "1")

    def interrupt(_s):
        raise KeyboardInterrupt

    monkeypatch.setattr("schwab_cli.browser.flow.time.sleep", interrupt)
    _, browser = _happy_page_and_browser()
    monkeypatch.setattr("schwab_cli.browser.flow._launch_browser", lambda headless, **_: browser)
    monkeypatch.setattr("schwab_cli.browser.flow.resolve_secret", lambda v: v)

    # Ctrl+C during hold must NOT propagate — browser still closes cleanly.
    run_full_auth(_cfg())
    assert browser.closed is True
