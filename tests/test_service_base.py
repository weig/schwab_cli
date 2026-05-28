"""Unit tests for the Layer-2 :class:`schwab_cli.service.base.BaseService`.

Covers the shared auth boilerplate factored out of every service
(``_authed_client``) and the default :class:`NullSink`:

  - ``NotConfigured`` when no config is on disk;
  - the yielded client is opened and closed (no pool leak);
  - ``SessionExpired`` from ``service.auth.get_session`` propagates;
  - ``NullSink`` ``info`` / ``progress`` are silent no-ops.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from schwab_cli.api.client import SessionExpired
from schwab_cli.config import Config
from schwab_cli.config import save as save_config
from schwab_cli.service.auth import NotConfigured
from schwab_cli.service.base import BaseService, NullSink
from schwab_cli.session import Session
from schwab_cli.session import save as save_session


def _save_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save_config(
        Config(
            client_id="cid",
            client_secret="csec",
            redirect_uri="https://127.0.0.1:8443",
        )
    )


def _save_session(tmp_path):
    now = int(time.time())
    save_session(
        Session(
            access_token="atok",
            refresh_token="rtok",
            expires_at=now + 3600,
            refresh_token_expires_at=now + 7 * 24 * 3600,
        )
    )


# ---- _authed_client ----------------------------------------------------


def test_authed_client_no_config_raises_not_configured(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    svc = BaseService()
    with pytest.raises(NotConfigured):
        with svc._authed_client():
            pass  # pragma: no cover — never reached


def test_authed_client_opens_and_closes_client(monkeypatch, tmp_path):
    """The context manager yields a usable client and closes it on exit —
    even though the body raised — so the HTTP pool never leaks."""
    _save_config(tmp_path, monkeypatch)
    _save_session(tmp_path)

    svc = BaseService()
    with patch(
        "schwab_cli.api.client.SchwabClient.close"
    ) as mock_close:
        with svc._authed_client() as client:
            assert client is not None
            mock_close.assert_not_called()
        # Closed exactly once on normal exit.
        mock_close.assert_called_once()


def test_authed_client_closes_client_on_body_error(monkeypatch, tmp_path):
    _save_config(tmp_path, monkeypatch)
    _save_session(tmp_path)

    svc = BaseService()
    with patch("schwab_cli.api.client.SchwabClient.close") as mock_close:
        with pytest.raises(RuntimeError):
            with svc._authed_client():
                raise RuntimeError("boom")
        mock_close.assert_called_once()


def test_authed_client_session_expired_propagates(monkeypatch, tmp_path):
    """A SessionExpired from ``service.auth.get_session`` (e.g. a dead
    refresh token) propagates through the boundary unchanged."""
    _save_config(tmp_path, monkeypatch)
    _save_session(tmp_path)

    svc = BaseService()
    with patch(
        "schwab_cli.service.base.service_auth.get_session",
        side_effect=SessionExpired("expired"),
    ):
        with pytest.raises(SessionExpired):
            with svc._authed_client():
                pass  # pragma: no cover — never reached


# ---- NullSink ----------------------------------------------------------


def test_null_sink_is_silent_noop(capsys):
    sink = NullSink()
    assert sink.info("notice") is None
    assert sink.progress("step") is None
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_base_service_defaults_to_null_sink():
    svc = BaseService()
    assert isinstance(svc._out, NullSink)


def test_base_service_uses_injected_sink():
    sink = NullSink()
    svc = BaseService(out=sink)
    assert svc._out is sink
