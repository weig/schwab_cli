"""TDD red-phase tests for Phase 5: dataset cron install becomes a deprecation NO-OP.

Contract:
  - `schwab dataset cron install` must NOT call install_plist (no launchd write).
  - Must print a deprecation message pointing to `schwab jobs migrate` / `schwab server`.
  - Must exit 0.
  - `schwab dataset cron uninstall` is left functional (still calls
    uninstall_all_schwab_plists).

All tests mock install_plist where dataset.py uses it so no real launchctl
or LaunchAgents writes happen.
These tests FAIL until Phase 5 implementation is merged.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from schwab_cli.cli import app as cli_app


# ---------------------------------------------------------------------------
# dataset cron install — deprecated NO-OP
# ---------------------------------------------------------------------------


class TestDatasetCronInstallDeprecated:
    """`schwab dataset cron install` is a NO-OP warning after Phase 5."""

    def test_exits_0(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        # Patch install_plist where dataset.py imports it to detect accidental calls.
        mock_install = MagicMock(return_value=tmp_path / "fake.plist")
        monkeypatch.setattr(
            "schwab_cli.commands.dataset.install_plist",
            mock_install,
        )
        # Also patch uninstall so there's no real launchctl call.
        monkeypatch.setattr(
            "schwab_cli.commands.dataset.uninstall_all_schwab_plists"
            if hasattr(  # guard in case the import path changes
                __import__("schwab_cli.commands.dataset", fromlist=["uninstall_all_schwab_plists"]),
                "uninstall_all_schwab_plists",
            ) else "schwab_cli.dataset.launchd.uninstall_all_schwab_plists",
            lambda: [],
        )
        runner = CliRunner()
        result = runner.invoke(cli_app, ["dataset", "cron", "install"])
        assert result.exit_code == 0, (
            f"Expected exit 0 for deprecated cron install, got {result.exit_code}.\n"
            f"Output: {result.output}"
        )

    def test_install_plist_not_called(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        mock_install = MagicMock(return_value=tmp_path / "fake.plist")
        monkeypatch.setattr(
            "schwab_cli.commands.dataset.install_plist",
            mock_install,
        )
        runner = CliRunner()
        runner.invoke(cli_app, ["dataset", "cron", "install"])
        mock_install.assert_not_called()

    def test_output_mentions_deprecation_keyword(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(
            "schwab_cli.commands.dataset.install_plist",
            MagicMock(return_value=tmp_path / "fake.plist"),
        )
        runner = CliRunner()
        result = runner.invoke(cli_app, ["dataset", "cron", "install"])
        output_lower = result.output.lower()
        # Must mention deprecation in some form
        deprecated_words = {"deprecated", "deprecat", "no longer", "removed", "replaced"}
        assert any(w in output_lower for w in deprecated_words), (
            f"Expected a deprecation notice in output. Got:\n{result.output}"
        )

    def test_output_mentions_jobs_migrate_or_server(self, monkeypatch, tmp_path):
        """Deprecation message must point users toward the new workflow."""
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(
            "schwab_cli.commands.dataset.install_plist",
            MagicMock(return_value=tmp_path / "fake.plist"),
        )
        runner = CliRunner()
        result = runner.invoke(cli_app, ["dataset", "cron", "install"])
        output = result.output
        # Must guide users toward either `schwab jobs migrate` or `schwab server`
        has_jobs = "jobs" in output.lower()
        has_server = "server" in output.lower()
        assert has_jobs or has_server, (
            f"Deprecation message must mention 'jobs' (migrate) or 'server'. Got:\n{output}"
        )

    def test_no_plist_file_written(self, monkeypatch, tmp_path):
        """No file must be written to LaunchAgents (or tmp_path)."""
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        # Count files before.
        monkeypatch.setattr(
            "schwab_cli.commands.dataset.install_plist",
            MagicMock(side_effect=AssertionError("install_plist called unexpectedly")),
        )
        runner = CliRunner()
        # If install_plist raises, it means the production code still calls it.
        # We expect it NOT to be called, so the command should succeed with exit 0.
        # If our mock raises, the test would fail with a non-0 exit — that's the red signal.
        result = runner.invoke(cli_app, ["dataset", "cron", "install"])
        # Should not have triggered the side_effect (install_plist not called).
        assert result.exit_code == 0

    def test_command_still_exists(self, monkeypatch, tmp_path):
        """The command must still be registered (not removed), just a NO-OP."""
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        runner = CliRunner()
        result = runner.invoke(cli_app, ["dataset", "cron", "install", "--help"])
        assert "No such command" not in (result.output or ""), (
            "dataset cron install must still be a registered command"
        )


# ---------------------------------------------------------------------------
# dataset cron uninstall — must remain functional
# ---------------------------------------------------------------------------


class TestDatasetCronUninstallStillFunctional:
    """`schwab dataset cron uninstall` must still call uninstall_all_schwab_plists."""

    def test_command_still_exists(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        runner = CliRunner()
        result = runner.invoke(cli_app, ["dataset", "cron", "uninstall", "--help"])
        assert "No such command" not in (result.output or "")

    def test_uninstall_calls_uninstall_all_schwab_plists(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        call_count = {"n": 0}

        def fake_uninstall():
            call_count["n"] += 1
            return []

        # Patch inside the dataset command module.
        try:
            monkeypatch.setattr(
                "schwab_cli.commands.dataset.uninstall_all_schwab_plists",
                fake_uninstall,
            )
        except AttributeError:
            # If not imported at module level, patch the launchd module directly.
            monkeypatch.setattr(
                "schwab_cli.dataset.launchd.uninstall_all_schwab_plists",
                fake_uninstall,
            )

        runner = CliRunner()
        result = runner.invoke(cli_app, ["dataset", "cron", "uninstall"])
        assert call_count["n"] >= 1, (
            f"Expected uninstall_all_schwab_plists to be called at least once, "
            f"got {call_count['n']}. Output:\n{result.output}"
        )

    def test_uninstall_exits_0_when_nothing_to_remove(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))

        def fake_uninstall():
            return []  # nothing removed

        try:
            monkeypatch.setattr(
                "schwab_cli.commands.dataset.uninstall_all_schwab_plists",
                fake_uninstall,
            )
        except AttributeError:
            monkeypatch.setattr(
                "schwab_cli.dataset.launchd.uninstall_all_schwab_plists",
                fake_uninstall,
            )

        runner = CliRunner()
        result = runner.invoke(cli_app, ["dataset", "cron", "uninstall"])
        assert result.exit_code == 0
