"""TDD red-phase tests for promote() and PromotionResult in
schwab_cli.server.jobs.config.

All imports are expected to fail (ModuleNotFoundError) until the module is implemented.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from schwab_cli.server.jobs.config import PromotionResult, promote


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_COMMAND_JOB = {
    "name": "My Job",
    "enabled": True,
    "cron": "0 3 * * *",
    "timezone": "UTC",
    "type": "command",
    "command": ["echo", "hello"],
}

_VALID_PYTHON_JOB = {
    "name": "Python Job",
    "enabled": True,
    "cron": "0 6 * * 1",
    "timezone": "America/New_York",
    "type": "python",
    "runner": "myapp.tasks.run",
}

_INVALID_JOB = {
    # cron is missing — invalid
    "name": "Bad Job",
    "enabled": True,
    "timezone": "UTC",
    "type": "command",
    "command": ["echo"],
}


def _write(directory: Path, job_id: str, payload: dict) -> Path:
    p = directory / f"{job_id}.json"
    p.write_text(json.dumps(payload))
    return p


def _make_dirs(tmp_path: Path):
    staging = tmp_path / "staging"
    current = tmp_path / "current"
    staging.mkdir()
    current.mkdir()
    return staging, current


# ---------------------------------------------------------------------------
# PromotionResult dataclass
# ---------------------------------------------------------------------------


def test_promotion_result_fields():
    r = PromotionResult(id="myjob", outcome="updated")
    assert r.id == "myjob"
    assert r.outcome == "updated"
    assert r.error is None


def test_promotion_result_with_error():
    r = PromotionResult(id="badjob", outcome="error", error="bad cron")
    assert r.error == "bad cron"


def test_promotion_result_is_frozen():
    r = PromotionResult(id="x", outcome="unchanged")
    with pytest.raises(Exception):
        r.outcome = "updated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# promote() – single-file scenarios
# ---------------------------------------------------------------------------


def test_promote_new_valid_file_is_updated(tmp_path):
    staging, current = _make_dirs(tmp_path)
    _write(staging, "alpha", _VALID_COMMAND_JOB)

    results = promote(staging, current)

    assert len(results) == 1
    assert results[0].id == "alpha"
    assert results[0].outcome == "updated"
    assert results[0].error is None
    assert (current / "alpha.json").exists()


def test_promote_new_valid_file_content_written_correctly(tmp_path):
    staging, current = _make_dirs(tmp_path)
    _write(staging, "alpha", _VALID_COMMAND_JOB)

    promote(staging, current)

    written = json.loads((current / "alpha.json").read_text())
    assert written["name"] == _VALID_COMMAND_JOB["name"]
    assert written["type"] == "command"


def test_promote_unchanged_second_run(tmp_path):
    staging, current = _make_dirs(tmp_path)
    _write(staging, "alpha", _VALID_COMMAND_JOB)

    promote(staging, current)  # first run → updated
    results = promote(staging, current)  # second run → unchanged

    assert len(results) == 1
    assert results[0].outcome == "unchanged"
    assert results[0].error is None


def test_promote_unchanged_does_not_modify_current_file(tmp_path):
    staging, current = _make_dirs(tmp_path)
    _write(staging, "alpha", _VALID_COMMAND_JOB)
    promote(staging, current)

    # Record mtime before second promote
    mtime_before = (current / "alpha.json").stat().st_mtime

    promote(staging, current)

    mtime_after = (current / "alpha.json").stat().st_mtime
    assert mtime_after == mtime_before


def test_promote_outdated_preserves_old_content(tmp_path):
    """
    Sequence:
    1. Promote a valid job → written to current.
    2. Replace staging with an invalid version.
    3. Promote again → outcome "outdated", current still holds the OLD content.
    """
    staging, current = _make_dirs(tmp_path)
    _write(staging, "alpha", _VALID_COMMAND_JOB)
    promote(staging, current)

    # Overwrite staging with invalid payload (bad cron)
    invalid_payload = dict(_VALID_COMMAND_JOB)
    invalid_payload["cron"] = "not a cron"
    _write(staging, "alpha", invalid_payload)

    results = promote(staging, current)

    assert results[0].outcome == "outdated"
    assert results[0].error is not None
    assert len(results[0].error) > 0

    # current must STILL hold the original valid content
    on_disk = json.loads((current / "alpha.json").read_text())
    assert on_disk["cron"] == _VALID_COMMAND_JOB["cron"]


def test_promote_outdated_error_message_is_non_empty(tmp_path):
    staging, current = _make_dirs(tmp_path)
    _write(staging, "alpha", _VALID_COMMAND_JOB)
    promote(staging, current)

    invalid_payload = dict(_VALID_COMMAND_JOB, cron="bad cron expression")
    _write(staging, "alpha", invalid_payload)

    results = promote(staging, current)
    assert results[0].outcome == "outdated"
    assert results[0].error


def test_promote_brand_new_invalid_file_yields_error(tmp_path):
    staging, current = _make_dirs(tmp_path)
    _write(staging, "newbad", _INVALID_JOB)

    results = promote(staging, current)

    assert len(results) == 1
    assert results[0].id == "newbad"
    assert results[0].outcome == "error"
    assert results[0].error is not None
    assert not (current / "newbad.json").exists()


def test_promote_brand_new_invalid_writes_nothing_to_current(tmp_path):
    staging, current = _make_dirs(tmp_path)
    _write(staging, "newbad", _INVALID_JOB)

    promote(staging, current)

    assert list(current.iterdir()) == []


def test_promote_unload_removes_current_file(tmp_path):
    staging, current = _make_dirs(tmp_path)
    _write(staging, "alpha", _VALID_COMMAND_JOB)
    promote(staging, current)

    # Remove from staging
    (staging / "alpha.json").unlink()

    results = promote(staging, current)

    assert len(results) == 1
    assert results[0].id == "alpha"
    assert results[0].outcome == "unloaded"
    assert not (current / "alpha.json").exists()


# ---------------------------------------------------------------------------
# promote() – atomicity
# ---------------------------------------------------------------------------


def test_promote_write_is_atomic(tmp_path):
    """
    The write to current must use an atomic temp+rename pattern within current_dir.
    We can't easily test the syscall directly, but we verify no partial file
    is left behind if the staging file is valid: the final file is complete JSON.
    """
    staging, current = _make_dirs(tmp_path)
    _write(staging, "beta", _VALID_PYTHON_JOB)

    promote(staging, current)

    content = (current / "beta.json").read_text()
    parsed = json.loads(content)  # would raise if partial/corrupt
    assert parsed["name"] == _VALID_PYTHON_JOB["name"]


# ---------------------------------------------------------------------------
# promote() – mixed batch and sort order
# ---------------------------------------------------------------------------


def test_promote_mixed_batch_correct_outcomes_sorted(tmp_path):
    """
    Staging: valid job 'apple', invalid job 'banana', valid job 'cherry'.
    Current: pre-existing valid 'cherry' (identical content).

    Expected outcomes sorted by id:
      apple   → updated  (new valid)
      banana  → error    (new invalid)
      cherry  → unchanged (already current)
    """
    staging, current = _make_dirs(tmp_path)

    # Pre-seed current with cherry
    _write(current, "cherry", _VALID_PYTHON_JOB)
    # Staging files
    _write(staging, "apple", _VALID_COMMAND_JOB)
    _write(staging, "banana", _INVALID_JOB)
    _write(staging, "cherry", _VALID_PYTHON_JOB)

    results = promote(staging, current)

    assert len(results) == 3
    assert results[0].id == "apple"
    assert results[0].outcome == "updated"
    assert results[1].id == "banana"
    assert results[1].outcome == "error"
    assert results[2].id == "cherry"
    assert results[2].outcome == "unchanged"


def test_promote_mixed_batch_sorted_by_id(tmp_path):
    staging, current = _make_dirs(tmp_path)
    _write(staging, "z-job", _VALID_COMMAND_JOB)
    _write(staging, "a-job", _VALID_COMMAND_JOB)
    _write(staging, "m-job", _VALID_COMMAND_JOB)

    results = promote(staging, current)

    assert [r.id for r in results] == ["a-job", "m-job", "z-job"]


def test_promote_unload_in_mixed_batch(tmp_path):
    """
    Staging: 'alpha' valid (new), no 'beta'.
    Current: pre-existing 'beta'.
    Expect: alpha=updated, beta=unloaded; sorted by id.
    """
    staging, current = _make_dirs(tmp_path)
    _write(current, "beta", _VALID_COMMAND_JOB)
    _write(staging, "alpha", _VALID_COMMAND_JOB)

    results = promote(staging, current)

    assert len(results) == 2
    assert results[0].id == "alpha"
    assert results[0].outcome == "updated"
    assert results[1].id == "beta"
    assert results[1].outcome == "unloaded"
    assert not (current / "beta.json").exists()


def test_promote_outdated_in_mixed_batch_leaves_old_current_intact(tmp_path):
    """
    Staging: valid 'good', invalid 'shaky'.
    Current: pre-existing valid 'shaky'.
    After promote: shaky=outdated, current/shaky.json still holds old content.
    """
    staging, current = _make_dirs(tmp_path)
    old_shaky = dict(_VALID_PYTHON_JOB, name="Shaky Old")
    _write(current, "shaky", old_shaky)
    _write(staging, "good", _VALID_COMMAND_JOB)
    _write(staging, "shaky", _INVALID_JOB)

    results = promote(staging, current)

    outcomes = {r.id: r.outcome for r in results}
    assert outcomes["good"] == "updated"
    assert outcomes["shaky"] == "outdated"

    on_disk = json.loads((current / "shaky.json").read_text())
    assert on_disk["name"] == "Shaky Old"


def test_promote_returns_list_not_generator(tmp_path):
    staging, current = _make_dirs(tmp_path)
    _write(staging, "alpha", _VALID_COMMAND_JOB)

    result = promote(staging, current)

    assert isinstance(result, list)


def test_promote_empty_staging_and_current_returns_empty(tmp_path):
    staging, current = _make_dirs(tmp_path)
    results = promote(staging, current)
    assert results == []


# ---------------------------------------------------------------------------
# _atomic_write – temp file cleanup on os.replace failure (finding 2)
# ---------------------------------------------------------------------------


def test_atomic_write_failure_leaves_no_temp_file(tmp_path, monkeypatch):
    """If os.replace fails, the .<name>.tmp staging file must be cleaned up
    and the original error re-raised."""
    from schwab_cli.server.jobs import config as config_mod

    staging, current = _make_dirs(tmp_path)
    _write(staging, "alpha", _VALID_COMMAND_JOB)

    def boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(config_mod.os, "replace", boom)

    with pytest.raises(OSError, match="simulated replace failure"):
        promote(staging, current)

    # No leftover temp file in current_dir.
    leftovers = [p.name for p in current.iterdir()]
    assert leftovers == [], f"temp file leaked: {leftovers}"
    assert not (current / ".alpha.json.tmp").exists()
