"""Spec tests for schwab_cli.auth_delegate — the client-side bridge to
the daemon's single-writer token refresh."""
from __future__ import annotations

import time

import httpx
import pytest
import respx

from schwab_cli import auth_delegate
from schwab_cli.session import Session
from schwab_cli.session import save as save_session


@pytest.fixture(autouse=True)
def _clear_local_refresher():
    auth_delegate.set_local_refresher(None)
    yield
    auth_delegate.set_local_refresher(None)


@pytest.fixture
def isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
    # conftest points SCHWAB_DAEMON_URL at a dead port for every test;
    # these tests mock httpx via respx, so restore the canonical URL.
    monkeypatch.setenv("SCHWAB_DAEMON_URL", "http://127.0.0.1:7234")
    return tmp_path


def _session(access="atok") -> Session:
    now = int(time.time())
    return Session(
        access_token=access,
        refresh_token="rtok",
        expires_at=now + 1800,
        refresh_token_expires_at=now + 7 * 24 * 3600,
    )


def test_local_refresher_short_circuits_http(isolated_config):
    fresh = _session("local_atok")
    auth_delegate.set_local_refresher(lambda: fresh)
    # No respx mock active — an HTTP attempt would raise loudly.
    assert auth_delegate.request_refresh() is fresh


def test_local_refresher_failure_returns_none(isolated_config):
    def boom():
        raise RuntimeError("manager exploded")

    auth_delegate.set_local_refresher(boom)
    assert auth_delegate.request_refresh() is None


@respx.mock
def test_http_success_rereads_session_from_disk(isolated_config):
    fresh = _session("daemon_atok")
    save_session(fresh)
    respx.post("http://127.0.0.1:7234/auth/refresh").mock(
        return_value=httpx.Response(200, json={"ok": True}),
    )
    got = auth_delegate.request_refresh()
    assert got is not None
    assert got.access_token == "daemon_atok"


@respx.mock
def test_http_503_returns_none_without_unreachable(isolated_config):
    respx.post("http://127.0.0.1:7234/auth/refresh").mock(
        return_value=httpx.Response(503, json={"ok": False}),
    )
    calls = []
    got = auth_delegate.request_refresh(on_unreachable=calls.append)
    assert got is None
    assert calls == []  # daemon answered — not an unreachable condition


@respx.mock
def test_connect_error_returns_none_and_fires_unreachable(isolated_config):
    respx.post("http://127.0.0.1:7234/auth/refresh").mock(
        side_effect=httpx.ConnectError("refused"),
    )
    calls = []
    got = auth_delegate.request_refresh(on_unreachable=calls.append)
    assert got is None
    assert len(calls) == 1 and "ConnectError" in calls[0]


@respx.mock
def test_daemon_url_env_override(isolated_config, monkeypatch):
    monkeypatch.setenv("SCHWAB_DAEMON_URL", "http://127.0.0.1:9999/")
    fresh = _session("alt_atok")
    save_session(fresh)
    route = respx.post("http://127.0.0.1:9999/auth/refresh").mock(
        return_value=httpx.Response(200, json={"ok": True}),
    )
    got = auth_delegate.request_refresh()
    assert got is not None and got.access_token == "alt_atok"
    assert route.called
