"""Spec tests for schwab_cli.server.token_runtime — the Phase-2 glue that
runs a TokenManager's two tracks as daemon threads and bridges its
notifications into the Notifier infra."""
from __future__ import annotations

import threading

from schwab_cli.config import Config
from schwab_cli.server.token_runtime import (
    build_token_manager,
    start_token_threads,
    stop_token_threads,
)
from schwab_cli.session import REFRESH_TOKEN_LIFETIME_SECONDS, Session

_NOW = 1_700_000_000


def _cfg() -> Config:
    return Config(
        client_id="cid",
        client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
        auto_login_command=("webauto",),
    )


def _fresh_session() -> Session:
    return Session(
        access_token="atok",
        refresh_token="rtok",
        expires_at=_NOW + 1800,
        refresh_token_expires_at=_NOW + REFRESH_TOKEN_LIFETIME_SECONDS,
    )


class _NotifierSpy:
    def __init__(self, *, boom: bool = False) -> None:
        self.events: list[tuple[str, dict]] = []
        self._boom = boom

    def emit(self, event: str, **fields) -> None:
        if self._boom:
            raise RuntimeError("notifier exploded")
        self.events.append((event, fields))


def test_build_wires_emit_through_notifier():
    notifier = _NotifierSpy()
    mgr = build_token_manager(
        _cfg(),
        notifier=notifier,
        load_session=lambda: None,
        save_session=lambda s: None,
    )
    mgr._emit("auth.recovery_succeeded", attempts=1)
    assert notifier.events == [("auth.recovery_succeeded", {"attempts": 1})]


def test_emit_swallows_notifier_errors():
    mgr = build_token_manager(
        _cfg(),
        notifier=_NotifierSpy(boom=True),
        load_session=lambda: None,
        save_session=lambda s: None,
    )
    # Must not raise — a broken notifier can never break a track.
    mgr._emit("auth.recovery_succeeded", attempts=1)


def test_build_accepts_no_notifier():
    mgr = build_token_manager(
        _cfg(),
        notifier=None,
        load_session=lambda: None,
        save_session=lambda s: None,
    )
    mgr._emit("auth.recovery_succeeded")  # no-op, no crash


def test_on_session_replaced_passthrough():
    from schwab_cli.oauth import TokenResponse

    seen = []
    store = {"s": _fresh_session()}
    mgr = build_token_manager(
        _cfg(),
        notifier=None,
        on_session_replaced=seen.append,
        load_session=lambda: store.get("s"),
        save_session=lambda s: store.__setitem__("s", s),
        now=lambda: _NOW,
        exchange=lambda cfg, rt: TokenResponse(
            access_token="atok-2", refresh_token="rtok", expires_in=1800,
        ),
    )
    fresh = mgr.force_exchange()
    assert fresh is not None
    assert seen and seen[0].access_token == "atok-2"


def test_start_and_stop_token_threads():
    store = {"s": _fresh_session()}
    mgr = build_token_manager(
        _cfg(),
        notifier=None,
        load_session=lambda: store.get("s"),
        save_session=lambda s: store.__setitem__("s", s),
        now=lambda: _NOW,
    )
    stop = threading.Event()
    threads = start_token_threads(mgr, stop)
    assert len(threads) == 2
    assert all(t.daemon for t in threads)
    assert all(t.is_alive() for t in threads)
    stop_token_threads(mgr, stop, threads, timeout=5.0)
    assert not any(t.is_alive() for t in threads)
