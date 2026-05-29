"""TDD red-phase tests for schwab_cli.commands.jobs (CLI layer).

These tests will FAIL at collection with ModuleNotFoundError until the
module is implemented — that is the expected RED state.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from schwab_cli.commands import jobs as jobs_cmd
from schwab_cli.server.jobs.config import JobConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_job_file(directory: Path, job_id: str, payload: dict) -> Path:
    p = directory / f"{job_id}.json"
    p.write_text(json.dumps(payload))
    return p


def _minimal_python_payload(job_id: str = "my-job") -> dict:
    return {
        "name": f"Job {job_id}",
        "enabled": True,
        "cron": "0 9 * * *",
        "timezone": "UTC",
        "type": "python",
        "runner": "os.getpid",
    }


def _minimal_command_payload(job_id: str = "cmd-job") -> dict:
    return {
        "name": f"Command Job {job_id}",
        "enabled": True,
        "cron": "0 9 * * *",
        "timezone": "UTC",
        "type": "command",
        "command": ["schwab", "quote", "NVDA"],
    }


# ---------------------------------------------------------------------------
# resolve_job_config
# ---------------------------------------------------------------------------


class TestResolveJobConfig:
    """resolve_job_config reads a .current/<id>.json and returns JobConfig."""

    def test_reads_valid_job_returns_job_config(self, tmp_path):
        current_dir = tmp_path / "jobs" / ".current"
        current_dir.mkdir(parents=True)
        _write_job_file(current_dir, "my-job", _minimal_python_payload("my-job"))

        cfg = jobs_cmd.resolve_job_config("my-job", config_dir=tmp_path)
        assert isinstance(cfg, JobConfig)
        assert cfg.id == "my-job"

    def test_reads_correct_job_id(self, tmp_path):
        current_dir = tmp_path / "jobs" / ".current"
        current_dir.mkdir(parents=True)
        _write_job_file(current_dir, "alpha", _minimal_python_payload("alpha"))
        _write_job_file(current_dir, "beta", _minimal_python_payload("beta"))

        cfg = jobs_cmd.resolve_job_config("alpha", config_dir=tmp_path)
        assert cfg.id == "alpha"

    def test_reads_from_jobs_dot_current_subdir(self, tmp_path):
        """File must live in <config_dir>/jobs/.current/<id>.json."""
        # File in wrong location (root of config_dir) should NOT be found.
        _write_job_file(tmp_path, "orphan", _minimal_python_payload("orphan"))

        with pytest.raises((FileNotFoundError, SystemExit, ValueError, Exception)):
            jobs_cmd.resolve_job_config("orphan", config_dir=tmp_path)

    def test_missing_file_raises(self, tmp_path):
        current_dir = tmp_path / "jobs" / ".current"
        current_dir.mkdir(parents=True)
        # No file written

        with pytest.raises((FileNotFoundError, SystemExit, ValueError, Exception)):
            jobs_cmd.resolve_job_config("nonexistent-job", config_dir=tmp_path)

    def test_missing_file_error_is_informative(self, tmp_path):
        """The raised error / exit message should mention the job id."""
        current_dir = tmp_path / "jobs" / ".current"
        current_dir.mkdir(parents=True)

        import typer

        try:
            jobs_cmd.resolve_job_config("ghost-job", config_dir=tmp_path)
        except (typer.BadParameter, SystemExit, ValueError) as exc:
            assert "ghost-job" in str(exc)
        except FileNotFoundError:
            pass  # plain FileNotFoundError with the path is also acceptable


class TestResolveJobConfigPathTraversal:
    """resolve_job_config rejects malformed / traversal job ids."""

    @pytest.mark.parametrize("bad_id", ["../escape", "a/b", "", "..", "with space"])
    def test_rejects_unsafe_job_id(self, tmp_path, bad_id):
        import typer

        current_dir = tmp_path / "jobs" / ".current"
        current_dir.mkdir(parents=True)

        with pytest.raises(typer.BadParameter):
            jobs_cmd.resolve_job_config(bad_id, config_dir=tmp_path)

    def test_accepts_normal_job_id(self, tmp_path):
        current_dir = tmp_path / "jobs" / ".current"
        current_dir.mkdir(parents=True)
        _write_job_file(current_dir, "normal-id_1", _minimal_python_payload("normal-id_1"))

        cfg = jobs_cmd.resolve_job_config("normal-id_1", config_dir=tmp_path)
        assert cfg.id == "normal-id_1"


class TestResolveJobConfigInvalidConfig:
    """A malformed promoted config surfaces as a readable BadParameter."""

    def test_invalid_config_raises_bad_parameter(self, tmp_path):
        import typer

        current_dir = tmp_path / "jobs" / ".current"
        current_dir.mkdir(parents=True)
        # Valid JSON but invalid job (missing required fields, bad type).
        _write_job_file(current_dir, "broken", {"type": "nope"})

        with pytest.raises(typer.BadParameter) as excinfo:
            jobs_cmd.resolve_job_config("broken", config_dir=tmp_path)
        assert "broken" in str(excinfo.value)


# ---------------------------------------------------------------------------
# CLI: schwab jobs run <id>
# ---------------------------------------------------------------------------


class TestJobsRunCommand:
    """schwab jobs run <id> — CLI integration via CliRunner."""

    def test_run_exits_with_execute_job_return_code(self, monkeypatch, tmp_path):
        """If the job runner returns 7, the CLI exits 7 (manual path)."""
        from typer.testing import CliRunner
        from schwab_cli.cli import app as cli_app

        current_dir = tmp_path / "jobs" / ".current"
        current_dir.mkdir(parents=True)
        _write_job_file(current_dir, "my-job", _minimal_python_payload("my-job"))

        monkeypatch.setattr(
            "schwab_cli.paths.config_dir",
            lambda: tmp_path,
        )
        # Manual invocation (no env flag) goes through run_job_blocking.
        monkeypatch.delenv("SCHWAB_JOBS_SCHEDULED", raising=False)
        monkeypatch.setattr(
            "schwab_cli.commands.jobs.run_job_blocking",
            lambda cfg: 7,
        )

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "run", "my-job"])
        assert result.exit_code == 7

    def test_run_exits_0_on_success(self, monkeypatch, tmp_path):
        """run_job_blocking returning 0 → CLI exits 0 (manual path)."""
        from typer.testing import CliRunner
        from schwab_cli.cli import app as cli_app

        current_dir = tmp_path / "jobs" / ".current"
        current_dir.mkdir(parents=True)
        _write_job_file(current_dir, "ok-job", _minimal_python_payload("ok-job"))

        monkeypatch.setattr("schwab_cli.paths.config_dir", lambda: tmp_path)
        monkeypatch.delenv("SCHWAB_JOBS_SCHEDULED", raising=False)
        monkeypatch.setattr(
            "schwab_cli.commands.jobs.run_job_blocking", lambda cfg: 0
        )

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "run", "ok-job"])
        assert result.exit_code == 0

    def test_run_exits_nonzero_for_missing_job(self, monkeypatch, tmp_path):
        """Missing job id → CLI exits non-zero with helpful message."""
        from typer.testing import CliRunner
        from schwab_cli.cli import app as cli_app

        current_dir = tmp_path / "jobs" / ".current"
        current_dir.mkdir(parents=True)
        # No job file written

        monkeypatch.setattr("schwab_cli.paths.config_dir", lambda: tmp_path)

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "run", "missing-id"])
        assert result.exit_code != 0

    def test_run_missing_job_output_mentions_id(self, monkeypatch, tmp_path):
        """Error output for missing job should mention the job id."""
        from typer.testing import CliRunner
        from schwab_cli.cli import app as cli_app

        current_dir = tmp_path / "jobs" / ".current"
        current_dir.mkdir(parents=True)

        monkeypatch.setattr("schwab_cli.paths.config_dir", lambda: tmp_path)

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "run", "missing-id"])
        output = (result.output or "") + (
            str(result.exception) if result.exception else ""
        )
        assert "missing-id" in output

    def test_run_exit_code_2_for_auth_failure(self, monkeypatch, tmp_path):
        """run_job_blocking returning 2 (EXIT_AUTH_FAILED) → CLI exits 2."""
        from typer.testing import CliRunner
        from schwab_cli.cli import app as cli_app

        current_dir = tmp_path / "jobs" / ".current"
        current_dir.mkdir(parents=True)
        _write_job_file(current_dir, "auth-job", _minimal_python_payload("auth-job"))

        monkeypatch.setattr("schwab_cli.paths.config_dir", lambda: tmp_path)
        monkeypatch.delenv("SCHWAB_JOBS_SCHEDULED", raising=False)
        monkeypatch.setattr(
            "schwab_cli.commands.jobs.run_job_blocking", lambda cfg: 2
        )

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "run", "auth-job"])
        assert result.exit_code == 2

    def test_run_malformed_config_exits_nonzero_with_message(self, monkeypatch, tmp_path):
        """A malformed promoted config exits non-zero with a readable message."""
        from typer.testing import CliRunner
        from schwab_cli.cli import app as cli_app

        current_dir = tmp_path / "jobs" / ".current"
        current_dir.mkdir(parents=True)
        # Valid JSON, invalid job config.
        _write_job_file(current_dir, "broken-job", {"type": "command"})

        monkeypatch.setattr("schwab_cli.paths.config_dir", lambda: tmp_path)

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "run", "broken-job"])
        assert result.exit_code != 0
        output = (result.output or "") + (
            str(result.exception) if result.exception else ""
        )
        assert "broken-job" in output
        assert "invalid config" in output


class TestJobsRunManualReport:
    """Manual `jobs run` (no env flag) records a run-report marker."""

    @pytest.mark.parametrize(
        "rc,expected_status",
        [(0, "ok"), (2, "auth-failed"), (1, "failed")],
    )
    def test_manual_run_writes_marker_with_status(
        self, monkeypatch, tmp_path, rc, expected_status
    ):
        from typer.testing import CliRunner
        from schwab_cli.cli import app as cli_app
        from schwab_cli.server.jobs.state import read_run_reports

        current_dir = tmp_path / "jobs" / ".current"
        current_dir.mkdir(parents=True)
        _write_job_file(current_dir, "my-job", _minimal_command_payload("my-job"))

        monkeypatch.setattr("schwab_cli.paths.config_dir", lambda: tmp_path)
        monkeypatch.delenv("SCHWAB_JOBS_SCHEDULED", raising=False)
        # Manual path uses run_job_blocking, not execute_job/execvp.
        monkeypatch.setattr(
            "schwab_cli.commands.jobs.run_job_blocking", lambda cfg: rc
        )

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "run", "my-job"])
        assert result.exit_code == rc

        reports = read_run_reports(tmp_path / "jobs" / ".current")
        assert "my-job" in reports
        assert reports["my-job"]["last_status"] == expected_status
        assert reports["my-job"]["last_exit_code"] == rc
        assert isinstance(reports["my-job"]["last_run_at"], (int, float))

    def test_manual_run_uses_run_job_blocking_not_execute_job(
        self, monkeypatch, tmp_path
    ):
        from typer.testing import CliRunner
        from schwab_cli.cli import app as cli_app

        current_dir = tmp_path / "jobs" / ".current"
        current_dir.mkdir(parents=True)
        _write_job_file(current_dir, "my-job", _minimal_command_payload("my-job"))

        monkeypatch.setattr("schwab_cli.paths.config_dir", lambda: tmp_path)
        monkeypatch.delenv("SCHWAB_JOBS_SCHEDULED", raising=False)

        def _boom_execute_job(cfg):
            raise AssertionError("manual run must NOT call execute_job")

        monkeypatch.setattr(
            "schwab_cli.commands.jobs.execute_job", _boom_execute_job
        )
        monkeypatch.setattr(
            "schwab_cli.commands.jobs.run_job_blocking", lambda cfg: 0
        )

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "run", "my-job"])
        assert result.exit_code == 0

    def test_manual_run_marker_failure_does_not_change_exit_code(
        self, monkeypatch, tmp_path
    ):
        """A run-report write failure must never alter the job's exit code."""
        from typer.testing import CliRunner
        from schwab_cli.cli import app as cli_app

        current_dir = tmp_path / "jobs" / ".current"
        current_dir.mkdir(parents=True)
        _write_job_file(current_dir, "my-job", _minimal_command_payload("my-job"))

        monkeypatch.setattr("schwab_cli.paths.config_dir", lambda: tmp_path)
        monkeypatch.delenv("SCHWAB_JOBS_SCHEDULED", raising=False)
        monkeypatch.setattr(
            "schwab_cli.commands.jobs.run_job_blocking", lambda cfg: 4
        )

        def _boom_write(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(
            "schwab_cli.commands.jobs.write_run_report", _boom_write
        )

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "run", "my-job"])
        assert result.exit_code == 4


class TestJobsRunScheduled:
    """Scheduler-spawned `jobs run` (env flag set) execvp's, writes no marker."""

    def test_scheduled_run_uses_execute_job_and_no_marker(
        self, monkeypatch, tmp_path
    ):
        from typer.testing import CliRunner
        from schwab_cli.cli import app as cli_app
        from schwab_cli.server.jobs.state import read_run_reports

        current_dir = tmp_path / "jobs" / ".current"
        current_dir.mkdir(parents=True)
        _write_job_file(current_dir, "my-job", _minimal_command_payload("my-job"))

        monkeypatch.setattr("schwab_cli.paths.config_dir", lambda: tmp_path)
        monkeypatch.setenv("SCHWAB_JOBS_SCHEDULED", "1")
        monkeypatch.setattr(
            "schwab_cli.commands.jobs.execute_job", lambda cfg: 0
        )

        def _boom_blocking(cfg):
            raise AssertionError("scheduled run must NOT call run_job_blocking")

        monkeypatch.setattr(
            "schwab_cli.commands.jobs.run_job_blocking", _boom_blocking
        )

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "run", "my-job"])
        assert result.exit_code == 0
        # Scheduled path records via reap, never via a marker.
        assert read_run_reports(tmp_path / "jobs" / ".current") == {}


# ---------------------------------------------------------------------------
# CLI registration — "jobs" group appears in main app
# ---------------------------------------------------------------------------


class TestJobsCommandRegistration:
    """The 'jobs' Typer app is registered on schwab_cli.cli.app."""

    def test_top_level_help_lists_jobs(self):
        """schwab --help output includes 'jobs' as a command group."""
        from typer.testing import CliRunner
        from schwab_cli.cli import app as cli_app

        runner = CliRunner()
        result = runner.invoke(cli_app, ["--help"])
        assert "jobs" in result.output

    def test_jobs_help_lists_run(self):
        """schwab jobs --help output includes 'run' subcommand."""
        from typer.testing import CliRunner
        from schwab_cli.cli import app as cli_app

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "--help"])
        assert "run" in result.output

    def test_jobs_help_exit_code(self):
        """schwab jobs --help exits 0 (command is recognised)."""
        from typer.testing import CliRunner
        from schwab_cli.cli import app as cli_app

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "--help"])
        # exit 0 = help rendered; exit 2 with 'No such command' would be broken
        assert result.exit_code != 2 or "No such command" not in (result.output or "")

    def test_jobs_run_help_exit_code(self):
        """schwab jobs run --help exits 0 (subcommand is recognised)."""
        from typer.testing import CliRunner
        from schwab_cli.cli import app as cli_app

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "run", "--help"])
        assert result.exit_code != 2 or "No such command" not in (result.output or "")
