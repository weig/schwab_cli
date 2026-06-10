"""Spec tests for the AuthMonitor-free startup auto-login helper."""
from __future__ import annotations

import io

from schwab_cli.commands._daemon import attempt_startup_autologin
from schwab_cli.config import Config
from schwab_cli.mcp_server.logbook import LogBook
from schwab_cli.session import Session


def _cfg() -> Config:
    return Config(
        client_id="cid",
        client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
        auto_login_command=("webauto",),
    )


def _session() -> Session:
    return Session(
        access_token="atok", refresh_token="rtok",
        expires_at=2_000_000_000, refresh_token_expires_at=2_000_000_000,
    )


class _NotifierSpy:
    def __init__(self) -> None:
        self.events: list[str] = []

    def emit(self, event: str, **fields) -> None:
        self.events.append(event)


def test_startup_autologin_success_returns_session_and_notifies():
    notifier = _NotifierSpy()
    fresh = _session()
    got = attempt_startup_autologin(
        _cfg(), LogBook(stream=io.StringIO()), notifier,
        full_auth=lambda cfg: fresh,
    )
    assert got is fresh
    assert "auth.auto_login.succeeded" in notifier.events


def test_startup_autologin_failure_returns_none_and_notifies():
    notifier = _NotifierSpy()

    def boom(cfg):
        raise RuntimeError("webauto crashed")

    got = attempt_startup_autologin(
        _cfg(), LogBook(stream=io.StringIO()), notifier, full_auth=boom,
    )
    assert got is None
    assert "auth.auto_login.failed" in notifier.events


def test_startup_autologin_notifier_errors_swallowed():
    class _Boom:
        def emit(self, event, **fields):
            raise RuntimeError("notifier down")

    got = attempt_startup_autologin(
        _cfg(), LogBook(stream=io.StringIO()), _Boom(),
        full_auth=lambda cfg: _session(),
    )
    assert got is not None
