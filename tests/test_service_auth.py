from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from schwab_cli.api.client import SessionExpired
from schwab_cli.config import Config
from schwab_cli.oauth import OAuthError, TokenResponse
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


def test_fresh_token_returned_unchanged_no_mint(isolated_home):
    _save_session(expires_in=3600, refresh_in=7 * 24 * 3600)
    with patch("schwab_cli.oauth.refresh") as mock_refresh:
        session = get_session(_CFG)
    mock_refresh.assert_not_called()
    assert session.access_token == "atok"


def test_no_session_raises_not_authenticated(isolated_home):
    with pytest.raises(NotAuthenticated):
        get_session(_CFG)


def test_stale_token_valid_refresh_mints_and_persists(isolated_home):
    # Access token already expired, refresh token still valid.
    _save_session(expires_in=-10, refresh_in=7 * 24 * 3600)
    new_tr = TokenResponse(
        access_token="new_atok",
        refresh_token="new_rtok",
        expires_in=1800,
    )
    with patch("schwab_cli.oauth.refresh", return_value=new_tr) as mock_refresh:
        session = get_session(_CFG)
    mock_refresh.assert_called_once_with(_CFG, "rtok")
    assert session.access_token == "new_atok"
    # Persisted to disk.
    on_disk = load_session()
    assert on_disk is not None
    assert on_disk.access_token == "new_atok"
    assert on_disk.refresh_token == "new_rtok"


def test_dead_refresh_token_raises_session_expired(isolated_home):
    # Both access and refresh tokens expired -> never even calls refresh.
    _save_session(expires_in=-10, refresh_in=-10)
    with pytest.raises(SessionExpired):
        get_session(_CFG)


def test_refresh_oauth_error_raises_session_expired(isolated_home):
    # Access stale, refresh window still open, but the mint itself fails.
    _save_session(expires_in=-10, refresh_in=3600)
    with patch("schwab_cli.oauth.refresh", side_effect=OAuthError("boom")):
        with pytest.raises(SessionExpired):
            get_session(_CFG)


def test_refresh_network_error_raises_session_expired(isolated_home):
    # A transient network failure during the mint must surface as a
    # recoverable SessionExpired, not an uncaught httpx error.
    import httpx

    _save_session(expires_in=-10, refresh_in=3600)
    with patch("schwab_cli.oauth.refresh", side_effect=httpx.RequestError("timeout")):
        with pytest.raises(SessionExpired):
            get_session(_CFG)


def test_refresh_http_status_error_raises_session_expired(isolated_home):
    import httpx

    _save_session(expires_in=-10, refresh_in=3600)
    req = httpx.Request("POST", "https://api.schwabapi.com")
    resp = httpx.Response(400, request=req)
    err = httpx.HTTPStatusError("400", request=req, response=resp)
    with patch("schwab_cli.oauth.refresh", side_effect=err):
        with pytest.raises(SessionExpired):
            get_session(_CFG)


def test_mint_never_spawns_webauto(isolated_home):
    """The pure-HTTP mint path must never spawn the webauto/browser
    subprocess. ``auth_handlers`` owns every ``subprocess.Popen`` that
    launches the auto-login helper; if the mint path ever touches it the
    patched Popen blows up."""

    def _boom(*_args, **_kwargs):
        raise AssertionError("mint must not spawn a subprocess / webauto")

    _save_session(expires_in=-10, refresh_in=7 * 24 * 3600)
    new_tr = TokenResponse(
        access_token="new_atok", refresh_token="new_rtok", expires_in=1800,
    )
    with patch("schwab_cli.auth_handlers.subprocess.Popen", _boom), patch(
        "schwab_cli.oauth.refresh", return_value=new_tr
    ):
        session = get_session(_CFG)
    assert session.access_token == "new_atok"
