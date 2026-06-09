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


def _make_bridge(*, idle_linger_s: float = 0.0):
    buf = io.StringIO()
    lb = LogBook(stream=buf)
    mgr = SubscriptionManager()
    bridge = StreamerBridge(
        _FakeClient(), lb, mgr, idle_linger_s=idle_linger_s,
    )
    return bridge, buf, mgr


def _patches():
    """The two patches every bridge test needs: fake streamer info +
    fake Streamer class. Returns a tuple for ``with``."""
    return (
        patch(
            "schwab_cli.mcp_server.streamer_bridge.fetch_streamer_info",
            return_value=_FAKE_INFO,
        ),
        patch(
            "schwab_cli.mcp_server.streamer_bridge.Streamer",
            _FakeStreamer,
        ),
    )


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


# ---- streamer_info cache (Phase 2) -----------------------------------------


def test_ensure_connected_caches_streamer_info():
    """A reconnect (e.g. after the linger closes the socket) reuses the
    cached streamer_info instead of re-hitting /userPreference."""
    bridge, _, _ = _make_bridge(idle_linger_s=0.0)  # close immediately

    async def run():
        with patch(
            "schwab_cli.mcp_server.streamer_bridge.fetch_streamer_info",
            return_value=_FAKE_INFO,
        ) as fetch_mock, patch(
            "schwab_cli.mcp_server.streamer_bridge.Streamer",
            _FakeStreamer,
        ):
            # connect → close → connect again (two open cycles)
            await bridge.add_subscription("s1", "t1", "LEVELONE_EQUITIES", ["NVDA"])
            await bridge.remove_subscription("s1", "t1")  # closes (linger 0)
            await bridge.add_subscription("s2", "t2", "LEVELONE_EQUITIES", ["AAPL"])
            await bridge.remove_subscription("s2", "t2")
            # userPreference fetched once, reused on the second open.
            assert fetch_mock.call_count == 1

    asyncio.run(run())


def test_rotation_refetches_streamer_info():
    """Token rotation invalidates the cached streamer_info so the next
    connect re-fetches (guards against stale socket metadata after a
    long-lived daemon session)."""
    bridge, _, _ = _make_bridge(idle_linger_s=0.0)

    async def run():
        with patch(
            "schwab_cli.mcp_server.streamer_bridge.fetch_streamer_info",
            return_value=_FAKE_INFO,
        ) as fetch_mock, patch(
            "schwab_cli.mcp_server.streamer_bridge.Streamer",
            _FakeStreamer,
        ):
            await bridge.add_subscription("s1", "t1", "LEVELONE_EQUITIES", ["NVDA"])
            assert fetch_mock.call_count == 1
            await bridge.reconnect_after_rotation()  # active sub → reconnects
            # cache cleared by rotation → second fetch on reconnect
            assert fetch_mock.call_count == 2
            await bridge.remove_subscription("s1", "t1")

    asyncio.run(run())


# ---- idle linger (Phase 1) -------------------------------------------------


def _capturing_streamer_class(sink: list):
    class _Capturing(_FakeStreamer):
        def __init__(self, info, token):
            super().__init__(info, token)
            sink.append(self)
    return _Capturing


def test_idle_linger_zero_closes_immediately():
    """idle_linger_s=0 keeps the legacy behavior: the socket closes the
    moment the last subscription is removed."""
    bridge, buf, _ = _make_bridge(idle_linger_s=0.0)
    captured: list = []

    async def run():
        info_p, _ = _patches()
        with info_p, patch(
            "schwab_cli.mcp_server.streamer_bridge.Streamer",
            _capturing_streamer_class(captured),
        ):
            await bridge.add_subscription("s1", "t1", "LEVELONE_EQUITIES", ["NVDA"])
            await bridge.remove_subscription("s1", "t1")
            assert captured[0].close_called is True
            assert bridge.streamer_state() == "idle"

    asyncio.run(run())


def test_remove_last_subscription_with_linger_keeps_socket_open():
    """With a positive linger, removing the last subscription must NOT
    close the socket — it enters the 'lingering' state instead."""
    bridge, _, _ = _make_bridge(idle_linger_s=5.0)
    captured: list = []

    async def run():
        info_p, _ = _patches()
        with info_p, patch(
            "schwab_cli.mcp_server.streamer_bridge.Streamer",
            _capturing_streamer_class(captured),
        ):
            await bridge.add_subscription("s1", "t1", "LEVELONE_EQUITIES", ["NVDA"])
            await bridge.remove_subscription("s1", "t1")
            assert captured[0].close_called is False
            assert bridge.streamer_state() == "lingering"
            await bridge.close()  # teardown: cancels timer + closes

    asyncio.run(run())


def test_resubscribe_within_linger_reuses_socket_no_reconnect():
    """A subscribe arriving during the linger window cancels the pending
    close and reuses the SAME streamer — no reconnect."""
    bridge, _, _ = _make_bridge(idle_linger_s=5.0)
    captured: list = []

    async def run():
        info_p, _ = _patches()
        with info_p, patch(
            "schwab_cli.mcp_server.streamer_bridge.Streamer",
            _capturing_streamer_class(captured),
        ):
            await bridge.add_subscription("s1", "t1", "LEVELONE_EQUITIES", ["NVDA"])
            await bridge.remove_subscription("s1", "t1")  # -> lingering
            await bridge.add_subscription("s2", "t2", "LEVELONE_EQUITIES", ["AAPL"])
            # Exactly one streamer ever created (no reconnect), still open.
            assert len(captured) == 1
            assert captured[0].close_called is False
            assert bridge.streamer_state() == "connected"
            await bridge.remove_subscription("s2", "t2")
            await bridge.close()

    asyncio.run(run())


def test_linger_timer_fires_and_closes_when_still_idle():
    """When the linger elapses with zero subscribers, the socket closes."""
    bridge, buf, _ = _make_bridge(idle_linger_s=0.05)
    captured: list = []

    async def run():
        info_p, _ = _patches()
        with info_p, patch(
            "schwab_cli.mcp_server.streamer_bridge.Streamer",
            _capturing_streamer_class(captured),
        ):
            await bridge.add_subscription("s1", "t1", "LEVELONE_EQUITIES", ["NVDA"])
            await bridge.remove_subscription("s1", "t1")
            assert captured[0].close_called is False  # still lingering
            await asyncio.sleep(0.2)  # let the timer fire
            assert captured[0].close_called is True
            assert bridge.streamer_state() == "idle"

    asyncio.run(run())
    events = [e["event"] for e in _parse_log(buf)]
    assert "streamer.disconnect" in events


def test_reconnect_after_rotation_while_lingering_closes_socket():
    """A token rotation during the linger window (zero subscribers) must
    close the lingering socket rather than leave it idling on the old
    token — the next subscriber reconnects fresh."""
    bridge, _, _ = _make_bridge(idle_linger_s=5.0)
    captured: list = []

    async def run():
        info_p, _ = _patches()
        with info_p, patch(
            "schwab_cli.mcp_server.streamer_bridge.Streamer",
            _capturing_streamer_class(captured),
        ):
            await bridge.add_subscription("s1", "t1", "LEVELONE_EQUITIES", ["NVDA"])
            await bridge.remove_subscription("s1", "t1")  # -> lingering
            assert bridge.streamer_state() == "lingering"
            await bridge.reconnect_after_rotation()
            assert captured[0].close_called is True
            assert bridge.streamer_state() == "idle"
            # Timer was cancelled by the close — no dangling task.
            assert bridge._idle_close_task is None

    asyncio.run(run())


def test_resubscribe_cancels_linger_timer_before_it_fires():
    """A re-subscribe during the window must cancel the timer so the
    socket is NOT closed after the linger elapses."""
    bridge, _, _ = _make_bridge(idle_linger_s=0.1)
    captured: list = []

    async def run():
        info_p, _ = _patches()
        with info_p, patch(
            "schwab_cli.mcp_server.streamer_bridge.Streamer",
            _capturing_streamer_class(captured),
        ):
            await bridge.add_subscription("s1", "t1", "LEVELONE_EQUITIES", ["NVDA"])
            await bridge.remove_subscription("s1", "t1")
            await bridge.add_subscription("s2", "t2", "LEVELONE_EQUITIES", ["AAPL"])
            await asyncio.sleep(0.25)  # past the original linger
            assert len(captured) == 1
            assert captured[0].close_called is False
            assert bridge.streamer_state() == "connected"
            await bridge.remove_subscription("s2", "t2")
            await bridge.close()

    asyncio.run(run())


def test_close_during_linger_cancels_timer_and_closes_socket():
    """Daemon-shutdown teardown: close() while lingering must cancel the
    pending idle timer and close the socket (no task left pending when
    the loop tears down). Idempotent."""
    bridge, _, _ = _make_bridge(idle_linger_s=30.0)
    captured: list = []

    async def run():
        info_p, _ = _patches()
        with info_p, patch(
            "schwab_cli.mcp_server.streamer_bridge.Streamer",
            _capturing_streamer_class(captured),
        ):
            await bridge.add_subscription("s1", "t1", "LEVELONE_EQUITIES", ["NVDA"])
            await bridge.remove_subscription("s1", "t1")  # -> lingering
            assert bridge.streamer_state() == "lingering"
            await bridge.close()
            assert captured[0].close_called is True
            assert bridge.streamer_state() == "idle"
            assert bridge._idle_close_task is None
            await bridge.close()  # idempotent — no raise

    asyncio.run(run())
