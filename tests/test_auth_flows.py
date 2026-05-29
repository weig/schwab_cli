"""Tests for :mod:`schwab_cli.auth_flows`.

Post-refactor contract:
  - get_auth_response raises AuthFlowError for legacy auth_flow values
    (code_relay, client) with an actionable message mentioning 'schwab setup'.
  - _build_handlers for local_server:
      * human path (no auto_login_command, or --manual):
          handlers == {UserInputHandler, LocalServerHandler}
      * auto path (auto_login_command set AND not --manual):
          handlers == {LocalServerHandler} only (UserInputHandler excluded)
  - LocalServerHandler is constructed with (redirect_uri, certfile, keyfile)
    from _resolve_cert_paths seam; never binds a real port in unit tests.
  - _resolve_cert_paths seam: for loopback-https URI → returns (certfile, keyfile);
    when cert absent → raises AuthFlowError("run `schwab cert install` first").
  - _maybe_start_supervisor: starts AutoLoginSupervisor for local_server when
    auto_login_command set AND not --manual; returns None otherwise.
  - CodeRelayHandler is NOT imported / used.
  - _race_handlers tests are flow-agnostic and are kept.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

from schwab_cli.auth_flows import (
    AuthFlowError,
    _build_handlers,
    _maybe_start_supervisor,
    _open_and_print,
    _race_handlers,
    get_auth_response,
)
from schwab_cli.auth_handlers import (
    AuthHandlerError,
    AuthResult,
    UserInputHandler,
)
from schwab_cli.config import Config


# ---- Config helpers --------------------------------------------------------


def _cfg_local_server(**overrides) -> Config:
    base = dict(
        client_id="cid",
        client_secret="csec",
        redirect_uri="https://127.0.0.1:19806/schwab/callback",
        auth_flow="local_server",
    )
    base.update(overrides)
    return Config(**base)


def _cfg_legacy_code_relay(**overrides) -> Config:
    """A legacy config that load() tolerates but auth rejects."""
    base = dict(
        client_id="cid",
        client_secret="csec",
        redirect_uri="https://relay.example.com/uuid/callback",
        auth_flow="code_relay",
    )
    base.update(overrides)
    return Config(**base)


def _cfg_legacy_client(**overrides) -> Config:
    """A legacy 'client' config that load() tolerates but auth rejects."""
    base = dict(
        client_id="cid",
        client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
        auth_flow="client",
    )
    base.update(overrides)
    return Config(**base)


_AUTH_URL = "https://schwab/auth?state=S"

# Fake cert paths returned by the monkeypatched _resolve_cert_paths seam.
_FAKE_CERTFILE = "/fake/certs/127.0.0.1.pem"
_FAKE_KEYFILE = "/fake/certs/127.0.0.1-key.pem"


def _names(handlers):
    return {type(h).__name__ for h in handlers}


# ---- Legacy flow rejection in get_auth_response ----------------------------


def test_get_auth_response_raises_for_legacy_code_relay_flow(monkeypatch):
    """H4 deferred hard failure: a legacy code_relay config passes load() but
    must raise AuthFlowError when auth is actually attempted."""
    monkeypatch.setattr("webbrowser.open", lambda *a, **kw: True)
    cfg = _cfg_legacy_code_relay()
    with pytest.raises(AuthFlowError) as exc:
        get_auth_response(cfg)
    msg = str(exc.value).lower()
    # Message must be actionable — mention re-running setup.
    assert "setup" in msg


def test_get_auth_response_raises_for_legacy_client_flow(monkeypatch):
    """Legacy 'client' flow also triggers the deferred hard failure."""
    monkeypatch.setattr("webbrowser.open", lambda *a, **kw: True)
    cfg = _cfg_legacy_client()
    with pytest.raises(AuthFlowError) as exc:
        get_auth_response(cfg)
    msg = str(exc.value).lower()
    assert "setup" in msg


# ---- _build_handlers — local_server dispatch matrix -----------------------


def _patch_resolve_cert(monkeypatch, *, certfile=_FAKE_CERTFILE, keyfile=_FAKE_KEYFILE):
    """Monkeypatch _resolve_cert_paths so no real keychain is touched."""
    monkeypatch.setattr(
        "schwab_cli.auth_flows._resolve_cert_paths",
        lambda uri: (certfile, keyfile),
    )


class _FakeLocalServerHandler:
    """Records constructor args; never binds a real port."""
    instances: list = []

    def __init__(self, redirect_uri, *, certfile=None, keyfile=None, **kwargs):
        self.redirect_uri = redirect_uri
        self.certfile = certfile
        self.keyfile = keyfile
        _FakeLocalServerHandler.instances.append(self)

    def wait_for_response(self, *, expected_state, cancel=None) -> AuthResult:
        return {"kind": "code", "code": "FROM_LOCAL_SERVER", "state": expected_state}


def _reset_fake_handler():
    _FakeLocalServerHandler.instances.clear()


def test_build_handlers_local_server_human_path(monkeypatch):
    """Human-driven, local_server, no auto_login_command:
    handlers == {UserInputHandler, LocalServerHandler}."""
    _reset_fake_handler()
    _patch_resolve_cert(monkeypatch)
    monkeypatch.setattr(
        "schwab_cli.auth_flows.LocalServerHandler", _FakeLocalServerHandler,
    )
    handlers = _build_handlers(
        _cfg_local_server(), manual=False, auth_url=_AUTH_URL,
    )
    assert _names(handlers) == {"UserInputHandler", "_FakeLocalServerHandler"}


def test_build_handlers_local_server_manual_overrides_auto_login(monkeypatch):
    """--manual forces the human path even when auto_login_command is set."""
    _reset_fake_handler()
    _patch_resolve_cert(monkeypatch)
    monkeypatch.setattr(
        "schwab_cli.auth_flows.LocalServerHandler", _FakeLocalServerHandler,
    )
    handlers = _build_handlers(
        _cfg_local_server(auto_login_command=("webauto", "script.py")),
        manual=True, auth_url=_AUTH_URL,
    )
    assert _names(handlers) == {"UserInputHandler", "_FakeLocalServerHandler"}


def test_build_handlers_local_server_auto_path_excludes_user_input(monkeypatch):
    """Auto-driven, local_server: only {LocalServerHandler}; UserInput excluded."""
    _reset_fake_handler()
    _patch_resolve_cert(monkeypatch)
    monkeypatch.setattr(
        "schwab_cli.auth_flows.LocalServerHandler", _FakeLocalServerHandler,
    )
    handlers = _build_handlers(
        _cfg_local_server(auto_login_command=("webauto", "script.py")),
        manual=False, auth_url=_AUTH_URL,
    )
    assert _names(handlers) == {"_FakeLocalServerHandler"}
    assert not any(isinstance(h, UserInputHandler) for h in handlers)


def test_build_handlers_local_server_constructs_handler_with_cert_paths(monkeypatch):
    """LocalServerHandler must receive the certfile/keyfile from _resolve_cert_paths."""
    _reset_fake_handler()
    _patch_resolve_cert(monkeypatch, certfile="/cert/leaf.pem", keyfile="/cert/leaf-key.pem")
    monkeypatch.setattr(
        "schwab_cli.auth_flows.LocalServerHandler", _FakeLocalServerHandler,
    )
    _build_handlers(_cfg_local_server(), manual=False, auth_url=_AUTH_URL)
    # At least one LocalServerHandler was constructed.
    assert len(_FakeLocalServerHandler.instances) >= 1
    inst = _FakeLocalServerHandler.instances[-1]
    assert inst.certfile == "/cert/leaf.pem"
    assert inst.keyfile == "/cert/leaf-key.pem"


def test_build_handlers_local_server_constructs_handler_with_redirect_uri(monkeypatch):
    """LocalServerHandler must receive cfg.redirect_uri."""
    _reset_fake_handler()
    _patch_resolve_cert(monkeypatch)
    monkeypatch.setattr(
        "schwab_cli.auth_flows.LocalServerHandler", _FakeLocalServerHandler,
    )
    cfg = _cfg_local_server(redirect_uri="https://127.0.0.1:15000/schwab/callback")
    _build_handlers(cfg, manual=False, auth_url=_AUTH_URL)
    assert len(_FakeLocalServerHandler.instances) >= 1
    inst = _FakeLocalServerHandler.instances[-1]
    assert inst.redirect_uri == "https://127.0.0.1:15000/schwab/callback"


def test_build_handlers_raises_when_cert_absent(monkeypatch):
    """When _resolve_cert_paths raises AuthFlowError (cert not installed),
    _build_handlers propagates it."""
    def _no_cert(uri):
        raise AuthFlowError("run `schwab cert install` first")

    monkeypatch.setattr("schwab_cli.auth_flows._resolve_cert_paths", _no_cert)
    monkeypatch.setattr(
        "schwab_cli.auth_flows.LocalServerHandler", _FakeLocalServerHandler,
    )
    with pytest.raises(AuthFlowError, match="cert install"):
        _build_handlers(_cfg_local_server(), manual=False, auth_url=_AUTH_URL)


# ---- _resolve_cert_paths seam contract ------------------------------------


def test_resolve_cert_paths_is_importable_from_auth_flows():
    """The seam must exist at module level so tests can monkeypatch it."""
    import schwab_cli.auth_flows as af
    assert hasattr(af, "_resolve_cert_paths"), (
        "_resolve_cert_paths seam must exist in auth_flows"
    )


def test_resolve_cert_paths_callable():
    """_resolve_cert_paths must be callable with a URI argument."""
    import schwab_cli.auth_flows as af
    assert callable(af._resolve_cert_paths)


# ---- _maybe_start_supervisor — local_server wiring -------------------------


def test_maybe_start_supervisor_starts_for_local_server_with_auto_login(monkeypatch):
    """local_server + auto_login_command + !manual → supervisor.start() called."""
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

    _FakeSupervisor.instances.clear()
    monkeypatch.setattr("schwab_cli.auth_flows.AutoLoginSupervisor", _FakeSupervisor)

    cfg = _cfg_local_server(auto_login_command=("webauto", "script.py"))
    sup = _maybe_start_supervisor(
        cfg, manual=False, auth_url=_AUTH_URL, state="S",
    )
    assert sup is not None
    assert len(_FakeSupervisor.instances) == 1
    assert _FakeSupervisor.instances[0].started is True


def test_maybe_start_supervisor_returns_none_without_auto_login():
    """No auto_login_command → no supervisor."""
    cfg = _cfg_local_server()  # no auto_login_command
    sup = _maybe_start_supervisor(
        cfg, manual=False, auth_url=_AUTH_URL, state="S",
    )
    assert sup is None


def test_maybe_start_supervisor_returns_none_on_manual(monkeypatch):
    """--manual → supervisor skipped even if auto_login_command is set."""
    class _NeverConstructed:
        def __init__(self, *args, **kwargs):
            raise AssertionError("AutoLoginSupervisor must NOT be constructed under --manual")

    monkeypatch.setattr("schwab_cli.auth_flows.AutoLoginSupervisor", _NeverConstructed)

    cfg = _cfg_local_server(auto_login_command=("webauto", "script.py"))
    sup = _maybe_start_supervisor(
        cfg, manual=True, auth_url=_AUTH_URL, state="S",
    )
    assert sup is None


def test_maybe_start_supervisor_returns_none_for_legacy_flow():
    """Legacy flow values → _maybe_start_supervisor returns None (auth path
    will raise before reaching here, but the function must be safe)."""
    cfg = _cfg_legacy_code_relay(auto_login_command=("webauto", "script.py"))
    # Should return None because local_server is the only recognized flow for supervisor.
    sup = _maybe_start_supervisor(
        cfg, manual=False, auth_url=_AUTH_URL, state="S",
    )
    assert sup is None


# ---- get_auth_response top-level wiring ------------------------------------


def test_get_auth_response_starts_supervisor_for_local_server_with_auto_login(
    monkeypatch,
):
    """local_server + auto_login_command + !manual → supervisor started and terminated."""
    monkeypatch.setattr("webbrowser.open", lambda *a, **kw: True)
    _reset_fake_handler()

    canned: AuthResult = {"kind": "code", "code": "FROM_LOCAL", "state": "S"}

    class _LocalOK:
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

    _FakeSupervisor.instances.clear()

    def _fake_resolve(uri):
        return (_FAKE_CERTFILE, _FAKE_KEYFILE)

    cfg = _cfg_local_server(auto_login_command=("webauto", "script.py"))
    with patch("schwab_cli.auth_flows._resolve_cert_paths", _fake_resolve), \
         patch("schwab_cli.auth_flows.LocalServerHandler", return_value=_LocalOK()), \
         patch("schwab_cli.auth_flows.AutoLoginSupervisor", _FakeSupervisor):
        result = get_auth_response(cfg, manual=False)

    assert result == canned
    assert len(_FakeSupervisor.instances) == 1
    sup = _FakeSupervisor.instances[0]
    assert sup.started is True
    assert sup.terminated is True


def test_get_auth_response_skips_supervisor_on_manual(monkeypatch):
    """--manual must skip supervisor spawning for local_server."""
    monkeypatch.setattr("webbrowser.open", lambda *a, **kw: True)
    _reset_fake_handler()

    canned: AuthResult = {"kind": "code", "code": "FROM_LOCAL", "state": "S"}

    class _LocalOK:
        def wait_for_response(self, *, expected_state, cancel=None):
            return canned

    class _NeverConstructed:
        def __init__(self, *args, **kwargs):
            raise AssertionError(
                "AutoLoginSupervisor must NOT be constructed under --manual"
            )

    def _fake_resolve(uri):
        return (_FAKE_CERTFILE, _FAKE_KEYFILE)

    cfg = _cfg_local_server(auto_login_command=("webauto", "script.py"))
    with patch("schwab_cli.auth_flows._resolve_cert_paths", _fake_resolve), \
         patch("schwab_cli.auth_flows.LocalServerHandler", return_value=_LocalOK()), \
         patch("schwab_cli.auth_flows.AutoLoginSupervisor", _NeverConstructed):
        result = get_auth_response(cfg, manual=True)
    assert result == canned


def test_get_auth_response_terminates_supervisor_even_on_race_failure(monkeypatch):
    """If all handlers fail, supervisor.terminate() still runs (finally)."""
    monkeypatch.setattr("webbrowser.open", lambda *a, **kw: True)

    class _FailingLocal:
        def wait_for_response(self, *, expected_state, cancel=None):
            raise AuthHandlerError("local server timeout")

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

    def _fake_resolve(uri):
        return (_FAKE_CERTFILE, _FAKE_KEYFILE)

    cfg = _cfg_local_server(auto_login_command=("webauto", "script.py"))
    with patch("schwab_cli.auth_flows._resolve_cert_paths", _fake_resolve), \
         patch("schwab_cli.auth_flows.LocalServerHandler", return_value=_FailingLocal()), \
         patch("schwab_cli.auth_flows.UserInputHandler", return_value=_FailingUser()), \
         patch("schwab_cli.auth_flows.AutoLoginSupervisor", _FakeSupervisor):
        with pytest.raises(AuthFlowError):
            get_auth_response(cfg, manual=False)
    assert terminated["flag"] is True


def test_get_auth_response_does_not_open_browser_with_auto_login(monkeypatch):
    """When auto_login_command is set, webauto opens its own browser —
    schwab_cli must NOT call ``webbrowser.open``."""
    calls = []
    monkeypatch.setattr("webbrowser.open", lambda *a, **kw: calls.append(a) or True)

    canned: AuthResult = {"kind": "code", "code": "X", "state": "S"}

    class _LocalOK:
        def wait_for_response(self, *, expected_state, cancel=None):
            return canned

    class _NopSupervisor:
        def __init__(self, *args, **kwargs): pass
        def start(self): pass
        def terminate(self): pass

    def _fake_resolve(uri):
        return (_FAKE_CERTFILE, _FAKE_KEYFILE)

    cfg = _cfg_local_server(auto_login_command=("webauto", "script.py"))
    with patch("schwab_cli.auth_flows._resolve_cert_paths", _fake_resolve), \
         patch("schwab_cli.auth_flows.LocalServerHandler", return_value=_LocalOK()), \
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

    class _LocalOK:
        def wait_for_response(self, *, expected_state, cancel=None):
            return canned

    def _fake_resolve(uri):
        return (_FAKE_CERTFILE, _FAKE_KEYFILE)

    with patch("schwab_cli.auth_flows._resolve_cert_paths", _fake_resolve), \
         patch("schwab_cli.auth_flows.UserInputHandler", return_value=_UserOK()), \
         patch("schwab_cli.auth_flows.LocalServerHandler", return_value=_LocalOK()):
        get_auth_response(_cfg_local_server(), manual=False)
    assert len(calls) == 1


def test_get_auth_response_opens_browser_when_manual_overrides_auto_login(monkeypatch):
    """--manual disables auto-login → schwab_cli must open the browser itself."""
    calls = []
    monkeypatch.setattr("webbrowser.open", lambda *a, **kw: calls.append(a) or True)
    canned: AuthResult = {"kind": "code", "code": "X", "state": "S"}

    class _UserOK:
        def wait_for_response(self, *, expected_state, cancel=None):
            return canned

    class _LocalOK:
        def wait_for_response(self, *, expected_state, cancel=None):
            return canned

    def _fake_resolve(uri):
        return (_FAKE_CERTFILE, _FAKE_KEYFILE)

    cfg = _cfg_local_server(auto_login_command=("webauto", "script.py"))
    with patch("schwab_cli.auth_flows._resolve_cert_paths", _fake_resolve), \
         patch("schwab_cli.auth_flows.LocalServerHandler", return_value=_LocalOK()), \
         patch("schwab_cli.auth_flows.UserInputHandler", return_value=_UserOK()):
        get_auth_response(cfg, manual=True)
    assert len(calls) == 1


def test_get_auth_response_opens_browser_and_returns_handler_result(
    monkeypatch, capsys,
):
    """End-to-end (mocked): URL is printed, browser is opened."""
    canned: AuthResult = {"kind": "code", "code": "FROM_USER", "state": "x"}
    monkeypatch.setattr("webbrowser.open", lambda *a, **kw: True)

    class _UserInputOK:
        def wait_for_response(self, *, expected_state, cancel=None):
            return canned

    class _LocalOK:
        def wait_for_response(self, *, expected_state, cancel=None):
            raise AuthHandlerError("local server lost race")

    def _fake_resolve(uri):
        return (_FAKE_CERTFILE, _FAKE_KEYFILE)

    with patch("schwab_cli.auth_flows._resolve_cert_paths", _fake_resolve), \
         patch("schwab_cli.auth_flows.UserInputHandler", return_value=_UserInputOK()), \
         patch("schwab_cli.auth_flows.LocalServerHandler", return_value=_LocalOK()):
        result = get_auth_response(_cfg_local_server())
    assert result == canned
    err = capsys.readouterr().err
    assert "https://api.schwabapi.com/v1/oauth/authorize" in err


# ---- _open_and_print -------------------------------------------------------


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
    ``webbrowser.open`` and emit a short "Auto-login to schwab..." banner."""
    calls = []
    monkeypatch.setattr("webbrowser.open", lambda *a, **kw: calls.append(a) or True)
    _open_and_print("https://x/y", open_browser=False)
    assert calls == []
    err = capsys.readouterr().err
    assert "Auto-login to schwab" in err


# ---- _race_handlers (handler-agnostic; kept) --------------------------------


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
            raise AuthHandlerError("observer never wins")

    _race_handlers([fast, _CancelObserver()], expected_state="S")
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
    """ErrorResult is a valid race winner (Schwab said 'no')."""
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
    assert isinstance(fast_error.last_cancel, threading.Event)


def test_race_state_propagated_to_every_handler():
    h1 = _ImmediateOK(_OK_RESULT)
    h2 = _ImmediateFail()
    _race_handlers([h1, h2], expected_state="THE_STATE")
    assert h1.last_state == "THE_STATE"
