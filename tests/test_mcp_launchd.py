"""Tests for the launchd plist generator."""

from __future__ import annotations

import plistlib

from schwab_cli.mcp_server.launchd import (
    LABEL,
    LaunchdPlistSpec,
    build_plist,
    write_plist,
)


def _parse(payload: bytes) -> dict:
    return plistlib.loads(payload)


def test_build_plist_carries_label():
    data = _parse(build_plist(LaunchdPlistSpec(binary_path="/usr/bin/schwab_cli")))
    assert data["Label"] == LABEL


def test_build_plist_program_arguments_shape():
    data = _parse(build_plist(
        LaunchdPlistSpec(
            binary_path="/abs/schwab_cli", host="127.0.0.1", port=7234,
        ),
    ))
    args = data["ProgramArguments"]
    assert args[0] == "/abs/schwab_cli"
    assert args[1] == "mcp"
    # HTTP-only daemon: the plist no longer bakes the legacy --sse flag.
    assert "--sse" not in args
    assert "--host" in args
    assert "--port" in args
    assert "7234" in args


def test_build_plist_marks_launchd_managed_env_var():
    data = _parse(build_plist(
        LaunchdPlistSpec(binary_path="/abs/schwab_cli"),
    ))
    env = data["EnvironmentVariables"]
    assert env["LAUNCHD_MANAGED"] == "1"


def test_build_plist_keepalive_and_runatload_true():
    data = _parse(build_plist(LaunchdPlistSpec(binary_path="/x")))
    assert data["KeepAlive"] is True
    assert data["RunAtLoad"] is True


def test_build_plist_log_file_wires_stdout_and_stderr():
    data = _parse(build_plist(LaunchdPlistSpec(
        binary_path="/x", log_file="/tmp/mcp.log",
    )))
    assert data["StandardOutPath"] == "/tmp/mcp.log"
    assert data["StandardErrorPath"] == "/tmp/mcp.log"


def test_build_plist_no_log_file_omits_redirect():
    data = _parse(build_plist(LaunchdPlistSpec(binary_path="/x")))
    assert "StandardOutPath" not in data
    assert "StandardErrorPath" not in data


def test_write_plist_creates_parent_dirs(tmp_path):
    target = tmp_path / "nested" / "launchagents" / "com.schwab-cli.mcp.plist"
    # ``launcher_dir=tmp_path`` keeps the launcher script inside the test's
    # tmpdir; without it ``write_launcher`` clobbers the real
    # ``~/Library/Application Support/schwab_cli/launchers`` script and
    # breaks the user's launchd setup. Don't drop it.
    path = write_plist(
        LaunchdPlistSpec(binary_path="/x", launcher_dir=tmp_path),
        target,
    )
    assert path == target
    assert target.exists()
    # Round-trip parse.
    data = plistlib.loads(target.read_bytes())
    assert data["Label"] == LABEL


def test_write_plist_overwrites_existing(tmp_path):
    target = tmp_path / "p.plist"
    target.write_text("leftover content")
    write_plist(
        LaunchdPlistSpec(binary_path="/new", launcher_dir=tmp_path),
        target,
    )
    data = plistlib.loads(target.read_bytes())
    # The plist's ProgramArguments[0] is the launcher *script*, not the
    # binary — the binary lives inside the launcher's exec line. Verify
    # both: the plist points at the launcher in tmp_path, and the
    # launcher itself contains our chosen binary.
    launcher = tmp_path / "Schwab MCP Server"
    assert data["ProgramArguments"][0] == str(launcher)
    assert "/new mcp " in launcher.read_text()
