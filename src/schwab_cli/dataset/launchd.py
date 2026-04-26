"""Crontab string → launchd plist generators.

We only support the standard 5-field grammar with literal integers
or ``*``. No steps (``*/15``), no ranges (``9-17``), no name lists
(``MON,FRI``), no named shorthand (``@daily``). The error is
explicit so the user knows to rewrite their crontab into the simple
form rather than wonder why their job didn't fire.
"""
from __future__ import annotations

import plistlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_FIELD_RANGES = [
    ("minute",   0, 59),
    ("hour",     0, 23),
    ("day",      1, 31),
    ("month",    1, 12),
    ("weekday",  0, 6),
]

_FIELD_TO_LAUNCHD_KEY = {
    "minute":  "Minute",
    "hour":    "Hour",
    "day":     "Day",
    "month":   "Month",
    "weekday": "Weekday",
}


def crontab_to_calendar_interval(expr: str) -> list[dict[str, int]]:
    """Translate a 5-field crontab to launchd StartCalendarInterval.

    Returns a list of dicts (one entry — launchd accepts arrays for
    multi-time triggers, but we only emit one). Literal ``*`` becomes
    "match every value", which in launchd is achieved by *omitting*
    the key. So ``"0 22 * * *"`` → ``[{"Hour": 22, "Minute": 0}]``.
    """
    stripped = expr.strip()
    if stripped.startswith("@"):
        raise ValueError(
            f"crontab expression {stripped!r}: cannot translate "
            f"named shorthand (@daily, @weekly, …) into launchd StartCalendarInterval"
        )
    fields = stripped.split()
    if len(fields) != 5:
        raise ValueError(
            f"crontab expression must have 5 fields, got {len(fields)}: "
            f"{expr!r}"
        )
    out: dict[str, int] = {}
    for value, (name, lo, hi) in zip(fields, _FIELD_RANGES):
        if value == "*":
            continue
        if any(c in value for c in "/-,"):
            raise ValueError(
                f"crontab field {name}={value!r}: cannot translate "
                f"steps/ranges/lists into launchd StartCalendarInterval"
            )
        try:
            n = int(value)
        except ValueError:
            raise ValueError(
                f"crontab field {name}={value!r}: cannot translate "
                f"named shorthand into launchd"
            )
        if n < lo or n > hi:
            raise ValueError(
                f"crontab field {name}={n} out of range [{lo}, {hi}]"
            )
        out[_FIELD_TO_LAUNCHD_KEY[name]] = n
    return [out]


INDICES_LABEL    = "com.schwab-cli.dataset.indices"
VOLATILITY_LABEL = "com.schwab-cli.dataset.volatility"

def _default_dir() -> Path:
    """Return the LaunchAgents directory, evaluated at call time.

    This is a function rather than a module-level constant so that
    tests can monkeypatch HOME before the path is resolved.
    """
    return Path.home() / "Library" / "LaunchAgents"


@dataclass
class DatasetPlistSpec:
    binary_path: str
    cron:        str
    kind:        str  # 'indices' or 'volatility'
    log_file:    str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("indices", "volatility"):
            raise ValueError(
                f"unsupported plist kind: {self.kind!r} "
                f"(expected 'indices' or 'volatility')"
            )

    @property
    def label(self) -> str:
        return INDICES_LABEL if self.kind == "indices" else VOLATILITY_LABEL

    @property
    def program_args(self) -> list[str]:
        if self.kind == "indices":
            return [self.binary_path, "dataset", "update", "--indices"]
        return [self.binary_path, "dataset", "update", "--group", "volatility"]

    @property
    def plist_path(self) -> Path:
        return _default_dir() / f"{self.label}.plist"


def build_dataset_plist(spec: DatasetPlistSpec) -> bytes:
    plist: dict[str, Any] = {
        "Label":                 spec.label,
        "ProgramArguments":      spec.program_args,
        "StartCalendarInterval": crontab_to_calendar_interval(spec.cron),
        "RunAtLoad":             False,
        "KeepAlive":             False,
    }
    if spec.log_file:
        plist["StandardOutPath"] = spec.log_file
        plist["StandardErrorPath"] = spec.log_file
    return plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=True)


def install_plist(spec: DatasetPlistSpec) -> Path:
    """Write the plist and ``launchctl load`` it (G13.2 — one verb)."""
    spec.plist_path.parent.mkdir(parents=True, exist_ok=True)
    spec.plist_path.write_bytes(build_dataset_plist(spec))
    subprocess.run(
        ["launchctl", "load", "-w", str(spec.plist_path)],
        check=True,
    )
    return spec.plist_path


def uninstall_plist(kind: str) -> Path:
    """``launchctl unload`` then remove the plist file."""
    label = INDICES_LABEL if kind == "indices" else VOLATILITY_LABEL
    path = _default_dir() / f"{label}.plist"
    if path.exists():
        subprocess.run(
            ["launchctl", "unload", str(path)],
            check=False,  # already-unloaded is fine
        )
        path.unlink()
    return path
