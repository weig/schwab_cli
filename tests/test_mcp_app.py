"""Tests for the SchwabMcpServer tool handlers.

Exercises the tool handlers directly by calling the private
coroutine methods — avoids needing an actual MCP client for the
assertions while still walking the same code paths a live MCP
client would trigger.
"""

from __future__ import annotations

import asyncio
import io
import json
from unittest.mock import patch

import pytest

from schwab_cli.mcp_server.app import (
    _DEFAULT_IDLE_LINGER_S,
    SchwabMcpServer,
    _idle_linger_default,
)
from schwab_cli.mcp_server.logbook import LogBook


class _FakeSession:
    access_token = "atok"
    refresh_token = "rtok"
    expires_at = 9_000_000_000
    refresh_token_expires_at = 9_000_000_000


class _FakeClient:
    """Minimal stand-in for SchwabClient — methods override the
    REST-calling helpers via module-level patch in each test."""

    @property
    def session(self):
        return _FakeSession()


def _make_server() -> SchwabMcpServer:
    buf = io.StringIO()
    logbook = LogBook(stream=buf)
    return SchwabMcpServer(_FakeClient(), logbook)


def _call(coro) -> list:
    return asyncio.run(coro)


# ---- idle-linger env default ------------------------------------------


@pytest.mark.parametrize(
    "env, expected",
    [
        (None, _DEFAULT_IDLE_LINGER_S),   # unset -> default
        ("0", 0.0),                       # explicit close-immediately
        ("10", 10.0),                     # explicit seconds
        ("12.5", 12.5),                   # float
        ("nonsense", _DEFAULT_IDLE_LINGER_S),  # invalid -> default, no crash
        ("-5", _DEFAULT_IDLE_LINGER_S),   # negative -> default
    ],
)
def test_idle_linger_default_parses_env(monkeypatch, env, expected):
    if env is None:
        monkeypatch.delenv("SCHWAB_STREAMER_IDLE_LINGER_S", raising=False)
    else:
        monkeypatch.setenv("SCHWAB_STREAMER_IDLE_LINGER_S", env)
    assert _idle_linger_default() == expected


# ---- get_quote --------------------------------------------------------


def test_get_quote_returns_json_payload():
    server = _make_server()
    fake_quotes = {"NVDA": {"symbol": "NVDA", "lastPrice": 250.1}}
    with patch(
        "schwab_cli.service.quotes.QuoteService.get_quote_payload",
        return_value=fake_quotes,
    ):
        result = _call(server._tool_get_quote({"symbols": ["NVDA"]}))
    assert len(result) == 1
    data = json.loads(result[0].text)
    assert data["NVDA"]["lastPrice"] == 250.1


def test_get_quote_upcases_symbols():
    server = _make_server()
    captured = {}

    def fake_quotes(symbols):
        captured["symbols"] = list(symbols)
        return {}

    with patch(
        "schwab_cli.service.quotes.QuoteService.get_quote_payload",
        side_effect=fake_quotes,
    ):
        _call(server._tool_get_quote({"symbols": ["nvda", "aapl"]}))
    assert captured["symbols"] == ["NVDA", "AAPL"]


def test_get_quote_auth_error_returns_text_not_raise():
    from schwab_cli.service.auth import NotAuthenticated

    server = _make_server()
    with patch(
        "schwab_cli.service.quotes.QuoteService.get_quote_payload",
        side_effect=NotAuthenticated("no session"),
    ):
        result = _call(server._tool_get_quote({"symbols": ["NVDA"]}))
    assert len(result) == 1
    assert "schwab error" in result[0].text
    assert "NotAuthenticated" in result[0].text


def test_get_quote_empty_symbols_errors():
    server = _make_server()
    result = _call(server._tool_get_quote({"symbols": []}))
    assert "empty" in result[0].text


def test_get_quote_rejects_non_list():
    server = _make_server()
    result = _call(server._tool_get_quote({"symbols": "NVDA"}))
    assert "list of strings" in result[0].text


# ---- get_chain --------------------------------------------------------


def test_get_chain_happy_path():
    server = _make_server()
    # The service returns an already-shaped envelope; the tool serializes
    # it unchanged. Use a representative envelope so the JSON the tool
    # emits matches the pre-refactor (shape_envelope) output.
    fake_envelope = {
        "symbol": "AMZN",
        "underlying": {"last": 255.0, "change": 0, "percentChange": 0},
        "contracts": [],
    }
    with patch(
        "schwab_cli.service.chains.ChainsService.get_chain_envelope",
        return_value=fake_envelope,
    ):
        result = _call(server._tool_get_chain({
            "symbol": "AMZN",
            "expiry": "2026-05-01",
            "strike_count": 10,
        }))
    data = json.loads(result[0].text)
    assert data["symbol"] == "AMZN"
    assert data["underlying"]["last"] == 255.0


def test_get_chain_auth_error_returns_text_not_raise():
    from schwab_cli.api.client import SessionExpired

    server = _make_server()
    with patch(
        "schwab_cli.service.chains.ChainsService.get_chain_envelope",
        side_effect=SessionExpired("expired"),
    ):
        result = _call(server._tool_get_chain({
            "symbol": "AMZN", "expiry": "2026-05-01",
        }))
    assert len(result) == 1
    assert "schwab error" in result[0].text
    assert "SessionExpired" in result[0].text


def test_get_chain_requires_symbol_and_expiry():
    server = _make_server()
    result = _call(server._tool_get_chain({"symbol": "AMZN"}))
    assert "required" in result[0].text


def test_get_chain_rejects_bad_expiry():
    server = _make_server()
    result = _call(server._tool_get_chain({
        "symbol": "AMZN", "expiry": "260501",
    }))
    assert "invalid expiry" in result[0].text


# ---- server_status ----------------------------------------------------


def test_server_status_shape():
    server = _make_server()
    result = _call(server._tool_server_status())
    data = json.loads(result[0].text)
    assert data["server_name"] == "schwab"
    assert "subscription_summary" in data
    # Empty manager → no sessions.
    assert data["subscription_summary"]["session_count"] == 0


def test_server_status_reflects_live_subscription_state():
    server = _make_server()
    # Simulate an active subscription.
    server.subscription_manager.add("s1", "t1", "LEVELONE_EQUITIES", ["NVDA"])
    result = _call(server._tool_server_status())
    data = json.loads(result[0].text)
    summary = data["subscription_summary"]
    assert summary["session_count"] == 1
    assert summary["subscription_count"] == 1


# ---- list_tools has the expected tools --------------------------------


def test_server_exposes_expected_tool_names():
    server = _make_server()
    # Server.list_tools() via the decorator registers a handler;
    # poke at the internal server's registered tool list.
    from mcp.types import ListToolsRequest
    handler = server._server.request_handlers.get(ListToolsRequest)
    assert handler is not None
    result = _call(handler(ListToolsRequest(method="tools/list")))
    names = {t.name for t in result.root.tools}
    assert names == {
        "get_quote", "get_chain", "stream_quote", "server_status",
        "dataset.history", "dataset.iv_rank", "dataset.status",
    }


# ---- stream_quote cleanup on client disconnect ------------------------


class _RecordingBridge:
    """Minimal fake bridge — records add/remove calls and returns a
    real asyncio.Queue so the stream loop can block on `queue.get()`.
    """

    def __init__(self) -> None:
        self.add_calls: list[tuple] = []
        self.remove_calls: list[tuple] = []
        self._queue: asyncio.Queue = asyncio.Queue()

    async def add_subscription(self, session, token, service, symbols):
        self.add_calls.append((session, token, service, tuple(symbols)))
        return self._queue

    async def remove_subscription(self, session, token):
        # Real bridge awaits a lock (plus network unsubscribe); mimic
        # the suspension point so a cancelled cancel-scope has a chance
        # to inject CancelledError here. Without a real await the bug
        # wouldn't reproduce.
        await asyncio.sleep(0)
        self.remove_calls.append((session, token))


def test_stream_quote_cleans_up_when_anyio_scope_cancelled():
    """Regression: MCP client disconnect triggers an anyio
    task-group cancel. The tool's cleanup must complete even though
    every subsequent `await` inside the cancelled scope would
    otherwise re-raise CancelledError immediately, leaking the
    subscription.
    """
    import anyio
    from mcp.shared.context import RequestContext
    from mcp.server.lowlevel.server import request_ctx

    server = _make_server()
    bridge = _RecordingBridge()
    server._bridge = bridge  # type: ignore[attr-defined]

    fake_ctx = RequestContext(
        request_id=1,
        meta=None,
        session=None,
        lifespan_context=None,
        request=None,
    )

    async def driver():
        async with anyio.create_task_group() as tg:
            async def run_tool():
                token = request_ctx.set(fake_ctx)
                try:
                    await server._tool_stream_quote({"symbols": ["NVDA"]})
                finally:
                    request_ctx.reset(token)

            tg.start_soon(run_tool)
            # Let the tool enter its queue.get() wait.
            await anyio.sleep(0.05)
            # Simulate what mcp Server.run does on client disconnect:
            # cancel the whole task group's scope.
            tg.cancel_scope.cancel()

    anyio.run(driver)

    assert bridge.add_calls, "add_subscription should have been called"
    assert bridge.remove_calls, (
        "remove_subscription must be called even when the surrounding "
        "anyio scope is cancelled — otherwise the subscription leaks"
    )
    session_id, token = bridge.remove_calls[0]
    assert (session_id, token) == bridge.add_calls[0][:2]


# ---- auth subprocess timeout resolver ---------------------------------


def _resolver_logbook() -> LogBook:
    return LogBook(stream=io.StringIO())


def test_resolve_auth_timeout_reads_config_and_adds_buffer():
    """Outer kill must be longer than the inner auto_login budget so
    webauto isn't SIGKILLed mid-flow. Buffer covers the post-webauto
    token exchange + session.json write."""
    from schwab_cli.mcp_server import app as app_module

    fake_cfg = type(
        "Cfg", (),
        {"auto_login_timeout_seconds": 250},
    )()
    with patch.object(
        app_module.config_module, "load", return_value=fake_cfg,
    ):
        timeout = app_module._resolve_auth_subprocess_timeout(
            _resolver_logbook(),
        )
    assert timeout == 250 + app_module._AUTH_SUBPROCESS_BUFFER_SECONDS


def test_resolve_auth_timeout_falls_back_when_config_missing():
    """No config on disk yet (e.g. fresh machine). Monitor still
    starts — just with the module default envelope."""
    from schwab_cli.mcp_server import app as app_module
    from schwab_cli.mcp_server.auth_monitor import (
        DEFAULT_SUBPROCESS_TIMEOUT_SECONDS,
    )

    with patch.object(app_module.config_module, "load", return_value=None):
        timeout = app_module._resolve_auth_subprocess_timeout(
            _resolver_logbook(),
        )
    assert timeout == DEFAULT_SUBPROCESS_TIMEOUT_SECONDS


def test_resolve_auth_timeout_falls_back_on_config_error():
    """Malformed config shouldn't crash the daemon — fall back to
    the default and log a warning so the operator can see it."""
    from schwab_cli.mcp_server import app as app_module
    from schwab_cli.mcp_server.auth_monitor import (
        DEFAULT_SUBPROCESS_TIMEOUT_SECONDS,
    )

    def _boom():
        raise app_module.config_module.ConfigError("bad json")

    buf = io.StringIO()
    lb = LogBook(stream=buf)
    with patch.object(app_module.config_module, "load", side_effect=_boom):
        timeout = app_module._resolve_auth_subprocess_timeout(lb)
    assert timeout == DEFAULT_SUBPROCESS_TIMEOUT_SECONDS
    assert "auth_monitor.config_load_failed" in buf.getvalue()
