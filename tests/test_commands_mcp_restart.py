"""Tests for `schwab_cli mcp restart` — launchd-aware bounce path.

Two paths exercised:

* When ``launchctl list com.schwab-cli.mcp`` returns 0 (job loaded),
  restart should call ``launchctl kickstart -k`` and exit cleanly,
  NOT fall through to ``os.execvp``.
* When the job isn't loaded (or we're not on Darwin), restart keeps
  the historical foreground behavior — logout via admin endpoint and
  ``os.execvp`` a fresh ``mcp`` invocation (Streamable HTTP only).
"""
from __future__ import annotations

import subprocess
from unittest.mock import patch

from typer.testing import CliRunner

from schwab_cli.cli import app

runner = CliRunner()


def _list_loaded() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["launchctl", "list", "com.schwab-cli.mcp"],
        returncode=0, stdout="", stderr="",
    )


def _list_not_loaded() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["launchctl", "list", "com.schwab-cli.mcp"],
        returncode=113, stdout="",
        stderr="Could not find service \"com.schwab-cli.mcp\"",
    )


def _kickstart_ok() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["launchctl", "kickstart", "-k", "gui/501/com.schwab-cli.mcp"],
        returncode=0, stdout="", stderr="",
    )


def test_restart_kickstarts_when_launchd_job_is_loaded():
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        if args[:2] == ["launchctl", "list"]:
            return _list_loaded()
        if args[:3] == ["launchctl", "kickstart", "-k"]:
            return _kickstart_ok()
        raise AssertionError(f"unexpected subprocess call: {args}")

    # execvp must NOT be reached when launchd takes the bounce.
    with patch("schwab_cli.commands.mcp.subprocess.run", side_effect=fake_run), \
         patch("os.execvp") as mock_execvp:
        result = runner.invoke(app, ["mcp", "restart"])

    assert result.exit_code == 0, result.output
    assert "kickstarting launchd job" in result.output
    assert any(c[:3] == ["launchctl", "kickstart", "-k"] for c in calls)
    assert not mock_execvp.called, "execvp must not run when launchd handles it"


def test_restart_falls_back_to_execvp_when_no_launchd_job():
    def fake_run(args, **kwargs):
        if args[:2] == ["launchctl", "list"]:
            return _list_not_loaded()
        # ``run_logout`` does an HTTP call, not subprocess; if subprocess
        # is invoked for anything else from this test, that's a bug.
        raise AssertionError(f"unexpected subprocess call: {args}")

    with patch("schwab_cli.commands.mcp.subprocess.run", side_effect=fake_run), \
         patch("schwab_cli.commands.mcp.run_logout") as mock_logout, \
         patch("schwab_cli.commands.mcp.time.sleep"), \
         patch("os.execvp") as mock_execvp:
        # execvp normally replaces the process; mocking lets the test
        # observe the args without actually exec'ing.
        result = runner.invoke(app, ["mcp", "restart"])

    assert result.exit_code == 0, result.output
    assert mock_logout.called
    assert mock_execvp.called
    # Reconstruct the spawn command from the execvp call. The daemon is
    # HTTP-only now, so the fresh invocation carries no transport flag.
    _file, args = mock_execvp.call_args.args
    assert "mcp" in args
    assert "--stdio" not in args
    assert "--sse" not in args


def test_restart_warns_on_non_default_host_port_in_launchd_mode():
    def fake_run(args, **kwargs):
        if args[:2] == ["launchctl", "list"]:
            return _list_loaded()
        if args[:3] == ["launchctl", "kickstart", "-k"]:
            return _kickstart_ok()
        raise AssertionError(f"unexpected subprocess call: {args}")

    with patch("schwab_cli.commands.mcp.subprocess.run", side_effect=fake_run), \
         patch("os.execvp"):
        result = runner.invoke(
            app, ["mcp", "restart", "--port", "9999"],
        )

    assert result.exit_code == 0, result.output
    # Warning shows up on stderr (typer.secho err=True). CliRunner mixes
    # by default unless mix_stderr=False; check the merged output.
    assert "ignored in launchd mode" in result.output
