"""Tests for `schwab_cli.auth_handlers`.

Two concrete handlers ship today:
  * ``UserInputHandler`` — prompts on stderr, reads from stdin.
  * ``CodeRelayHandler`` — long-polls a configured relay URL.

Both return ``AuthResult`` (``CodeResult`` today). ``TokenResult`` is the
future shape for ``AuthServerHandler`` and is type-checked here against
the ``AuthHandler`` Protocol.
"""
from __future__ import annotations

import io
import threading
import time

import httpx
import pytest

from schwab_cli.auth_handlers import (
    AuthHandler,
    AuthHandlerError,
    AuthResult,
    CodeRelayHandler,
    UserInputHandler,
)


# ---------------------------------------------------------------------
# UserInputHandler
# ---------------------------------------------------------------------


def _set_stdin(monkeypatch, text: str) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(text))


def test_user_input_parses_bare_code(monkeypatch, capsys):
    _set_stdin(monkeypatch, "C0.bare-code-value\n")
    h = UserInputHandler()
    r = h.wait_for_response(expected_state="STATE-ABC")
    assert r == {"kind": "code", "code": "C0.bare-code-value", "state": None}
    err = capsys.readouterr().err
    assert "state verification skipped" in err.lower()


def test_user_input_parses_querystring(monkeypatch):
    _set_stdin(monkeypatch, "code=C0.x.y.z&state=STATE-ABC&session=foo\n")
    h = UserInputHandler()
    r = h.wait_for_response(expected_state="STATE-ABC")
    assert r["kind"] == "code"
    assert r["code"] == "C0.x.y.z"
    assert r["state"] == "STATE-ABC"


def test_user_input_parses_full_url(monkeypatch):
    _set_stdin(
        monkeypatch,
        "https://127.0.0.1:8443/?code=C0.url-form&state=STATE-XYZ&session=x\n",
    )
    h = UserInputHandler()
    r = h.wait_for_response(expected_state="STATE-XYZ")
    assert r["code"] == "C0.url-form"
    assert r["state"] == "STATE-XYZ"


def test_user_input_rejects_state_mismatch(monkeypatch):
    _set_stdin(monkeypatch, "code=ABC&state=WRONG\n")
    h = UserInputHandler()
    with pytest.raises(AuthHandlerError, match="state"):
        h.wait_for_response(expected_state="EXPECTED")


def test_user_input_rejects_empty(monkeypatch):
    _set_stdin(monkeypatch, "\n")
    h = UserInputHandler()
    with pytest.raises(AuthHandlerError):
        h.wait_for_response(expected_state="S")


def test_user_input_rejects_querystring_without_code(monkeypatch):
    _set_stdin(monkeypatch, "state=S&foo=bar\n")
    h = UserInputHandler()
    with pytest.raises(AuthHandlerError, match="code"):
        h.wait_for_response(expected_state="S")


def test_user_input_returns_code_kind_discriminator(monkeypatch):
    """Sanity-check the AuthResult shape carries the kind discriminator."""
    _set_stdin(monkeypatch, "code=A&state=S\n")
    h = UserInputHandler()
    r = h.wait_for_response(expected_state="S")
    assert r["kind"] == "code"


# ---------------------------------------------------------------------
# CodeRelayHandler
# ---------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


def _patch_httpx_get(monkeypatch, side_effect):
    """side_effect: callable taking (url, **kwargs) returning _FakeResponse,
    or raising an exception."""
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        result = side_effect(url, **kwargs) if callable(side_effect) else side_effect
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr("httpx.get", fake_get)
    return calls


def test_relay_returns_code_on_200(monkeypatch):
    _patch_httpx_get(
        monkeypatch,
        lambda url, **kw: _FakeResponse(200, "code=ABC&state=S"),
    )
    h = CodeRelayHandler("https://relay/wait")
    r = h.wait_for_response(expected_state="S")
    assert r == {"kind": "code", "code": "ABC", "state": "S"}


def test_relay_retries_on_408(monkeypatch):
    seq = [_FakeResponse(408), _FakeResponse(408), _FakeResponse(200, "code=Z&state=S")]

    def side(url, **kw):
        return seq.pop(0)

    _patch_httpx_get(monkeypatch, side)
    h = CodeRelayHandler("https://relay/wait")
    r = h.wait_for_response(expected_state="S")
    assert r["code"] == "Z"


def test_relay_retries_on_read_timeout(monkeypatch):
    calls = [0]

    def side(url, **kw):
        calls[0] += 1
        if calls[0] == 1:
            return httpx.ReadTimeout("slow")
        return _FakeResponse(200, "code=T&state=S")

    _patch_httpx_get(monkeypatch, side)
    h = CodeRelayHandler("https://relay/wait")
    r = h.wait_for_response(expected_state="S")
    assert r["code"] == "T"
    assert calls[0] == 2


def test_relay_raises_on_403(monkeypatch):
    _patch_httpx_get(monkeypatch, lambda url, **kw: _FakeResponse(403, "denied"))
    h = CodeRelayHandler("https://relay/wait")
    with pytest.raises(AuthHandlerError, match="403"):
        h.wait_for_response(expected_state="S")


def test_relay_raises_on_unexpected_status(monkeypatch):
    _patch_httpx_get(monkeypatch, lambda url, **kw: _FakeResponse(500, "boom"))
    h = CodeRelayHandler("https://relay/wait")
    with pytest.raises(AuthHandlerError, match="500"):
        h.wait_for_response(expected_state="S")


def test_relay_raises_on_state_mismatch(monkeypatch):
    _patch_httpx_get(
        monkeypatch,
        lambda url, **kw: _FakeResponse(200, "code=X&state=WRONG"),
    )
    h = CodeRelayHandler("https://relay/wait")
    with pytest.raises(AuthHandlerError, match="state"):
        h.wait_for_response(expected_state="EXPECTED")


def test_relay_cancels_between_polls(monkeypatch):
    """Setting the cancel event between polls should make the handler raise
    AuthHandlerError so the race aggregator can record it as a non-winner.
    """
    cancel = threading.Event()
    calls = [0]

    def side(url, **kw):
        calls[0] += 1
        # First call: pretend the relay had nothing yet (408). Set cancel.
        cancel.set()
        return _FakeResponse(408)

    _patch_httpx_get(monkeypatch, side)
    h = CodeRelayHandler("https://relay/wait")
    with pytest.raises(AuthHandlerError, match="cancel"):
        h.wait_for_response(expected_state="S", cancel=cancel)
    assert calls[0] == 1  # didn't loop after cancel


def test_relay_raises_after_deadline(monkeypatch):
    """If the relay keeps 408'ing past the deadline, raise."""
    _patch_httpx_get(monkeypatch, lambda url, **kw: _FakeResponse(408))
    h = CodeRelayHandler("https://relay/wait", deadline_seconds=0.1)
    start = time.time()
    with pytest.raises(AuthHandlerError):
        h.wait_for_response(expected_state="S")
    assert time.time() - start < 5  # bounded


# ---------------------------------------------------------------------
# Protocol type seam
# ---------------------------------------------------------------------


def test_token_result_handler_satisfies_protocol():
    """Future AuthServerHandler will return TokenResult. The Protocol
    must accept that shape via duck typing."""

    class _FakeTokenHandler:
        def wait_for_response(
            self, *, expected_state: str, cancel=None,
        ) -> AuthResult:
            return {
                "kind": "token",
                "access_token": "AT",
                "refresh_token": "RT",
                "expires_in": 1800,
            }

    h: AuthHandler = _FakeTokenHandler()  # type-only assertion via assignment
    r = h.wait_for_response(expected_state="S")
    assert r["kind"] == "token"
