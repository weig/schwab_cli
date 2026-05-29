"""Tests for the doctor "Data Sync Service" Jobs block (H3 observability).

Contract (``_print_jobs_block``, called from the doctor run):
  - Reads promoted jobs + run state via ``status_payload(config_dir=...)``.
  - Renders one stanza per job (sorted by id): a header line, then an
    indented ``last run`` line and (when scheduled) a ``next run`` line.
  - A job whose ``last_status`` is in {failed, auth-failed, timeout,
    interrupted} (or whose state is ``error``) is loudly surfaced with a
    ``✗`` failure marker; healthy jobs get a ``✓``/plain header.

Tests seed ``jobs/.current/state.json`` via ``save_state`` and seed promoted
configs via ``jobs/.current/<id>.json`` so ``status_payload`` lists them.
All tests set ``SCHWAB_CLI_CONFIG_DIR`` for isolation — the real
``~/.config/schwab_cli/jobs`` is never touched.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from schwab_cli.cli import app as cli_app
from schwab_cli.commands.doctor import _print_jobs_block
from schwab_cli.server.jobs.state import (
    JobRunState,
    SchedulerState,
    save_state,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_FAIL_STATUSES = ("failed", "auth-failed", "timeout", "interrupted")

# Minimal promoted-job config fields status_payload's config loader needs.
_DEF_CRON = "0 17 * * *"
_DEF_TZ = "America/New_York"


def _seed_config(current: Path, job_id: str, *, enabled: bool = True) -> None:
    """Write a promoted job config so status_payload lists it as a job."""
    current.mkdir(parents=True, exist_ok=True)
    (current / f"{job_id}.json").write_text(
        json.dumps(
            {
                "name": job_id,
                "enabled": enabled,
                "cron": _DEF_CRON,
                "timezone": _DEF_TZ,
                "type": "command",
                "command": ["schwab", "dataset", "sync"],
            }
        ),
        encoding="utf-8",
    )


def _seed_jobs(tmp_path: Path, jobs: dict[str, str | None]) -> None:
    """Seed promoted configs + state.json for {id: last_status}."""
    current = tmp_path / "jobs" / ".current"
    for job_id in jobs:
        _seed_config(current, job_id)
    job_states = {
        job_id: JobRunState(
            id=job_id,
            last_status=status,
            last_run_at=1700000000.0 if status is not None else None,
            next_run_at=1700100000.0,
        )
        for job_id, status in jobs.items()
    }
    state = SchedulerState(jobs=job_states, updated_at=1700000000.0)
    save_state(current, state)


def _make_doctor_mocks(monkeypatch, tmp_path: Path) -> None:
    """Mock heavy doctor dependencies so `schwab doctor` runs isolated."""
    monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))

    monkeypatch.setattr(
        "schwab_cli.commands.doctor._mcp_status", lambda: None
    )
    monkeypatch.setattr(
        "schwab_cli.commands.doctor._launchctl_loaded", lambda label: False
    )

    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/local/bin/{name}")

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

    try:
        from schwab_cli.notify import config as notify_config_mod
        from unittest.mock import MagicMock

        fake_notify_cfg = MagicMock()
        fake_notify_cfg.telegram.configured = False
        monkeypatch.setattr(notify_config_mod, "load", lambda: fake_notify_cfg)
    except Exception:
        pass

    # Stub the DB so _check_dataset + _print_data_freshness don't hit disk.
    try:
        import contextlib
        from unittest.mock import MagicMock

        import schwab_cli.storage.vol_history as vol_hist_mod

        @contextlib.contextmanager
        def fake_connect():
            conn = MagicMock()
            conn.execute.return_value.fetchall.return_value = []
            conn.execute.return_value.fetchone.return_value = None
            yield conn

        monkeypatch.setattr(vol_hist_mod, "connect", fake_connect)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tests: _print_jobs_block helper directly
# ---------------------------------------------------------------------------


class TestPrintJobsBlock:
    """Direct unit tests for _print_jobs_block."""

    def test_signature_accepts_config_dir(self):
        import inspect

        params = inspect.signature(_print_jobs_block).parameters
        assert "config_dir" in params, (
            f"_print_jobs_block must accept 'config_dir' kwarg. "
            f"Params: {list(params)}"
        )

    def test_lists_failing_job_with_status(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        _seed_jobs(tmp_path, {"market-data": "failed"})

        _print_jobs_block(config_dir=tmp_path)

        out = capsys.readouterr().out
        assert "market-data" in out, f"expected job id in output:\n{out}"
        assert "✗" in out, f"expected failure marker for failed job:\n{out}"

    @pytest.mark.parametrize("bad_status", _FAIL_STATUSES)
    def test_marks_each_bad_status_loud(
        self, monkeypatch, tmp_path, capsys, bad_status
    ):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        _seed_jobs(tmp_path, {"my-job": bad_status})

        _print_jobs_block(config_dir=tmp_path)

        out = capsys.readouterr().out
        assert "my-job" in out
        assert "✗" in out, f"status={bad_status!r} should be loud:\n{out}"

    def test_ok_job_not_marked_failed(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        _seed_jobs(tmp_path, {"market-data": "success"})

        _print_jobs_block(config_dir=tmp_path)

        out = capsys.readouterr().out
        assert "market-data" in out
        assert "✗" not in out, f"healthy job should not be loud:\n{out}"

    def test_scheduled_ok_and_failed_together(
        self, monkeypatch, tmp_path, capsys
    ):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        _seed_jobs(tmp_path, {"accounts": "success", "indices": "failed"})

        _print_jobs_block(config_dir=tmp_path)

        out = capsys.readouterr().out
        # Both listed.
        assert "accounts" in out
        assert "indices" in out
        # Exactly one failure marker, attached to indices.
        idx = out.find("indices")
        acct = out.find("accounts")
        # The failing job's header carries the ✗.
        assert "✗" in out
        # The ok job's header line itself must not be a ✗ line.
        acct_line = next(ln for ln in out.splitlines() if ln.strip().startswith(
            ("✓", "✗")) and "accounts" in ln)
        assert acct_line.lstrip().startswith("✓"), acct_line

    def test_empty_when_no_jobs(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        # No promoted configs at all.
        _print_jobs_block(config_dir=tmp_path)

        out = capsys.readouterr().out
        assert "none configured" in out.lower()
        assert "schwab jobs init" in out

    def test_shows_last_and_next_run_for_scheduled(
        self, monkeypatch, tmp_path, capsys
    ):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        _seed_jobs(tmp_path, {"market-data": "success"})

        _print_jobs_block(config_dir=tmp_path)

        out = capsys.readouterr().out
        assert "last run" in out
        assert "next run" in out


# ---------------------------------------------------------------------------
# Tests: schwab doctor CLI integration
# ---------------------------------------------------------------------------


class TestDoctorJobsSection:
    """`schwab doctor` surfaces the jobs block, loud on failures."""

    def test_doctor_runs_without_crashing(self, monkeypatch, tmp_path):
        _make_doctor_mocks(monkeypatch, tmp_path)
        result = CliRunner().invoke(cli_app, ["doctor"])
        assert result.exception is None or isinstance(
            result.exception, SystemExit
        ), (
            f"doctor raised unexpected exception: {result.exception}\n"
            f"Output:\n{result.output}"
        )

    def test_doctor_mentions_failing_job(self, monkeypatch, tmp_path):
        _make_doctor_mocks(monkeypatch, tmp_path)
        _seed_jobs(tmp_path, {"market-data": "failed"})

        result = CliRunner().invoke(cli_app, ["doctor"])

        assert "market-data" in result.output, (
            f"Expected failing job in doctor output.\n{result.output}"
        )
        assert "✗" in result.output

    def test_doctor_quiet_for_all_ok_jobs(self, monkeypatch, tmp_path):
        _make_doctor_mocks(monkeypatch, tmp_path)
        _seed_jobs(
            tmp_path,
            {"market-data": "success", "accounts": "success"},
        )

        result = CliRunner().invoke(cli_app, ["doctor"])

        out_lower = result.output.lower()
        for stem in ("market-data", "accounts"):
            assert stem in out_lower
            for ln in result.output.splitlines():
                if stem in ln and ln.strip().startswith(("✓", "✗")):
                    assert ln.lstrip().startswith("✓"), (
                        f"all-ok job {stem!r} got a failure marker: {ln!r}"
                    )

    def test_doctor_shows_runner_line(self, monkeypatch, tmp_path):
        _make_doctor_mocks(monkeypatch, tmp_path)
        result = CliRunner().invoke(cli_app, ["doctor"])
        assert "Runner" in result.output, (
            f"Expected Runner line in doctor output.\n{result.output}"
        )

    def test_doctor_marks_auth_failed_loud(self, monkeypatch, tmp_path):
        _make_doctor_mocks(monkeypatch, tmp_path)
        _seed_jobs(tmp_path, {"market-data": "auth-failed"})

        result = CliRunner().invoke(cli_app, ["doctor"])

        assert "market-data" in result.output
        assert "✗" in result.output


class TestLegacyLeftover:
    """Legacy launchd scheduler leftover detection."""

    def test_warns_when_legacy_plist_present(self, monkeypatch, tmp_path):
        _make_doctor_mocks(monkeypatch, tmp_path)
        legacy = tmp_path / "com.schwab-cli.scheduler.plist"
        legacy.write_bytes(b"<plist></plist>")
        monkeypatch.setattr(
            "schwab_cli.commands.doctor._SCHEDULER_PLIST", legacy
        )

        result = CliRunner().invoke(cli_app, ["doctor"])

        assert "Legacy scheduler still present" in result.output, (
            f"Expected legacy leftover warning.\n{result.output}"
        )
        assert "dataset cron uninstall" in result.output

    def test_no_warning_when_absent(self, monkeypatch, tmp_path):
        _make_doctor_mocks(monkeypatch, tmp_path)
        # _SCHEDULER_PLIST points at a non-existent path; launchctl mocked False.
        monkeypatch.setattr(
            "schwab_cli.commands.doctor._SCHEDULER_PLIST",
            tmp_path / "absent.plist",
        )

        result = CliRunner().invoke(cli_app, ["doctor"])

        assert "Legacy scheduler still present" not in result.output
