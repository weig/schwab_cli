"""Tests for the startup-time refresh-expired auto-login path.

Only exercises the helper function — running the full daemon in a
CliRunner would block on uvicorn. The helper is the whole
surface area the CLI adds on top of the existing session-check
logic, so this covers the behaviour change without the
daemon-lifecycle complexity.
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from schwab_cli.cli import app
from schwab_cli.commands.mcp import _attempt_startup_autologin
from schwab_cli.mcp_server.auth_monitor import AuthMonitorResult
from schwab_cli.mcp_server.logbook import LogBook
from schwab_cli.notify import Notifier
from schwab_cli.notify import config as notify_config
from schwab_cli.session import Session


def _make_logbook_and_notifier():
    buf = io.StringIO()
    lb = LogBook(stream=buf)
    n = Notifier(notify_config.NotificationConfig(), logbook=lb)
    return buf, lb, n


def _fresh_session() -> Session:
    return Session(
        access_token="new_at",
        refresh_token="new_rt",
        expires_at=9_000_000_000,
        refresh_token_expires_at=9_000_000_000,
    )


class _FakeMonitor:
    """Stubs ``AuthMonitor`` for startup tests — accepts the same
    positional args and returns a canned ``AuthMonitorResult``."""

    result: AuthMonitorResult

    def __init__(self, *a, **k):
        # Pull the canned result off the class; tests set it per-test.
        pass

    async def run_once(self, *, reason):
        return type(self).result


def test_autologin_success_returns_fresh_session():
    _, lb, n = _make_logbook_and_notifier()
    _FakeMonitor.result = AuthMonitorResult(
        ok=True, stderr_tail="", duration_sec=2.5,
    )
    result = _attempt_startup_autologin(
        lb, n,
        monitor_cls=_FakeMonitor,
        session_loader=_fresh_session,
    )
    assert result is not None
    assert result.access_token == "new_at"


def test_autologin_failure_returns_none():
    _, lb, n = _make_logbook_and_notifier()
    _FakeMonitor.result = AuthMonitorResult(
        ok=False, stderr_tail="401: bad creds", duration_sec=1.0,
    )
    result = _attempt_startup_autologin(
        lb, n,
        monitor_cls=_FakeMonitor,
        session_loader=_fresh_session,  # won't be called, but must be a callable
    )
    assert result is None


def test_sse_flag_still_launches_http_daemon():
    """The legacy launchd plist bakes in ``mcp --sse``. After dropping
    stdio, ``--sse`` must remain an accepted no-op that still starts the
    (only) Streamable HTTP transport."""
    runner = CliRunner()

    fresh = Session(
        access_token="at", refresh_token="rt",
        expires_at=9_000_000_000,
        refresh_token_expires_at=9_000_000_000,
    )

    captured: dict[str, object] = {}

    class _FakeServer:
        def __init__(self, *a, **k):
            pass

        async def run_http(self, host, port):
            captured["host"] = host
            captured["port"] = port

    with patch("schwab_cli.commands.mcp.config_module.load", return_value=object()), \
         patch("schwab_cli.commands.mcp.load_session", return_value=fresh), \
         patch("schwab_cli.commands.mcp.SchwabClient"), \
         patch("schwab_cli.commands.mcp.SchwabMcpServer", _FakeServer):
        result = runner.invoke(app, ["mcp", "--sse", "--no-log-file"])

    assert result.exit_code == 0, result.output
    # run_http was reached (not stdio) and bound the default address.
    assert captured == {"host": "127.0.0.1", "port": 7234}


def test_autologin_session_load_failure_returns_none():
    """Subprocess reports success but session.json wasn't updated
    (or was deleted). We must treat this as failure."""
    _, lb, n = _make_logbook_and_notifier()
    _FakeMonitor.result = AuthMonitorResult(ok=True)
    result = _attempt_startup_autologin(
        lb, n,
        monitor_cls=_FakeMonitor,
        session_loader=lambda: None,
    )
    assert result is None
