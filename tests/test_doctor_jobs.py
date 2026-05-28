"""TDD red-phase tests for Phase 5: doctor jobs status section (H3 observability).

Contract (_print_jobs_status helper, called from the doctor run):
  - Reads jobs state via load_state(current_dir()).
  - If any job's last_status is in {"failed","auth-failed","timeout","interrupted"},
    prints a loud failure block listing those job ids + statuses.
  - Otherwise (all-ok state) the jobs section is quiet — no failure block printed.

Tests seed state.json via save_state and then either:
  a) Invoke `schwab doctor` via CliRunner and assert presence/absence of failure text.
  b) Call _print_jobs_status directly if the full doctor command is too heavy.

All tests set SCHWAB_CLI_CONFIG_DIR and mock heavy external dependencies
(MCP status check, launchctl, DB, session, config loading) to keep tests isolated.
These tests FAIL until Phase 5 _print_jobs_status is implemented.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from schwab_cli.cli import app as cli_app
from schwab_cli.server.jobs.state import (
    JobRunState,
    SchedulerState,
    save_state,
)
from schwab_cli.server.jobs.runtime import current_dir as get_current_dir

# ---------------------------------------------------------------------------
# Import guard for _print_jobs_status
# ---------------------------------------------------------------------------

try:
    from schwab_cli.commands.doctor import _print_jobs_status
    _HAS_PRINT_JOBS_STATUS = True
except (ImportError, AttributeError):
    _print_jobs_status = None  # type: ignore[assignment]
    _HAS_PRINT_JOBS_STATUS = False


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_FAIL_STATUSES = ("failed", "auth-failed", "timeout", "interrupted")


def _seed_state(tmp_path: Path, jobs: dict[str, str | None]) -> None:
    """Write state.json under tmp_path/jobs/.current with the given {id: last_status}."""
    current = tmp_path / "jobs" / ".current"
    current.mkdir(parents=True, exist_ok=True)
    job_states = {
        job_id: JobRunState(id=job_id, last_status=status)
        for job_id, status in jobs.items()
    }
    state = SchedulerState(jobs=job_states, updated_at=1700000000.0)
    save_state(current, state)


def _make_doctor_mocks(monkeypatch, tmp_path: Path) -> None:
    """Mock all heavy doctor dependencies so `schwab doctor` can run in an isolated tmp dir."""
    # Redirect config dir.
    monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))

    # Mock httpx calls (MCP status check, Telegram).
    try:
        import httpx
        monkeypatch.setattr(
            "schwab_cli.commands.doctor._mcp_status",
            lambda: None,
        )
    except Exception:
        pass

    # Mock launchctl so _launchctl_loaded always returns False.
    monkeypatch.setattr(
        "schwab_cli.commands.doctor._launchctl_loaded",
        lambda label: False,
    )

    # Mock shutil.which so binary is "found".
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/local/bin/{name}")

    # Mock config + session so auth section doesn't fail on missing files.
    try:
        import schwab_cli.config as config_mod
        from unittest.mock import MagicMock
        fake_cfg = MagicMock()
        fake_cfg.auth_flow = "oauth"
        fake_cfg.auto_login_command = None
        monkeypatch.setattr(config_mod, "load", lambda: fake_cfg)
    except Exception:
        pass

    try:
        import schwab_cli.session as session_mod
        monkeypatch.setattr(session_mod, "load", lambda: None)
    except Exception:
        pass

    # Mock Telegram notify config so it doesn't load real files.
    try:
        from schwab_cli.notify import config as notify_config_mod
        from unittest.mock import MagicMock
        fake_notify_cfg = MagicMock()
        fake_notify_cfg.telegram.configured = False
        monkeypatch.setattr(notify_config_mod, "load", lambda: fake_notify_cfg)
    except Exception:
        pass

    # Mock vol_history DB connection so _check_dataset doesn't fail.
    try:
        import schwab_cli.storage.vol_history as vol_hist_mod
        from unittest.mock import MagicMock, patch
        import contextlib

        @contextlib.contextmanager
        def fake_connect():
            conn = MagicMock()
            # Make execute().fetchall() / fetchone() return empty results.
            conn.execute.return_value.fetchall.return_value = []
            conn.execute.return_value.fetchone.return_value = None
            yield conn

        monkeypatch.setattr(vol_hist_mod, "connect", fake_connect)
    except Exception:
        pass

    # Mock sync_scheduler._last_run_path so it doesn't hit the real filesystem.
    try:
        import schwab_cli.dataset.sync_scheduler as sync_sched_mod
        monkeypatch.setattr(
            sync_sched_mod,
            "_last_run_path",
            lambda: tmp_path / "nonexistent_last_run.json",
        )
    except Exception:
        pass

    # Mock audit_log so _parse_last_indices_run doesn't scan real files.
    try:
        import schwab_cli.dataset.audit_log as audit_log_mod
        monkeypatch.setattr(
            audit_log_mod,
            "audit_log_path",
            lambda: tmp_path / "scheduler.log",
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tests: _print_jobs_status helper directly
# ---------------------------------------------------------------------------


class TestPrintJobsStatusHelper:
    """Direct unit tests for _print_jobs_status."""

    def test_function_importable(self):
        assert _HAS_PRINT_JOBS_STATUS, (
            "schwab_cli.commands.doctor._print_jobs_status not importable"
        )

    def test_prints_failure_block_for_failed_job(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        assert _HAS_PRINT_JOBS_STATUS

        _seed_state(tmp_path, {"market-data": "failed"})

        # Call _print_jobs_status with the config dir pointing at tmp_path.
        _print_jobs_status(config_dir=tmp_path)

        captured = capsys.readouterr()
        out = captured.out + captured.err
        assert "market-data" in out, (
            f"Expected job id 'market-data' in output. Got:\n{out}"
        )

    def test_prints_failed_status_label(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        assert _HAS_PRINT_JOBS_STATUS

        _seed_state(tmp_path, {"accounts": "failed"})
        _print_jobs_status(config_dir=tmp_path)

        captured = capsys.readouterr()
        out = captured.out + captured.err
        # Must mention "fail" in some form.
        assert "fail" in out.lower(), (
            f"Expected 'fail' in output for failed job. Got:\n{out}"
        )

    @pytest.mark.parametrize("bad_status", _FAIL_STATUSES)
    def test_prints_failure_block_for_each_bad_status(
        self, monkeypatch, tmp_path, capsys, bad_status
    ):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        assert _HAS_PRINT_JOBS_STATUS

        _seed_state(tmp_path, {"my-job": bad_status})
        _print_jobs_status(config_dir=tmp_path)

        captured = capsys.readouterr()
        out = captured.out + captured.err
        # Must mention the job id.
        assert "my-job" in out, (
            f"For status={bad_status!r}, expected 'my-job' in output. Got:\n{out}"
        )

    def test_quiet_when_all_ok(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        assert _HAS_PRINT_JOBS_STATUS

        _seed_state(tmp_path, {"market-data": "success", "accounts": "success"})
        _print_jobs_status(config_dir=tmp_path)

        captured = capsys.readouterr()
        out = captured.out + captured.err
        # Must NOT print a failure block.
        assert "fail" not in out.lower(), (
            f"Expected no failure block for all-ok state. Got:\n{out}"
        )
        # The jobs section should be quiet (no loud failure markers).
        assert "market-data" not in out or "fail" not in out.lower(), (
            f"Unexpected failure mention for all-ok state. Got:\n{out}"
        )

    def test_quiet_when_no_state_file(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        assert _HAS_PRINT_JOBS_STATUS

        # No state.json written — empty state.
        _print_jobs_status(config_dir=tmp_path)

        captured = capsys.readouterr()
        out = captured.out + captured.err
        assert "fail" not in out.lower(), (
            f"Expected no failure output for empty state. Got:\n{out}"
        )

    def test_lists_multiple_failing_jobs(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        assert _HAS_PRINT_JOBS_STATUS

        _seed_state(tmp_path, {
            "market-data": "failed",
            "accounts": "timeout",
            "indices": "success",
        })
        _print_jobs_status(config_dir=tmp_path)

        captured = capsys.readouterr()
        out = captured.out + captured.err
        assert "market-data" in out, f"Expected 'market-data' in failure output. Got:\n{out}"
        assert "accounts" in out, f"Expected 'accounts' in failure output. Got:\n{out}"

    def test_does_not_mention_ok_job_in_failure_block(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        assert _HAS_PRINT_JOBS_STATUS

        _seed_state(tmp_path, {
            "market-data": "failed",
            "indices": "success",
        })
        _print_jobs_status(config_dir=tmp_path)

        captured = capsys.readouterr()
        out = captured.out + captured.err
        # "market-data" should be in the failure block, "indices" should not be
        # mentioned as a failure.
        assert "market-data" in out
        # "indices" might appear, but must not be labelled as failed.
        if "indices" in out:
            # Confirm there's no "fail" adjacent to "indices" — rough heuristic.
            idx = out.lower().find("indices")
            nearby = out[max(0, idx - 20): idx + 30].lower()
            assert "fail" not in nearby, (
                f"'indices' (ok job) appears near 'fail' in output. Got:\n{out}"
            )

    def test_signature_accepts_config_dir(self):
        """_print_jobs_status must accept a config_dir keyword argument."""
        assert _HAS_PRINT_JOBS_STATUS
        import inspect
        sig = inspect.signature(_print_jobs_status)
        params = sig.parameters
        assert "config_dir" in params, (
            f"_print_jobs_status must accept 'config_dir' kwarg. Params: {list(params)}"
        )


# ---------------------------------------------------------------------------
# Tests: schwab doctor CLI integration
# ---------------------------------------------------------------------------


class TestDoctorJobsSection:
    """`schwab doctor` output includes jobs status when there are failures."""

    def test_doctor_runs_without_crashing(self, monkeypatch, tmp_path):
        _make_doctor_mocks(monkeypatch, tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli_app, ["doctor"])
        # Should not hard-crash (exit code 0 or non-zero, but not an unhandled exception).
        assert result.exception is None or isinstance(result.exception, SystemExit), (
            f"doctor raised unexpected exception: {result.exception}\n"
            f"Output:\n{result.output}"
        )

    def test_doctor_mentions_failing_job(self, monkeypatch, tmp_path):
        _make_doctor_mocks(monkeypatch, tmp_path)
        _seed_state(tmp_path, {"market-data": "failed"})

        runner = CliRunner()
        result = runner.invoke(cli_app, ["doctor"])

        assert "market-data" in result.output, (
            f"Expected failing job 'market-data' in doctor output.\n"
            f"Output:\n{result.output}"
        )

    def test_doctor_mentions_fail_keyword_for_failed_job(self, monkeypatch, tmp_path):
        _make_doctor_mocks(monkeypatch, tmp_path)
        _seed_state(tmp_path, {"accounts": "failed"})

        runner = CliRunner()
        result = runner.invoke(cli_app, ["doctor"])

        assert "fail" in result.output.lower(), (
            f"Expected 'fail' in doctor output for failed job.\n"
            f"Output:\n{result.output}"
        )

    def test_doctor_quiet_for_all_ok_jobs(self, monkeypatch, tmp_path):
        _make_doctor_mocks(monkeypatch, tmp_path)
        _seed_state(tmp_path, {
            "market-data": "success",
            "accounts": "success",
            "indices": "success",
        })

        runner = CliRunner()
        result = runner.invoke(cli_app, ["doctor"])

        # The failure block should NOT appear.
        # "fail" might appear in other sections (e.g. "failed to connect"), so
        # we specifically check the jobs section is not producing it.
        # We check that none of our job ids appear next to "fail".
        output_lower = result.output.lower()
        for stem in ("market-data", "accounts", "indices"):
            if stem in output_lower:
                # Find all occurrences and check no "fail" in nearby context.
                idx = 0
                while True:
                    pos = output_lower.find(stem, idx)
                    if pos == -1:
                        break
                    nearby = output_lower[max(0, pos - 10): pos + len(stem) + 20]
                    assert "fail" not in nearby, (
                        f"Job {stem!r} appeared near 'fail' in all-ok doctor output. "
                        f"Context: {nearby!r}\nFull output:\n{result.output}"
                    )
                    idx = pos + 1

    def test_doctor_mentions_auth_failed_status(self, monkeypatch, tmp_path):
        _make_doctor_mocks(monkeypatch, tmp_path)
        _seed_state(tmp_path, {"market-data": "auth-failed"})

        runner = CliRunner()
        result = runner.invoke(cli_app, ["doctor"])

        assert "market-data" in result.output, (
            f"Expected 'market-data' (auth-failed status) in doctor output.\n"
            f"Output:\n{result.output}"
        )

    def test_doctor_mentions_timeout_status(self, monkeypatch, tmp_path):
        _make_doctor_mocks(monkeypatch, tmp_path)
        _seed_state(tmp_path, {"indices": "timeout"})

        runner = CliRunner()
        result = runner.invoke(cli_app, ["doctor"])

        assert "indices" in result.output, (
            f"Expected 'indices' (timeout status) in doctor output.\n"
            f"Output:\n{result.output}"
        )
