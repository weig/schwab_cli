"""Job configuration: parsing, validation, loading and promotion.

A job is described by a single JSON file ``jobs/<id>.json`` whose ``id`` is the
file stem. This module turns those files into frozen, equality-comparable
:class:`JobConfig` instances, collects validation errors without raising, and
atomically promotes a staging directory into a ``current`` directory.
"""
from __future__ import annotations

import json
import logging
import os
import types
import zoneinfo
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from croniter import croniter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JOB_TYPES: tuple[str, ...] = ("command", "python")
DEFAULT_TIMEOUT_S: int = 16 * 3600
DEFAULT_RETRIES: int = 1
DEFAULT_RETRY_DELAY_S: int = 120

_REQUIRED_FIELDS: tuple[str, ...] = ("name", "enabled", "cron", "timezone", "type")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JobConfig:
    """Immutable, equality-comparable description of a scheduled job."""

    id: str
    name: str
    enabled: bool
    cron: str
    timezone: str
    type: str
    command: tuple[str, ...] | None = None
    runner: str | None = None
    args: tuple = ()
    kwargs: Mapping[str, Any] = field(default_factory=dict)
    timeout_s: int = DEFAULT_TIMEOUT_S
    retries: int = DEFAULT_RETRIES
    retry_delay_s: int = DEFAULT_RETRY_DELAY_S
    schema_version: int = 1

    def __post_init__(self) -> None:
        # Coerce sequence-ish fields to tuples without mutating the instance.
        if self.command is not None and not isinstance(self.command, tuple):
            object.__setattr__(self, "command", tuple(self.command))
        if not isinstance(self.args, tuple):
            object.__setattr__(self, "args", tuple(self.args))
        # Coerce kwargs to a read-only mapping so the frozen instance cannot be
        # mutated through it (a plain dict would also break hashing/equality).
        if not isinstance(self.kwargs, types.MappingProxyType):
            object.__setattr__(
                self, "kwargs", types.MappingProxyType(dict(self.kwargs))
            )

    def __hash__(self) -> int:
        # The dataclass-generated hash would choke on the MappingProxyType
        # kwargs (mappings are unhashable). Hash a frozenset of its items so
        # the instance stays hashable whenever the kwargs values are hashable.
        return hash(
            (
                self.id,
                self.name,
                self.enabled,
                self.cron,
                self.timezone,
                self.type,
                self.command,
                self.runner,
                self.args,
                frozenset(self.kwargs.items()),
                self.timeout_s,
                self.retries,
                self.retry_delay_s,
                self.schema_version,
            )
        )


class JobConfigError(Exception):
    """Raised when a single job file is invalid.

    ``job_id`` identifies the offending job; ``str(error)`` contains ``message``.
    """

    def __init__(self, job_id: str, message: str) -> None:
        super().__init__(message)
        self.job_id = job_id
        self.message = message


@dataclass(frozen=True)
class PromotionResult:
    """Outcome of promoting a single job id from staging to current."""

    id: str
    outcome: str
    error: str | None = None


# ---------------------------------------------------------------------------
# Parsing / validation
# ---------------------------------------------------------------------------


def _validate_command(job_id: str, raw: Mapping[str, Any]) -> tuple[str, ...]:
    command = raw.get("command")
    if not command or not isinstance(command, list):
        raise JobConfigError(job_id, "command job requires a non-empty 'command' list")
    if not all(isinstance(part, str) for part in command):
        raise JobConfigError(job_id, "'command' must be a list of strings")
    return tuple(command)


def _validate_runner(job_id: str, raw: Mapping[str, Any]) -> str:
    runner = raw.get("runner")
    if not isinstance(runner, str) or "." not in runner:
        raise JobConfigError(
            job_id, "python job requires 'runner' as a dotted path (e.g. 'pkg.mod.fn')"
        )
    return runner


def _validate_int(
    job_id: str, raw: Mapping[str, Any], key: str, default: int, *, minimum: int
) -> int:
    """Return an int field, enforcing it is a real int (not bool) >= minimum."""
    value = raw.get(key, default)
    # bool is an int subclass; reject it so True/False can't masquerade as 1/0.
    if isinstance(value, bool) or not isinstance(value, int):
        raise JobConfigError(
            job_id, f"'{key}' must be an integer; got {type(value).__name__}"
        )
    if value < minimum:
        raise JobConfigError(job_id, f"'{key}' must be >= {minimum}; got {value}")
    return value


def parse_job(path: Path) -> JobConfig:
    """Read and validate one ``jobs/<id>.json`` file.

    The job id is the file stem. Any invalid input raises
    :class:`JobConfigError` carrying that job id.
    """
    job_id = path.stem
    try:
        # utf-8-sig transparently strips a leading BOM so a BOM-prefixed file
        # parses fine instead of failing with a cryptic JSON error.
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise JobConfigError(job_id, f"malformed JSON: {exc}") from exc
    except OSError as exc:
        raise JobConfigError(job_id, f"cannot read job file: {exc}") from exc

    if not isinstance(raw, dict):
        raise JobConfigError(job_id, "job file must contain a JSON object")

    schema_version = raw.get("schema_version", 1)
    if schema_version != 1:
        raise JobConfigError(job_id, f"unsupported schema_version: {schema_version!r}")

    for key in _REQUIRED_FIELDS:
        if key not in raw:
            raise JobConfigError(job_id, f"missing required field: '{key}'")

    name = raw["name"]
    if not isinstance(name, str):
        raise JobConfigError(job_id, "'name' must be a string")

    enabled = raw["enabled"]
    if not isinstance(enabled, bool):
        raise JobConfigError(job_id, "'enabled' must be a boolean")

    job_type = raw["type"]
    if job_type not in JOB_TYPES:
        raise JobConfigError(job_id, f"'type' must be one of {JOB_TYPES}, got {job_type!r}")

    command: tuple[str, ...] | None = None
    runner: str | None = None
    if job_type == "command":
        command = _validate_command(job_id, raw)
    else:  # python
        runner = _validate_runner(job_id, raw)

    cron = raw["cron"]
    # Require exactly 5 fields: we fire per-minute, so the 6-field seconds
    # extension that croniter also accepts must be rejected.
    if (
        not isinstance(cron, str)
        or len(cron.split()) != 5
        or not croniter.is_valid(cron)
    ):
        raise JobConfigError(job_id, f"invalid cron expression: {cron!r}")

    timezone = raw["timezone"]
    try:
        zoneinfo.ZoneInfo(timezone)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise JobConfigError(job_id, f"invalid timezone: {timezone!r}") from exc

    timeout_s = _validate_int(job_id, raw, "timeout_s", DEFAULT_TIMEOUT_S, minimum=1)
    retries = _validate_int(job_id, raw, "retries", DEFAULT_RETRIES, minimum=0)
    retry_delay_s = _validate_int(
        job_id, raw, "retry_delay_s", DEFAULT_RETRY_DELAY_S, minimum=1
    )

    return JobConfig(
        id=job_id,
        name=name,
        enabled=enabled,
        cron=cron,
        timezone=timezone,
        type=job_type,
        command=command,
        runner=runner,
        args=tuple(raw.get("args", ())),
        kwargs=dict(raw.get("kwargs", {})),
        timeout_s=timeout_s,
        retries=retries,
        retry_delay_s=retry_delay_s,
        schema_version=1,
    )


def _iter_job_files(directory: Path) -> list[Path]:
    """Return ``*.json`` files in ``directory``, ignoring dotfiles and subdirs."""
    return sorted(
        p
        for p in directory.glob("*.json")
        if p.is_file() and not p.name.startswith(".")
    )


def load_jobs(jobs_dir: Path) -> tuple[list[JobConfig], dict[str, str]]:
    """Parse every job file in ``jobs_dir``.

    Returns ``(valid_jobs_sorted_by_id, {id: error_message})``. The ``.current``
    subdir and dotfiles are ignored. A missing directory yields ``([], {})``.
    A single bad file never aborts the scan.
    """
    if not jobs_dir.is_dir():
        return [], {}

    valid: list[JobConfig] = []
    errors: dict[str, str] = {}
    for path in _iter_job_files(jobs_dir):
        try:
            valid.append(parse_job(path))
        except JobConfigError as exc:
            errors[exc.job_id] = exc.message

    valid.sort(key=lambda cfg: cfg.id)
    return valid, errors


# ---------------------------------------------------------------------------
# Promotion
# ---------------------------------------------------------------------------


def _try_parse(path: Path) -> JobConfig | None:
    """Parse a file, returning ``None`` if it is invalid or missing."""
    try:
        return parse_job(path)
    except JobConfigError as exc:
        logger.debug("ignoring unparseable job file %s: %s", path, exc.message)
        return None


def _atomic_write(src: Path, dest: Path) -> None:
    """Atomically copy ``src`` contents to ``dest`` via temp file + os.replace."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".{dest.name}.tmp"
    tmp.write_bytes(src.read_bytes())
    try:
        os.replace(tmp, dest)
    except OSError:
        # Clean up the temp file so a failed replace doesn't leave stragglers.
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def _promote_one(
    job_id: str, staging_dir: Path, current_dir: Path
) -> PromotionResult:
    staging_file = staging_dir / f"{job_id}.json"
    current_file = current_dir / f"{job_id}.json"
    staging_exists = staging_file.is_file()
    current_exists = current_file.is_file()

    if not staging_exists and current_exists:
        current_file.unlink()
        return PromotionResult(id=job_id, outcome="unloaded")

    try:
        staging_cfg = parse_job(staging_file)
    except JobConfigError as exc:
        if current_exists:
            return PromotionResult(id=job_id, outcome="outdated", error=exc.message)
        return PromotionResult(id=job_id, outcome="error", error=exc.message)

    if not current_exists:
        _atomic_write(staging_file, current_file)
        return PromotionResult(id=job_id, outcome="updated")

    current_cfg = _try_parse(current_file)
    if current_cfg is not None and current_cfg == staging_cfg:
        return PromotionResult(id=job_id, outcome="unchanged")

    _atomic_write(staging_file, current_file)
    return PromotionResult(id=job_id, outcome="updated")


def promote(staging_dir: Path, current_dir: Path) -> list[PromotionResult]:
    """Promote validated staging jobs into ``current_dir`` atomically.

    For every id across the union of staging and current ``*.json`` files:

    * staging valid & (no current OR differs) -> write to current, ``updated``
    * staging valid & identical to current     -> ``unchanged``
    * staging invalid & current exists         -> current untouched, ``outdated``
    * staging invalid & no current             -> ``error``
    * present in current but not staging       -> current removed, ``unloaded``

    Equality is decided by comparing parsed :class:`JobConfig` objects; an
    unparseable current file is treated as differing. Results are sorted by id.
    """
    current_dir.mkdir(parents=True, exist_ok=True)

    staging_ids = {p.stem for p in _iter_job_files(staging_dir)} if staging_dir.is_dir() else set()
    current_ids = {p.stem for p in _iter_job_files(current_dir)}
    all_ids = sorted(staging_ids | current_ids)

    return [_promote_one(job_id, staging_dir, current_dir) for job_id in all_ids]
