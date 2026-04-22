"""Tests for :mod:`schwab_cli.auth_flows`.

``run_browser_auth`` (the SeleniumBase-backed browser flow) is patched out in
every test — we only verify the orchestration in ``get_code``: the right flow
handler runs, ``automate`` tracks ``manual``, the state param is honored, and
relay/URL responses are parsed correctly.
"""

from unittest.mock import patch

import httpx
import pytest
import respx

from schwab_cli.auth_flows import (
    AuthFlowError,
    _extract_code_from_query,
    get_code,
)
from schwab_cli.config import Config

_SB_TARGET = "schwab_cli.browser._seleniumbase_flow.run_browser_auth"


def _cfg_client() -> Config:
    return Config(
        client_id="cid",
        client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
        auth_flow="client",
    )


def _cfg_code_relay() -> Config:
    return Config(
        client_id="cid",
        client_secret="csec",
        redirect_uri="https://relay.example.com/uuid/secret",
        auth_flow="code_relay",
        code_relay_url="https://relay.example.com/uuid/secret/wait",
    )


# ---- dispatch ---------------------------------------------------------------


def test_unknown_flow_raises():
    cfg = Config(
        client_id="cid",
        client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
        auth_flow="bogus",  # bypasses load() validation
    )
    with pytest.raises(AuthFlowError, match="unknown auth_flow"):
        get_code(cfg)


# ---- client flow ------------------------------------------------------------


def test_client_returns_code_from_browser_url():
    cfg = _cfg_client()
    captured = {}

    def fake_run(cfg_, *, automate, state):
        captured["automate"] = automate
        captured["state"] = state
        return f"https://127.0.0.1:8443/?code=ABC&state={state}"

    with patch(_SB_TARGET, side_effect=fake_run):
        assert get_code(cfg) == "ABC"
    assert captured["automate"] is True
    # Non-empty state must have been generated for the browser flow.
    assert captured["state"]


def test_client_manual_sets_automate_false():
    cfg = _cfg_client()
    captured = {}

    def fake_run(cfg_, *, automate, state):
        captured["automate"] = automate
        return f"https://127.0.0.1:8443/?code=X&state={state}"

    with patch(_SB_TARGET, side_effect=fake_run):
        get_code(cfg, manual=True)
    assert captured["automate"] is False


def test_client_state_mismatch_raises():
    cfg = _cfg_client()

    def fake_run(cfg_, *, automate, state):
        return "https://127.0.0.1:8443/?code=X&state=wrong_state"

    with patch(_SB_TARGET, side_effect=fake_run):
        with pytest.raises(AuthFlowError, match="state mismatch"):
            get_code(cfg)


def test_client_oauth_error_in_url_raises():
    cfg = _cfg_client()

    def fake_run(cfg_, *, automate, state):
        return (
            f"https://127.0.0.1:8443/?error=access_denied"
            f"&error_description=user+canceled&state={state}"
        )

    with patch(_SB_TARGET, side_effect=fake_run):
        with pytest.raises(AuthFlowError, match="access_denied"):
            get_code(cfg)


def test_client_missing_code_raises():
    cfg = _cfg_client()

    def fake_run(cfg_, *, automate, state):
        return f"https://127.0.0.1:8443/?state={state}"

    with patch(_SB_TARGET, side_effect=fake_run):
        with pytest.raises(AuthFlowError, match="code"):
            get_code(cfg)


# ---- code_relay flow --------------------------------------------------------


@respx.mock
def test_code_relay_returns_code_from_relay():
    cfg = _cfg_code_relay()
    captured = {}

    def fake_run(cfg_, *, automate, state):
        captured["state"] = state
        captured["automate"] = automate
        # Return value is unused by the caller — the relay holds the code.
        return f"{cfg_.redirect_uri}?code=IGNORED&state={state}"

    def respond(request):
        return httpx.Response(
            200, text=f"code=RELAY_CODE&state={captured['state']}"
        )

    respx.get(cfg.code_relay_url).mock(side_effect=respond)

    with patch(_SB_TARGET, side_effect=fake_run):
        assert get_code(cfg) == "RELAY_CODE"
    assert captured["automate"] is True


@respx.mock
def test_code_relay_manual_sets_automate_false():
    cfg = _cfg_code_relay()
    captured = {}

    def fake_run(cfg_, *, automate, state):
        captured["automate"] = automate
        captured["state"] = state

    def respond(request):
        return httpx.Response(200, text=f"code=C&state={captured['state']}")

    respx.get(cfg.code_relay_url).mock(side_effect=respond)

    with patch(_SB_TARGET, side_effect=fake_run):
        get_code(cfg, manual=True)
    assert captured["automate"] is False


@respx.mock
def test_code_relay_retries_on_408():
    cfg = _cfg_code_relay()
    captured = {}
    calls = {"n": 0}

    def fake_run(cfg_, *, automate, state):
        captured["state"] = state

    def respond(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(408, text="timeout")
        return httpx.Response(200, text=f"code=C&state={captured['state']}")

    respx.get(cfg.code_relay_url).mock(side_effect=respond)

    with patch(_SB_TARGET, side_effect=fake_run):
        assert get_code(cfg) == "C"
    assert calls["n"] == 2


@respx.mock
def test_code_relay_403_raises():
    cfg = _cfg_code_relay()
    respx.get(cfg.code_relay_url).mock(return_value=httpx.Response(403))

    with patch(_SB_TARGET, side_effect=lambda *a, **kw: None):
        with pytest.raises(AuthFlowError, match="Relay rejected"):
            get_code(cfg)


@respx.mock
def test_code_relay_state_mismatch_raises():
    cfg = _cfg_code_relay()
    respx.get(cfg.code_relay_url).mock(
        return_value=httpx.Response(200, text="code=X&state=wrong")
    )

    with patch(_SB_TARGET, side_effect=lambda *a, **kw: None):
        with pytest.raises(AuthFlowError, match="state mismatch"):
            get_code(cfg)


@respx.mock
def test_code_relay_oauth_error_payload_raises():
    cfg = _cfg_code_relay()
    captured = {}

    def fake_run(cfg_, *, automate, state):
        captured["state"] = state

    def respond(request):
        return httpx.Response(
            200,
            text=f"error=access_denied&error_description=user+canceled"
                 f"&state={captured['state']}",
        )

    respx.get(cfg.code_relay_url).mock(side_effect=respond)

    with patch(_SB_TARGET, side_effect=fake_run):
        with pytest.raises(AuthFlowError, match="access_denied"):
            get_code(cfg)


def test_code_relay_missing_url_raises_defensively():
    cfg = Config(
        client_id="cid",
        client_secret="csec",
        redirect_uri="https://r/u/s",
        auth_flow="code_relay",
        code_relay_url=None,
    )
    with pytest.raises(AuthFlowError, match="code_relay_url"):
        get_code(cfg)


# ---- _extract_code_from_query -----------------------------------------------


def test_extract_code_success():
    assert _extract_code_from_query("code=X&state=S1", expected_state="S1") == "X"


def test_extract_code_state_mismatch():
    with pytest.raises(AuthFlowError, match="state mismatch"):
        _extract_code_from_query("code=X&state=wrong", expected_state="S1")


def test_extract_code_oauth_error():
    with pytest.raises(AuthFlowError, match="access_denied"):
        _extract_code_from_query(
            "error=access_denied&state=S1", expected_state="S1"
        )


def test_extract_code_missing_code():
    with pytest.raises(AuthFlowError, match="code"):
        _extract_code_from_query("state=S1", expected_state="S1")
