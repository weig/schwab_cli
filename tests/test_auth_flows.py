"""Tests for :mod:`schwab_cli.auth_flows`.

We unit-test:

* ``_build_handlers`` — config-driven handler selection.
* ``_open_and_print`` — URL always printed; ``webbrowser.open`` failures
  are swallowed.
* ``_race_handlers`` — the race semantics: first success wins, late
  successes ignored, all-failures aggregate.
* ``get_auth_response`` — wires the pieces; state token is propagated.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

from schwab_cli.auth_flows import (
    AuthFlowError,
    _build_handlers,
    _open_and_print,
    _race_handlers,
    get_auth_response,
)
from schwab_cli.auth_handlers import (
    AuthHandlerError,
    AuthResult,
    CodeRelayHandler,
    UserInputHandler,
)
from schwab_cli.config import Config


def _cfg_code_relay() -> Config:
    return Config(
        client_id="cid",
        client_secret="csec",
        redirect_uri="https://relay.example.com/uuid/callback",
        auth_flow="code_relay",
        code_relay_url="https://relay.example.com/uuid/wait",
    )


def _cfg_user_input_only() -> Config:
    """A non-code_relay config — only UserInputHandler will run."""
    return Config(
        client_id="cid",
        client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
        auth_flow="other",  # any value that isn't "code_relay"
    )


# ----- _build_handlers ----------------------------------------------------


def test_build_handlers_user_input_only_when_not_code_relay():
    handlers = _build_handlers(_cfg_user_input_only())
    assert len(handlers) == 1
    assert isinstance(handlers[0], UserInputHandler)


def test_build_handlers_adds_relay_when_code_relay_configured():
    handlers = _build_handlers(_cfg_code_relay())
    assert len(handlers) == 2
    assert any(isinstance(h, UserInputHandler) for h in handlers)
    assert any(isinstance(h, CodeRelayHandler) for h in handlers)


def test_build_handlers_raises_when_code_relay_url_missing():
    cfg = Config(
        client_id="cid",
        client_secret="csec",
        redirect_uri="https://relay.example.com",
        auth_flow="code_relay",
        code_relay_url=None,
    )
    with pytest.raises(AuthFlowError, match="code_relay_url"):
        _build_handlers(cfg)


# ----- _open_and_print -----------------------------------------------------


def test_open_and_print_emits_url_to_stderr(capsys, monkeypatch):
    monkeypatch.setattr("webbrowser.open", lambda *a, **kw: True)
    _open_and_print("https://schwab/authorize?x=1")
    err = capsys.readouterr().err
    assert "https://schwab/authorize?x=1" in err


def test_open_and_print_swallows_webbrowser_failure(capsys, monkeypatch):
    """If webbrowser.open raises, the URL is still printed and no
    exception escapes."""
    import webbrowser

    def boom(*a, **kw):
        raise webbrowser.Error("no display")

    monkeypatch.setattr("webbrowser.open", boom)
    # Should not raise.
    _open_and_print("https://x/y")
    assert "https://x/y" in capsys.readouterr().err


def test_open_and_print_handles_open_returning_false(capsys, monkeypatch):
    """``webbrowser.open`` can return False without raising; we tolerate that."""
    monkeypatch.setattr("webbrowser.open", lambda *a, **kw: False)
    _open_and_print("https://x/y")
    assert "https://x/y" in capsys.readouterr().err


# ----- _race_handlers -----------------------------------------------------


class _ImmediateOK:
    """Handler that succeeds immediately with a canned result."""

    def __init__(self, result: AuthResult):
        self._result = result
        self.last_state = None
        self.last_cancel = None

    def wait_for_response(self, *, expected_state, cancel=None) -> AuthResult:
        self.last_state = expected_state
        self.last_cancel = cancel
        return self._result


class _ImmediateFail:
    """Handler that always raises."""

    def __init__(self, msg="boom"):
        self._msg = msg

    def wait_for_response(self, *, expected_state, cancel=None) -> AuthResult:
        raise AuthHandlerError(self._msg)


class _SlowOK:
    """Handler that sleeps then succeeds — used to test "loser sees cancel"."""

    def __init__(self, result: AuthResult, sleep: float = 0.2):
        self._result = result
        self._sleep = sleep

    def wait_for_response(self, *, expected_state, cancel=None) -> AuthResult:
        time.sleep(self._sleep)
        return self._result


_OK_RESULT: AuthResult = {"kind": "code", "code": "WIN", "state": "S"}


def test_race_single_handler_success():
    h = _ImmediateOK(_OK_RESULT)
    assert _race_handlers([h], expected_state="S") == _OK_RESULT
    assert h.last_state == "S"
    assert isinstance(h.last_cancel, threading.Event)


def test_race_first_winner_returns_immediately():
    fast = _ImmediateOK(_OK_RESULT)
    slow = _SlowOK({"kind": "code", "code": "LATE", "state": "S"}, sleep=0.5)
    result = _race_handlers([fast, slow], expected_state="S")
    assert result["code"] == "WIN"


def test_race_cancel_set_after_winner():
    """The cancel event must be signalled so loser handlers can bail."""
    fast = _ImmediateOK(_OK_RESULT)
    captured = {}

    class _CancelObserver:
        def wait_for_response(self, *, expected_state, cancel=None):
            time.sleep(0.1)
            captured["cancelled_at_observe"] = cancel.is_set()
            # Loser doesn't return a result.
            raise AuthHandlerError("observer never wins")

    _race_handlers([fast, _CancelObserver()], expected_state="S")
    # Give the loser thread a moment to run after the winner returned.
    time.sleep(0.2)
    assert captured.get("cancelled_at_observe") is True


def test_race_one_fails_other_succeeds_returns_success():
    """An early failure must NOT abort the race — the second handler can still win."""
    fail = _ImmediateFail("relay 403")
    slow = _SlowOK(_OK_RESULT, sleep=0.05)
    result = _race_handlers([fail, slow], expected_state="S")
    assert result == _OK_RESULT


def test_race_all_handlers_fail_raises_aggregate():
    fail1 = _ImmediateFail("first error")
    fail2 = _ImmediateFail("second error")
    with pytest.raises(AuthFlowError, match="all auth handlers failed") as exc:
        _race_handlers([fail1, fail2], expected_state="S")
    msg = str(exc.value)
    assert "first error" in msg
    assert "second error" in msg


def test_race_state_propagated_to_every_handler():
    h1 = _ImmediateOK(_OK_RESULT)
    h2 = _ImmediateFail()
    _race_handlers([h1, h2], expected_state="THE_STATE")
    assert h1.last_state == "THE_STATE"


# ----- get_auth_response top-level wiring ---------------------------------


def test_get_auth_response_opens_browser_and_returns_handler_result(
    monkeypatch, capsys,
):
    """End-to-end (mocked): URL is printed, browser is opened, the
    user-input handler is replaced with one that returns a code."""

    canned: AuthResult = {"kind": "code", "code": "FROM_USER", "state": "x"}
    monkeypatch.setattr("webbrowser.open", lambda *a, **kw: True)

    class _UserInputOK:
        def wait_for_response(self, *, expected_state, cancel=None):
            return canned

    with patch(
        "schwab_cli.auth_flows.UserInputHandler",
        return_value=_UserInputOK(),
    ):
        result = get_auth_response(_cfg_user_input_only())
    assert result == canned
    err = capsys.readouterr().err
    assert "https://api.schwabapi.com/v1/oauth/authorize" in err
