"""Tests for the streamer client.

Pure logic (frame builders, classifiers, login-response parsing)
is covered directly. The WebSocket interaction is exercised through
a tiny mock that mimics ``websockets.connect``'s async API surface
(send/recv + async-iter + close).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from schwab_cli.api.client import ApiError, SchwabClient
from schwab_cli.api.streamer import (
    Streamer,
    StreamerError,
    StreamerInfo,
    StreamerLoginError,
    build_login_request,
    build_logout_request,
    build_subs_request,
    classify_frame,
    fetch_streamer_info,
    is_heartbeat,
    login_response_ok,
)


INFO = StreamerInfo(
    socket_url="wss://streamer-api.schwabapi.com/ws",
    customer_id="cust123",
    correl_id="correl456",
    channel="IO",
    function_id="APIAPP",
)


# ---- frame builders ----------------------------------------------------


def test_build_login_request_has_required_fields():
    req = build_login_request(INFO, "at_tok", "42")
    body = req["requests"][0]
    assert body["service"] == "ADMIN"
    assert body["command"] == "LOGIN"
    assert body["requestid"] == "42"
    assert body["SchwabClientCustomerId"] == "cust123"
    assert body["SchwabClientCorrelId"] == "correl456"
    params = body["parameters"]
    assert params["Authorization"] == "at_tok"
    assert params["SchwabClientChannel"] == "IO"
    assert params["SchwabClientFunctionId"] == "APIAPP"


def test_build_subs_request_joins_keys_with_comma():
    req = build_subs_request(
        INFO,
        service="LEVELONE_EQUITIES",
        keys=["NVDA", "AAPL", "MSFT"],
        fields="0,1,2",
        request_id="7",
    )
    body = req["requests"][0]
    assert body["service"] == "LEVELONE_EQUITIES"
    assert body["command"] == "SUBS"
    assert body["parameters"]["keys"] == "NVDA,AAPL,MSFT"
    assert body["parameters"]["fields"] == "0,1,2"


def test_build_unsubs_request_preserves_command():
    req = build_subs_request(
        INFO,
        service="LEVELONE_EQUITIES",
        keys=["NVDA"],
        fields="0",
        request_id="9",
        command="UNSUBS",
    )
    assert req["requests"][0]["command"] == "UNSUBS"


def test_build_logout_request_shape():
    req = build_logout_request(INFO, "3")
    body = req["requests"][0]
    assert body["service"] == "ADMIN"
    assert body["command"] == "LOGOUT"


# ---- frame classification ---------------------------------------------


def test_classify_data_frame():
    assert classify_frame({"data": [{}]}) == "data"


def test_classify_response_frame():
    assert classify_frame({"response": [{}]}) == "response"


def test_classify_notify_frame():
    assert classify_frame({"notify": [{}]}) == "notify"


def test_classify_unknown():
    assert classify_frame({"junk": 1}) == "unknown"


def test_is_heartbeat_true_for_notify_heartbeat():
    assert is_heartbeat({"notify": [{"heartbeat": "1234"}]}) is True


def test_is_heartbeat_false_for_data_frame():
    assert is_heartbeat({"data": [{}]}) is False


def test_is_heartbeat_false_for_notify_without_heartbeat_key():
    assert is_heartbeat({"notify": [{"service": "X"}]}) is False


def test_login_response_ok_true_for_code_zero():
    frame = {"response": [{
        "service": "ADMIN", "command": "LOGIN",
        "content": {"code": 0, "msg": "server=x"},
    }]}
    assert login_response_ok(frame) is True


def test_login_response_ok_false_for_non_zero_code():
    frame = {"response": [{
        "service": "ADMIN", "command": "LOGIN",
        "content": {"code": 3, "msg": "bad token"},
    }]}
    assert login_response_ok(frame) is False


def test_login_response_ok_false_for_unrelated_response():
    frame = {"response": [{"service": "LEVELONE_EQUITIES", "command": "SUBS"}]}
    assert login_response_ok(frame) is False


# ---- fetch_streamer_info -----------------------------------------------


class _FakeClient:
    """Stand-in for SchwabClient.get that returns canned JSON."""

    def __init__(self, payload):
        self._payload = payload

    def get(self, url, *, params=None):
        return self._payload


def test_fetch_streamer_info_happy_path():
    payload = {"streamerInfo": [{
        "streamerSocketUrl": "wss://x",
        "schwabClientCustomerId": "c",
        "schwabClientCorrelId": "corr",
        "schwabClientChannel": "IO",
        "schwabClientFunctionId": "APIAPP",
    }]}
    info = fetch_streamer_info(_FakeClient(payload))
    assert info.socket_url == "wss://x"
    assert info.customer_id == "c"


def test_fetch_streamer_info_raises_on_missing_block():
    with pytest.raises(StreamerError, match="streamerInfo"):
        fetch_streamer_info(_FakeClient({}))


def test_fetch_streamer_info_raises_on_missing_field():
    payload = {"streamerInfo": [{"streamerSocketUrl": "wss://x"}]}
    with pytest.raises(StreamerError):
        fetch_streamer_info(_FakeClient(payload))


def test_fetch_streamer_info_wraps_api_error():
    class _BrokenClient:
        def get(self, *a, **k):
            raise ApiError("upstream 500")

    with pytest.raises(StreamerError, match="upstream 500"):
        fetch_streamer_info(_BrokenClient())


# ---- Streamer with mock WebSocket -------------------------------------


class FakeWebSocket:
    """Minimal async-iter WebSocket stand-in for Streamer tests.

    Records outgoing sends for assertions; yields queued incoming
    frames when the streamer reads.
    """

    def __init__(self, incoming: list[str]):
        self._incoming = list(incoming)
        self.sent: list[str] = []
        self.closed = False

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def recv(self) -> str:
        if not self._incoming:
            # Simulate EOF — raise so Streamer.login times out or errors.
            await asyncio.sleep(3600)
            raise AssertionError("recv should have been awaited on timeout")
        return self._incoming.pop(0)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._incoming:
            raise StopAsyncIteration
        return self._incoming.pop(0)

    async def close(self):
        self.closed = True


def _make_streamer(ws: FakeWebSocket) -> Streamer:
    async def ws_factory(url):
        return ws
    return Streamer(INFO, "at_tok", ws_factory=ws_factory)


def test_streamer_login_sends_login_frame_and_succeeds():
    login_ack = json.dumps({"response": [{
        "service": "ADMIN", "command": "LOGIN",
        "content": {"code": 0, "msg": "ok"},
    }]})
    ws = FakeWebSocket(incoming=[login_ack])
    s = _make_streamer(ws)

    async def run():
        await s.connect()
        await s.login()

    asyncio.run(run())

    assert len(ws.sent) == 1
    sent = json.loads(ws.sent[0])
    assert sent["requests"][0]["command"] == "LOGIN"


def test_streamer_login_raises_on_failure_code():
    login_ack = json.dumps({"response": [{
        "service": "ADMIN", "command": "LOGIN",
        "content": {"code": 3, "msg": "bad"},
    }]})
    ws = FakeWebSocket(incoming=[login_ack])
    s = _make_streamer(ws)

    async def run():
        await s.connect()
        await s.login()

    with pytest.raises(StreamerLoginError):
        asyncio.run(run())


def test_streamer_subscribe_writes_subs_frame_after_login():
    login_ack = json.dumps({"response": [{
        "service": "ADMIN", "command": "LOGIN",
        "content": {"code": 0},
    }]})
    ws = FakeWebSocket(incoming=[login_ack])
    s = _make_streamer(ws)

    async def run():
        await s.connect()
        await s.login()
        await s.subscribe(service="LEVELONE_EQUITIES", keys=["NVDA"], fields="0,1,2")

    asyncio.run(run())
    # Two sends: LOGIN + SUBS.
    assert len(ws.sent) == 2
    subs = json.loads(ws.sent[1])
    assert subs["requests"][0]["command"] == "SUBS"
    assert subs["requests"][0]["parameters"]["keys"] == "NVDA"


def test_streamer_messages_yields_data_frames():
    login_ack = json.dumps({"response": [{
        "service": "ADMIN", "command": "LOGIN",
        "content": {"code": 0},
    }]})
    data_frame = json.dumps({"data": [{
        "service": "LEVELONE_EQUITIES",
        "content": [{"key": "NVDA", "1": 250.1}],
    }]})
    ws = FakeWebSocket(incoming=[login_ack, data_frame])
    s = _make_streamer(ws)

    received = []

    async def run():
        await s.connect()
        await s.login()
        async for frame in s.messages():
            received.append(frame)

    asyncio.run(run())

    # Only the data frame — login ack was consumed by login().
    assert len(received) == 1
    assert received[0]["data"][0]["content"][0]["key"] == "NVDA"


def test_streamer_close_sends_logout_and_closes_socket():
    login_ack = json.dumps({"response": [{
        "service": "ADMIN", "command": "LOGIN",
        "content": {"code": 0},
    }]})
    ws = FakeWebSocket(incoming=[login_ack])
    s = _make_streamer(ws)

    async def run():
        await s.connect()
        await s.login()
        await s.close()

    asyncio.run(run())

    assert ws.closed is True
    # First send = LOGIN, second = LOGOUT.
    assert len(ws.sent) == 2
    logout = json.loads(ws.sent[1])
    assert logout["requests"][0]["command"] == "LOGOUT"


def test_streamer_subscribe_before_connect_raises():
    s = Streamer(INFO, "at_tok")

    async def run():
        await s.subscribe(service="LEVELONE_EQUITIES", keys=["NVDA"], fields="0")

    with pytest.raises(StreamerError, match="connect"):
        asyncio.run(run())
