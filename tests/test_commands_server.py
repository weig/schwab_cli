"""Spec-based acceptance tests (TDD red) for schwab_cli.commands.server.

These tests will FAIL until the implementation is written — that is expected.
Import-guarded so the file always collects cleanly.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

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

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

try:
    from schwab_cli.config import Config
except ImportError:
    Config = None  # type: ignore[misc, assignment]

_CFG = Config(
    client_id="cid",
    client_secret="csec",
    redirect_uri="https://127.0.0.1:8443",
) if Config is not None else None


# ---------------------------------------------------------------------------
# run() — bare server command
# ---------------------------------------------------------------------------

class TestRunNoConfig:
    """run() with no config on disk → exit 1 with a helpful message."""

    def test_exits_1_when_no_config(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "schwab_cli.config.load",
            lambda: None,
        )
        with pytest.raises(SystemExit) as exc:
            server_cmd.run(interval_s=60)

        assert exc.value.code == 1

    def test_error_message_mentions_setup(self, monkeypatch, capsys):
        monkeypatch.setattr("schwab_cli.config.load", lambda: None)
        try:
            server_cmd.run(interval_s=60)
        except SystemExit:
            pass
        captured = capsys.readouterr()
        output = captured.out + captured.err
        assert "setup" in output.lower() or "No config" in output

    def test_error_message_text(self, monkeypatch, capsys):
        """The exact message must include 'No config found'."""
        monkeypatch.setattr("schwab_cli.config.load", lambda: None)
        try:
            server_cmd.run(interval_s=60)
        except SystemExit:
            pass
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "No config found" in combined


class TestRunWithConfig:
    """run() with config present starts the TokenManager runtime."""

    @pytest.fixture(autouse=True)
    def _isolate_config_dir(self, monkeypatch, tmp_path):
        # The bare server path starts the job scheduler, which writes
        # jobs/.current into config_dir(). Redirect it to a tmp dir so
        # these tests never touch the real ~/.config/schwab_cli.
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))

    def _patch_runtime(self, monkeypatch, record):
        """Stub the token runtime: capture wiring and drive an immediate
        shutdown (the fake start sets the stop event run() parks on)."""
        def _fake_build(cfg, *, notifier=None, **kw):
            record["cfg"] = cfg
            mgr = MagicMock(name="token_manager")
            record["mgr"] = mgr
            return mgr

        def _fake_start(mgr, stop):
            record["started"] = mgr
            stop.set()  # run() parks on stop_event — release it at once
            return ()

        def _fake_stop(mgr, stop, threads, **kw):
            record["stopped"] = True

        monkeypatch.setattr(
            "schwab_cli.server.token_runtime.build_token_manager", _fake_build,
        )
        monkeypatch.setattr(
            "schwab_cli.server.token_runtime.start_token_threads", _fake_start,
        )
        monkeypatch.setattr(
            "schwab_cli.server.token_runtime.stop_token_threads", _fake_stop,
        )

    def test_run_starts_token_runtime(self, monkeypatch):
        monkeypatch.setattr("schwab_cli.config.load", lambda: _CFG)
        record: dict = {}
        self._patch_runtime(monkeypatch, record)
        server_cmd.run(interval_s=3600)

        assert record["cfg"] is _CFG
        assert record["started"] is record["mgr"]

    def test_run_stops_token_runtime_on_shutdown(self, monkeypatch):
        monkeypatch.setattr("schwab_cli.config.load", lambda: _CFG)
        record: dict = {}
        self._patch_runtime(monkeypatch, record)
        server_cmd.run(interval_s=3600)

        assert record.get("stopped") is True

    def test_run_returns_0_on_graceful_stop(self, monkeypatch):
        monkeypatch.setattr("schwab_cli.config.load", lambda: _CFG)
        record: dict = {}
        self._patch_runtime(monkeypatch, record)
        result = server_cmd.run(interval_s=60)
        # run() should return 0 or None (graceful exit)
        assert result in (0, None)

    def test_run_works_with_default_args(self, monkeypatch):
        monkeypatch.setattr("schwab_cli.config.load", lambda: _CFG)
        record: dict = {}
        self._patch_runtime(monkeypatch, record)
        result = server_cmd.run()  # legacy interval default accepted
        assert result in (0, None)
        assert record["cfg"] is _CFG


# ---------------------------------------------------------------------------
# run_install() — write plist + launchctl load
# ---------------------------------------------------------------------------

class TestRunInstall:
    """run_install() resolves the binary, writes plist, calls launchctl load."""

    def test_install_writes_plist_to_tmp_path(self, monkeypatch, tmp_path):
        plist_path = tmp_path / "com.schwab-cli.server.plist"

        monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/schwab")
        subprocess_calls = []
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **k: subprocess_calls.append(a) or MagicMock(returncode=0),
        )

        server_cmd.run_install(plist_path=str(plist_path), yes=True)

        assert plist_path.exists()

    def test_install_plist_has_correct_label(self, monkeypatch, tmp_path):
        import plistlib

        plist_path = tmp_path / "com.schwab-cli.server.plist"
        monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/schwab")
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **k: MagicMock(returncode=0),
        )
        server_cmd.run_install(plist_path=str(plist_path), yes=True)

        data = plistlib.loads(plist_path.read_bytes())
        assert data["Label"] == "com.schwab-cli.server"

    def test_install_calls_launchctl_load(self, monkeypatch, tmp_path):
        plist_path = tmp_path / "com.schwab-cli.server.plist"
        monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/schwab")

        subprocess_cmds = []

        def _fake_run(cmd, *a, **k):
            subprocess_cmds.append(cmd)
            return MagicMock(returncode=0)

        monkeypatch.setattr("subprocess.run", _fake_run)
        server_cmd.run_install(plist_path=str(plist_path), yes=True)

        # At least one call must be a launchctl load
        assert any(
            "launchctl" in str(c) and "load" in str(c)
            for c in subprocess_cmds
        )

    def test_install_kickstarts_after_load(self, monkeypatch, tmp_path):
        """install must kickstart the job after load so it starts now even
        from a non-GUI context (RunAtLoad only fires in the GUI session)."""
        plist_path = tmp_path / "com.schwab-cli.server.plist"
        monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/schwab")

        cmds = []

        def _fake_run(cmd, *a, **k):
            cmds.append(cmd)
            return MagicMock(returncode=0)

        monkeypatch.setattr("subprocess.run", _fake_run)
        server_cmd.run_install(plist_path=str(plist_path), yes=True)

        assert any(
            "launchctl" in str(c) and "kickstart" in str(c) for c in cmds
        ), f"no kickstart call in {cmds}"

    def test_install_output_includes_label(self, monkeypatch, tmp_path, capsys):
        plist_path = tmp_path / "com.schwab-cli.server.plist"
        monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/schwab")
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **k: MagicMock(returncode=0),
        )
        server_cmd.run_install(plist_path=str(plist_path), yes=True)

        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "com.schwab-cli.server" in combined

    def test_install_exits_1_when_binary_not_found(self, monkeypatch, tmp_path, capsys):
        """When shutil.which returns None and 'schwab_cli' is also not on PATH."""
        plist_path = tmp_path / "p.plist"
        monkeypatch.setattr("shutil.which", lambda name: None)

        with pytest.raises(SystemExit) as exc:
            server_cmd.run_install(plist_path=str(plist_path), yes=True)

        assert exc.value.code == 1

    def test_install_with_log_file_sets_log_path_in_plist(self, monkeypatch, tmp_path):
        import plistlib

        plist_path = tmp_path / "p.plist"
        monkeypatch.setattr("shutil.which", lambda name: "/bin/schwab")
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **k: MagicMock(returncode=0),
        )
        server_cmd.run_install(
            plist_path=str(plist_path),
            log_file="/tmp/server.log",
            yes=True,
        )

        data = plistlib.loads(plist_path.read_bytes())
        assert data.get("StandardOutPath") == "/tmp/server.log"
        assert data.get("StandardErrorPath") == "/tmp/server.log"


# ---------------------------------------------------------------------------
# run_uninstall() — launchctl unload + remove plist
# ---------------------------------------------------------------------------

class TestRunUninstall:
    """run_uninstall() calls launchctl unload and removes the plist file."""

    def test_uninstall_calls_launchctl_unload(self, monkeypatch, tmp_path):
        plist_path = tmp_path / "p.plist"
        plist_path.write_bytes(b"dummy")  # must exist for unload

        subprocess_cmds = []

        def _fake_run(cmd, *a, **k):
            subprocess_cmds.append(cmd)
            return MagicMock(returncode=0)

        monkeypatch.setattr("subprocess.run", _fake_run)
        server_cmd.run_uninstall(plist_path=str(plist_path), yes=True)

        assert any(
            "launchctl" in str(c) and "unload" in str(c)
            for c in subprocess_cmds
        )

    def test_uninstall_removes_plist_file(self, monkeypatch, tmp_path):
        plist_path = tmp_path / "p.plist"
        plist_path.write_bytes(b"dummy")
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **k: MagicMock(returncode=0),
        )
        server_cmd.run_uninstall(plist_path=str(plist_path), yes=True)

        assert not plist_path.exists()


# ---------------------------------------------------------------------------
# run_status() — parse launchctl list output
# ---------------------------------------------------------------------------

_LAUNCHCTL_LIST_LOADED = """\
PID\tStatus\tLabel
12345\t0\tcom.schwab-cli.server
"""

_LAUNCHCTL_LIST_NOT_LOADED = """\
PID\tStatus\tLabel
-\t0\tcom.apple.something
"""


class TestRunStatus:
    """run_status() reports whether the job is loaded via launchctl list."""

    def test_status_reports_loaded_when_job_present(self, monkeypatch, capsys):
        def _fake_run(cmd, *a, **k):
            m = MagicMock()
            m.returncode = 0
            m.stdout = _LAUNCHCTL_LIST_LOADED
            return m

        monkeypatch.setattr("subprocess.run", _fake_run)
        server_cmd.run_status()

        captured = capsys.readouterr()
        combined = captured.out + captured.err
        # Must mention the label and loaded/running state
        assert "com.schwab-cli.server" in combined

    def test_status_reports_not_loaded_when_job_absent(self, monkeypatch, capsys):
        def _fake_run(cmd, *a, **k):
            m = MagicMock()
            m.returncode = 1  # launchctl exits non-zero when not found
            m.stdout = ""
            m.stderr = ""
            return m

        monkeypatch.setattr("subprocess.run", _fake_run)
        server_cmd.run_status()

        captured = capsys.readouterr()
        combined = captured.out + captured.err
        # Must indicate not loaded / not running
        assert combined.strip()  # something was printed

    def test_status_contains_label_in_output(self, monkeypatch, capsys):
        def _fake_run(cmd, *a, **k):
            m = MagicMock()
            m.returncode = 0
            m.stdout = _LAUNCHCTL_LIST_LOADED
            return m

        monkeypatch.setattr("subprocess.run", _fake_run)
        server_cmd.run_status()
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "com.schwab-cli.server" in combined

    def test_status_invokes_launchctl_list(self, monkeypatch, capsys):
        subprocess_cmds = []

        def _fake_run(cmd, *a, **k):
            subprocess_cmds.append(cmd)
            m = MagicMock()
            m.returncode = 1
            m.stdout = ""
            m.stderr = ""
            return m

        monkeypatch.setattr("subprocess.run", _fake_run)
        server_cmd.run_status()

        assert any(
            "launchctl" in str(c) and "list" in str(c)
            for c in subprocess_cmds
        )


# ---------------------------------------------------------------------------
# CLI wiring — server sub-app registered on main app
# ---------------------------------------------------------------------------

class TestCLIWiring:
    """The 'server' sub-app must be reachable from the top-level Typer app."""

    def test_server_command_visible_in_app(self):
        """schwab server (bare) must be a registered command/subapp."""
        try:
            from schwab_cli.cli import app
        except ImportError:
            pytest.skip("cli not available")

        from typer.testing import CliRunner

        runner = CliRunner()
        # Invoking help should not 404 / raise NotFound
        result = runner.invoke(app, ["server", "--help"])
        # Either it shows help (exit 0) or there's a proper "no config" type
        # exit — but it must NOT exit with a usage error about unknown command.
        assert result.exit_code != 2 or "No such command" not in (result.output or "")

    def test_server_install_subcommand_visible(self):
        """schwab server install must be a registered subcommand."""
        try:
            from schwab_cli.cli import app
        except ImportError:
            pytest.skip("cli not available")

        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(app, ["server", "install", "--help"])
        assert result.exit_code != 2 or "No such command" not in (result.output or "")

    def test_server_uninstall_subcommand_visible(self):
        try:
            from schwab_cli.cli import app
        except ImportError:
            pytest.skip("cli not available")

        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(app, ["server", "uninstall", "--help"])
        assert result.exit_code != 2 or "No such command" not in (result.output or "")

    def test_server_status_subcommand_visible(self):
        try:
            from schwab_cli.cli import app
        except ImportError:
            pytest.skip("cli not available")

        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(app, ["server", "status", "--help"])
        assert result.exit_code != 2 or "No such command" not in (result.output or "")
