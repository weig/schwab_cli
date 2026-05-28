"""Spec-based acceptance tests (TDD red) for schwab_cli.server.launchd.

These tests will FAIL until the implementation is written — that is expected.
Import-guarded so the file always collects cleanly.
"""
from __future__ import annotations

import plistlib

import pytest

# ---------------------------------------------------------------------------
# Import guard
# ---------------------------------------------------------------------------
try:
    from schwab_cli.server.launchd import (
        DEFAULT_PLIST_PATH,
        LABEL,
        ServerPlistSpec,
        build_plist,
        write_plist,
    )
    _MODULE_AVAILABLE = True
except ModuleNotFoundError:
    _MODULE_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not _MODULE_AVAILABLE,
    reason="schwab_cli.server.launchd not implemented yet",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse(payload: bytes) -> dict:
    return plistlib.loads(payload)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class TestConstants:
    def test_label_is_correct(self):
        assert LABEL == "com.schwab-cli.server"

    def test_default_plist_path_under_launch_agents(self):
        from pathlib import Path

        expected = Path.home() / "Library" / "LaunchAgents" / "com.schwab-cli.server.plist"
        assert DEFAULT_PLIST_PATH == expected

    def test_default_plist_path_is_path_object(self):
        from pathlib import Path

        assert isinstance(DEFAULT_PLIST_PATH, Path)


# ---------------------------------------------------------------------------
# build_plist — structural assertions
# ---------------------------------------------------------------------------

class TestBuildPlistLabel:
    def test_label_matches_constant(self):
        data = _parse(build_plist(ServerPlistSpec(binary_path="/usr/bin/schwab")))
        assert data["Label"] == LABEL

    def test_process_type_is_interactive(self):
        # Interactive => launchd spawns it promptly on load (parity with the
        # pre-restructure mcp daemon; without it RunAtLoad is lazier).
        data = _parse(build_plist(ServerPlistSpec(binary_path="/x")))
        assert data["ProcessType"] == "Interactive"

    def test_label_is_exactly_com_schwab_cli_server(self):
        data = _parse(build_plist(ServerPlistSpec(binary_path="/x")))
        assert data["Label"] == "com.schwab-cli.server"


class TestBuildPlistProgramArguments:
    def test_program_arguments_first_element_is_binary(self):
        data = _parse(build_plist(ServerPlistSpec(binary_path="/usr/local/bin/schwab")))
        args = data["ProgramArguments"]
        assert args[0] == "/usr/local/bin/schwab"

    def test_program_arguments_contains_server_subcommand(self):
        """ProgramArguments must end with …, 'server' so launchd invokes the server loop."""
        data = _parse(build_plist(ServerPlistSpec(binary_path="/bin/schwab")))
        args = data["ProgramArguments"]
        assert "server" in args
        # The last meaningful arg should be 'server' (bare invocation).
        assert args[-1] == "server"

    def test_program_arguments_is_list_of_strings(self):
        data = _parse(build_plist(ServerPlistSpec(binary_path="/x")))
        args = data["ProgramArguments"]
        assert isinstance(args, list)
        assert all(isinstance(a, str) for a in args)

    def test_program_arguments_binary_and_server_pair(self):
        """The exact form: [binary_path, 'server']."""
        data = _parse(build_plist(ServerPlistSpec(binary_path="/path/to/schwab")))
        args = data["ProgramArguments"]
        assert args[0] == "/path/to/schwab"
        assert args[1] == "server"


class TestBuildPlistModeFlags:
    """The plist bakes the `schwab server` mode flags into ProgramArguments."""

    def test_enable_mcp_bakes_flag(self):
        data = _parse(build_plist(
            ServerPlistSpec(binary_path="/bin/schwab", enable_mcp=True),
        ))
        args = data["ProgramArguments"]
        assert args[:2] == ["/bin/schwab", "server"]
        assert "--enable-mcp" in args

    def test_enable_mcp_bakes_host_and_port(self):
        data = _parse(build_plist(ServerPlistSpec(
            binary_path="/bin/schwab", enable_mcp=True,
            host="127.0.0.1", port=7234,
        )))
        args = data["ProgramArguments"]
        assert "--mcp-host" in args
        assert args[args.index("--mcp-host") + 1] == "127.0.0.1"
        assert "--mcp-port" in args
        assert args[args.index("--mcp-port") + 1] == "7234"

    def test_enable_mcp_bakes_log_file(self):
        data = _parse(build_plist(ServerPlistSpec(
            binary_path="/bin/schwab", enable_mcp=True,
            mcp_log_file="/tmp/mcp.log",
        )))
        args = data["ProgramArguments"]
        assert "--log-file" in args
        assert args[args.index("--log-file") + 1] == "/tmp/mcp.log"

    def test_host_port_ignored_without_enable_mcp(self):
        """--host/--port only matter with --enable-mcp; bare stays bare."""
        data = _parse(build_plist(ServerPlistSpec(
            binary_path="/bin/schwab", host="0.0.0.0", port=9999,
        )))
        args = data["ProgramArguments"]
        assert args == ["/bin/schwab", "server"]
        assert "--mcp-host" not in args

    def test_enable_rest_bakes_flag(self):
        data = _parse(build_plist(
            ServerPlistSpec(binary_path="/bin/schwab", enable_rest=True),
        ))
        args = data["ProgramArguments"]
        assert "--enable-rest" in args

    def test_default_is_bare_server(self):
        data = _parse(build_plist(ServerPlistSpec(binary_path="/bin/schwab")))
        assert data["ProgramArguments"] == ["/bin/schwab", "server"]


class TestBuildPlistLaunchdBooleans:
    def test_run_at_load_is_true(self):
        data = _parse(build_plist(ServerPlistSpec(binary_path="/x")))
        assert data["RunAtLoad"] is True

    def test_keep_alive_is_true(self):
        data = _parse(build_plist(ServerPlistSpec(binary_path="/x")))
        assert data["KeepAlive"] is True


class TestBuildPlistEnvironmentVariables:
    def test_launchd_managed_env_var_is_set(self):
        data = _parse(build_plist(ServerPlistSpec(binary_path="/x")))
        env = data["EnvironmentVariables"]
        assert env["LAUNCHD_MANAGED"] == "1"

    def test_environment_variables_key_present(self):
        data = _parse(build_plist(ServerPlistSpec(binary_path="/x")))
        assert "EnvironmentVariables" in data


class TestBuildPlistLogFile:
    def test_log_file_wires_standard_out_path(self):
        data = _parse(build_plist(
            ServerPlistSpec(binary_path="/x", log_file="/tmp/server.log"),
        ))
        assert data["StandardOutPath"] == "/tmp/server.log"

    def test_log_file_wires_standard_error_path(self):
        data = _parse(build_plist(
            ServerPlistSpec(binary_path="/x", log_file="/tmp/server.log"),
        ))
        assert data["StandardErrorPath"] == "/tmp/server.log"

    def test_log_file_stdout_and_stderr_are_same_path(self):
        log = "/var/log/schwab-server.log"
        data = _parse(build_plist(
            ServerPlistSpec(binary_path="/x", log_file=log),
        ))
        assert data["StandardOutPath"] == data["StandardErrorPath"] == log

    def test_no_log_file_omits_standard_out_path(self):
        data = _parse(build_plist(ServerPlistSpec(binary_path="/x")))
        assert "StandardOutPath" not in data

    def test_no_log_file_omits_standard_error_path(self):
        data = _parse(build_plist(ServerPlistSpec(binary_path="/x")))
        assert "StandardErrorPath" not in data


class TestBuildPlistReturnType:
    def test_build_plist_returns_bytes(self):
        result = build_plist(ServerPlistSpec(binary_path="/x"))
        assert isinstance(result, bytes)

    def test_build_plist_is_valid_xml_plist(self):
        """plistlib.loads must not raise."""
        payload = build_plist(ServerPlistSpec(binary_path="/x"))
        data = _parse(payload)
        assert isinstance(data, dict)


# ---------------------------------------------------------------------------
# ServerPlistSpec — dataclass contract
# ---------------------------------------------------------------------------

class TestServerPlistSpec:
    def test_binary_path_required(self):
        spec = ServerPlistSpec(binary_path="/usr/bin/schwab")
        assert spec.binary_path == "/usr/bin/schwab"

    def test_log_file_defaults_to_none(self):
        spec = ServerPlistSpec(binary_path="/x")
        assert spec.log_file is None

    def test_log_file_can_be_set(self):
        spec = ServerPlistSpec(binary_path="/x", log_file="/tmp/out.log")
        assert spec.log_file == "/tmp/out.log"


# ---------------------------------------------------------------------------
# write_plist — filesystem round-trip
# ---------------------------------------------------------------------------

class TestWritePlist:
    def test_write_plist_returns_path(self, tmp_path):
        target = tmp_path / "com.schwab-cli.server.plist"
        result = write_plist(
            ServerPlistSpec(binary_path="/x"),
            path=target,
        )
        assert result == target

    def test_write_plist_creates_file(self, tmp_path):
        target = tmp_path / "com.schwab-cli.server.plist"
        write_plist(ServerPlistSpec(binary_path="/x"), path=target)
        assert target.exists()

    def test_write_plist_produces_readable_plist(self, tmp_path):
        target = tmp_path / "test.plist"
        write_plist(ServerPlistSpec(binary_path="/usr/bin/schwab"), path=target)
        data = plistlib.loads(target.read_bytes())
        assert data["Label"] == "com.schwab-cli.server"

    def test_write_plist_creates_parent_dirs(self, tmp_path):
        target = tmp_path / "nested" / "agents" / "com.schwab-cli.server.plist"
        write_plist(ServerPlistSpec(binary_path="/x"), path=target)
        assert target.exists()

    def test_write_plist_overwrites_existing_file(self, tmp_path):
        target = tmp_path / "p.plist"
        target.write_text("leftover")
        write_plist(ServerPlistSpec(binary_path="/new-binary"), path=target)
        data = plistlib.loads(target.read_bytes())
        # The new binary path should be reflected.
        args = data["ProgramArguments"]
        assert args[0] == "/new-binary"

    def test_write_plist_round_trips_log_file(self, tmp_path):
        target = tmp_path / "p.plist"
        write_plist(
            ServerPlistSpec(binary_path="/x", log_file="/tmp/s.log"),
            path=target,
        )
        data = plistlib.loads(target.read_bytes())
        assert data["StandardOutPath"] == "/tmp/s.log"
        assert data["StandardErrorPath"] == "/tmp/s.log"

    def test_write_plist_uses_default_path_when_none(self, monkeypatch, tmp_path):
        """When path=None, write_plist writes to DEFAULT_PLIST_PATH (redirected in test)."""
        fake_default = tmp_path / "com.schwab-cli.server.plist"
        monkeypatch.setattr(
            "schwab_cli.server.launchd.DEFAULT_PLIST_PATH",
            fake_default,
        )
        fake_default.parent.mkdir(parents=True, exist_ok=True)
        result = write_plist(ServerPlistSpec(binary_path="/x"), path=None)
        assert result == fake_default
        assert fake_default.exists()
