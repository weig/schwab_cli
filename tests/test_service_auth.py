from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from schwab_cli.api.client import SessionExpired
from schwab_cli.config import Config
from schwab_cli.service.auth import NotAuthenticated, get_session
from schwab_cli.session import Session
from schwab_cli.session import load as load_session
from schwab_cli.session import save as save_session

_CFG = Config(
    client_id="cid",
    client_secret="csec",
    redirect_uri="https://127.0.0.1:8443",
)


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("SCHWAB_CLI_CONFIG_DIR", raising=False)
    monkeypatch.delenv("SCHWAB_JOBS_SCHEDULED", raising=False)
    return tmp_path


def _save_session(*, expires_in: int, refresh_in: int) -> None:
    now = int(time.time())
    save_session(
        Session(
            access_token="atok",
            refresh_token="rtok",
            expires_at=now + expires_in,
            refresh_token_expires_at=now + refresh_in,
        )
    )


def _fresh_session(access="fresh_atok") -> Session:
    now = int(time.time())
    return Session(
        access_token=access,
        refresh_token="rtok",
        expires_at=now + 1800,
        refresh_token_expires_at=now + 7 * 24 * 3600,
    )


def test_fresh_token_returned_unchanged_no_delegate(isolated_home):
    _save_session(expires_in=3600, refresh_in=7 * 24 * 3600)
    with patch("schwab_cli.auth_delegate.request_refresh") as mock_delegate:
        session = get_session(_CFG)
    mock_delegate.assert_not_called()
    assert session.access_token == "atok"


def test_no_session_raises_not_authenticated(isolated_home):
    with pytest.raises(NotAuthenticated):
        get_session(_CFG)


def test_stale_token_delegates_to_daemon(isolated_home):
    # Access token already expired, refresh token still valid → the
    # service asks the daemon (the single token writer) and returns the
    # fresh session WITHOUT writing anything itself.
    _save_session(expires_in=-10, refresh_in=7 * 24 * 3600)
    fresh = _fresh_session()
    with patch(
        "schwab_cli.auth_delegate.request_refresh", return_value=fresh,
    ) as mock_delegate:
        session = get_session(_CFG)
    mock_delegate.assert_called_once()
    assert session.access_token == "fresh_atok"
    # READ-ONLY: the stale on-disk session was not touched by the service.
    on_disk = load_session()
    assert on_disk is not None and on_disk.access_token == "atok"


def test_dead_refresh_token_raises_without_delegating(isolated_home):
    # Both access and refresh tokens expired -> fail fast, no daemon call.
    _save_session(expires_in=-10, refresh_in=-10)
    with patch("schwab_cli.auth_delegate.request_refresh") as mock_delegate:
        with pytest.raises(SessionExpired):
            get_session(_CFG)
    mock_delegate.assert_not_called()


def test_delegate_failure_raises_session_expired(isolated_home):
    # Daemon down or refresh rejected → the service surfaces an auth
    # failure for the user instead of self-healing.
    _save_session(expires_in=-10, refresh_in=3600)
    with patch("schwab_cli.auth_delegate.request_refresh", return_value=None):
        with pytest.raises(SessionExpired, match="daemon"):
            get_session(_CFG)


def test_delegate_returning_still_stale_session_raises(isolated_home):
    # Defensive: a "fresh" session that is somehow still expired must not
    # be handed to callers as usable.
    _save_session(expires_in=-10, refresh_in=3600)
    now = int(time.time())
    stale = Session(
        access_token="stale",
        refresh_token="rtok",
        expires_at=now - 5,
        refresh_token_expires_at=now + 3600,
    )
    with patch("schwab_cli.auth_delegate.request_refresh", return_value=stale):
        with pytest.raises(SessionExpired):
            get_session(_CFG)


def test_service_never_exchanges_or_writes(isolated_home):
    """The service must never run an OAuth exchange, spawn webauto, or
    write session.json — those are the daemon's exclusive jobs."""

    def _boom(*_args, **_kwargs):
        raise AssertionError("service-layer auth must not do this")

    _save_session(expires_in=-10, refresh_in=7 * 24 * 3600)
    fresh = _fresh_session()
    with patch("schwab_cli.oauth.refresh", _boom), patch(
        "schwab_cli.auth_handlers.subprocess.Popen", _boom
    ), patch("schwab_cli.session.save", _boom), patch(
        "schwab_cli.auth_delegate.request_refresh", return_value=fresh,
    ):
        session = get_session(_CFG)
    assert session.access_token == "fresh_atok"


def test_unreachable_notifies_only_in_automated_context(isolated_home, monkeypatch):
    """Interactive CLI: no notification hook. Scheduled worker
    (SCHWAB_JOBS_SCHEDULED=1): a daemon.unreachable notifier is passed."""
    _save_session(expires_in=-10, refresh_in=3600)
    seen_hooks = []

    def _capture(*, on_unreachable=None):
        seen_hooks.append(on_unreachable)
        return _fresh_session()

    with patch("schwab_cli.auth_delegate.request_refresh", _capture):
        get_session(_CFG)
    assert seen_hooks == [None]  # interactive: no hook

    monkeypatch.setenv("SCHWAB_JOBS_SCHEDULED", "1")
    with patch("schwab_cli.auth_delegate.request_refresh", _capture):
        get_session(_CFG)
    assert callable(seen_hooks[1])  # automated: notifier wired
