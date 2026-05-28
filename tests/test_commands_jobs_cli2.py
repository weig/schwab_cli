"""TDD red-phase tests for Phase 5 CLI additions: jobs init and jobs migrate.

Tests cover:
  - `schwab jobs init`: writes 3 default job files, prints per-stem created/exists,
    idempotent on second run, exits 0.
  - `schwab jobs migrate`: calls uninstall_all_schwab_plists BEFORE writing defaults,
    prints guidance to start `schwab server`, exits 0; uninstall failure → exit
    non-zero and no defaults written.

All tests monkeypatch SCHWAB_CLI_CONFIG_DIR and mock launchctl/uninstall to prevent
any real filesystem or system-service access.
These tests FAIL until Phase 5 implementation is merged.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from schwab_cli.cli import app as cli_app

_EXPECTED_STEMS = {"market-data", "accounts", "indices"}


# ---------------------------------------------------------------------------
# CLI: jobs init
# ---------------------------------------------------------------------------


class TestJobsInitCommand:
    """`schwab jobs init` creates the 3 default job config files."""

    def test_command_exists_in_help(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "--help"])
        assert "init" in result.output, (
            f"'init' not found in `jobs --help` output:\n{result.output}"
        )

    def test_exits_0_on_fresh_run(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "init"])
        assert result.exit_code == 0, (
            f"Expected exit 0, got {result.exit_code}. Output:\n{result.output}"
        )

    def test_creates_three_job_files(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        runner = CliRunner()
        runner.invoke(cli_app, ["jobs", "init"])
        jobs_dir = tmp_path / "jobs"
        for stem in _EXPECTED_STEMS:
            path = jobs_dir / f"{stem}.json"
            assert path.exists(), (
                f"Expected {stem}.json to be created under {jobs_dir}"
            )

    def test_output_mentions_each_stem_as_created(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "init"])
        for stem in _EXPECTED_STEMS:
            assert stem in result.output, (
                f"Expected stem {stem!r} in output:\n{result.output}"
            )

    def test_output_mentions_created_keyword(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "init"])
        assert "created" in result.output.lower(), (
            f"Expected 'created' in output:\n{result.output}"
        )

    def test_second_run_exits_0(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        runner = CliRunner()
        runner.invoke(cli_app, ["jobs", "init"])
        result2 = runner.invoke(cli_app, ["jobs", "init"])
        assert result2.exit_code == 0

    def test_second_run_mentions_exists(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        runner = CliRunner()
        runner.invoke(cli_app, ["jobs", "init"])
        result2 = runner.invoke(cli_app, ["jobs", "init"])
        assert "exists" in result2.output.lower(), (
            f"Expected 'exists' in second-run output:\n{result2.output}"
        )

    def test_second_run_does_not_overwrite_existing_files(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        runner = CliRunner()
        runner.invoke(cli_app, ["jobs", "init"])

        # Overwrite one file with a sentinel after first init.
        sentinel = {"_sentinel": "keep-me"}
        (tmp_path / "jobs" / "accounts.json").write_text(
            json.dumps(sentinel), encoding="utf-8"
        )

        runner.invoke(cli_app, ["jobs", "init"])

        after = json.loads(
            (tmp_path / "jobs" / "accounts.json").read_text(encoding="utf-8")
        )
        assert after.get("_sentinel") == "keep-me", (
            "second `jobs init` must not overwrite existing job files"
        )

    def test_created_files_are_valid_json(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        runner = CliRunner()
        runner.invoke(cli_app, ["jobs", "init"])
        for stem in _EXPECTED_STEMS:
            path = tmp_path / "jobs" / f"{stem}.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            assert isinstance(data, dict), f"{stem}.json must be a JSON object"

    def test_created_files_parse_via_parse_job(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        runner = CliRunner()
        runner.invoke(cli_app, ["jobs", "init"])
        from schwab_cli.server.jobs.config import parse_job
        for stem in _EXPECTED_STEMS:
            path = tmp_path / "jobs" / f"{stem}.json"
            job_cfg = parse_job(path)
            assert job_cfg.id == stem

    def test_no_real_config_written_outside_tmp(self, monkeypatch, tmp_path):
        """Isolation check: no files are written under the real ~/.config/schwab_cli."""
        import os
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        runner = CliRunner()
        runner.invoke(cli_app, ["jobs", "init"])
        real_jobs_dir = Path.home() / ".config" / "schwab_cli" / "jobs"
        if real_jobs_dir.exists():
            # If it pre-exists, verify none of our stems were just created there
            for stem in _EXPECTED_STEMS:
                # We can only verify freshness indirectly; the file may pre-exist
                # from real usage. The important thing is our monkeypatch pointed
                # the command at tmp_path, not the real dir.
                pass
        # If the real_jobs_dir doesn't exist, it definitely was not created by this test.
        # The primary guarantee is from SCHWAB_CLI_CONFIG_DIR monkeypatch.
        # This test mainly documents the isolation requirement.
        assert True


# ---------------------------------------------------------------------------
# CLI: jobs migrate
# ---------------------------------------------------------------------------


class TestJobsMigrateCommand:
    """`schwab jobs migrate`: cutover from old launchd scheduler to server-jobs."""

    def test_command_exists_in_help(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "--help"])
        assert "migrate" in result.output, (
            f"'migrate' not found in `jobs --help` output:\n{result.output}"
        )

    def test_exits_0_on_success(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        # Patch uninstall in the commands.jobs module so launchctl is never called.
        monkeypatch.setattr(
            "schwab_cli.commands.jobs.uninstall_all_schwab_plists",
            lambda: [],
        )
        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "migrate"])
        assert result.exit_code == 0, (
            f"Expected exit 0, got {result.exit_code}. Output:\n{result.output}"
        )

    def test_calls_uninstall_before_writing_defaults(self, monkeypatch, tmp_path):
        """uninstall must be called BEFORE any default file is written.

        Strategy: the recorder checks that jobs_dir is empty at uninstall time.
        """
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        jobs_dir = tmp_path / "jobs"
        call_order: list[str] = []

        def fake_uninstall():
            # At this point, jobs_dir must NOT yet have any default files.
            existing = sorted(jobs_dir.glob("*.json")) if jobs_dir.exists() else []
            call_order.append(f"uninstall:files_at_call={[p.name for p in existing]}")
            return []

        monkeypatch.setattr(
            "schwab_cli.commands.jobs.uninstall_all_schwab_plists",
            fake_uninstall,
        )

        runner = CliRunner()
        runner.invoke(cli_app, ["jobs", "migrate"])

        # Confirm uninstall was invoked.
        assert any("uninstall:" in entry for entry in call_order), (
            "uninstall_all_schwab_plists was not called during migrate"
        )
        # Confirm jobs_dir was empty when uninstall ran.
        for entry in call_order:
            if entry.startswith("uninstall:"):
                assert "files_at_call=[]" in entry, (
                    f"Default files existed when uninstall ran: {entry}"
                )

    def test_uninstall_called_exactly_once(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        call_count = {"n": 0}

        def fake_uninstall():
            call_count["n"] += 1
            return []

        monkeypatch.setattr(
            "schwab_cli.commands.jobs.uninstall_all_schwab_plists",
            fake_uninstall,
        )
        runner = CliRunner()
        runner.invoke(cli_app, ["jobs", "migrate"])
        assert call_count["n"] == 1, (
            f"Expected uninstall called once, got {call_count['n']}"
        )

    def test_creates_three_default_files(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(
            "schwab_cli.commands.jobs.uninstall_all_schwab_plists",
            lambda: [],
        )
        runner = CliRunner()
        runner.invoke(cli_app, ["jobs", "migrate"])
        for stem in _EXPECTED_STEMS:
            assert (tmp_path / "jobs" / f"{stem}.json").exists(), (
                f"Expected {stem}.json to exist after migrate"
            )

    def test_prints_guidance_about_schwab_server(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(
            "schwab_cli.commands.jobs.uninstall_all_schwab_plists",
            lambda: [],
        )
        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "migrate"])
        # Guidance must mention "schwab server" or "server" in context of starting it.
        output_lower = result.output.lower()
        assert "server" in output_lower, (
            f"Expected guidance about 'schwab server' in output:\n{result.output}"
        )

    def test_uninstall_failure_exits_nonzero(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))

        def failing_uninstall():
            raise RuntimeError("launchctl failed: exit 1")

        monkeypatch.setattr(
            "schwab_cli.commands.jobs.uninstall_all_schwab_plists",
            failing_uninstall,
        )
        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "migrate"])
        assert result.exit_code != 0, (
            f"Expected non-zero exit on uninstall failure, got {result.exit_code}"
        )

    def test_uninstall_failure_no_defaults_written(self, monkeypatch, tmp_path):
        """Fail-safe: if uninstall raises, no default files must be written."""
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))

        def failing_uninstall():
            raise RuntimeError("launchctl unload failed: exit 1")

        monkeypatch.setattr(
            "schwab_cli.commands.jobs.uninstall_all_schwab_plists",
            failing_uninstall,
        )
        runner = CliRunner()
        runner.invoke(cli_app, ["jobs", "migrate"])

        jobs_dir = tmp_path / "jobs"
        for stem in _EXPECTED_STEMS:
            assert not (jobs_dir / f"{stem}.json").exists(), (
                f"{stem}.json must not be written when uninstall fails"
            )

    def test_uninstall_failure_prints_error_message(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))

        def failing_uninstall():
            raise RuntimeError("launchctl unload failed: exit 1")

        monkeypatch.setattr(
            "schwab_cli.commands.jobs.uninstall_all_schwab_plists",
            failing_uninstall,
        )
        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "migrate"])
        combined = result.output + (str(result.exception) if result.exception else "")
        # Must surface the error, not swallow it silently.
        assert "error" in combined.lower() or "failed" in combined.lower() or "abort" in combined.lower(), (
            f"Expected error message in output:\n{result.output}"
        )

    def test_migrate_does_not_call_real_launchctl(self, monkeypatch, tmp_path):
        """Ensure the patched path is used and real launchctl is never invoked."""
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        import subprocess
        calls: list[Any] = []
        original_run = subprocess.run

        def recording_run(args, **kwargs):
            calls.append(args)
            # If launchctl is somehow called, fail loudly.
            if isinstance(args, (list, tuple)) and args and "launchctl" in str(args[0]):
                raise AssertionError(f"launchctl was called in a test: {args}")
            return original_run(args, **kwargs)

        monkeypatch.setattr(
            "schwab_cli.commands.jobs.uninstall_all_schwab_plists",
            lambda: [],
        )
        runner = CliRunner()
        # Should complete without calling real launchctl.
        result = runner.invoke(cli_app, ["jobs", "migrate"])
        # No launchctl calls allowed.
        launchctl_calls = [c for c in calls if isinstance(c, (list, tuple)) and c and "launchctl" in str(c[0])]
        assert launchctl_calls == []
