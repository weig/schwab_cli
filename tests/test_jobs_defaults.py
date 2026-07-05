"""TDD red-phase tests for Phase 5 — schwab_cli.server.jobs.defaults.

Tests verify:
  - DEFAULT_JOB_CONFIGS has exactly {"market-data", "accounts", "indices"}
  - Each default config is valid per parse_job (type=="command", non-empty command,
    expected cron, timezone "America/New_York")
  - write_default_jobs on an empty dir creates all 3 files + returns {"created"}
  - Second call returns all "exists" and does NOT overwrite existing files

All tests isolate the config dir via SCHWAB_CLI_CONFIG_DIR.
These tests FAIL until the Phase 5 defaults module is implemented.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import guards — collected cleanly even before implementation exists.
# ---------------------------------------------------------------------------

try:
    from schwab_cli.server.jobs.defaults import DEFAULT_JOB_CONFIGS
    _HAS_DEFAULT_JOB_CONFIGS = True
except (ImportError, AttributeError):
    DEFAULT_JOB_CONFIGS = None  # type: ignore[assignment]
    _HAS_DEFAULT_JOB_CONFIGS = False

try:
    from schwab_cli.server.jobs.defaults import write_default_jobs
    _HAS_WRITE_DEFAULT_JOBS = True
except (ImportError, AttributeError):
    write_default_jobs = None  # type: ignore[assignment]
    _HAS_WRITE_DEFAULT_JOBS = False

try:
    from schwab_cli.server.jobs.config import parse_job
    _HAS_PARSE_JOB = True
except (ImportError, AttributeError):
    parse_job = None  # type: ignore[assignment]
    _HAS_PARSE_JOB = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EXPECTED_STEMS = {
    "market-data", "accounts", "indices", "screener", "screener-earnings",
}

# The original dataset jobs carry invariants (--skip-wait, enabled) the
# screener jobs deliberately don't share.
_DATASET_STEMS = {"market-data", "accounts", "indices"}
# The screener jobs ship disabled — opt-in once the earnings feed is
# validated (a heavy ~600-symbol daily fetch; empty output until then).
_DISABLED_STEMS = {"screener", "screener-earnings"}

_EXPECTED_CRONS = {
    "market-data":       "0 17 * * 1-5",
    "accounts":          "0 17 * * 1-5",
    "indices":           "0 18 * * *",
    "screener":          "10 17 * * 1-5",
    "screener-earnings": "30 15 * * 1-5",
}


# ---------------------------------------------------------------------------
# DEFAULT_JOB_CONFIGS shape
# ---------------------------------------------------------------------------


class TestDefaultJobConfigs:
    """DEFAULT_JOB_CONFIGS is a dict keyed by exactly the three stems."""

    def test_exists(self):
        assert _HAS_DEFAULT_JOB_CONFIGS, (
            "schwab_cli.server.jobs.defaults.DEFAULT_JOB_CONFIGS not importable"
        )

    def test_is_dict(self):
        assert _HAS_DEFAULT_JOB_CONFIGS
        assert isinstance(DEFAULT_JOB_CONFIGS, dict), (
            f"DEFAULT_JOB_CONFIGS must be a dict, got {type(DEFAULT_JOB_CONFIGS)}"
        )

    def test_has_exactly_three_stems(self):
        assert _HAS_DEFAULT_JOB_CONFIGS
        assert set(DEFAULT_JOB_CONFIGS.keys()) == _EXPECTED_STEMS, (
            f"Expected stems {_EXPECTED_STEMS}, got {set(DEFAULT_JOB_CONFIGS.keys())}"
        )

    def test_market_data_stem_present(self):
        assert _HAS_DEFAULT_JOB_CONFIGS
        assert "market-data" in DEFAULT_JOB_CONFIGS

    def test_accounts_stem_present(self):
        assert _HAS_DEFAULT_JOB_CONFIGS
        assert "accounts" in DEFAULT_JOB_CONFIGS

    def test_indices_stem_present(self):
        assert _HAS_DEFAULT_JOB_CONFIGS
        assert "indices" in DEFAULT_JOB_CONFIGS

    def test_each_value_is_dict(self):
        assert _HAS_DEFAULT_JOB_CONFIGS
        for stem, val in DEFAULT_JOB_CONFIGS.items():
            assert isinstance(val, dict), f"DEFAULT_JOB_CONFIGS[{stem!r}] must be a dict"

    def test_each_has_schema_version_1(self):
        assert _HAS_DEFAULT_JOB_CONFIGS
        for stem, val in DEFAULT_JOB_CONFIGS.items():
            assert val.get("schema_version") == 1, (
                f"DEFAULT_JOB_CONFIGS[{stem!r}] must have schema_version=1"
            )

    def test_each_has_name(self):
        assert _HAS_DEFAULT_JOB_CONFIGS
        for stem, val in DEFAULT_JOB_CONFIGS.items():
            assert "name" in val and isinstance(val["name"], str), (
                f"DEFAULT_JOB_CONFIGS[{stem!r}] must have a string 'name'"
            )

    def test_each_enabled_flag(self):
        assert _HAS_DEFAULT_JOB_CONFIGS
        for stem, val in DEFAULT_JOB_CONFIGS.items():
            expected = stem not in _DISABLED_STEMS
            assert val.get("enabled") is expected, (
                f"DEFAULT_JOB_CONFIGS[{stem!r}] enabled must be {expected}"
            )

    def test_each_type_is_command(self):
        assert _HAS_DEFAULT_JOB_CONFIGS
        for stem, val in DEFAULT_JOB_CONFIGS.items():
            assert val.get("type") == "command", (
                f"DEFAULT_JOB_CONFIGS[{stem!r}] must have type='command'"
            )

    def test_each_timezone_is_america_new_york(self):
        assert _HAS_DEFAULT_JOB_CONFIGS
        for stem, val in DEFAULT_JOB_CONFIGS.items():
            assert val.get("timezone") == "America/New_York", (
                f"DEFAULT_JOB_CONFIGS[{stem!r}] must have timezone='America/New_York'"
            )

    def test_each_has_non_empty_command_list(self):
        assert _HAS_DEFAULT_JOB_CONFIGS
        for stem, val in DEFAULT_JOB_CONFIGS.items():
            cmd = val.get("command")
            assert isinstance(cmd, list) and len(cmd) > 0, (
                f"DEFAULT_JOB_CONFIGS[{stem!r}] must have a non-empty 'command' list"
            )

    def test_each_command_has_skip_wait(self):
        """Dataset commands must include --skip-wait so cron controls timing.

        The screener subcommands don't take --skip-wait (they compute their
        own NY snapshot day), so the invariant applies to dataset stems only.
        """
        assert _HAS_DEFAULT_JOB_CONFIGS
        for stem in _DATASET_STEMS:
            cmd = DEFAULT_JOB_CONFIGS[stem].get("command", [])
            assert "--skip-wait" in cmd, (
                f"DEFAULT_JOB_CONFIGS[{stem!r}] command must include --skip-wait; "
                f"got {cmd}"
            )

    @pytest.mark.parametrize("stem,expected_cron", list(_EXPECTED_CRONS.items()))
    def test_cron_expression(self, stem, expected_cron):
        assert _HAS_DEFAULT_JOB_CONFIGS
        val = DEFAULT_JOB_CONFIGS[stem]
        assert val.get("cron") == expected_cron, (
            f"DEFAULT_JOB_CONFIGS[{stem!r}] cron must be {expected_cron!r}, "
            f"got {val.get('cron')!r}"
        )

    def test_market_data_command_uses_dataset_update_volatility(self):
        """market-data job must invoke 'dataset update --group volatility --skip-wait'."""
        assert _HAS_DEFAULT_JOB_CONFIGS
        cmd = DEFAULT_JOB_CONFIGS["market-data"]["command"]
        assert "dataset" in cmd
        assert "update" in cmd
        assert "--group" in cmd
        assert "volatility" in cmd
        assert "--skip-wait" in cmd

    def test_accounts_command_uses_dataset_accounts_snapshot(self):
        """accounts job must invoke 'dataset accounts snapshot --skip-wait'."""
        assert _HAS_DEFAULT_JOB_CONFIGS
        cmd = DEFAULT_JOB_CONFIGS["accounts"]["command"]
        assert "dataset" in cmd
        assert "accounts" in cmd
        assert "snapshot" in cmd
        assert "--skip-wait" in cmd

    def test_indices_command_uses_dataset_update_indices(self):
        """indices job must invoke 'dataset update --indices ... --skip-wait'."""
        assert _HAS_DEFAULT_JOB_CONFIGS
        cmd = DEFAULT_JOB_CONFIGS["indices"]["command"]
        assert "dataset" in cmd
        assert "update" in cmd
        assert "--indices" in cmd
        assert "--skip-wait" in cmd

    def test_indices_command_has_max_age_days(self):
        """indices job must include --max-age-days 6."""
        assert _HAS_DEFAULT_JOB_CONFIGS
        cmd = DEFAULT_JOB_CONFIGS["indices"]["command"]
        assert "--max-age-days" in cmd
        idx = cmd.index("--max-age-days")
        assert cmd[idx + 1] == "6", (
            f"Expected '--max-age-days 6', got '{cmd[idx]}' '{cmd[idx+1]}'"
        )


# ---------------------------------------------------------------------------
# parse_job round-trip: each default is schema-valid
# ---------------------------------------------------------------------------


class TestDefaultJobConfigsParseJobRoundTrip:
    """Writing each default to disk and parsing it with parse_job must succeed."""

    @pytest.mark.parametrize("stem", list(_EXPECTED_STEMS))
    def test_parse_job_accepts_default(self, monkeypatch, tmp_path, stem):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        assert _HAS_DEFAULT_JOB_CONFIGS, "defaults not importable"
        assert _HAS_PARSE_JOB, "parse_job not importable"

        cfg_dict = DEFAULT_JOB_CONFIGS[stem]
        p = tmp_path / f"{stem}.json"
        p.write_text(json.dumps(cfg_dict), encoding="utf-8")

        # Should not raise
        job_cfg = parse_job(p)
        assert job_cfg is not None

    @pytest.mark.parametrize("stem", list(_EXPECTED_STEMS))
    def test_parsed_type_is_command(self, monkeypatch, tmp_path, stem):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        assert _HAS_DEFAULT_JOB_CONFIGS
        assert _HAS_PARSE_JOB

        cfg_dict = DEFAULT_JOB_CONFIGS[stem]
        p = tmp_path / f"{stem}.json"
        p.write_text(json.dumps(cfg_dict), encoding="utf-8")
        job_cfg = parse_job(p)
        assert job_cfg.type == "command"

    @pytest.mark.parametrize("stem", list(_EXPECTED_STEMS))
    def test_parsed_command_non_empty(self, monkeypatch, tmp_path, stem):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        assert _HAS_DEFAULT_JOB_CONFIGS
        assert _HAS_PARSE_JOB

        cfg_dict = DEFAULT_JOB_CONFIGS[stem]
        p = tmp_path / f"{stem}.json"
        p.write_text(json.dumps(cfg_dict), encoding="utf-8")
        job_cfg = parse_job(p)
        assert job_cfg.command is not None and len(job_cfg.command) > 0

    @pytest.mark.parametrize("stem,expected_cron", list(_EXPECTED_CRONS.items()))
    def test_parsed_cron_matches(self, monkeypatch, tmp_path, stem, expected_cron):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        assert _HAS_DEFAULT_JOB_CONFIGS
        assert _HAS_PARSE_JOB

        cfg_dict = DEFAULT_JOB_CONFIGS[stem]
        p = tmp_path / f"{stem}.json"
        p.write_text(json.dumps(cfg_dict), encoding="utf-8")
        job_cfg = parse_job(p)
        assert job_cfg.cron == expected_cron

    @pytest.mark.parametrize("stem", list(_EXPECTED_STEMS))
    def test_parsed_timezone_is_america_new_york(self, monkeypatch, tmp_path, stem):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        assert _HAS_DEFAULT_JOB_CONFIGS
        assert _HAS_PARSE_JOB

        cfg_dict = DEFAULT_JOB_CONFIGS[stem]
        p = tmp_path / f"{stem}.json"
        p.write_text(json.dumps(cfg_dict), encoding="utf-8")
        job_cfg = parse_job(p)
        assert job_cfg.timezone == "America/New_York"

    @pytest.mark.parametrize("stem", list(_EXPECTED_STEMS))
    def test_parsed_enabled_true(self, monkeypatch, tmp_path, stem):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        assert _HAS_DEFAULT_JOB_CONFIGS
        assert _HAS_PARSE_JOB

        cfg_dict = DEFAULT_JOB_CONFIGS[stem]
        p = tmp_path / f"{stem}.json"
        p.write_text(json.dumps(cfg_dict), encoding="utf-8")
        job_cfg = parse_job(p)
        assert job_cfg.enabled is (stem not in _DISABLED_STEMS)


# ---------------------------------------------------------------------------
# write_default_jobs
# ---------------------------------------------------------------------------


class TestWriteDefaultJobs:
    """write_default_jobs creates files the first time and skips on subsequent calls."""

    def test_function_importable(self):
        assert _HAS_WRITE_DEFAULT_JOBS, (
            "schwab_cli.server.jobs.defaults.write_default_jobs not importable"
        )

    def test_returns_dict(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        assert _HAS_WRITE_DEFAULT_JOBS
        jobs_dir = tmp_path / "jobs"
        result = write_default_jobs(jobs_dir)
        assert isinstance(result, dict), f"Expected dict, got {type(result)}"

    def test_first_call_returns_all_created(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        assert _HAS_WRITE_DEFAULT_JOBS
        jobs_dir = tmp_path / "jobs"
        result = write_default_jobs(jobs_dir)
        for stem in _EXPECTED_STEMS:
            assert result.get(stem) == "created", (
                f"Expected result[{stem!r}]=='created', got {result.get(stem)!r}"
            )

    def test_first_call_creates_three_files(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        assert _HAS_WRITE_DEFAULT_JOBS
        jobs_dir = tmp_path / "jobs"
        write_default_jobs(jobs_dir)
        for stem in _EXPECTED_STEMS:
            assert (jobs_dir / f"{stem}.json").exists(), (
                f"Expected {stem}.json to exist after write_default_jobs"
            )

    def test_creates_directory_if_needed(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        assert _HAS_WRITE_DEFAULT_JOBS
        jobs_dir = tmp_path / "deep" / "nested" / "jobs"
        assert not jobs_dir.exists()
        write_default_jobs(jobs_dir)
        assert jobs_dir.is_dir()

    def test_written_files_parse_cleanly(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        assert _HAS_WRITE_DEFAULT_JOBS
        assert _HAS_PARSE_JOB
        jobs_dir = tmp_path / "jobs"
        write_default_jobs(jobs_dir)
        for stem in _EXPECTED_STEMS:
            path = jobs_dir / f"{stem}.json"
            # Should not raise
            job_cfg = parse_job(path)
            assert job_cfg.id == stem

    def test_second_call_returns_all_exists(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        assert _HAS_WRITE_DEFAULT_JOBS
        jobs_dir = tmp_path / "jobs"
        write_default_jobs(jobs_dir)
        result2 = write_default_jobs(jobs_dir)
        for stem in _EXPECTED_STEMS:
            assert result2.get(stem) == "exists", (
                f"Second call expected result[{stem!r}]=='exists', got {result2.get(stem)!r}"
            )

    def test_second_call_does_not_overwrite_existing_files(self, monkeypatch, tmp_path):
        """A file with a sentinel value must be preserved across the second call."""
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        assert _HAS_WRITE_DEFAULT_JOBS
        jobs_dir = tmp_path / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)

        # Write a sentinel into "accounts.json" before calling write_default_jobs.
        sentinel_content = json.dumps({"_sentinel": "DO_NOT_OVERWRITE"})
        sentinel_path = jobs_dir / "accounts.json"
        sentinel_path.write_text(sentinel_content, encoding="utf-8")

        write_default_jobs(jobs_dir)

        # The sentinel file must be unchanged.
        after = json.loads(sentinel_path.read_text(encoding="utf-8"))
        assert after.get("_sentinel") == "DO_NOT_OVERWRITE", (
            "write_default_jobs must not overwrite an existing file"
        )

    def test_second_call_only_skips_existing_writes_others(self, monkeypatch, tmp_path):
        """When one file pre-exists, only the other two are created."""
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        assert _HAS_WRITE_DEFAULT_JOBS
        jobs_dir = tmp_path / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)

        # Pre-create only "accounts.json".
        (jobs_dir / "accounts.json").write_text(
            json.dumps({"_existing": True}), encoding="utf-8"
        )

        result = write_default_jobs(jobs_dir)
        assert result.get("accounts") == "exists"
        assert result.get("market-data") == "created"
        assert result.get("indices") == "created"

    def test_result_has_exactly_three_keys(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        assert _HAS_WRITE_DEFAULT_JOBS
        jobs_dir = tmp_path / "jobs"
        result = write_default_jobs(jobs_dir)
        assert set(result.keys()) == _EXPECTED_STEMS

    def test_written_json_is_pretty_formatted(self, monkeypatch, tmp_path):
        """Files must be pretty-printed (multiline), not single-line blobs."""
        monkeypatch.setenv("SCHWAB_CLI_CONFIG_DIR", str(tmp_path))
        assert _HAS_WRITE_DEFAULT_JOBS
        jobs_dir = tmp_path / "jobs"
        write_default_jobs(jobs_dir)
        for stem in _EXPECTED_STEMS:
            raw = (jobs_dir / f"{stem}.json").read_text(encoding="utf-8")
            assert "\n" in raw, (
                f"{stem}.json must be pretty-printed (contains newlines)"
            )
