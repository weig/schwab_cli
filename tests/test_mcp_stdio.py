"""Tests for the stdio MCP server (`schwab mcp --stdio`) and its remote bridge."""
from __future__ import annotations

import anyio
import pytest

from schwab_cli.commands import mcp as mcp_cmd
from schwab_cli.mcp_server import remote_bridge as rb


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ---- _mcp_endpoint ----------------------------------------------------


def test_endpoint_defaults(monkeypatch):
    monkeypatch.delenv("SCHWAB_DAEMON_URL", raising=False)
    assert mcp_cmd._mcp_endpoint() == "http://127.0.0.1:7234/mcp"


def test_endpoint_respects_env(monkeypatch):
    monkeypatch.setenv("SCHWAB_DAEMON_URL", "http://127.0.0.1:9999/")
    assert mcp_cmd._mcp_endpoint() == "http://127.0.0.1:9999/mcp"


# ---- RemoteStreamerBridge --------------------------------------------


@pytest.mark.anyio
async def test_remote_bridge_forwards_updates(monkeypatch):
    async def fake_stream(symbols, *, mcp_url, on_decoded):
        on_decoded({"SPY": {"last": 1.0}})
        on_decoded({"SPY": {"last": 2.0}})
        await anyio.sleep_forever()  # real one blocks until cancelled

    monkeypatch.setattr(rb, "stream_quotes_via_mcp", fake_stream)
    bridge = rb.RemoteStreamerBridge("http://x/mcp")
    q = await bridge.add_subscription("s", "t", "LEVELONE_EQUITIES", ["SPY"])
    with anyio.fail_after(1):
        a = await q.get()
        b = await q.get()
    assert a["SPY"]["last"] == 1.0 and b["SPY"]["last"] == 2.0
    await bridge.close()


@pytest.mark.anyio
async def test_remote_bridge_surfaces_daemon_unreachable(monkeypatch):
    async def fake_stream(symbols, *, mcp_url, on_decoded):
        raise rb.McpUnreachable("daemon down")

    monkeypatch.setattr(rb, "stream_quotes_via_mcp", fake_stream)
    bridge = rb.RemoteStreamerBridge("http://x/mcp")
    q = await bridge.add_subscription("s", "t", "LEVELONE_EQUITIES", ["SPY"])
    with anyio.fail_after(1):
        got = await q.get()
    assert "error" in got and "unreachable" in got["error"]
    await bridge.close()


@pytest.mark.anyio
async def test_remote_bridge_remove_cancels(monkeypatch):
    async def fake_stream(symbols, *, mcp_url, on_decoded):
        await anyio.sleep_forever()

    monkeypatch.setattr(rb, "stream_quotes_via_mcp", fake_stream)
    bridge = rb.RemoteStreamerBridge("http://x/mcp")
    await bridge.add_subscription("s", "t", "LEVELONE_EQUITIES", ["SPY"])
    assert ("s", "t") in bridge._tasks
    await bridge.remove_subscription("s", "t")
    assert ("s", "t") not in bridge._tasks


# ---- SchwabMcpServer injected bridge ---------------------------------


def test_server_uses_injected_bridge():
    """An injected bridge must be used verbatim — no local StreamerBridge
    (which would open a second Schwab stream) is constructed."""
    from schwab_cli.mcp_server.app import SchwabMcpServer
    from schwab_cli.mcp_server.logbook import LogBook
    from schwab_cli.notify import Notifier
    from schwab_cli.notify import config as notify_config

    sentinel = rb.RemoteStreamerBridge("http://x/mcp")
    server = SchwabMcpServer(
        client=object(),  # never touched without a REST/stream call
        logbook=LogBook(),
        notifier=Notifier(notify_config.NotificationConfig()),
        bridge=sentinel,
    )
    assert server._bridge is sentinel


# ---- run_stdio_server error paths ------------------------------------


def test_run_stdio_server_exits_without_config(monkeypatch):
    monkeypatch.setattr("schwab_cli.config.load", lambda: None)
    with pytest.raises(SystemExit) as e:
        mcp_cmd.run_stdio_server()
    assert e.value.code == 1


def test_run_stdio_server_exits_without_session(monkeypatch):
    monkeypatch.setattr("schwab_cli.config.load", lambda: object())
    monkeypatch.setattr("schwab_cli.session.load", lambda: None)
    with pytest.raises(SystemExit) as e:
        mcp_cmd.run_stdio_server()
    assert e.value.code == 1
