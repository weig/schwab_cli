"""TDD red-phase tests for schwab_cli.server.jobs.config.

Covers: JobConfig dataclass, JobConfigError, parse_job, load_jobs, promote/PromotionResult.
All imports are expected to fail (ModuleNotFoundError) until the module is implemented.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from schwab_cli.server.jobs.config import (
    DEFAULT_RETRIES,
    DEFAULT_RETRY_DELAY_S,
    DEFAULT_TIMEOUT_S,
    JOB_TYPES,
    JobConfig,
    JobConfigError,
    load_jobs,
    parse_job,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_job_types_tuple():
    assert JOB_TYPES == ("command", "python")


def test_default_timeout():
    assert DEFAULT_TIMEOUT_S == 16 * 3600


def test_default_retries():
    assert DEFAULT_RETRIES == 1


def test_default_retry_delay():
    assert DEFAULT_RETRY_DELAY_S == 120


# ---------------------------------------------------------------------------
# JobConfig dataclass
# ---------------------------------------------------------------------------


def test_job_config_is_frozen(tmp_path):
    cfg = JobConfig(
        id="myjob",
        name="My Job",
        enabled=True,
        cron="0 9 * * *",
        timezone="America/New_York",
        type="command",
        command=("echo", "hello"),
    )
    with pytest.raises(Exception):
        cfg.id = "other"  # type: ignore[misc]


def test_job_config_defaults():
    cfg = JobConfig(
        id="myjob",
        name="My Job",
        enabled=True,
        cron="0 9 * * *",
        timezone="UTC",
        type="command",
        command=("echo",),
    )
    assert cfg.runner is None
    assert cfg.args == ()
    assert cfg.kwargs == {} or cfg.kwargs == ()  # empty mapping
    assert cfg.timeout_s == DEFAULT_TIMEOUT_S
    assert cfg.retries == DEFAULT_RETRIES
    assert cfg.retry_delay_s == DEFAULT_RETRY_DELAY_S
    assert cfg.schema_version == 1


def test_job_config_command_tuple():
    cfg = JobConfig(
        id="j",
        name="J",
        enabled=True,
        cron="0 0 * * *",
        timezone="UTC",
        type="command",
        command=("ls", "-la"),
    )
    assert isinstance(cfg.command, tuple)
    assert cfg.command == ("ls", "-la")


def test_job_config_python_fields():
    cfg = JobConfig(
        id="j",
        name="J",
        enabled=False,
        cron="0 0 * * *",
        timezone="UTC",
        type="python",
        runner="mypackage.tasks.run",
        args=(1, 2),
        kwargs={"verbose": True},
    )
    assert cfg.runner == "mypackage.tasks.run"
    assert cfg.args == (1, 2)
    assert cfg.kwargs == {"verbose": True}


# ---------------------------------------------------------------------------
# JobConfigError
# ---------------------------------------------------------------------------


def test_job_config_error_has_job_id():
    err = JobConfigError("my-job", "something went wrong")
    assert err.job_id == "my-job"


def test_job_config_error_str_contains_message():
    err = JobConfigError("my-job", "something went wrong")
    assert "something went wrong" in str(err)


# ---------------------------------------------------------------------------
# parse_job – valid cases
# ---------------------------------------------------------------------------


def _write_job(tmp_path: Path, job_id: str, payload: dict) -> Path:
    p = tmp_path / f"{job_id}.json"
    p.write_text(json.dumps(payload))
    return p


def test_parse_job_valid_command(tmp_path):
    path = _write_job(tmp_path, "backup", {
        "name": "Backup Job",
        "enabled": True,
        "cron": "0 3 * * *",
        "timezone": "UTC",
        "type": "command",
        "command": ["/usr/bin/rsync", "-av", "/src", "/dst"],
    })
    cfg = parse_job(path)
    assert cfg.id == "backup"
    assert cfg.name == "Backup Job"
    assert cfg.enabled is True
    assert cfg.cron == "0 3 * * *"
    assert cfg.timezone == "UTC"
    assert cfg.type == "command"
    assert cfg.command == ("/usr/bin/rsync", "-av", "/src", "/dst")
    assert cfg.runner is None
    assert cfg.timeout_s == DEFAULT_TIMEOUT_S
    assert cfg.retries == DEFAULT_RETRIES
    assert cfg.retry_delay_s == DEFAULT_RETRY_DELAY_S
    assert cfg.schema_version == 1


def test_parse_job_valid_python(tmp_path):
    path = _write_job(tmp_path, "report", {
        "name": "Report Job",
        "enabled": False,
        "cron": "30 18 * * 1-5",
        "timezone": "America/Chicago",
        "type": "python",
        "runner": "myapp.jobs.generate_report",
        "args": ["daily"],
        "kwargs": {"format": "pdf"},
        "timeout_s": 600,
        "retries": 3,
        "retry_delay_s": 30,
    })
    cfg = parse_job(path)
    assert cfg.id == "report"
    assert cfg.enabled is False
    assert cfg.runner == "myapp.jobs.generate_report"
    assert cfg.args == ("daily",)
    assert cfg.kwargs == {"format": "pdf"}
    assert cfg.timeout_s == 600
    assert cfg.retries == 3
    assert cfg.retry_delay_s == 30


def test_parse_job_id_comes_from_filename_stem(tmp_path):
    path = _write_job(tmp_path, "nightly-sync", {
        "name": "Nightly Sync",
        "enabled": True,
        "cron": "0 2 * * *",
        "timezone": "UTC",
        "type": "command",
        "command": ["./sync.sh"],
    })
    cfg = parse_job(path)
    assert cfg.id == "nightly-sync"


def test_parse_job_command_becomes_tuple(tmp_path):
    path = _write_job(tmp_path, "cmd", {
        "name": "Cmd",
        "enabled": True,
        "cron": "0 0 * * *",
        "timezone": "UTC",
        "type": "command",
        "command": ["echo", "hi"],
    })
    cfg = parse_job(path)
    assert isinstance(cfg.command, tuple)


def test_parse_job_kwargs_preserved(tmp_path):
    path = _write_job(tmp_path, "kw", {
        "name": "KW Job",
        "enabled": True,
        "cron": "0 0 * * *",
        "timezone": "UTC",
        "type": "python",
        "runner": "pkg.mod.fn",
        "kwargs": {"a": 1, "b": "x"},
    })
    cfg = parse_job(path)
    assert cfg.kwargs == {"a": 1, "b": "x"}


# ---------------------------------------------------------------------------
# parse_job – invalid cases → JobConfigError
# ---------------------------------------------------------------------------


def test_parse_job_malformed_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not valid json")
    with pytest.raises(JobConfigError) as exc_info:
        parse_job(p)
    assert exc_info.value.job_id == "bad"


def test_parse_job_wrong_schema_version(tmp_path):
    path = _write_job(tmp_path, "ver", {
        "schema_version": 99,
        "name": "V",
        "enabled": True,
        "cron": "0 0 * * *",
        "timezone": "UTC",
        "type": "command",
        "command": ["echo"],
    })
    with pytest.raises(JobConfigError):
        parse_job(path)


def test_parse_job_missing_name(tmp_path):
    path = _write_job(tmp_path, "noname", {
        "enabled": True,
        "cron": "0 0 * * *",
        "timezone": "UTC",
        "type": "command",
        "command": ["echo"],
    })
    with pytest.raises(JobConfigError):
        parse_job(path)


def test_parse_job_missing_enabled(tmp_path):
    path = _write_job(tmp_path, "noenabled", {
        "name": "X",
        "cron": "0 0 * * *",
        "timezone": "UTC",
        "type": "command",
        "command": ["echo"],
    })
    with pytest.raises(JobConfigError):
        parse_job(path)


def test_parse_job_missing_cron(tmp_path):
    path = _write_job(tmp_path, "nocron", {
        "name": "X",
        "enabled": True,
        "timezone": "UTC",
        "type": "command",
        "command": ["echo"],
    })
    with pytest.raises(JobConfigError):
        parse_job(path)


def test_parse_job_missing_timezone(tmp_path):
    path = _write_job(tmp_path, "notz", {
        "name": "X",
        "enabled": True,
        "cron": "0 0 * * *",
        "type": "command",
        "command": ["echo"],
    })
    with pytest.raises(JobConfigError):
        parse_job(path)


def test_parse_job_missing_type(tmp_path):
    path = _write_job(tmp_path, "notype", {
        "name": "X",
        "enabled": True,
        "cron": "0 0 * * *",
        "timezone": "UTC",
        "command": ["echo"],
    })
    with pytest.raises(JobConfigError):
        parse_job(path)


def test_parse_job_enabled_not_bool(tmp_path):
    path = _write_job(tmp_path, "notbool", {
        "name": "X",
        "enabled": "yes",
        "cron": "0 0 * * *",
        "timezone": "UTC",
        "type": "command",
        "command": ["echo"],
    })
    with pytest.raises(JobConfigError):
        parse_job(path)


def test_parse_job_invalid_type_value(tmp_path):
    path = _write_job(tmp_path, "badtype", {
        "name": "X",
        "enabled": True,
        "cron": "0 0 * * *",
        "timezone": "UTC",
        "type": "shell",  # not in JOB_TYPES
        "command": ["echo"],
    })
    with pytest.raises(JobConfigError):
        parse_job(path)


def test_parse_job_command_type_missing_command(tmp_path):
    path = _write_job(tmp_path, "nocmd", {
        "name": "X",
        "enabled": True,
        "cron": "0 0 * * *",
        "timezone": "UTC",
        "type": "command",
    })
    with pytest.raises(JobConfigError):
        parse_job(path)


def test_parse_job_command_type_empty_command(tmp_path):
    path = _write_job(tmp_path, "emptycmd", {
        "name": "X",
        "enabled": True,
        "cron": "0 0 * * *",
        "timezone": "UTC",
        "type": "command",
        "command": [],
    })
    with pytest.raises(JobConfigError):
        parse_job(path)


def test_parse_job_command_not_list_of_strings(tmp_path):
    path = _write_job(tmp_path, "badcmd", {
        "name": "X",
        "enabled": True,
        "cron": "0 0 * * *",
        "timezone": "UTC",
        "type": "command",
        "command": [1, 2, 3],
    })
    with pytest.raises(JobConfigError):
        parse_job(path)


def test_parse_job_python_type_missing_runner(tmp_path):
    path = _write_job(tmp_path, "norunner", {
        "name": "X",
        "enabled": True,
        "cron": "0 0 * * *",
        "timezone": "UTC",
        "type": "python",
    })
    with pytest.raises(JobConfigError):
        parse_job(path)


def test_parse_job_python_runner_not_dotted_path(tmp_path):
    path = _write_job(tmp_path, "baddot", {
        "name": "X",
        "enabled": True,
        "cron": "0 0 * * *",
        "timezone": "UTC",
        "type": "python",
        "runner": "nodot",  # no dot → invalid
    })
    with pytest.raises(JobConfigError):
        parse_job(path)


def test_parse_job_invalid_cron_bad_field(tmp_path):
    path = _write_job(tmp_path, "badcron", {
        "name": "X",
        "enabled": True,
        "cron": "* * 99 * *",  # day-of-month 99 is invalid
        "timezone": "UTC",
        "type": "command",
        "command": ["echo"],
    })
    with pytest.raises(JobConfigError):
        parse_job(path)


def test_parse_job_invalid_cron_not_cron_string(tmp_path):
    path = _write_job(tmp_path, "notcron", {
        "name": "X",
        "enabled": True,
        "cron": "not a cron",
        "timezone": "UTC",
        "type": "command",
        "command": ["echo"],
    })
    with pytest.raises(JobConfigError):
        parse_job(path)


def test_parse_job_invalid_timezone(tmp_path):
    path = _write_job(tmp_path, "badtz", {
        "name": "X",
        "enabled": True,
        "cron": "0 0 * * *",
        "timezone": "Mars/Phobos",
        "type": "command",
        "command": ["echo"],
    })
    with pytest.raises(JobConfigError):
        parse_job(path)


# ---------------------------------------------------------------------------
# load_jobs
# ---------------------------------------------------------------------------


def test_load_jobs_empty_dir(tmp_path):
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    valid, errors = load_jobs(jobs_dir)
    assert valid == []
    assert errors == {}


def test_load_jobs_returns_sorted_by_id(tmp_path):
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    for job_id in ("z-job", "a-job", "m-job"):
        (jobs_dir / f"{job_id}.json").write_text(json.dumps({
            "name": job_id,
            "enabled": True,
            "cron": "0 0 * * *",
            "timezone": "UTC",
            "type": "command",
            "command": ["echo"],
        }))
    valid, errors = load_jobs(jobs_dir)
    assert [c.id for c in valid] == ["a-job", "m-job", "z-job"]
    assert errors == {}


def test_load_jobs_invalid_file_goes_to_errors(tmp_path):
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    (jobs_dir / "good.json").write_text(json.dumps({
        "name": "Good",
        "enabled": True,
        "cron": "0 0 * * *",
        "timezone": "UTC",
        "type": "command",
        "command": ["echo"],
    }))
    (jobs_dir / "bad.json").write_text("{broken json")
    valid, errors = load_jobs(jobs_dir)
    assert len(valid) == 1
    assert valid[0].id == "good"
    assert "bad" in errors


def test_load_jobs_does_not_raise_for_invalid_file(tmp_path):
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    (jobs_dir / "broken.json").write_text("{not valid")
    # must not raise
    valid, errors = load_jobs(jobs_dir)
    assert valid == []
    assert "broken" in errors


def test_load_jobs_ignores_dotfiles(tmp_path):
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    (jobs_dir / ".hidden.json").write_text(json.dumps({
        "name": "Hidden",
        "enabled": True,
        "cron": "0 0 * * *",
        "timezone": "UTC",
        "type": "command",
        "command": ["echo"],
    }))
    valid, errors = load_jobs(jobs_dir)
    assert valid == []
    assert errors == {}


def test_load_jobs_ignores_current_subdir(tmp_path):
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    current_dir = jobs_dir / ".current"
    current_dir.mkdir()
    (current_dir / "sneaky.json").write_text(json.dumps({
        "name": "Sneaky",
        "enabled": True,
        "cron": "0 0 * * *",
        "timezone": "UTC",
        "type": "command",
        "command": ["echo"],
    }))
    valid, errors = load_jobs(jobs_dir)
    assert valid == []
    assert errors == {}


def test_load_jobs_error_message_is_string(tmp_path):
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    (jobs_dir / "bad.json").write_text("{not json}")
    _, errors = load_jobs(jobs_dir)
    assert isinstance(errors["bad"], str)
    assert len(errors["bad"]) > 0


def test_load_jobs_multiple_errors_collected(tmp_path):
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    (jobs_dir / "err1.json").write_text("{bad")
    (jobs_dir / "err2.json").write_text(json.dumps({
        "name": "E2",
        "enabled": True,
        "cron": "not a cron",
        "timezone": "UTC",
        "type": "command",
        "command": ["echo"],
    }))
    valid, errors = load_jobs(jobs_dir)
    assert valid == []
    assert set(errors.keys()) == {"err1", "err2"}


# ---------------------------------------------------------------------------
# JobConfig kwargs immutability / hashability / equality (finding 1)
# ---------------------------------------------------------------------------


def _make_cfg(**overrides):
    base = dict(
        id="j",
        name="J",
        enabled=True,
        cron="0 0 * * *",
        timezone="UTC",
        type="command",
        command=("echo",),
    )
    base.update(overrides)
    return JobConfig(**base)


def test_job_config_kwargs_is_immutable():
    cfg = _make_cfg(kwargs={"a": 1})
    with pytest.raises(TypeError):
        cfg.kwargs["x"] = 1  # type: ignore[index]


def test_job_config_equal_kwargs_compare_equal():
    a = _make_cfg(kwargs={"a": 1, "b": 2})
    b = _make_cfg(kwargs={"a": 1, "b": 2})
    assert a == b


def test_job_config_is_hashable_with_hashable_values():
    a = _make_cfg(kwargs={"a": 1, "b": "x"})
    b = _make_cfg(kwargs={"a": 1, "b": "x"})
    assert hash(a) == hash(b)
    # usable as a set/dict key
    assert len({a, b}) == 1


# ---------------------------------------------------------------------------
# parse_job – name validation (finding 5)
# ---------------------------------------------------------------------------


def test_parse_job_name_not_string(tmp_path):
    path = _write_job(tmp_path, "badname", {
        "name": 123,
        "enabled": True,
        "cron": "0 0 * * *",
        "timezone": "UTC",
        "type": "command",
        "command": ["echo"],
    })
    with pytest.raises(JobConfigError):
        parse_job(path)


# ---------------------------------------------------------------------------
# parse_job – numeric field validation (finding 4)
# ---------------------------------------------------------------------------


def test_parse_job_timeout_not_int(tmp_path):
    path = _write_job(tmp_path, "badtimeout", {
        "name": "X",
        "enabled": True,
        "cron": "0 0 * * *",
        "timezone": "UTC",
        "type": "command",
        "command": ["echo"],
        "timeout_s": "forever",
    })
    with pytest.raises(JobConfigError):
        parse_job(path)


def test_parse_job_timeout_zero_rejected(tmp_path):
    path = _write_job(tmp_path, "zerotimeout", {
        "name": "X",
        "enabled": True,
        "cron": "0 0 * * *",
        "timezone": "UTC",
        "type": "command",
        "command": ["echo"],
        "timeout_s": 0,
    })
    with pytest.raises(JobConfigError):
        parse_job(path)


def test_parse_job_retries_negative_rejected(tmp_path):
    path = _write_job(tmp_path, "negretries", {
        "name": "X",
        "enabled": True,
        "cron": "0 0 * * *",
        "timezone": "UTC",
        "type": "command",
        "command": ["echo"],
        "retries": -1,
    })
    with pytest.raises(JobConfigError):
        parse_job(path)


def test_parse_job_retries_float_rejected(tmp_path):
    path = _write_job(tmp_path, "floatretries", {
        "name": "X",
        "enabled": True,
        "cron": "0 0 * * *",
        "timezone": "UTC",
        "type": "command",
        "command": ["echo"],
        "retries": 1.5,
    })
    with pytest.raises(JobConfigError):
        parse_job(path)


def test_parse_job_retries_bool_rejected(tmp_path):
    path = _write_job(tmp_path, "boolretries", {
        "name": "X",
        "enabled": True,
        "cron": "0 0 * * *",
        "timezone": "UTC",
        "type": "command",
        "command": ["echo"],
        "retries": True,
    })
    with pytest.raises(JobConfigError):
        parse_job(path)


def test_parse_job_retry_delay_zero_rejected(tmp_path):
    path = _write_job(tmp_path, "zerodelay", {
        "name": "X",
        "enabled": True,
        "cron": "0 0 * * *",
        "timezone": "UTC",
        "type": "command",
        "command": ["echo"],
        "retry_delay_s": 0,
    })
    with pytest.raises(JobConfigError):
        parse_job(path)


def test_parse_job_retries_zero_allowed(tmp_path):
    path = _write_job(tmp_path, "zeroretries", {
        "name": "X",
        "enabled": True,
        "cron": "0 0 * * *",
        "timezone": "UTC",
        "type": "command",
        "command": ["echo"],
        "retries": 0,
    })
    cfg = parse_job(path)
    assert cfg.retries == 0


# ---------------------------------------------------------------------------
# parse_job – UTF-8 BOM (finding 6)
# ---------------------------------------------------------------------------


def test_parse_job_utf8_bom_parses_fine(tmp_path):
    p = tmp_path / "bom.json"
    payload = {
        "name": "BOM Job",
        "enabled": True,
        "cron": "0 0 * * *",
        "timezone": "UTC",
        "type": "command",
        "command": ["echo"],
    }
    # Write with an explicit UTF-8 BOM prefix.
    p.write_text("﻿" + json.dumps(payload), encoding="utf-8")
    cfg = parse_job(p)
    assert cfg.name == "BOM Job"


# ---------------------------------------------------------------------------
# parse_job – reject 6-field cron (finding 7)
# ---------------------------------------------------------------------------


def test_parse_job_rejects_six_field_cron(tmp_path):
    path = _write_job(tmp_path, "sixfield", {
        "name": "X",
        "enabled": True,
        "cron": "0 0 0 * * *",  # 6-field seconds extension must be rejected
        "timezone": "UTC",
        "type": "command",
        "command": ["echo"],
    })
    with pytest.raises(JobConfigError):
        parse_job(path)
