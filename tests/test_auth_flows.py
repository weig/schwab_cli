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


def _cfg_code_relay(**overrides) -> Config:
    base = dict(
        client_id="cid",
        client_secret="csec",
        redirect_uri="https://relay.example.com/uuid/callback",
        auth_flow="code_relay",
        code_relay_url="https://relay.example.com/uuid/wait",
    )
    base.update(overrides)
    return Config(**base)


def _cfg_client(**overrides) -> Config:
    base = dict(
        client_id="cid",
        client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
        auth_flow="client",
    )
    base.update(overrides)
    return Config(**base)


_AUTH_URL = "https://schwab/auth?state=S"


def _names(handlers):
    return {type(h).__name__ for h in handlers}


# ----- _build_handlers — dispatch matrix --------------------------------


def test_build_handlers_code_relay_no_auto_login_no_manual():
    """Human-driven, code_relay: {UserInput, CodeRelay}."""
    handlers = _build_handlers(
        _cfg_code_relay(), manual=False, auth_url=_AUTH_URL,
    )
    assert _names(handlers) == {"UserInputHandler", "CodeRelayHandler"}


def test_build_handlers_code_relay_with_auto_login_no_manual():
    """Auto-driven, code_relay: just {CodeRelay}. UserInput excluded —
    auto-login is hands-off, no terminal interaction expected. The
    supervisor (spawned in ``get_auth_response``) drives webauto outside
    the race."""
    handlers = _build_handlers(
        _cfg_code_relay(auto_login_command=("webauto", "script.py")),
        manual=False, auth_url=_AUTH_URL,
    )
    assert _names(handlers) == {"CodeRelayHandler"}


def test_build_handlers_client_no_auto_login():
    """Human-driven, client: {UserInput} only (no relay configured)."""
    handlers = _build_handlers(
        _cfg_client(), manual=False, auth_url=_AUTH_URL,
    )
    assert _names(handlers) == {"UserInputHandler"}


def test_build_handlers_client_with_auto_login_no_manual():
    """Auto-driven, client: just {AutoLogin}. UserInput excluded."""
    handlers = _build_handlers(
        _cfg_client(auto_login_command=("webauto", "script.py")),
        manual=False, auth_url=_AUTH_URL,
    )
    assert _names(handlers) == {"AutoLoginHandler"}


def test_build_handlers_client_with_auto_login_and_manual():
    """``--manual`` overrides auto-login → human-driven path is restored."""
    handlers = _build_handlers(
        _cfg_client(auto_login_command=("webauto", "script.py")),
        manual=True, auth_url=_AUTH_URL,
    )
    assert _names(handlers) == {"UserInputHandler"}


def test_build_handlers_code_relay_with_auto_login_and_manual():
    """``--manual`` overrides auto-login → human-driven path with relay
    polling."""
    handlers = _build_handlers(
        _cfg_code_relay(auto_login_command=("webauto", "script.py")),
        manual=True, auth_url=_AUTH_URL,
    )
    assert _names(handlers) == {"UserInputHandler", "CodeRelayHandler"}


def test_build_handlers_raises_when_code_relay_url_missing():
    cfg = _cfg_code_relay(code_relay_url=None)
    with pytest.raises(AuthFlowError, match="code_relay_url"):
        _build_handlers(cfg, manual=False, auth_url=_AUTH_URL)


def test_build_handlers_auto_login_code_relay_missing_url_still_raises():
    """Even on the auto-login path, missing code_relay_url is a config error."""
    cfg = _cfg_code_relay(
        code_relay_url=None,
        auto_login_command=("webauto", "script.py"),
    )
    with pytest.raises(AuthFlowError, match="code_relay_url"):
        _build_handlers(cfg, manual=False, auth_url=_AUTH_URL)


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


def test_open_and_print_skips_browser_when_open_browser_false(
    capsys, monkeypatch,
):
    """When ``open_browser=False`` (auto-login active), do NOT call
    ``webbrowser.open`` and emit a short "Auto-login to schwab..." banner
    instead of the paste-prompt banner. UserInputHandler is excluded from
    the race in this mode so the URL doesn't need to be visible."""
    calls = []
    monkeypatch.setattr("webbrowser.open", lambda *a, **kw: calls.append(a) or True)
    _open_and_print("https://x/y", open_browser=False)
    assert calls == []
    err = capsys.readouterr().err
    assert "Auto-login to schwab" in err


def test_get_auth_response_does_not_open_browser_with_auto_login(monkeypatch):
    """When auto_login_command is set, webauto opens its own browser —
    schwab_cli must NOT call ``webbrowser.open``."""
    calls = []
    monkeypatch.setattr("webbrowser.open", lambda *a, **kw: calls.append(a) or True)
    canned: AuthResult = {"kind": "code", "code": "X", "state": "S"}

    class _RelayOK:
        def wait_for_response(self, *, expected_state, cancel=None):
            return canned

    class _NopSupervisor:
        def __init__(self, *args, **kwargs): pass
        def start(self): pass
        def terminate(self): pass

    cfg = _cfg_code_relay(auto_login_command=("webauto", "script.py"))
    with patch("schwab_cli.auth_flows.CodeRelayHandler", return_value=_RelayOK()), \
         patch("schwab_cli.auth_flows.AutoLoginSupervisor", _NopSupervisor):
        get_auth_response(cfg, manual=False)
    assert calls == [], "webbrowser.open must not be called when auto-login is active"


def test_get_auth_response_opens_browser_without_auto_login(monkeypatch):
    """Without auto_login_command, schwab_cli IS responsible for opening
    the default browser."""
    calls = []
    monkeypatch.setattr("webbrowser.open", lambda *a, **kw: calls.append(a) or True)
    canned: AuthResult = {"kind": "code", "code": "X", "state": "S"}

    class _UserOK:
        def wait_for_response(self, *, expected_state, cancel=None):
            return canned

    with patch("schwab_cli.auth_flows.UserInputHandler", return_value=_UserOK()):
        get_auth_response(_cfg_client(), manual=False)
    assert len(calls) == 1


def test_get_auth_response_opens_browser_when_manual_overrides_auto_login(
    monkeypatch,
):
    """--manual disables auto-login → schwab_cli must open the browser
    itself (since webauto won't)."""
    calls = []
    monkeypatch.setattr("webbrowser.open", lambda *a, **kw: calls.append(a) or True)
    canned: AuthResult = {"kind": "code", "code": "X", "state": "S"}

    class _RelayOK:
        def wait_for_response(self, *, expected_state, cancel=None):
            return canned

    cfg = _cfg_code_relay(auto_login_command=("webauto", "script.py"))
    with patch("schwab_cli.auth_flows.CodeRelayHandler", return_value=_RelayOK()):
        get_auth_response(cfg, manual=True)
    assert len(calls) == 1


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


def test_race_error_result_short_circuits():
    """ErrorResult is a valid race winner (Schwab said 'no'); the race
    short-circuits the same way it does on CodeResult."""
    error_result: AuthResult = {
        "kind": "error",
        "error": "access_denied",
        "error_description": "user rejected",
        "state": "S",
    }
    fast_error = _ImmediateOK(error_result)
    slow_code = _SlowOK({"kind": "code", "code": "LATE", "state": "S"}, sleep=0.3)
    result = _race_handlers([fast_error, slow_code], expected_state="S")
    assert result == error_result
    # The loser saw cancel.
    assert isinstance(fast_error.last_cancel, threading.Event)


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
        result = get_auth_response(_cfg_client())
    assert result == canned
    err = capsys.readouterr().err
    assert "https://api.schwabapi.com/v1/oauth/authorize" in err


# ----- AutoLoginSupervisor wiring in get_auth_response --------------------


def test_get_auth_response_starts_supervisor_for_code_relay_with_auto_login(
    monkeypatch,
):
    """code_relay + auto_login_command + !manual → supervisor.start() called,
    and supervisor.terminate() called in finally."""
    monkeypatch.setattr("webbrowser.open", lambda *a, **kw: True)
    canned: AuthResult = {"kind": "code", "code": "FROM_RELAY", "state": "S"}

    class _RelayOK:
        def wait_for_response(self, *, expected_state, cancel=None):
            return canned

    class _FakeSupervisor:
        instances: list = []

        def __init__(self, *args, **kwargs):
            self.started = False
            self.terminated = False
            _FakeSupervisor.instances.append(self)

        def start(self):
            self.started = True

        def terminate(self):
            self.terminated = True

    cfg = _cfg_code_relay(auto_login_command=("webauto", "script.py"))
    with patch("schwab_cli.auth_flows.CodeRelayHandler", return_value=_RelayOK()), \
         patch("schwab_cli.auth_flows.AutoLoginSupervisor", _FakeSupervisor):
        result = get_auth_response(cfg, manual=False)

    assert result == canned
    assert len(_FakeSupervisor.instances) == 1
    sup = _FakeSupervisor.instances[0]
    assert sup.started is True
    assert sup.terminated is True


def test_get_auth_response_skips_supervisor_for_client_flow(monkeypatch):
    """auth_flow=client + auto_login is the AutoLoginHandler path, not the
    supervisor path. No AutoLoginSupervisor instances should be created."""
    monkeypatch.setattr("webbrowser.open", lambda *a, **kw: True)
    canned: AuthResult = {"kind": "code", "code": "X", "state": "S"}

    class _AutoOK:
        def wait_for_response(self, *, expected_state, cancel=None):
            return canned

    class _NeverConstructed:
        def __init__(self, *args, **kwargs):
            raise AssertionError(
                "AutoLoginSupervisor must NOT be constructed for client flow"
            )

    cfg = _cfg_client(auto_login_command=("webauto", "script.py"))
    with patch("schwab_cli.auth_flows.AutoLoginHandler", return_value=_AutoOK()), \
         patch("schwab_cli.auth_flows.AutoLoginSupervisor", _NeverConstructed):
        result = get_auth_response(cfg, manual=False)
    assert result == canned


def test_get_auth_response_skips_supervisor_on_manual(monkeypatch):
    """--manual must skip supervisor spawning even for code_relay."""
    monkeypatch.setattr("webbrowser.open", lambda *a, **kw: True)
    canned: AuthResult = {"kind": "code", "code": "X", "state": "S"}

    class _RelayOK:
        def wait_for_response(self, *, expected_state, cancel=None):
            return canned

    class _NeverConstructed:
        def __init__(self, *args, **kwargs):
            raise AssertionError(
                "AutoLoginSupervisor must NOT be constructed under --manual"
            )

    cfg = _cfg_code_relay(auto_login_command=("webauto", "script.py"))
    with patch("schwab_cli.auth_flows.CodeRelayHandler", return_value=_RelayOK()), \
         patch("schwab_cli.auth_flows.AutoLoginSupervisor", _NeverConstructed):
        result = get_auth_response(cfg, manual=True)
    assert result == canned


def test_get_auth_response_terminates_supervisor_even_on_race_failure(monkeypatch):
    """If all handlers fail, supervisor.terminate() still runs (finally)."""
    monkeypatch.setattr("webbrowser.open", lambda *a, **kw: True)

    class _FailingRelay:
        def wait_for_response(self, *, expected_state, cancel=None):
            raise AuthHandlerError("relay 500")

    class _FailingUser:
        def wait_for_response(self, *, expected_state, cancel=None):
            raise AuthHandlerError("empty input")

    terminated = {"flag": False}

    class _FakeSupervisor:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def terminate(self):
            terminated["flag"] = True

    cfg = _cfg_code_relay(auto_login_command=("webauto", "script.py"))
    with patch("schwab_cli.auth_flows.UserInputHandler",
               return_value=_FailingUser()), \
         patch("schwab_cli.auth_flows.CodeRelayHandler",
               return_value=_FailingRelay()), \
         patch("schwab_cli.auth_flows.AutoLoginSupervisor", _FakeSupervisor):
        with pytest.raises(AuthFlowError):
            get_auth_response(cfg, manual=False)
    assert terminated["flag"] is True
