"""Tests for `schwab server --enable-mcp` (Phase 3c-ii).

`schwab server` (no flags) stays the Phase 2 maintenance-only loop.
`schwab server --enable-mcp` composes the Streamable HTTP MCP server on
top of the always-running maintenance loop:

* the maintenance loop runs in a daemon thread (single proactive
  refresh-token renewer);
* the MCP server runs on the main thread with
  ``auth_monitor_enabled=False``.

All tests use mocking — NO real network, uvicorn, or thread sleeping.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Import guard
# ---------------------------------------------------------------------------
try:
    from schwab_cli.commands import server as server_cmd
    _CMD_MODULE_AVAILABLE = True
except (ModuleNotFoundError, ImportError):
    _CMD_MODULE_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not _CMD_MODULE_AVAILABLE,
    reason="schwab_cli.commands.server not implemented yet",
)

try:
    from schwab_cli.config import Config
except ImportError:
    Config = None  # type: ignore[misc, assignment]

try:
    from schwab_cli.session import Session
except ImportError:
    Session = None  # type: ignore[misc, assignment]

_CFG = Config(
    client_id="cid",
    client_secret="csec",
    redirect_uri="https://127.0.0.1:8443",
) if Config is not None else None


def _future_session() -> "Session":
    """A session whose refresh token is far from expiry (no auto-login)."""
    now = int(time.time())
    return Session(
        access_token="acc",
        refresh_token="ref",
        expires_at=now + 3600,
        refresh_token_expires_at=now + 7 * 24 * 3600,
    )


# ---------------------------------------------------------------------------
# Shared mock setup for the happy path
# ---------------------------------------------------------------------------

def _patch_happy_path(monkeypatch):
    """Patch cfg/session present, MCP server, run_loop, asyncio.run.

    Returns a dict of recording structures the tests inspect.
    """
    monkeypatch.setattr("schwab_cli.config.load", lambda: _CFG)
    monkeypatch.setattr(
        "schwab_cli.session.load", lambda: _future_session()
    )

    # LogBook + Notifier — keep them cheap, no disk/network.
    monkeypatch.setattr(
        "schwab_cli.mcp_server.logbook.LogBook",
        lambda *a, **k: MagicMock(),
    )
    monkeypatch.setattr(
        "schwab_cli.notify.Notifier.from_file",
        classmethod(lambda cls, **k: MagicMock()),
    )
    rec: dict = {
        "server_kwargs": [], "run_http_args": [], "run_loop_kwargs": [],
        "clients": [],
    }

    def _make_client(*a, **k):
        c = MagicMock()
        rec["clients"].append(c)
        return c

    monkeypatch.setattr("schwab_cli.api.client.SchwabClient", _make_client)

    class _FakeServer:
        def __init__(self, client, logbook, *, notifier=None,
                     auth_monitor_enabled=True):
            rec["server_kwargs"].append(
                {"auth_monitor_enabled": auth_monitor_enabled}
            )

        async def run_http(self, host, port):
            rec["run_http_args"].append((host, port))

    monkeypatch.setattr(
        "schwab_cli.mcp_server.app.SchwabMcpServer", _FakeServer
    )

    threads_started: list = []
    real_thread = server_cmd.threading.Thread

    def _record_thread(*args, **kwargs):
        rec["run_loop_kwargs"].append(kwargs.get("kwargs", {}))
        t = real_thread(*args, **kwargs)
        threads_started.append(t)
        return t

    monkeypatch.setattr(server_cmd.threading, "Thread", _record_thread)
    rec["threads"] = threads_started

    # run_loop must not actually loop — return immediately.
    monkeypatch.setattr(
        "schwab_cli.server.maintenance.run_loop",
        lambda *a, **k: None,
    )

    # asyncio.run drives server.run_http synchronously without a loop
    # owning signals.
    def _fake_asyncio_run(coro):
        # Execute the coroutine to record the run_http args, then close.
        try:
            coro.send(None)
        except StopIteration:
            pass
        return None

    monkeypatch.setattr(server_cmd.asyncio, "run", _fake_asyncio_run)
    return rec


# ---------------------------------------------------------------------------
# enable_mcp=True happy path
# ---------------------------------------------------------------------------

@pytest.mark.skipif(Session is None, reason="Session not importable")
class TestEnableMcpHappyPath:
    def test_run_http_invoked_with_host_port(self, monkeypatch):
        rec = _patch_happy_path(monkeypatch)
        server_cmd.run(
            enable_mcp=True, mcp_host="127.0.0.1", mcp_port=7234,
            interval_s=60,
        )
        assert rec["run_http_args"] == [("127.0.0.1", 7234)]

    def test_run_http_honors_custom_host_port(self, monkeypatch):
        rec = _patch_happy_path(monkeypatch)
        server_cmd.run(
            enable_mcp=True, mcp_host="0.0.0.0", mcp_port=9999,
            interval_s=60,
        )
        assert rec["run_http_args"] == [("0.0.0.0", 9999)]

    def test_mcp_server_auth_monitor_disabled(self, monkeypatch):
        rec = _patch_happy_path(monkeypatch)
        server_cmd.run(enable_mcp=True, interval_s=60)
        assert rec["server_kwargs"][0]["auth_monitor_enabled"] is False

    def test_maintenance_thread_started_with_stop_and_sleep(self, monkeypatch):
        rec = _patch_happy_path(monkeypatch)
        server_cmd.run(enable_mcp=True, interval_s=120)

        assert len(rec["threads"]) == 1
        kwargs = rec["run_loop_kwargs"][0]
        assert callable(kwargs["stop"])
        assert callable(kwargs["sleep"])
        assert kwargs["interval_s"] == 120

    def test_maintenance_thread_is_daemon_and_joined(self, monkeypatch):
        rec = _patch_happy_path(monkeypatch)
        server_cmd.run(enable_mcp=True, interval_s=60)

        thread = rec["threads"][0]
        assert thread.daemon is True
        # run_loop returns immediately, so by the time run() returns
        # the thread has finished and the stop flag was set.
        assert not thread.is_alive()

    def test_stop_event_set_on_return(self, monkeypatch):
        """stop callable reports True after run() returns (event set)."""
        rec = _patch_happy_path(monkeypatch)
        server_cmd.run(enable_mcp=True, interval_s=60)
        stop = rec["run_loop_kwargs"][0]["stop"]
        assert stop() is True

    def test_returns_zero(self, monkeypatch):
        _patch_happy_path(monkeypatch)
        assert server_cmd.run(enable_mcp=True, interval_s=60) == 0


# ---------------------------------------------------------------------------
# enable_mcp=True failure paths
# ---------------------------------------------------------------------------

class TestEnableMcpMissingPrereqs:
    def test_no_config_exits_1(self, monkeypatch, capsys):
        monkeypatch.setattr("schwab_cli.config.load", lambda: None)
        with pytest.raises(SystemExit) as exc:
            server_cmd.run(enable_mcp=True, interval_s=60)
        assert exc.value.code == 1
        combined = "".join(capsys.readouterr())
        assert "No config" in combined

    @pytest.mark.skipif(Session is None, reason="Session not importable")
    def test_no_session_exits_1(self, monkeypatch, capsys):
        monkeypatch.setattr("schwab_cli.config.load", lambda: _CFG)
        monkeypatch.setattr("schwab_cli.session.load", lambda: None)
        with pytest.raises(SystemExit) as exc:
            server_cmd.run(enable_mcp=True, interval_s=60)
        assert exc.value.code == 1
        combined = "".join(capsys.readouterr())
        assert "No session" in combined


# ---------------------------------------------------------------------------
# enable_mcp=False — Phase 2 path unchanged (no MCP)
# ---------------------------------------------------------------------------

class TestEnableMcpFalse:
    def test_maintenance_only_does_not_run_http(self, monkeypatch):
        monkeypatch.setattr("schwab_cli.config.load", lambda: _CFG)

        run_http_calls: list = []

        class _FakeServer:
            def __init__(self, *a, **k):
                pass

            async def run_http(self, host, port):
                run_http_calls.append((host, port))

        monkeypatch.setattr(
            "schwab_cli.mcp_server.app.SchwabMcpServer", _FakeServer
        )
        monkeypatch.setattr(
            "schwab_cli.server.maintenance.run_loop",
            lambda *a, **k: None,
        )

        result = server_cmd.run(enable_mcp=False, interval_s=60)
        assert result in (0, None)
        assert run_http_calls == []


# ---------------------------------------------------------------------------
# CLI wiring — `schwab server --enable-mcp` reaches run(enable_mcp=True)
# ---------------------------------------------------------------------------

class TestCLIEnableMcpWiring:
    def test_cli_enable_mcp_passes_through(self, monkeypatch):
        try:
            from schwab_cli.cli import app
        except ImportError:
            pytest.skip("cli not available")
        from typer.testing import CliRunner

        calls: list = []
        monkeypatch.setattr(
            "schwab_cli.commands.server.run",
            lambda **k: calls.append(k) or 0,
        )

        runner = CliRunner()
        result = runner.invoke(
            app,
            ["server", "--enable-mcp", "--mcp-host", "1.2.3.4",
             "--mcp-port", "8765"],
        )
        assert result.exit_code == 0, result.output
        assert len(calls) == 1
        assert calls[0]["enable_mcp"] is True
        assert calls[0]["mcp_host"] == "1.2.3.4"
        assert calls[0]["mcp_port"] == 8765

    def test_cli_bare_server_disables_mcp(self, monkeypatch):
        try:
            from schwab_cli.cli import app
        except ImportError:
            pytest.skip("cli not available")
        from typer.testing import CliRunner

        calls: list = []
        monkeypatch.setattr(
            "schwab_cli.commands.server.run",
            lambda **k: calls.append(k) or 0,
        )

        runner = CliRunner()
        result = runner.invoke(app, ["server"])
        assert result.exit_code == 0, result.output
        assert len(calls) == 1
        assert calls[0]["enable_mcp"] is False

    def test_enable_mcp_help_works(self, monkeypatch):
        try:
            from schwab_cli.cli import app
        except ImportError:
            pytest.skip("cli not available")
        from typer.testing import CliRunner

        # Force a wide terminal so Rich does not wrap option names.
        monkeypatch.setenv("COLUMNS", "240")
        runner = CliRunner()
        result = runner.invoke(app, ["server", "--help"])
        assert result.exit_code == 0
        import re
        clean = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        clean = clean.replace("\n", " ")
        compact = clean.replace(" ", "")
        assert "--enable-mcp" in compact
        assert "--mcp-host" in compact
        assert "--mcp-port" in compact


@pytest.mark.skipif(Session is None, reason="Session not importable")
class TestSessionHandoff:
    """The maintenance loop's notifier must hand a renewed session back to
    the persistent client's in-memory state (regression: otherwise the
    client keeps a refresh token the loop just rotated out)."""

    def _tick(self, action):
        from schwab_cli.server.maintenance import MaintenanceTick
        return MaintenanceTick(action=action, detail="x")

    def test_renewed_tick_updates_client_session(self, monkeypatch):
        rec = _patch_happy_path(monkeypatch)
        fresh = _future_session()
        monkeypatch.setattr("schwab_cli.session.load", lambda: fresh)
        server_cmd.run(enable_mcp=True, interval_s=60)

        client = rec["clients"][0]
        notifier = rec["run_loop_kwargs"][0]["notifier"]
        notifier(self._tick("renewed"))
        assert client._session is fresh

    def test_renew_failed_tick_does_not_touch_client_session(self, monkeypatch):
        rec = _patch_happy_path(monkeypatch)
        server_cmd.run(enable_mcp=True, interval_s=60)

        client = rec["clients"][0]
        sentinel = object()
        client._session = sentinel
        notifier = rec["run_loop_kwargs"][0]["notifier"]
        notifier(self._tick("renew_failed"))  # must not raise / must not reload
        assert client._session is sentinel
