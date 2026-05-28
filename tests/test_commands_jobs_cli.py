"""TDD red-phase tests for Phase 4 CLI surface additions to schwab_cli.commands.jobs.

Tests cover:
- Pure helper functions: status_payload, render_status, render_reload_report
- CLI commands: jobs list, jobs status [--json], jobs reload (alias sync),
  jobs enable <id>, jobs disable <id>

All tests set SCHWAB_CLI_CONFIG_DIR via monkeypatch.setenv and build job files
and state.json under tmp_path/"jobs" / tmp_path/"jobs"/".current".

These tests FAIL until the Phase 4 implementation is merged.
"""
from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path
from typing import Any

import pytest

from typer.testing import CliRunner

from schwab_cli.cli import app as cli_app

# ---------------------------------------------------------------------------
# Import guards — collected cleanly even before implementation exists.
# The tests themselves will fail; the imports must not block collection.
# ---------------------------------------------------------------------------

try:
    from schwab_cli.commands.jobs import status_payload
    _HAS_STATUS_PAYLOAD = True
except (ImportError, AttributeError):
    status_payload = None  # type: ignore[assignment]
    _HAS_STATUS_PAYLOAD = False

try:
    from schwab_cli.commands.jobs import render_status
    _HAS_RENDER_STATUS = True
except (ImportError, AttributeError):
    render_status = None  # type: ignore[assignment]
    _HAS_RENDER_STATUS = False

try:
    from schwab_cli.commands.jobs import render_reload_report
    _HAS_RENDER_RELOAD_REPORT = True
except (ImportError, AttributeError):
    render_reload_report = None  # type: ignore[assignment]
    _HAS_RENDER_RELOAD_REPORT = False


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _write_job_file(directory: Path, job_id: str, payload: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / f"{job_id}.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def _minimal_job_payload(
    job_id: str = "my-job",
    *,
    enabled: bool = True,
    cron: str = "0 9 * * *",
    timezone: str = "America/New_York",
) -> dict:
    return {
        "name": f"Job {job_id}",
        "enabled": enabled,
        "cron": cron,
        "timezone": timezone,
        "type": "command",
        "command": ["schwab", "quote", "AAPL"],
    }


def _write_state(current_dir: Path, jobs_state: dict[str, dict], last_reload: list | None = None) -> None:
    """Write a state.json into current_dir with the given per-job state dicts."""
    current_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "jobs": {},
        "last_reload": last_reload,
        "updated_at": time.time(),
    }
    for job_id, state_fields in jobs_state.items():
        payload["jobs"][job_id] = {"id": job_id, **state_fields}
    (current_dir / "state.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_pidfile(current_dir: Path, pid: int) -> None:
    """Write a server.pid under current_dir."""
    current_dir.mkdir(parents=True, exist_ok=True)
    payload = {"pid": pid, "pgid": pid, "start_time": time.time()}
    (current_dir / "server.pid").write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests: status_payload (pure helper)
# ---------------------------------------------------------------------------


class TestStatusPayload:
    """status_payload returns the merged JSON-serialisable status view."""

    def test_returns_dict_with_jobs_and_server_running_keys(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        current_dir = tmp_path / "jobs" / ".current"
        current_dir.mkdir(parents=True, exist_ok=True)

        from schwab_cli.commands import jobs as jobs_cmd
        result = jobs_cmd.status_payload(config_dir=tmp_path)

        assert isinstance(result, dict), "status_payload must return a dict"
        assert "jobs" in result, "result must contain 'jobs' key"
        assert "server_running" in result, "result must contain 'server_running' key"

    def test_jobs_is_a_list(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        current_dir = tmp_path / "jobs" / ".current"
        current_dir.mkdir(parents=True, exist_ok=True)

        from schwab_cli.commands import jobs as jobs_cmd
        result = jobs_cmd.status_payload(config_dir=tmp_path)

        assert isinstance(result["jobs"], list)

    def test_promoted_job_appears_in_jobs(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        current_dir = tmp_path / "jobs" / ".current"
        _write_job_file(current_dir, "alpha", _minimal_job_payload("alpha"))

        from schwab_cli.commands import jobs as jobs_cmd
        result = jobs_cmd.status_payload(config_dir=tmp_path)

        ids = [j["id"] for j in result["jobs"]]
        assert "alpha" in ids

    def test_two_promoted_jobs_both_appear(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        current_dir = tmp_path / "jobs" / ".current"
        _write_job_file(current_dir, "alpha", _minimal_job_payload("alpha"))
        _write_job_file(current_dir, "beta", _minimal_job_payload("beta"))

        from schwab_cli.commands import jobs as jobs_cmd
        result = jobs_cmd.status_payload(config_dir=tmp_path)

        ids = {j["id"] for j in result["jobs"]}
        assert {"alpha", "beta"} <= ids

    def test_each_job_has_required_fields(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        current_dir = tmp_path / "jobs" / ".current"
        _write_job_file(current_dir, "alpha", _minimal_job_payload("alpha"))

        from schwab_cli.commands import jobs as jobs_cmd
        result = jobs_cmd.status_payload(config_dir=tmp_path)

        job = next(j for j in result["jobs"] if j["id"] == "alpha")
        for field in ("id", "name", "enabled", "cron", "timezone", "state",
                      "next_run_at", "last_run_at", "last_status", "last_exit_code",
                      "running_pid", "outdated", "edit_error"):
            assert field in job, f"job entry missing field: {field!r}"

    def test_server_running_false_when_no_pidfile(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        current_dir = tmp_path / "jobs" / ".current"
        current_dir.mkdir(parents=True, exist_ok=True)

        from schwab_cli.commands import jobs as jobs_cmd
        result = jobs_cmd.status_payload(config_dir=tmp_path)

        assert result["server_running"] is False

    def test_server_running_true_when_pidfile_present_and_alive(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        current_dir = tmp_path / "jobs" / ".current"
        my_pid = os.getpid()
        _write_pidfile(current_dir, my_pid)

        # Mock os.kill so the liveness check sees the process as alive.
        original_kill = os.kill
        kill_calls: list[tuple[int, int]] = []

        def fake_kill(pid: int, sig: int) -> None:
            kill_calls.append((pid, sig))
            if pid == my_pid and sig == 0:
                return  # alive
            original_kill(pid, sig)

        monkeypatch.setattr(os, "kill", fake_kill)

        from schwab_cli.commands import jobs as jobs_cmd
        result = jobs_cmd.status_payload(config_dir=tmp_path)

        assert result["server_running"] is True

    def test_disabled_job_has_disabled_state(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        current_dir = tmp_path / "jobs" / ".current"
        _write_job_file(current_dir, "alpha", _minimal_job_payload("alpha", enabled=False))

        from schwab_cli.commands import jobs as jobs_cmd
        result = jobs_cmd.status_payload(config_dir=tmp_path)

        job = next(j for j in result["jobs"] if j["id"] == "alpha")
        assert job["state"] == "disabled"

    def test_running_job_has_running_state_when_pid_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        current_dir = tmp_path / "jobs" / ".current"
        _write_job_file(current_dir, "alpha", _minimal_job_payload("alpha"))
        _write_state(current_dir, {"alpha": {"running_pid": 99999, "next_run_at": None}})

        from schwab_cli.commands import jobs as jobs_cmd
        result = jobs_cmd.status_payload(config_dir=tmp_path)

        job = next(j for j in result["jobs"] if j["id"] == "alpha")
        assert job["state"] == "running"
        assert job["running_pid"] == 99999

    def test_scheduled_job_has_scheduled_state_when_next_run_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        current_dir = tmp_path / "jobs" / ".current"
        _write_job_file(current_dir, "alpha", _minimal_job_payload("alpha"))
        future = time.time() + 3600
        _write_state(current_dir, {"alpha": {"next_run_at": future}})

        from schwab_cli.commands import jobs as jobs_cmd
        result = jobs_cmd.status_payload(config_dir=tmp_path)

        job = next(j for j in result["jobs"] if j["id"] == "alpha")
        assert job["state"] == "scheduled"
        assert job["next_run_at"] == future

    def test_outdated_true_when_staging_error_but_current_exists(self, monkeypatch, tmp_path):
        """A staging file with a validation error + existing .current config → outdated=True."""
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        jobs_dir = tmp_path / "jobs"
        current_dir = jobs_dir / ".current"
        # Staging file is INVALID (bad type field):
        _write_job_file(jobs_dir, "alpha", {"type": "invalid_type", "name": "Alpha", "enabled": True,
                                             "cron": "0 9 * * *", "timezone": "UTC"})
        # Current file is valid:
        _write_job_file(current_dir, "alpha", _minimal_job_payload("alpha"))

        from schwab_cli.commands import jobs as jobs_cmd
        result = jobs_cmd.status_payload(config_dir=tmp_path)

        job = next(j for j in result["jobs"] if j["id"] == "alpha")
        assert job["outdated"] is True
        assert job["edit_error"] is not None

    def test_outdated_false_when_staging_valid(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        jobs_dir = tmp_path / "jobs"
        current_dir = jobs_dir / ".current"
        _write_job_file(jobs_dir, "alpha", _minimal_job_payload("alpha"))
        _write_job_file(current_dir, "alpha", _minimal_job_payload("alpha"))

        from schwab_cli.commands import jobs as jobs_cmd
        result = jobs_cmd.status_payload(config_dir=tmp_path)

        job = next(j for j in result["jobs"] if j["id"] == "alpha")
        assert job["outdated"] is False

    def test_state_fields_merged_from_state_json(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        current_dir = tmp_path / "jobs" / ".current"
        _write_job_file(current_dir, "alpha", _minimal_job_payload("alpha"))
        _write_state(current_dir, {
            "alpha": {
                "last_run_at": 1700000000.0,
                "last_status": "success",
                "last_exit_code": 0,
            }
        })

        from schwab_cli.commands import jobs as jobs_cmd
        result = jobs_cmd.status_payload(config_dir=tmp_path)

        job = next(j for j in result["jobs"] if j["id"] == "alpha")
        assert job["last_run_at"] == 1700000000.0
        assert job["last_status"] == "success"
        assert job["last_exit_code"] == 0

    def test_result_is_json_serializable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        current_dir = tmp_path / "jobs" / ".current"
        _write_job_file(current_dir, "alpha", _minimal_job_payload("alpha"))

        from schwab_cli.commands import jobs as jobs_cmd
        result = jobs_cmd.status_payload(config_dir=tmp_path)
        # Should not raise
        json.dumps(result)


# ---------------------------------------------------------------------------
# Tests: render_status (pure helper)
# ---------------------------------------------------------------------------


class TestRenderStatus:
    """render_status returns a multi-line stanza string."""

    def _make_job_entry(self, **overrides) -> dict:
        base = {
            "id": "alpha",
            "name": "Alpha Job",
            "enabled": True,
            "cron": "0 9 * * *",
            "timezone": "America/New_York",
            "state": "scheduled",
            "next_run_at": time.time() + 3600,
            "last_run_at": None,
            "last_status": None,
            "last_exit_code": None,
            "running_pid": None,
            "outdated": False,
            "edit_error": None,
        }
        base.update(overrides)
        return base

    def test_returns_string(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        from schwab_cli.commands import jobs as jobs_cmd
        payload = {"jobs": [self._make_job_entry()], "server_running": False}
        result = jobs_cmd.render_status(payload)
        assert isinstance(result, str)

    def test_contains_job_id(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        from schwab_cli.commands import jobs as jobs_cmd
        payload = {"jobs": [self._make_job_entry(id="alpha")], "server_running": False}
        result = jobs_cmd.render_status(payload)
        assert "alpha" in result

    def test_contains_cron(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        from schwab_cli.commands import jobs as jobs_cmd
        payload = {"jobs": [self._make_job_entry(cron="0 9 * * *")], "server_running": False}
        result = jobs_cmd.render_status(payload)
        assert "0 9 * * *" in result

    def test_header_contains_state(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        from schwab_cli.commands import jobs as jobs_cmd
        payload = {"jobs": [self._make_job_entry(state="scheduled")], "server_running": False}
        result = jobs_cmd.render_status(payload)
        assert "scheduled" in result

    def test_scheduled_shows_next_run(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        from schwab_cli.commands import jobs as jobs_cmd
        future = time.time() + 3600
        payload = {
            "jobs": [self._make_job_entry(state="scheduled", next_run_at=future)],
            "server_running": False,
        }
        result = jobs_cmd.render_status(payload)
        assert "next run" in result.lower()

    def test_running_shows_pid(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        from schwab_cli.commands import jobs as jobs_cmd
        payload = {
            "jobs": [self._make_job_entry(state="running", running_pid=12345, next_run_at=None)],
            "server_running": True,
        }
        result = jobs_cmd.render_status(payload)
        assert "12345" in result

    def test_running_shows_running_text(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        from schwab_cli.commands import jobs as jobs_cmd
        payload = {
            "jobs": [self._make_job_entry(state="running", running_pid=12345, next_run_at=None)],
            "server_running": True,
        }
        result = jobs_cmd.render_status(payload)
        assert "running" in result.lower()

    def test_last_run_shown_when_present(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        from schwab_cli.commands import jobs as jobs_cmd
        payload = {
            "jobs": [self._make_job_entry(
                last_run_at=1700000000.0,
                last_status="success",
            )],
            "server_running": False,
        }
        result = jobs_cmd.render_status(payload)
        assert "last run" in result.lower()

    def test_last_run_status_shown(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        from schwab_cli.commands import jobs as jobs_cmd
        payload = {
            "jobs": [self._make_job_entry(
                last_run_at=1700000000.0,
                last_status="success",
            )],
            "server_running": False,
        }
        result = jobs_cmd.render_status(payload)
        assert "success" in result

    def test_outdated_shows_warning_line(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        from schwab_cli.commands import jobs as jobs_cmd
        payload = {
            "jobs": [self._make_job_entry(
                outdated=True,
                edit_error="invalid 'type': 'bad_type'",
            )],
            "server_running": False,
        }
        result = jobs_cmd.render_status(payload)
        # Must mention outdated or staged edit invalid
        assert "outdated" in result.lower() or "staged edit" in result.lower()
        assert "invalid 'type': 'bad_type'" in result

    def test_outdated_in_header_when_outdated_true(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        from schwab_cli.commands import jobs as jobs_cmd
        payload = {
            "jobs": [self._make_job_entry(outdated=True, edit_error="some error")],
            "server_running": False,
        }
        result = jobs_cmd.render_status(payload)
        assert "outdated" in result.lower()

    def test_disabled_shows_disabled(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        from schwab_cli.commands import jobs as jobs_cmd
        payload = {
            "jobs": [self._make_job_entry(state="disabled", enabled=False, next_run_at=None)],
            "server_running": False,
        }
        result = jobs_cmd.render_status(payload)
        assert "disabled" in result.lower()

    def test_disabled_does_not_show_next_run(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        from schwab_cli.commands import jobs as jobs_cmd
        payload = {
            "jobs": [self._make_job_entry(state="disabled", enabled=False, next_run_at=None)],
            "server_running": False,
        }
        result = jobs_cmd.render_status(payload)
        assert "next run" not in result.lower()

    def test_two_jobs_both_ids_present(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        from schwab_cli.commands import jobs as jobs_cmd
        payload = {
            "jobs": [
                self._make_job_entry(id="alpha"),
                self._make_job_entry(id="beta"),
            ],
            "server_running": False,
        }
        result = jobs_cmd.render_status(payload)
        assert "alpha" in result
        assert "beta" in result

    def test_empty_jobs_list_returns_string(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        from schwab_cli.commands import jobs as jobs_cmd
        payload = {"jobs": [], "server_running": False}
        result = jobs_cmd.render_status(payload)
        assert isinstance(result, str)

    def test_timezone_in_output(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        from schwab_cli.commands import jobs as jobs_cmd
        payload = {
            "jobs": [self._make_job_entry(timezone="America/New_York")],
            "server_running": False,
        }
        result = jobs_cmd.render_status(payload)
        assert "America/New_York" in result


# ---------------------------------------------------------------------------
# Tests: render_reload_report (pure helper)
# ---------------------------------------------------------------------------


class TestRenderReloadReport:
    """render_reload_report renders one line per transition."""

    def test_returns_string(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        from schwab_cli.commands import jobs as jobs_cmd
        transitions = [{"id": "alpha", "old": None, "new": "loaded", "next_run_at": None, "error": None}]
        result = jobs_cmd.render_reload_report(transitions)
        assert isinstance(result, str)

    def test_id_in_output(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        from schwab_cli.commands import jobs as jobs_cmd
        transitions = [{"id": "alpha", "old": None, "new": "loaded", "next_run_at": None, "error": None}]
        result = jobs_cmd.render_reload_report(transitions)
        assert "alpha" in result

    def test_old_and_new_state_in_output(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        from schwab_cli.commands import jobs as jobs_cmd
        transitions = [{"id": "alpha", "old": "disabled", "new": "updated", "next_run_at": None, "error": None}]
        result = jobs_cmd.render_reload_report(transitions)
        assert "disabled" in result
        assert "updated" in result

    def test_arrow_separator(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        from schwab_cli.commands import jobs as jobs_cmd
        transitions = [{"id": "alpha", "old": "scheduled", "new": "updated", "next_run_at": None, "error": None}]
        result = jobs_cmd.render_reload_report(transitions)
        assert "→" in result or "->" in result

    def test_next_run_shown_for_updated(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        from schwab_cli.commands import jobs as jobs_cmd
        future = time.time() + 3600
        transitions = [{"id": "alpha", "old": None, "new": "updated", "next_run_at": future, "error": None}]
        result = jobs_cmd.render_reload_report(transitions)
        assert "next run" in result.lower()

    def test_next_run_not_shown_for_error(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        from schwab_cli.commands import jobs as jobs_cmd
        future = time.time() + 3600
        transitions = [{"id": "alpha", "old": None, "new": "error", "next_run_at": future, "error": "bad config"}]
        result = jobs_cmd.render_reload_report(transitions)
        # Error outcomes should NOT show next run
        assert "next run" not in result.lower()

    def test_error_shown_in_output(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        from schwab_cli.commands import jobs as jobs_cmd
        transitions = [{"id": "alpha", "old": None, "new": "error", "next_run_at": None, "error": "bad config"}]
        result = jobs_cmd.render_reload_report(transitions)
        assert "bad config" in result

    def test_multiple_transitions_each_get_a_line(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        from schwab_cli.commands import jobs as jobs_cmd
        transitions = [
            {"id": "alpha", "old": None, "new": "updated", "next_run_at": None, "error": None},
            {"id": "beta", "old": "loaded", "new": "unchanged", "next_run_at": None, "error": None},
        ]
        result = jobs_cmd.render_reload_report(transitions)
        assert "alpha" in result
        assert "beta" in result

    def test_empty_transitions_returns_string(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        from schwab_cli.commands import jobs as jobs_cmd
        result = jobs_cmd.render_reload_report([])
        assert isinstance(result, str)

    def test_unchanged_transition_shows_unchanged(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        from schwab_cli.commands import jobs as jobs_cmd
        future = time.time() + 3600
        transitions = [{"id": "alpha", "old": "loaded", "new": "unchanged", "next_run_at": future, "error": None}]
        result = jobs_cmd.render_reload_report(transitions)
        assert "unchanged" in result

    def test_outdated_transition_shows_outdated(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        from schwab_cli.commands import jobs as jobs_cmd
        transitions = [{"id": "alpha", "old": "loaded", "new": "outdated", "next_run_at": None, "error": "bad type"}]
        result = jobs_cmd.render_reload_report(transitions)
        assert "outdated" in result


# ---------------------------------------------------------------------------
# CLI: jobs list
# ---------------------------------------------------------------------------


class TestJobsListCommand:
    """`schwab jobs list` prints staged configs and any validation errors."""

    def test_exits_0_with_no_jobs(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "list"])
        assert result.exit_code == 0

    def test_exits_0_with_valid_staged_jobs(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        jobs_dir = tmp_path / "jobs"
        _write_job_file(jobs_dir, "alpha", _minimal_job_payload("alpha"))
        _write_job_file(jobs_dir, "beta", _minimal_job_payload("beta"))

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "list"])
        assert result.exit_code == 0

    def test_shows_valid_job_ids(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        jobs_dir = tmp_path / "jobs"
        _write_job_file(jobs_dir, "alpha", _minimal_job_payload("alpha"))
        _write_job_file(jobs_dir, "beta", _minimal_job_payload("beta"))

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "list"])
        assert "alpha" in result.output
        assert "beta" in result.output

    def test_shows_invalid_job_id_and_error(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        jobs_dir = tmp_path / "jobs"
        _write_job_file(jobs_dir, "alpha", _minimal_job_payload("alpha"))
        _write_job_file(jobs_dir, "broken", {"type": "nope", "name": "X"})  # invalid

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "list"])

        assert "alpha" in result.output
        assert "broken" in result.output

    def test_exits_0_even_when_staging_has_invalid_file(self, monkeypatch, tmp_path):
        """jobs list is read-only — always exits 0 even for invalid staged files."""
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        jobs_dir = tmp_path / "jobs"
        _write_job_file(jobs_dir, "broken", {"type": "nope"})

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "list"])
        assert result.exit_code == 0

    def test_shows_cron_and_timezone(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        jobs_dir = tmp_path / "jobs"
        _write_job_file(jobs_dir, "alpha", _minimal_job_payload("alpha", cron="30 14 * * 1-5"))

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "list"])
        assert "30 14 * * 1-5" in result.output
        assert "America/New_York" in result.output

    def test_list_command_exists(self, monkeypatch, tmp_path):
        """jobs list --help exits without 'No such command'."""
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "list", "--help"])
        assert "No such command" not in (result.output or "")


# ---------------------------------------------------------------------------
# CLI: jobs status
# ---------------------------------------------------------------------------


class TestJobsStatusCommand:
    """`schwab jobs status` prints stanza output for promoted jobs."""

    def test_exits_0(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        current_dir = tmp_path / "jobs" / ".current"
        current_dir.mkdir(parents=True, exist_ok=True)

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "status"])
        assert result.exit_code == 0

    def test_shows_promoted_job_id(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        current_dir = tmp_path / "jobs" / ".current"
        _write_job_file(current_dir, "alpha", _minimal_job_payload("alpha"))

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "status"])
        assert "alpha" in result.output

    def test_shows_both_promoted_job_ids(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        current_dir = tmp_path / "jobs" / ".current"
        _write_job_file(current_dir, "alpha", _minimal_job_payload("alpha"))
        _write_job_file(current_dir, "beta", _minimal_job_payload("beta"))

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "status"])
        assert "alpha" in result.output
        assert "beta" in result.output

    def test_running_job_shows_pid(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        current_dir = tmp_path / "jobs" / ".current"
        _write_job_file(current_dir, "alpha", _minimal_job_payload("alpha"))
        _write_state(current_dir, {"alpha": {"running_pid": 55555, "next_run_at": None}})

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "status"])
        assert "55555" in result.output

    def test_scheduled_job_shows_next_run(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        current_dir = tmp_path / "jobs" / ".current"
        _write_job_file(current_dir, "alpha", _minimal_job_payload("alpha"))
        future = time.time() + 7200
        _write_state(current_dir, {"alpha": {"next_run_at": future}})

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "status"])
        assert "next run" in result.output.lower()

    def test_json_flag_outputs_parseable_json(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        current_dir = tmp_path / "jobs" / ".current"
        _write_job_file(current_dir, "alpha", _minimal_job_payload("alpha"))

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "status", "--json"])
        assert result.exit_code == 0
        # Must be parseable JSON
        data = json.loads(result.output)
        assert "jobs" in data

    def test_json_flag_contains_jobs_list(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        current_dir = tmp_path / "jobs" / ".current"
        _write_job_file(current_dir, "alpha", _minimal_job_payload("alpha"))

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "status", "--json"])
        data = json.loads(result.output)
        ids = [j["id"] for j in data["jobs"]]
        assert "alpha" in ids

    def test_status_command_exists(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "status", "--help"])
        assert "No such command" not in (result.output or "")


# ---------------------------------------------------------------------------
# CLI: jobs reload (alias sync)
# ---------------------------------------------------------------------------


class TestJobsReloadCommand:
    """`schwab jobs reload` / `schwab jobs sync` sends SIGHUP to running server."""

    def _seed_server_with_reload_state(
        self,
        tmp_path: Path,
        pid: int,
        transitions: list[dict],
    ) -> Path:
        """Write pidfile + state.json with a last_reload list of transition dicts."""
        current_dir = tmp_path / "jobs" / ".current"
        _write_pidfile(current_dir, pid)
        _write_state(current_dir, {}, last_reload=transitions)
        return current_dir

    @staticmethod
    def _server_responding_kill(current_dir: Path, transitions: list[dict]):
        """Build a fake os.kill that simulates the server replying to SIGHUP.

        On SIGHUP it rewrites state.json with the given ``transitions`` and an
        advanced ``updated_at`` so the CLI's freshness poll detects a new report.
        """

        def fake_kill(pid: int, sig: int) -> None:
            if sig == signal.SIGHUP:
                payload = {
                    "jobs": {},
                    "last_reload": transitions,
                    "updated_at": time.time() + 1000.0,
                }
                (current_dir / "state.json").write_text(
                    json.dumps(payload), encoding="utf-8"
                )

        return fake_kill

    def test_kills_with_sighup_when_server_running(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        target_pid = os.getpid()  # use self as "server" pid
        current_dir = tmp_path / "jobs" / ".current"
        _write_pidfile(current_dir, target_pid)

        # Seed last_reload so the command has something to render.
        _write_state(current_dir, {}, last_reload=[
            {"id": "alpha", "old": None, "new": "updated", "next_run_at": None, "error": None}
        ])

        kill_calls: list[tuple[int, int]] = []

        def fake_kill(pid: int, sig: int) -> None:
            kill_calls.append((pid, sig))
            # Don't actually send SIGHUP to ourselves mid-test

        monkeypatch.setattr(os, "kill", fake_kill)

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "reload"])

        assert (target_pid, signal.SIGHUP) in kill_calls, (
            f"os.kill not called with (pid={target_pid}, SIGHUP). Calls: {kill_calls}"
        )

    def test_exits_0_when_reload_all_transitions_good(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        target_pid = os.getpid()
        current_dir = tmp_path / "jobs" / ".current"
        _write_pidfile(current_dir, target_pid)
        _write_state(current_dir, {}, last_reload=None)

        transitions = [
            {"id": "alpha", "old": None, "new": "updated", "next_run_at": None, "error": None},
            {"id": "beta", "old": "loaded", "new": "unchanged", "next_run_at": None, "error": None},
        ]
        monkeypatch.setattr(
            os, "kill", self._server_responding_kill(current_dir, transitions)
        )

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "reload"])
        assert result.exit_code == 0

    def test_exits_nonzero_when_reload_has_outdated_transition(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        target_pid = os.getpid()
        current_dir = tmp_path / "jobs" / ".current"
        _write_pidfile(current_dir, target_pid)
        _write_state(current_dir, {}, last_reload=None)

        transitions = [
            {"id": "alpha", "old": "loaded", "new": "outdated", "next_run_at": None, "error": "bad type"},
        ]
        monkeypatch.setattr(
            os, "kill", self._server_responding_kill(current_dir, transitions)
        )

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "reload"])
        assert result.exit_code != 0

    def test_exits_nonzero_when_reload_has_error_transition(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        target_pid = os.getpid()
        current_dir = tmp_path / "jobs" / ".current"
        _write_pidfile(current_dir, target_pid)
        _write_state(current_dir, {}, last_reload=None)

        transitions = [
            {"id": "alpha", "old": None, "new": "error", "next_run_at": None, "error": "cannot read"},
        ]
        monkeypatch.setattr(
            os, "kill", self._server_responding_kill(current_dir, transitions)
        )

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "reload"])
        assert result.exit_code != 0

    def test_no_kill_when_server_not_running(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        # No pidfile written — server not running.
        current_dir = tmp_path / "jobs" / ".current"
        current_dir.mkdir(parents=True, exist_ok=True)

        kill_calls: list[tuple] = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: kill_calls.append((pid, sig)))

        runner = CliRunner()
        runner.invoke(cli_app, ["jobs", "reload"])

        # SIGHUP should NOT have been sent.
        sighup_calls = [(pid, sig) for pid, sig in kill_calls if sig == signal.SIGHUP]
        assert len(sighup_calls) == 0, f"SIGHUP sent even without a pidfile: {sighup_calls}"

    def test_prints_server_not_running_when_no_pidfile(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        current_dir = tmp_path / "jobs" / ".current"
        current_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(os, "kill", lambda pid, sig: None)

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "reload"])
        assert "server not running" in result.output.lower() or "not running" in result.output.lower()

    def test_exits_0_no_server_no_staging_errors(self, monkeypatch, tmp_path):
        """No server + no staging errors → informational message + exit 0."""
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        # Staging dir has a valid job.
        jobs_dir = tmp_path / "jobs"
        _write_job_file(jobs_dir, "alpha", _minimal_job_payload("alpha"))
        current_dir = jobs_dir / ".current"
        current_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(os, "kill", lambda pid, sig: None)

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "reload"])
        assert result.exit_code == 0

    def test_exits_nonzero_no_server_with_staging_errors(self, monkeypatch, tmp_path):
        """No server + staging has invalid file → exit non-zero."""
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        jobs_dir = tmp_path / "jobs"
        _write_job_file(jobs_dir, "broken", {"type": "nope"})
        current_dir = jobs_dir / ".current"
        current_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(os, "kill", lambda pid, sig: None)

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "reload"])
        assert result.exit_code != 0

    def test_sync_alias_accepted(self, monkeypatch, tmp_path):
        """`schwab jobs sync` is recognised as an alias for reload."""
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        current_dir = tmp_path / "jobs" / ".current"
        current_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(os, "kill", lambda pid, sig: None)

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "sync"])
        assert "No such command" not in (result.output or "")

    def test_reload_renders_transition_report(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        target_pid = os.getpid()
        current_dir = tmp_path / "jobs" / ".current"
        _write_pidfile(current_dir, target_pid)
        _write_state(current_dir, {}, last_reload=None)

        transitions = [
            {"id": "alpha", "old": None, "new": "updated", "next_run_at": None, "error": None},
        ]
        monkeypatch.setattr(
            os, "kill", self._server_responding_kill(current_dir, transitions)
        )

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "reload"])
        # The report must mention "alpha"
        assert "alpha" in result.output

    def test_reload_shows_fresh_report_not_stale_seed(self, monkeypatch, tmp_path):
        """Regression: a second reload must reflect the NEW server response, not
        the previous (seed) last_reload.

        Seeds state.json with an initial last_reload + updated_at. The os.kill
        mock simulates the server by rewriting state.json with a DIFFERENT
        last_reload and an advanced updated_at. The rendered output must show the
        NEW transition (beta), never the stale seed (alpha).
        """
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        target_pid = os.getpid()
        current_dir = tmp_path / "jobs" / ".current"
        _write_pidfile(current_dir, target_pid)

        # Seed an OLD report — already non-None, the trap the bug fell into.
        _write_state(current_dir, {}, last_reload=[
            {"id": "alpha", "old": None, "new": "updated", "next_run_at": None, "error": None},
        ])

        new_transitions = [
            {"id": "beta", "old": "loaded", "new": "unchanged", "next_run_at": None, "error": None},
        ]
        monkeypatch.setattr(
            os, "kill", self._server_responding_kill(current_dir, new_transitions)
        )

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "reload"])

        assert result.exit_code == 0
        assert "beta" in result.output, "must render the post-SIGHUP (fresh) report"
        assert "alpha" not in result.output, "must NOT render the stale seed report"

    def test_reload_no_response_does_not_show_stale_report(self, monkeypatch, tmp_path):
        """If the server never advances updated_at, the prior report must NOT be
        shown as if fresh; the command reports no response and exits non-zero."""
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        target_pid = os.getpid()
        current_dir = tmp_path / "jobs" / ".current"
        _write_pidfile(current_dir, target_pid)
        # Seed a stale report; the kill mock does nothing (no server response).
        _write_state(current_dir, {}, last_reload=[
            {"id": "alpha", "old": None, "new": "updated", "next_run_at": None, "error": None},
        ])
        monkeypatch.setattr(os, "kill", lambda pid, sig: None)

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "reload"])

        assert result.exit_code != 0
        assert "alpha" not in result.output, "stale report must not be shown as fresh"
        assert "no response" in result.output.lower()

    def test_dead_pid_treated_as_not_running(self, monkeypatch, tmp_path):
        """A pidfile with a dead PID → treated as server not running."""
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        current_dir = tmp_path / "jobs" / ".current"
        # PID 99999999 almost certainly doesn't exist.
        _write_pidfile(current_dir, 99999999)

        kill_calls: list[tuple] = []

        def fake_kill(pid: int, sig: int) -> None:
            if sig == 0:
                raise ProcessLookupError("no such process")
            kill_calls.append((pid, sig))

        monkeypatch.setattr(os, "kill", fake_kill)

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "reload"])

        sighup_calls = [(pid, sig) for pid, sig in kill_calls if sig == signal.SIGHUP]
        assert len(sighup_calls) == 0, f"SIGHUP sent for dead PID: {sighup_calls}"


# ---------------------------------------------------------------------------
# CLI: jobs enable / jobs disable
# ---------------------------------------------------------------------------


class TestJobsEnableDisableCommands:
    """`schwab jobs enable <id>` and `schwab jobs disable <id>`."""

    def test_disable_flips_enabled_to_false(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        jobs_dir = tmp_path / "jobs"
        _write_job_file(jobs_dir, "alpha", _minimal_job_payload("alpha", enabled=True))

        monkeypatch.setattr(os, "kill", lambda pid, sig: None)

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "disable", "alpha"])
        assert result.exit_code == 0

        updated = json.loads((jobs_dir / "alpha.json").read_text())
        assert updated["enabled"] is False

    def test_enable_flips_enabled_to_true(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        jobs_dir = tmp_path / "jobs"
        _write_job_file(jobs_dir, "alpha", _minimal_job_payload("alpha", enabled=False))

        monkeypatch.setattr(os, "kill", lambda pid, sig: None)

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "enable", "alpha"])
        assert result.exit_code == 0

        updated = json.loads((jobs_dir / "alpha.json").read_text())
        assert updated["enabled"] is True

    def test_disable_file_remains_valid_after_edit(self, monkeypatch, tmp_path):
        """The rewritten file must parse cleanly via parse_job."""
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        jobs_dir = tmp_path / "jobs"
        _write_job_file(jobs_dir, "alpha", _minimal_job_payload("alpha", enabled=True))
        monkeypatch.setattr(os, "kill", lambda pid, sig: None)

        runner = CliRunner()
        runner.invoke(cli_app, ["jobs", "disable", "alpha"])

        from schwab_cli.server.jobs.config import parse_job
        cfg = parse_job(jobs_dir / "alpha.json")
        assert cfg.enabled is False

    def test_enable_file_remains_valid_after_edit(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        jobs_dir = tmp_path / "jobs"
        _write_job_file(jobs_dir, "alpha", _minimal_job_payload("alpha", enabled=False))
        monkeypatch.setattr(os, "kill", lambda pid, sig: None)

        runner = CliRunner()
        runner.invoke(cli_app, ["jobs", "enable", "alpha"])

        from schwab_cli.server.jobs.config import parse_job
        cfg = parse_job(jobs_dir / "alpha.json")
        assert cfg.enabled is True

    def test_disable_sends_sighup_when_server_running(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        jobs_dir = tmp_path / "jobs"
        _write_job_file(jobs_dir, "alpha", _minimal_job_payload("alpha", enabled=True))
        current_dir = jobs_dir / ".current"
        target_pid = os.getpid()
        _write_pidfile(current_dir, target_pid)

        kill_calls: list[tuple[int, int]] = []

        def fake_kill(pid: int, sig: int) -> None:
            kill_calls.append((pid, sig))

        monkeypatch.setattr(os, "kill", fake_kill)

        runner = CliRunner()
        runner.invoke(cli_app, ["jobs", "disable", "alpha"])

        sighup_calls = [(pid, sig) for pid, sig in kill_calls if sig == signal.SIGHUP]
        assert len(sighup_calls) >= 1, f"Expected SIGHUP to be sent. Calls: {kill_calls}"

    def test_enable_sends_sighup_when_server_running(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        jobs_dir = tmp_path / "jobs"
        _write_job_file(jobs_dir, "alpha", _minimal_job_payload("alpha", enabled=False))
        current_dir = jobs_dir / ".current"
        target_pid = os.getpid()
        _write_pidfile(current_dir, target_pid)

        kill_calls: list[tuple[int, int]] = []

        def fake_kill(pid: int, sig: int) -> None:
            kill_calls.append((pid, sig))

        monkeypatch.setattr(os, "kill", fake_kill)

        runner = CliRunner()
        runner.invoke(cli_app, ["jobs", "enable", "alpha"])

        sighup_calls = [(pid, sig) for pid, sig in kill_calls if sig == signal.SIGHUP]
        assert len(sighup_calls) >= 1, f"Expected SIGHUP to be sent. Calls: {kill_calls}"

    def test_disable_no_sighup_when_server_not_running(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        jobs_dir = tmp_path / "jobs"
        _write_job_file(jobs_dir, "alpha", _minimal_job_payload("alpha", enabled=True))
        # No pidfile.

        kill_calls: list[tuple] = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: kill_calls.append((pid, sig)))

        runner = CliRunner()
        runner.invoke(cli_app, ["jobs", "disable", "alpha"])

        sighup_calls = [(pid, sig) for pid, sig in kill_calls if sig == signal.SIGHUP]
        assert len(sighup_calls) == 0, f"SIGHUP sent without server: {sighup_calls}"

    def test_disable_unknown_id_exits_nonzero(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        jobs_dir = tmp_path / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(os, "kill", lambda pid, sig: None)

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "disable", "nonexistent"])
        assert result.exit_code != 0

    def test_enable_unknown_id_exits_nonzero(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        jobs_dir = tmp_path / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(os, "kill", lambda pid, sig: None)

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "enable", "nonexistent"])
        assert result.exit_code != 0

    def test_disable_unknown_id_error_message_mentions_id(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        jobs_dir = tmp_path / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(os, "kill", lambda pid, sig: None)

        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "disable", "ghost"])
        combined = (result.output or "") + (str(result.exception) if result.exception else "")
        assert "ghost" in combined

    def test_enable_command_exists(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "enable", "--help"])
        assert "No such command" not in (result.output or "")

    def test_disable_command_exists(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "disable", "--help"])
        assert "No such command" not in (result.output or "")

    def test_preserve_other_fields_on_disable(self, monkeypatch, tmp_path):
        """Disabling must not destroy other fields like cron, timezone, type, etc."""
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        jobs_dir = tmp_path / "jobs"
        payload = _minimal_job_payload("alpha", enabled=True, cron="15 8 * * *")
        _write_job_file(jobs_dir, "alpha", payload)
        monkeypatch.setattr(os, "kill", lambda pid, sig: None)

        runner = CliRunner()
        runner.invoke(cli_app, ["jobs", "disable", "alpha"])

        updated = json.loads((jobs_dir / "alpha.json").read_text())
        assert updated["cron"] == "15 8 * * *"
        assert updated["timezone"] == "America/New_York"
        assert updated["name"] == "Job alpha"

    def test_preserve_other_fields_on_enable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        jobs_dir = tmp_path / "jobs"
        payload = _minimal_job_payload("alpha", enabled=False, cron="45 10 * * 1")
        _write_job_file(jobs_dir, "alpha", payload)
        monkeypatch.setattr(os, "kill", lambda pid, sig: None)

        runner = CliRunner()
        runner.invoke(cli_app, ["jobs", "enable", "alpha"])

        updated = json.loads((jobs_dir / "alpha.json").read_text())
        assert updated["cron"] == "45 10 * * 1"
        assert updated["enabled"] is True


# ---------------------------------------------------------------------------
# CLI registration smoke test — new commands appear in help
# ---------------------------------------------------------------------------


class TestJobsPhase4CommandRegistration:
    """Verify all Phase 4 subcommands are wired into the jobs group."""

    def test_jobs_help_lists_list(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "--help"])
        assert "list" in result.output

    def test_jobs_help_lists_status(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "--help"])
        assert "status" in result.output

    def test_jobs_help_lists_reload(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "--help"])
        assert "reload" in result.output or "sync" in result.output

    def test_jobs_help_lists_enable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "--help"])
        assert "enable" in result.output

    def test_jobs_help_lists_disable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "--help"])
        assert "disable" in result.output

    def test_run_still_present(self, monkeypatch, tmp_path):
        """Phase 1 'run' command must still exist after Phase 4 additions."""
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        runner = CliRunner()
        result = runner.invoke(cli_app, ["jobs", "--help"])
        assert "run" in result.output
