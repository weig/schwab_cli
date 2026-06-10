"""Spec tests for the daemon's /auth/* surface + token-rotation handoff.

The endpoints are thin wrappers over an attached TokenManager:

* ``POST /auth/refresh`` — single-flight on-demand exchange; 200 with
  state on success, 503 on failure (incl. recovery-pending / no manager).
* ``GET /auth/status``  — TokenManager.state() snapshot.

``schedule_session_replaced`` is the cross-thread bridge the
TokenManager's threads call after replacing the session: it rebinds the
client's in-memory session and reconnects the streamer ONLY on a full
rotation (refresh token changed) — an access-only exchange every ~15min
must NOT bounce the shared websocket.
"""
from __future__ import annotations

import asyncio
import io

from schwab_cli.mcp_server.app import SchwabMcpServer
from schwab_cli.mcp_server.logbook import LogBook
from schwab_cli.session import Session

_NOW = 1_700_000_000


def _session(access_token="atok", refresh_token="rtok") -> Session:
    return Session(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=_NOW + 1800,
        refresh_token_expires_at=_NOW + 7 * 24 * 3600,
    )


class _FakeClient:
    def __init__(self) -> None:
        self._session = _session()

    @property
    def session(self):
        return self._session


class _FakeBridge:
    def __init__(self) -> None:
        self.reconnects = 0

    async def reconnect_after_rotation(self) -> None:
        self.reconnects += 1

    async def close(self) -> None:
        pass


class _FakeTokenManager:
    def __init__(self, *, fresh: Session | None) -> None:
        self.fresh = fresh
        self.calls = 0

    def force_exchange(self):
        self.calls += 1
        return self.fresh

    def state(self) -> dict:
        return {
            "access_expires_at": _NOW + 1800,
            "refresh_token_expires_at": _NOW + 7 * 24 * 3600,
            "recovery_pending": False,
            "manual_auth_required": False,
            "auto_login_enabled": True,
        }


def _server() -> tuple[SchwabMcpServer, _FakeClient, _FakeBridge]:
    client = _FakeClient()
    server = SchwabMcpServer(client, LogBook(stream=io.StringIO()))
    bridge = _FakeBridge()
    server._bridge = bridge
    return server, client, bridge


# ---------------------------------------------------------------------------
# /auth endpoints
# ---------------------------------------------------------------------------


def test_auth_status_returns_state():
    server, _, _ = _server()
    server.attach_token_manager(_FakeTokenManager(fresh=_session()))
    resp = asyncio.run(server._auth_status(None))
    assert resp.status_code == 200
    body = resp.body.decode()
    assert "recovery_pending" in body


def test_auth_refresh_success_returns_200_and_calls_manager():
    server, _, _ = _server()
    tm = _FakeTokenManager(fresh=_session(access_token="atok-2"))
    server.attach_token_manager(tm)
    resp = asyncio.run(server._auth_refresh(None))
    assert resp.status_code == 200
    assert tm.calls == 1
    assert b'"ok": true' in resp.body or b'"ok":true' in resp.body


def test_auth_refresh_failure_returns_503():
    server, _, _ = _server()
    server.attach_token_manager(_FakeTokenManager(fresh=None))
    resp = asyncio.run(server._auth_refresh(None))
    assert resp.status_code == 503


def test_auth_endpoints_without_manager_return_503():
    server, _, _ = _server()
    assert asyncio.run(server._auth_refresh(None)).status_code == 503
    assert asyncio.run(server._auth_status(None)).status_code == 503


# ---------------------------------------------------------------------------
# Session handoff
# ---------------------------------------------------------------------------


def test_handoff_access_only_rebinds_without_streamer_reconnect():
    server, client, bridge = _server()
    fresh = _session(access_token="atok-2", refresh_token="rtok")  # same rtok
    asyncio.run(server.handle_session_replaced(fresh))
    assert client._session.access_token == "atok-2"
    assert bridge.reconnects == 0


def test_handoff_full_rotation_reconnects_streamer():
    server, client, bridge = _server()
    fresh = _session(access_token="atok-2", refresh_token="rtok-NEW")
    asyncio.run(server.handle_session_replaced(fresh))
    assert client._session.refresh_token == "rtok-NEW"
    assert bridge.reconnects == 1


def test_handoff_streamer_failure_does_not_raise():
    server, client, bridge = _server()

    async def boom():
        raise RuntimeError("socket exploded")

    bridge.reconnect_after_rotation = boom
    fresh = _session(refresh_token="rtok-NEW")
    asyncio.run(server.handle_session_replaced(fresh))  # must not raise
    assert client._session.refresh_token == "rtok-NEW"


def test_schedule_without_running_loop_rebinds_directly():
    server, client, _ = _server()
    fresh = _session(access_token="atok-3")
    server.schedule_session_replaced(fresh)  # no event loop running
    assert client._session.access_token == "atok-3"


def test_schedule_falls_back_when_loop_stops_mid_flight():
    """TOCTOU: is_running() passed but the loop dies before
    run_coroutine_threadsafe lands — the handoff must degrade to a bare
    rebind, never raise into the TokenManager thread."""
    from unittest.mock import MagicMock, patch

    server, client, _ = _server()
    live_looking_loop = MagicMock()
    live_looking_loop.is_running.return_value = True
    server._loop = live_looking_loop
    fresh = _session(access_token="atok-4")
    with patch(
        "asyncio.run_coroutine_threadsafe",
        side_effect=RuntimeError("Event loop is closed"),
    ):
        server.schedule_session_replaced(fresh)  # must not raise
    assert client._session.access_token == "atok-4"
