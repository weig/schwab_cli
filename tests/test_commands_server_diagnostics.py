"""Tests for the `server` diagnostics subcommands migrated from `mcp`.

Covers:

* `server status` — launchd-label check + a real `GET /health` probe
  (and the `/admin/status` snapshot when reachable).
* `server log` — read / tail / filter the structured log.
* `server logout` — graceful `/admin/shutdown`.
* `server restart` — launchctl kickstart (loaded) vs. logout+execvp.
* `server register-claude` — write the Claude Code MCP entry.
* `server install --enable-mcp` — bakes the mode flags into the plist.

All network + subprocess + execvp are mocked.
"""
from __future__ import annotations

import json
import plistlib
import subprocess
from unittest.mock import MagicMock, patch

import httpx
from typer.testing import CliRunner

from schwab_cli.cli import app
from schwab_cli.commands import server as server_cmd

runner = CliRunner()


# ---------------------------------------------------------------------------
# server status — launchd + /health probe
# ---------------------------------------------------------------------------

class TestServerStatusHealthProbe:
    def _list_not_loaded(self):
        return subprocess.CompletedProcess(
            args=["launchctl", "list"], returncode=0, stdout="", stderr="",
        )

    def test_health_reachable_shows_snapshot(self, monkeypatch):
        monkeypatch.setattr(
            "subprocess.run", lambda *a, **k: self._list_not_loaded(),
        )

        def _fake_get(url, **kwargs):
            if url.endswith("/health"):
                return httpx.Response(200, json={"ok": True})
            if url.endswith("/admin/status"):
                return httpx.Response(200, json={
                    "pid": 4321, "transport": "http",
                    "subscription_summary": {"session_count": 0},
                })
            raise AssertionError(url)

        monkeypatch.setattr("httpx.get", _fake_get)
        result = runner.invoke(app, ["server", "status"])
        assert result.exit_code == 0, result.output
        assert "reachable" in result.output
        assert "4321" in result.output

    def test_health_unreachable_reports_clearly(self, monkeypatch):
        monkeypatch.setattr(
            "subprocess.run", lambda *a, **k: self._list_not_loaded(),
        )

        def _boom(url, **kwargs):
            raise httpx.ConnectError("refused")

        monkeypatch.setattr("httpx.get", _boom)
        result = runner.invoke(app, ["server", "status"])
        assert result.exit_code == 0, result.output
        assert "NOT reachable" in result.output

    def test_port_override_targets_loopback(self, monkeypatch):
        monkeypatch.setattr(
            "subprocess.run", lambda *a, **k: self._list_not_loaded(),
        )
        seen = {}

        def _fake_get(url, **kwargs):
            seen["url"] = url
            raise httpx.ConnectError("refused")

        monkeypatch.setattr("httpx.get", _fake_get)
        runner.invoke(app, ["server", "status", "--port", "9100"])
        assert "127.0.0.1:9100" in seen["url"]

    def test_json_output_has_health_keys(self, monkeypatch):
        monkeypatch.setattr(
            "subprocess.run", lambda *a, **k: self._list_not_loaded(),
        )

        def _fake_get(url, **kwargs):
            if url.endswith("/health"):
                return httpx.Response(200, json={"ok": True})
            return httpx.Response(200, json={"pid": 1})

        monkeypatch.setattr("httpx.get", _fake_get)
        result = runner.invoke(app, ["server", "status", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["health_reachable"] is True
        assert data["launchd_label"] == "com.schwab-cli.server"


# ---------------------------------------------------------------------------
# server logout
# ---------------------------------------------------------------------------

class TestServerLogout:
    def test_logout_posts_shutdown(self, monkeypatch):
        calls = []

        def _fake_post(url, **kwargs):
            calls.append(url)
            return httpx.Response(200, json={"ok": True})

        monkeypatch.setattr("httpx.post", _fake_post)
        result = runner.invoke(app, ["server", "logout"])
        assert result.exit_code == 0, result.output
        assert any(u.endswith("/admin/shutdown") for u in calls)
        assert "shutdown signalled" in result.output

    def test_logout_unreachable_exits_1(self, monkeypatch):
        def _boom(url, **kwargs):
            raise httpx.ConnectError("refused")

        monkeypatch.setattr("httpx.post", _boom)
        result = runner.invoke(app, ["server", "logout"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# server restart
# ---------------------------------------------------------------------------

def _list_loaded():
    return subprocess.CompletedProcess(
        args=["launchctl", "list", "com.schwab-cli.server"],
        returncode=0, stdout="", stderr="",
    )


def _list_not_loaded():
    return subprocess.CompletedProcess(
        args=["launchctl", "list", "com.schwab-cli.server"],
        returncode=113, stdout="",
        stderr='Could not find service "com.schwab-cli.server"',
    )


def _kickstart_ok():
    return subprocess.CompletedProcess(
        args=["launchctl", "kickstart", "-k", "gui/501/com.schwab-cli.server"],
        returncode=0, stdout="", stderr="",
    )


class TestServerRestart:
    def test_kickstarts_when_launchd_job_loaded(self):
        calls: list[list[str]] = []

        def fake_run(args, **kwargs):
            calls.append(list(args))
            if args[:2] == ["launchctl", "list"]:
                return _list_loaded()
            if args[:3] == ["launchctl", "kickstart", "-k"]:
                return _kickstart_ok()
            raise AssertionError(f"unexpected: {args}")

        with patch("schwab_cli.commands.server.subprocess.run", side_effect=fake_run), \
             patch("os.execvp") as mock_execvp:
            result = runner.invoke(app, ["server", "restart"])

        assert result.exit_code == 0, result.output
        assert "kickstarting launchd job" in result.output
        assert any(c[:3] == ["launchctl", "kickstart", "-k"] for c in calls)
        assert "com.schwab-cli.server" in calls[-1][-1]
        assert not mock_execvp.called

    def test_falls_back_to_execvp_when_no_launchd_job(self):
        def fake_run(args, **kwargs):
            if args[:2] == ["launchctl", "list"]:
                return _list_not_loaded()
            raise AssertionError(f"unexpected: {args}")

        with patch("schwab_cli.commands.server.subprocess.run", side_effect=fake_run), \
             patch("schwab_cli.commands.server.run_logout") as mock_logout, \
             patch("schwab_cli.commands.server.time.sleep"), \
             patch("os.execvp") as mock_execvp:
            result = runner.invoke(app, ["server", "restart"])

        assert result.exit_code == 0, result.output
        assert mock_logout.called
        assert mock_execvp.called
        _file, args = mock_execvp.call_args.args
        assert "server" in args

    def test_warns_on_non_default_host_port_in_launchd_mode(self):
        def fake_run(args, **kwargs):
            if args[:2] == ["launchctl", "list"]:
                return _list_loaded()
            if args[:3] == ["launchctl", "kickstart", "-k"]:
                return _kickstart_ok()
            raise AssertionError(f"unexpected: {args}")

        with patch("schwab_cli.commands.server.subprocess.run", side_effect=fake_run), \
             patch("os.execvp"):
            result = runner.invoke(app, ["server", "restart", "--port", "9999"])

        assert result.exit_code == 0, result.output
        assert "ignored in launchd mode" in result.output


# ---------------------------------------------------------------------------
# server log
# ---------------------------------------------------------------------------

def _write_log(path, entries):
    with path.open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


class TestServerLog:
    def test_missing_file_exits_zero_with_message(self, tmp_path):
        logfile = tmp_path / "mcp.log"
        result = runner.invoke(
            app, ["server", "log", "--log-file", str(logfile)],
        )
        assert result.exit_code == 0
        assert "not found" in result.output

    def test_pretty_prints_entries(self, tmp_path):
        logfile = tmp_path / "mcp.log"
        _write_log(logfile, [
            {"ts": "2026-04-24T12:00:00.000Z", "level": "info",
             "event": "subscribe", "session": "s1", "symbols": ["NVDA"]},
        ])
        result = runner.invoke(
            app, ["server", "log", "--log-file", str(logfile)],
        )
        assert result.exit_code == 0
        assert "subscribe" in result.output
        assert "NVDA" in result.output

    def test_json_passes_through_raw(self, tmp_path):
        logfile = tmp_path / "mcp.log"
        _write_log(logfile, [
            {"ts": "t", "level": "info", "event": "x"},
        ])
        result = runner.invoke(
            app, ["server", "log", "--log-file", str(logfile), "--json"],
        )
        assert result.exit_code == 0
        for line in result.output.strip().splitlines():
            assert json.loads(line)["event"] == "x"

    def test_level_filter_threshold(self, tmp_path):
        logfile = tmp_path / "mcp.log"
        _write_log(logfile, [
            {"ts": "t", "level": "info", "event": "a"},
            {"ts": "t", "level": "warning", "event": "b"},
            {"ts": "t", "level": "error", "event": "c"},
        ])
        result = runner.invoke(
            app, ["server", "log", "--log-file", str(logfile), "--level", "warning"],
        )
        assert result.exit_code == 0
        lines = [l for l in result.output.strip().splitlines() if l]
        assert len(lines) == 2

    def test_tail_limits_output(self, tmp_path):
        logfile = tmp_path / "mcp.log"
        _write_log(logfile, [
            {"ts": "t", "level": "info", "event": f"e{i}"} for i in range(10)
        ])
        result = runner.invoke(
            app, ["server", "log", "--log-file", str(logfile), "--tail", "3"],
        )
        assert result.exit_code == 0
        lines = [l for l in result.output.strip().splitlines() if l]
        assert len(lines) == 3
        assert "e9" in result.output
        assert "e0" not in result.output


# ---------------------------------------------------------------------------
# server register-claude
# ---------------------------------------------------------------------------

class TestServerRegisterClaude:
    def test_creates_http_entry(self, tmp_path):
        settings = tmp_path / "settings.json"
        result = runner.invoke(
            app,
            ["server", "register-claude", "--claude-settings", str(settings),
             "--yes", "--url", "http://127.0.0.1:7234"],
        )
        assert result.exit_code == 0, result.output
        entry = json.loads(settings.read_text())["mcpServers"]["schwab"]
        assert entry["type"] == "http"
        assert entry["url"].endswith("/mcp")

    def test_default_url_is_mcp_endpoint(self, tmp_path):
        settings = tmp_path / "settings.json"
        result = runner.invoke(
            app,
            ["server", "register-claude", "--claude-settings", str(settings), "--yes"],
        )
        assert result.exit_code == 0, result.output
        entry = json.loads(settings.read_text())["mcpServers"]["schwab"]
        assert entry["url"] == "http://127.0.0.1:7234/mcp"
        assert "command" not in entry

    def test_token_adds_authorization_header(self, tmp_path):
        settings = tmp_path / "settings.json"
        result = runner.invoke(
            app,
            ["server", "register-claude", "--claude-settings", str(settings),
             "--yes", "--token", "SECRET"],
        )
        assert result.exit_code == 0, result.output
        entry = json.loads(settings.read_text())["mcpServers"]["schwab"]
        assert entry["headers"] == {"Authorization": "Bearer SECRET"}

    def test_refuses_overwrite_without_force(self, tmp_path):
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"mcpServers": {"schwab": {"foo": "bar"}}}))
        result = runner.invoke(
            app,
            ["server", "register-claude", "--claude-settings", str(settings), "--yes"],
        )
        assert result.exit_code == 1

    def test_force_overwrites(self, tmp_path):
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"mcpServers": {"schwab": {"foo": "bar"}}}))
        result = runner.invoke(
            app,
            ["server", "register-claude", "--claude-settings", str(settings),
             "--yes", "--force"],
        )
        assert result.exit_code == 0, result.output
        assert "foo" not in json.loads(settings.read_text())["mcpServers"]["schwab"]

    def test_preserves_other_keys(self, tmp_path):
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({
            "theme": "dark",
            "mcpServers": {"other": {"command": "x"}},
        }))
        result = runner.invoke(
            app,
            ["server", "register-claude", "--claude-settings", str(settings), "--yes"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(settings.read_text())
        assert data["theme"] == "dark"
        assert "other" in data["mcpServers"]
        assert "schwab" in data["mcpServers"]


# ---------------------------------------------------------------------------
# server install --enable-mcp — plist content
# ---------------------------------------------------------------------------

class TestServerInstallEnableMcp:
    def test_install_bakes_enable_mcp_into_plist(self, monkeypatch, tmp_path):
        plist_path = tmp_path / "com.schwab-cli.server.plist"
        monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/schwab")
        monkeypatch.setattr(
            "subprocess.run", lambda *a, **k: MagicMock(returncode=0),
        )
        server_cmd.run_install(
            plist_path=str(plist_path), enable_mcp=True,
            host="127.0.0.1", port=7234, yes=True,
        )
        args = plistlib.loads(plist_path.read_bytes())["ProgramArguments"]
        assert "--enable-mcp" in args
        assert "--mcp-host" in args
        assert args[args.index("--mcp-port") + 1] == "7234"

    def test_bare_install_omits_mode_flags(self, monkeypatch, tmp_path):
        plist_path = tmp_path / "p.plist"
        monkeypatch.setattr("shutil.which", lambda name: "/bin/schwab")
        monkeypatch.setattr(
            "subprocess.run", lambda *a, **k: MagicMock(returncode=0),
        )
        server_cmd.run_install(plist_path=str(plist_path), yes=True)
        args = plistlib.loads(plist_path.read_bytes())["ProgramArguments"]
        assert args == ["/bin/schwab", "server"]

    def test_cli_install_enable_mcp_passes_through(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(
            "schwab_cli.commands.server.run_install",
            lambda **k: calls.append(k),
        )
        result = runner.invoke(
            app,
            ["server", "install", "--enable-mcp", "--host", "0.0.0.0",
             "--port", "8888", "--yes"],
        )
        assert result.exit_code == 0, result.output
        assert calls[0]["enable_mcp"] is True
        assert calls[0]["host"] == "0.0.0.0"
        assert calls[0]["port"] == 8888
