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


def test_wait_any_known_marker_takes_precedence_over_selector():
    page = FakePage(
        content_sequence=["Invalid login ID or password."],
        selectors_present={"#ready": True},  # selector is also present
    )
    # Marker should be reported because it indicates a definite failure.
    with pytest.raises(AuthError, match="incorrect"):
        wait_any(
            page,
            expected="#ready",
            known_errors={"Invalid login ID or password.": "Login failed — incorrect username/password."},
            timeout_ms=200,
        )
