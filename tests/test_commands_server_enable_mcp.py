"""Tests for `schwab server --enable-mcp`.

`schwab server` (no flags) runs the TokenManager daemon (single owner of
the OAuth token pair). `schwab server --enable-mcp` composes the
Streamable HTTP MCP server on top:

* the TokenManager's two tracks run as daemon threads;
* the MCP server runs on the main thread with the manager attached
  (/auth/* routes + session-handoff bridge).

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

def _patch_happy_path(monkeypatch, tmp_path):
    """Patch cfg/session present, MCP server, token runtime, asyncio.run.

    Returns a dict of recording structures the tests inspect.

    Isolation: points ``SCHWAB_CLI_CONFIG_DIR`` at ``tmp_path`` so the
    job scheduler startup (reconcile/apply_reload/write_pidfile,
    ``jobs/.current/state.json``) writes into the tmp dir instead of the
    real ``~/.config/schwab_cli``. ``paths.config_dir()`` reads this env
    var dynamically and ``runtime.jobs_dir``/``current_dir`` resolve
    through it.
    """
    monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
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
        "run_http_args": [],
        "clients": [],
        "attached": [],
        "handoffs": [],
    }

    def _make_client(*a, **k):
        c = MagicMock()
        rec["clients"].append(c)
        return c

    monkeypatch.setattr("schwab_cli.api.client.SchwabClient", _make_client)

    class _FakeServer:
        def __init__(self, client, logbook, *, notifier=None):
            rec["server"] = self

        def attach_token_manager(self, tm):
            rec["attached"].append(tm)

        def schedule_session_replaced(self, fresh):
            rec["handoffs"].append(fresh)

        async def run_http(self, host, port, *, extra_routes=None,
                           asgi_wrap=None, trusted_proxies=None):
            rec.setdefault("run_http_trusted_proxies", []).append(
                trusted_proxies)
            rec["run_http_args"].append((host, port))
            rec.setdefault("run_http_extra_routes", []).append(extra_routes)
            rec.setdefault("run_http_asgi_wrap", []).append(asgi_wrap)

    monkeypatch.setattr(
        "schwab_cli.mcp_server.app.SchwabMcpServer", _FakeServer
    )

    # Token runtime: capture wiring; never start real threads.
    def _fake_build(cfg, *, notifier=None, on_session_replaced=None, **kw):
        rec["tm_cfg"] = cfg
        rec["on_session_replaced"] = on_session_replaced
        tm = MagicMock(name="token_manager")
        rec["tm"] = tm
        return tm

    def _fake_start(mgr, stop):
        rec["tm_started"] = mgr
        rec["stop_event"] = stop
        return ()

    def _fake_stop(mgr, stop, threads, **kw):
        rec["tm_stopped"] = True
        stop.set()

    monkeypatch.setattr(
        "schwab_cli.server.token_runtime.build_token_manager", _fake_build,
    )
    monkeypatch.setattr(
        "schwab_cli.server.token_runtime.start_token_threads", _fake_start,
    )
    monkeypatch.setattr(
        "schwab_cli.server.token_runtime.stop_token_threads", _fake_stop,
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
    def test_run_http_invoked_with_host_port(self, monkeypatch, tmp_path):
        rec = _patch_happy_path(monkeypatch, tmp_path)
        server_cmd.run(
            enable_mcp=True, mcp_host="127.0.0.1", mcp_port=7234,
            interval_s=60,
        )
        assert rec["run_http_args"] == [("127.0.0.1", 7234)]

    def test_run_http_honors_custom_host_port(self, monkeypatch, tmp_path):
        rec = _patch_happy_path(monkeypatch, tmp_path)
        server_cmd.run(
            enable_mcp=True, mcp_host="0.0.0.0", mcp_port=9999,
            interval_s=60,
        )
        assert rec["run_http_args"] == [("0.0.0.0", 9999)]

    def test_token_manager_built_and_attached(self, monkeypatch, tmp_path):
        rec = _patch_happy_path(monkeypatch, tmp_path)
        server_cmd.run(enable_mcp=True, interval_s=60)
        assert rec["tm_cfg"] is _CFG
        assert rec["attached"] == [rec["tm"]]

    def test_token_threads_started_and_stopped(self, monkeypatch, tmp_path):
        rec = _patch_happy_path(monkeypatch, tmp_path)
        server_cmd.run(enable_mcp=True, interval_s=120)
        assert rec["tm_started"] is rec["tm"]
        assert rec.get("tm_stopped") is True

    def test_session_handoff_wired_to_server(self, monkeypatch, tmp_path):
        """The TokenManager's on_session_replaced must route to the MCP
        server's schedule_session_replaced (in-memory rebind + streamer
        reconnect on full rotation)."""
        rec = _patch_happy_path(monkeypatch, tmp_path)
        server_cmd.run(enable_mcp=True, interval_s=60)
        handoff = rec["on_session_replaced"]
        assert callable(handoff)
        fresh = _future_session()
        handoff(fresh)
        assert rec["handoffs"] == [fresh]

    def test_stop_event_set_on_return(self, monkeypatch, tmp_path):
        """The shared stop event is set after run() returns."""
        rec = _patch_happy_path(monkeypatch, tmp_path)
        server_cmd.run(enable_mcp=True, interval_s=60)
        assert rec["stop_event"].is_set() is True

    def test_returns_zero(self, monkeypatch, tmp_path):
        _patch_happy_path(monkeypatch, tmp_path)
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
    def test_bare_server_does_not_run_http(self, monkeypatch, tmp_path):
        # Even the bare path starts the job scheduler, which writes
        # jobs/.current — isolate it to a tmp config dir.
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr("schwab_cli.config.load", lambda: _CFG)

        run_http_calls: list = []

        class _FakeServer:
            def __init__(self, *a, **k):
                pass

            async def run_http(self, host, port, **kw):
                run_http_calls.append((host, port))

        monkeypatch.setattr(
            "schwab_cli.mcp_server.app.SchwabMcpServer", _FakeServer
        )
        monkeypatch.setattr(
            "schwab_cli.server.token_runtime.build_token_manager",
            lambda cfg, **kw: MagicMock(name="token_manager"),
        )

        def _fake_start(mgr, stop):
            stop.set()  # the bare path parks on stop_event — release it
            return ()

        monkeypatch.setattr(
            "schwab_cli.server.token_runtime.start_token_threads", _fake_start,
        )
        monkeypatch.setattr(
            "schwab_cli.server.token_runtime.stop_token_threads",
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
    """A replaced session must reach the persistent client's in-memory
    state via the server's schedule_session_replaced bridge (regression:
    otherwise the client keeps a refresh token the manager just rotated
    out). The bridge's rebind/reconnect behavior itself is covered in
    tests/test_mcp_auth_endpoints.py."""

    def test_handoff_callback_reaches_server_bridge(self, monkeypatch, tmp_path):
        rec = _patch_happy_path(monkeypatch, tmp_path)
        server_cmd.run(enable_mcp=True, interval_s=60)

        fresh = _future_session()
        rec["on_session_replaced"](fresh)
        assert rec["handoffs"] == [fresh]


# ---------------------------------------------------------------------------
# --enable-mcp --enable-rest — REST routes mounted on the MCP server's port
# ---------------------------------------------------------------------------

@pytest.mark.skipif(Session is None, reason="Session not importable")
class TestEnableMcpWithRest:
    def test_extra_routes_passed_to_run_http(self, monkeypatch, tmp_path):
        rec = _patch_happy_path(monkeypatch, tmp_path)
        server_cmd.run(enable_mcp=True, enable_rest=True, interval_s=60)
        extra = rec["run_http_extra_routes"]
        assert len(extra) == 1
        assert extra[0]  # non-empty list of Route objects
        paths = {r.path for r in extra[0]}
        # /admin/jobs is always mounted; REST adds the loopback PoC routes
        # AND the authenticated /api/v1 surface (exact endpoint list is
        # owned by tests/test_rest_api_v1.py).
        assert {"/health", "/quote/{symbol}", "/admin/jobs"} <= paths
        assert "/api/v1/health" in paths
        assert "/api/v1/quote/{symbol}" in paths
        assert all(
            p.startswith(("/health", "/quote", "/api/v1/", "/admin/jobs"))
            for p in paths
        )

    def test_no_rest_still_mounts_admin_jobs(self, monkeypatch, tmp_path):
        rec = _patch_happy_path(monkeypatch, tmp_path)
        server_cmd.run(enable_mcp=True, enable_rest=False, interval_s=60)
        extra = rec["run_http_extra_routes"]
        # /admin/jobs is always mounted even without REST; the REST routes
        # (loopback PoC + /api/v1) must NOT be present.
        assert len(extra) == 1
        paths = {r.path for r in extra[0]}
        assert paths == {"/admin/jobs"}

    def test_webauth_gate_always_wraps_the_app(self, monkeypatch, tmp_path):
        """The two-tier gate applies even WITHOUT --enable-rest so a wide
        bind can never expose /admin //auth //mcp."""
        rec = _patch_happy_path(monkeypatch, tmp_path)
        server_cmd.run(enable_mcp=True, enable_rest=False, interval_s=60)
        (wrap,) = rec["run_http_asgi_wrap"]
        assert callable(wrap)

    def test_trusted_proxies_come_from_web_allow(self, monkeypatch, tmp_path):
        """uvicorn may only honour X-Forwarded-* from peers already trusted
        for the public surface — never from an arbitrary origin."""
        rec = _patch_happy_path(monkeypatch, tmp_path)
        server_cmd.run(enable_mcp=True, enable_rest=False, interval_s=60)
        (proxies,) = rec["run_http_trusted_proxies"]
        from schwab_cli.config import load as load_cfg

        assert tuple(proxies) == tuple(load_cfg().web_allow)


# ---------------------------------------------------------------------------
# --enable-rest (standalone, no --enable-mcp) — uvicorn + maintenance thread
# ---------------------------------------------------------------------------

class TestEnableRestStandalone:
    def _patch(self, monkeypatch, tmp_path):
        """Patch cfg+session present, token runtime, uvicorn, asyncio.run
        — fully hermetic (no reliance on a real ~/.config).

        Isolation: points ``SCHWAB_CLI_CONFIG_DIR`` at ``tmp_path`` so the
        job scheduler startup writes ``jobs/.current`` into the tmp dir,
        never the real ``~/.config/schwab_cli``.
        """
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr("schwab_cli.config.load", lambda: _CFG)
        # Future-dated session so the startup refresh-expiry check skips
        # auto-login; and cheap logbook/notifier so no disk/network.
        monkeypatch.setattr(
            "schwab_cli.session.load", lambda: _future_session()
        )
        monkeypatch.setattr(
            "schwab_cli.mcp_server.logbook.LogBook", lambda *a, **k: MagicMock()
        )
        monkeypatch.setattr(
            "schwab_cli.notify.Notifier.from_file",
            classmethod(lambda cls, **k: MagicMock()),
        )
        rec: dict = {"served": False}

        def _fake_build(cfg, **kw):
            rec["tm_cfg"] = cfg
            tm = MagicMock(name="token_manager")
            rec["tm"] = tm
            return tm

        def _fake_start(mgr, stop):
            rec["tm_started"] = mgr
            rec["stop_event"] = stop
            return ()

        def _fake_stop(mgr, stop, threads, **kw):
            rec["tm_stopped"] = True
            stop.set()

        monkeypatch.setattr(
            "schwab_cli.server.token_runtime.build_token_manager", _fake_build,
        )
        monkeypatch.setattr(
            "schwab_cli.server.token_runtime.start_token_threads", _fake_start,
        )
        monkeypatch.setattr(
            "schwab_cli.server.token_runtime.stop_token_threads", _fake_stop,
        )

        # build_rest_app must not pull in a real Schwab client.
        monkeypatch.setattr(
            "schwab_cli.server.rest.build_rest_app",
            lambda: MagicMock(name="rest_app"),
        )

        # Fake uvicorn so .serve() records and returns without binding.
        fake_uvicorn = MagicMock()

        class _FakeServer:
            def __init__(self, config):
                rec["config"] = config

            async def serve(self):
                rec["served"] = True

        fake_uvicorn.Server = _FakeServer
        fake_uvicorn.Config = lambda *a, **k: {"args": a, "kwargs": k}
        monkeypatch.setitem(
            __import__("sys").modules, "uvicorn", fake_uvicorn
        )

        def _fake_asyncio_run(coro):
            try:
                coro.send(None)
            except StopIteration:
                pass
            return None

        monkeypatch.setattr(server_cmd.asyncio, "run", _fake_asyncio_run)
        return rec

    def test_starts_token_runtime_and_serves(self, monkeypatch, tmp_path):
        rec = self._patch(monkeypatch, tmp_path)
        result = server_cmd.run(
            enable_rest=True, rest_host="127.0.0.1", rest_port=8000,
            interval_s=90,
        )
        assert result == 0
        assert rec["served"] is True
        assert rec["tm_cfg"] is _CFG
        assert rec["tm_started"] is rec["tm"]

    def test_token_runtime_stopped_on_shutdown(self, monkeypatch, tmp_path):
        rec = self._patch(monkeypatch, tmp_path)
        server_cmd.run(enable_rest=True, interval_s=60)
        assert rec.get("tm_stopped") is True
        assert rec["stop_event"].is_set() is True

    def test_rest_host_port_passed_to_uvicorn_config(self, monkeypatch, tmp_path):
        rec = self._patch(monkeypatch, tmp_path)
        server_cmd.run(
            enable_rest=True, rest_host="0.0.0.0", rest_port=9100,
            interval_s=60,
        )
        cfg = rec["config"]
        assert cfg["kwargs"]["host"] == "0.0.0.0"
        assert cfg["kwargs"]["port"] == 9100

    def test_no_config_exits_1(self, monkeypatch, capsys):
        monkeypatch.setattr("schwab_cli.config.load", lambda: None)
        with pytest.raises(SystemExit) as exc:
            server_cmd.run(enable_rest=True, interval_s=60)
        assert exc.value.code == 1
        assert "No config" in "".join(capsys.readouterr())


# ---------------------------------------------------------------------------
# CLI wiring — `schwab server --enable-rest`
# ---------------------------------------------------------------------------

class TestCLIEnableRestWiring:
    def test_cli_enable_rest_passes_through(self, monkeypatch):
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
            ["server", "--enable-rest", "--rest-host", "1.2.3.4",
             "--rest-port", "8123"],
        )
        assert result.exit_code == 0, result.output
        assert len(calls) == 1
        assert calls[0]["enable_rest"] is True
        assert calls[0]["rest_host"] == "1.2.3.4"
        assert calls[0]["rest_port"] == 8123

    def test_cli_bare_server_disables_rest(self, monkeypatch):
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
        assert calls[0]["enable_rest"] is False

    def test_enable_rest_help_works(self, monkeypatch):
        try:
            from schwab_cli.cli import app
        except ImportError:
            pytest.skip("cli not available")
        from typer.testing import CliRunner

        monkeypatch.setenv("COLUMNS", "240")
        runner = CliRunner()
        result = runner.invoke(app, ["server", "--help"])
        assert result.exit_code == 0
        import re
        clean = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        compact = clean.replace("\n", " ").replace(" ", "")
        assert "--enable-rest" in compact
        assert "--rest-host" in compact
        assert "--rest-port" in compact
