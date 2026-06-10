"""Tests for the /api/v1/stream WebSocket endpoint.

Fake bridge, fake verifier, no network. The webauth middleware fronts
the route exactly as in production so the JWT/scope path is exercised
end to end (TestClient's websocket support drives the real ASGI stack).
"""
from __future__ import annotations

import asyncio

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from schwab_cli.server.stream_ws import stream_routes
from schwab_cli.webauth.middleware import WebAuthMiddleware
from schwab_cli.webauth.verify import Principal


class _FakeBridge:
    def __init__(self) -> None:
        self.added: list[tuple[str, str, str, list[str]]] = []
        self.dropped: list[str] = []
        self.queues: list[asyncio.Queue] = []
        self.prefill: list[dict] = []

    async def add_subscription(self, session, token, service, symbols):
        self.added.append((session, token, service, symbols))
        q: asyncio.Queue = asyncio.Queue()
        for item in self.prefill:
            q.put_nowait(item)
        self.queues.append(q)
        return q

    async def drop_session(self, session):
        self.dropped.append(session)


class _GrantVerifier:
    def __init__(self, scopes) -> None:
        self._scopes = frozenset(scopes)

    def verify(self, token: str) -> Principal:
        return Principal(
            provider="auth0", subject="auth0|abc", email=None,
            scopes=frozenset(self._scopes),
        )


def _client(*scopes, bridge=None, has_providers=True) -> tuple[TestClient, _FakeBridge | None]:
    app = Starlette(routes=stream_routes(lambda: bridge))
    wrapped = WebAuthMiddleware(
        app,
        verifier=_GrantVerifier(scopes) if has_providers else None,
        has_providers=has_providers,
        allow=("127.0.0.1",),
        peer_of=lambda scope: "127.0.0.1",
    )
    return TestClient(wrapped), bridge


_AUTH = {"Authorization": "Bearer x.y.z"}


def test_subscribe_and_receive_quote():
    bridge = _FakeBridge()
    bridge.prefill = [{"symbol": "SPY", "last": 600.0}]
    client, _ = _client("streaming", "marketdata", bridge=bridge)
    with client.websocket_connect("/api/v1/stream", headers=_AUTH) as ws:
        ws.send_json({"action": "subscribe", "symbols": ["spy"]})
        ack = ws.receive_json()
        assert ack == {"type": "subscribed", "symbols": ["SPY"]}
        frame = ws.receive_json()
        assert frame["type"] == "quote"
        assert frame["symbol"] == "SPY"
    assert bridge.added[0][2] == "LEVELONE_EQUITIES"
    assert bridge.added[0][3] == ["SPY"]
    assert len(bridge.dropped) == 1  # disconnect unwound the session


def test_streaming_scope_alone_cannot_pull_marketdata():
    """streaming is a transport modifier — without the data scope the
    subscribe is refused (error frame, connection stays open)."""
    bridge = _FakeBridge()
    client, _ = _client("streaming", bridge=bridge)  # NO marketdata
    with client.websocket_connect("/api/v1/stream", headers=_AUTH) as ws:
        ws.send_json({"action": "subscribe", "symbols": ["SPY"]})
        frame = ws.receive_json()
        assert frame["type"] == "error"
        assert "marketdata" in frame["error"]
    assert bridge.added == []


def test_without_streaming_scope_handshake_rejected():
    bridge = _FakeBridge()
    client, _ = _client("marketdata", bridge=bridge)  # NO streaming
    with pytest.raises(Exception):
        with client.websocket_connect("/api/v1/stream", headers=_AUTH):
            pass
    assert bridge.added == []


def test_without_token_middleware_closes_handshake():
    client, _ = _client("streaming", "marketdata", bridge=_FakeBridge())
    with pytest.raises(Exception):
        with client.websocket_connect("/api/v1/stream"):
            pass


def test_no_bridge_closes_1013():
    client, _ = _client("streaming", "marketdata", bridge=None)
    with pytest.raises(Exception):
        with client.websocket_connect("/api/v1/stream", headers=_AUTH):
            pass


def test_bad_frames_get_error_without_closing():
    bridge = _FakeBridge()
    client, _ = _client("streaming", "marketdata", bridge=bridge)
    with client.websocket_connect("/api/v1/stream", headers=_AUTH) as ws:
        ws.send_json({"action": "noop"})
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"action": "subscribe", "symbols": []})
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"action": "subscribe", "symbols": ["A"] * 51})
        err = ws.receive_json()
        assert "max 50" in err["error"]
        # connection still healthy: a valid subscribe works afterwards
        ws.send_json({"action": "subscribe", "symbols": ["SPY"]})
        assert ws.receive_json()["type"] == "subscribed"


def test_legacy_mode_loopback_allows_streaming():
    """No providers configured: loopback callers stream without scopes
    (same legacy contract as the rest of /api/v1)."""
    bridge = _FakeBridge()
    bridge.prefill = [{"symbol": "SPY", "last": 1.0}]
    client, _ = _client(bridge=bridge, has_providers=False)
    with client.websocket_connect("/api/v1/stream") as ws:  # no token
        ws.send_json({"action": "subscribe", "symbols": ["SPY"]})
        assert ws.receive_json()["type"] == "subscribed"
        assert ws.receive_json()["type"] == "quote"


def test_multiple_subscribes_share_one_session():
    bridge = _FakeBridge()
    client, _ = _client("streaming", "marketdata", bridge=bridge)
    with client.websocket_connect("/api/v1/stream", headers=_AUTH) as ws:
        ws.send_json({"action": "subscribe", "symbols": ["SPY"]})
        ws.receive_json()
        ws.send_json({"action": "subscribe", "symbols": ["QQQ"]})
        ws.receive_json()
    sessions = {added[0] for added in bridge.added}
    assert len(sessions) == 1            # one bridge session per connection
    tokens = {added[1] for added in bridge.added}
    assert len(tokens) == 2              # distinct per-subscribe tokens
    assert bridge.dropped == [next(iter(sessions))]
