"""Tests for the `schwab mcp --stdio` transparent bridge.

The bridge is a dumb pipe: it must forward JSON-RPC frames untouched in
both directions, drop transport-level ``Exception`` items it can't forward,
and tear itself down when either side disconnects. It owns no tokens, opens
no Schwab connection, and starts no server — those invariants are enforced
structurally (it only ever touches the four streams handed to it), so the
tests here pin the forwarding/teardown behaviour and the endpoint wiring.
"""

from __future__ import annotations

import anyio
import pytest
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCRequest

from schwab_cli.commands import mcp as mcp_cmd


def _msg(id_: int, method: str) -> SessionMessage:
    return SessionMessage(JSONRPCMessage(JSONRPCRequest(jsonrpc="2.0", id=id_, method=method)))


class _Wiring:
    """Four in-memory streams standing in for the real stdio/http transports.

    ``_run_bridge`` reads client→daemon frames from ``stdio_read`` and writes
    them to ``http_write``; it reads daemon→client frames from ``http_read``
    and writes them to ``stdio_write``. The test drives the far ends.
    """

    def __init__(self) -> None:
        self.client_send, self.stdio_read = anyio.create_memory_object_stream(8)
        self.stdio_write, self.client_recv = anyio.create_memory_object_stream(8)
        self.daemon_send, self.http_read = anyio.create_memory_object_stream(8)
        self.http_write, self.daemon_recv = anyio.create_memory_object_stream(8)

    def bridge_args(self) -> tuple:
        return (self.stdio_read, self.stdio_write, self.http_read, self.http_write)


@pytest.mark.anyio
async def test_forwards_client_to_daemon() -> None:
    w = _Wiring()
    async with anyio.create_task_group() as tg:
        tg.start_soon(mcp_cmd._run_bridge, *w.bridge_args())
        sent = _msg(1, "initialize")
        await w.client_send.send(sent)
        with anyio.fail_after(1):
            got = await w.daemon_recv.receive()
        assert got is sent
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_forwards_daemon_to_client() -> None:
    w = _Wiring()
    async with anyio.create_task_group() as tg:
        tg.start_soon(mcp_cmd._run_bridge, *w.bridge_args())
        sent = _msg(2, "notifications/message")
        await w.daemon_send.send(sent)
        with anyio.fail_after(1):
            got = await w.client_recv.receive()
        assert got is sent
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_skips_exception_items_but_keeps_pipe_open() -> None:
    w = _Wiring()
    async with anyio.create_task_group() as tg:
        tg.start_soon(mcp_cmd._run_bridge, *w.bridge_args())
        # A transport-level parse error surfaces as an Exception on the read
        # stream; it can't be forwarded (the sink only accepts SessionMessage)
        # so the bridge must drop it without dying.
        await w.client_send.send(ValueError("bad frame"))
        follow = _msg(3, "ping")
        await w.client_send.send(follow)
        with anyio.fail_after(1):
            got = await w.daemon_recv.receive()
        assert got is follow  # the Exception was skipped, next frame flows
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_bridge_exits_when_client_disconnects() -> None:
    w = _Wiring()
    # Closing the client's send end EOFs the stdio read side; the bridge must
    # tear down both pumps and return (so the client process can exit).
    await w.client_send.aclose()
    with anyio.fail_after(1):
        await mcp_cmd._run_bridge(*w.bridge_args())


@pytest.mark.anyio
async def test_bridge_exits_when_daemon_disconnects() -> None:
    w = _Wiring()
    await w.daemon_send.aclose()
    with anyio.fail_after(1):
        await mcp_cmd._run_bridge(*w.bridge_args())


def test_endpoint_defaults_to_local_daemon(monkeypatch) -> None:
    monkeypatch.delenv("SCHWAB_DAEMON_URL", raising=False)
    assert mcp_cmd._mcp_endpoint() == "http://127.0.0.1:7234/mcp"


def test_endpoint_respects_daemon_url_env(monkeypatch) -> None:
    monkeypatch.setenv("SCHWAB_DAEMON_URL", "http://127.0.0.1:9999/")
    assert mcp_cmd._mcp_endpoint() == "http://127.0.0.1:9999/mcp"


def test_run_bridge_exits_nonzero_when_daemon_unreachable(monkeypatch) -> None:
    monkeypatch.setattr(mcp_cmd, "probe_daemon", lambda url: False)
    called = False

    def _should_not_run() -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(anyio, "run", lambda *a, **k: _should_not_run())
    with pytest.raises(SystemExit) as exc:
        mcp_cmd.run_stdio_bridge()
    assert exc.value.code == 1
    assert called is False  # never attempted the bridge when daemon is down


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
