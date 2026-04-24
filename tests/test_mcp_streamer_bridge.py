"""Tests for the StreamerBridge — subscribe/unsubscribe logging and
fan-out. Uses a fake Streamer that never touches a real WebSocket.
"""

from __future__ import annotations

import asyncio
import io
import json
from unittest.mock import patch

import pytest

from schwab_cli.api.streamer import StreamerInfo
from schwab_cli.mcp_server.logbook import LogBook
from schwab_cli.mcp_server.streamer_bridge import StreamerBridge
from schwab_cli.mcp_server.subscription import SubscriptionManager


class _FakeSession:
    access_token = "atok"
    refresh_token = "rtok"
    expires_at = 9_000_000_000
    refresh_token_expires_at = 9_000_000_000


class _FakeClient:
    @property
    def session(self):
        return _FakeSession()


class _FakeStreamer:
    """Enough of ``Streamer`` for bridge tests — tracks SUBS/UNSUBS
    calls, skips real WebSocket work."""

    def __init__(self, info, token):
        self.info = info
        self.token = token
        self.subs: list[tuple[str, tuple[str, ...]]] = []
        self.unsubs: list[tuple[str, tuple[str, ...]]] = []
        self.login_called = False
        self.connect_called = False
        self.close_called = False

    async def connect(self):
        self.connect_called = True

    async def login(self):
        self.login_called = True

    async def subscribe(self, *, service, keys, fields):
        self.subs.append((service, tuple(keys)))

    async def unsubscribe(self, *, service, keys):
        self.unsubs.append((service, tuple(keys)))

    async def close(self):
        self.close_called = True

    async def messages(self):
        # No frames — tests don't exercise the reader loop.
        if False:
            yield {}
        return


_FAKE_INFO = StreamerInfo(
    socket_url="wss://x", customer_id="c", correl_id="corr",
    channel="IO", function_id="APIAPP",
)


def _parse_log(buf: io.StringIO) -> list[dict]:
    return [json.loads(l) for l in buf.getvalue().strip().splitlines() if l]


def _make_bridge():
    buf = io.StringIO()
    lb = LogBook(stream=buf)
    mgr = SubscriptionManager()
    bridge = StreamerBridge(_FakeClient(), lb, mgr)
    return bridge, buf, mgr


def test_add_subscription_logs_subscribe_event():
    bridge, buf, mgr = _make_bridge()

    async def run():
        with patch(
            "schwab_cli.mcp_server.streamer_bridge.fetch_streamer_info",
            return_value=_FAKE_INFO,
        ), patch(
            "schwab_cli.mcp_server.streamer_bridge.Streamer",
            _FakeStreamer,
        ):
            await bridge.add_subscription(
                "s1", "t1", "LEVELONE_EQUITIES", ["NVDA"],
            )

    asyncio.run(run())

    events = [e["event"] for e in _parse_log(buf)]
    assert "streamer.connect" in events
    assert "subscribe" in events
    assert "schwab.subs" in events


def test_duplicate_subscription_does_not_resubs_at_schwab():
    bridge, buf, mgr = _make_bridge()
    captured_streamers: list = []

    class _Capturing(_FakeStreamer):
        def __init__(self, info, token):
            super().__init__(info, token)
            captured_streamers.append(self)

    async def run():
        with patch(
            "schwab_cli.mcp_server.streamer_bridge.fetch_streamer_info",
            return_value=_FAKE_INFO,
        ), patch(
            "schwab_cli.mcp_server.streamer_bridge.Streamer",
            _Capturing,
        ):
            await bridge.add_subscription(
                "s1", "t1", "LEVELONE_EQUITIES", ["NVDA"],
            )
            await bridge.add_subscription(
                "s2", "t2", "LEVELONE_EQUITIES", ["NVDA"],
            )

    asyncio.run(run())

    assert len(captured_streamers) == 1
    # Only one SUBS for NVDA across the two adds.
    streamer = captured_streamers[0]
    total_subs = sum(1 for svc, keys in streamer.subs if "NVDA" in keys)
    assert total_subs == 1


def test_remove_last_subscription_closes_streamer():
    bridge, buf, _ = _make_bridge()
    captured: list = []

    class _Capturing(_FakeStreamer):
        def __init__(self, info, token):
            super().__init__(info, token)
            captured.append(self)

    async def run():
        with patch(
            "schwab_cli.mcp_server.streamer_bridge.fetch_streamer_info",
            return_value=_FAKE_INFO,
        ), patch(
            "schwab_cli.mcp_server.streamer_bridge.Streamer",
            _Capturing,
        ):
            await bridge.add_subscription(
                "s1", "t1", "LEVELONE_EQUITIES", ["NVDA"],
            )
            await bridge.remove_subscription("s1", "t1")

    asyncio.run(run())

    assert captured[0].close_called is True
    events = [e["event"] for e in _parse_log(buf)]
    assert "unsubscribe" in events
    assert "streamer.disconnect" in events


def test_drop_session_cleans_up_all_its_subs():
    bridge, buf, _ = _make_bridge()
    captured: list = []

    class _Capturing(_FakeStreamer):
        def __init__(self, info, token):
            super().__init__(info, token)
            captured.append(self)

    async def run():
        with patch(
            "schwab_cli.mcp_server.streamer_bridge.fetch_streamer_info",
            return_value=_FAKE_INFO,
        ), patch(
            "schwab_cli.mcp_server.streamer_bridge.Streamer",
            _Capturing,
        ):
            await bridge.add_subscription(
                "s1", "t1", "LEVELONE_EQUITIES", ["NVDA", "AAPL"],
            )
            await bridge.drop_session("s1")

    asyncio.run(run())

    events = [e["event"] for e in _parse_log(buf)]
    assert "session.drop" in events
