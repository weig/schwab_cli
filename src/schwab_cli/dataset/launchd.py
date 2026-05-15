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


INDICES_LABEL           = "com.schwab-cli.dataset.indices"
MARKET_DATA_LABEL       = "com.schwab-cli.dataset.market-data"
# Kept for ``uninstall_legacy_volatility_job`` and back-compat refs
# during the migration window; new code should use MARKET_DATA_LABEL.
LEGACY_VOLATILITY_LABEL = "com.schwab-cli.dataset.volatility"
VOLATILITY_LABEL        = LEGACY_VOLATILITY_LABEL  # back-compat alias

# Hardcoded cron expressions — installer-owned, not user-configurable.
# The market-data job's actual run time is anchored to NY 17:00 ET by
# ``sleep_until_ny`` inside the Python entry point — launchd only
# needs to fire EARLIER than the NY target in either DST mode.
# UTC+8 04:00 ≤ both NY 17:00 EDT (= 05:00 UTC+8) and 17:00 EST (= 06:00 UTC+8).
INDICES_CRON_LOCAL     = "0 6 * * 0"   # Sunday 06:00 local — weekly indices sync
MARKET_DATA_CRON_LOCAL = "0 4 * * *"   # daily 04:00 local — sleeps until NY 17:00 ET

# Launcher filenames are what macOS shows in
# System Settings → Login Items, since the displayed name is read
# from ``ProgramArguments[0]``. Using the bare ``schwab_cli``
# binary makes all three plists look identical there.
_LAUNCHER_NAME = {
    "indices":     "Schwab Indices Dataset",
    "market-data": "Schwab Market Data",
}


def _default_dir() -> Path:
    """Return the LaunchAgents directory, evaluated at call time.

    This is a function rather than a module-level constant so that
    tests can monkeypatch HOME before the path is resolved.
    """
    return Path.home() / "Library" / "LaunchAgents"


def _launcher_dir() -> Path:
    """Where we drop the friendly-named launcher scripts."""
    return (
        Path.home() / "Library" / "Application Support"
        / "schwab_cli" / "launchers"
    )


def _launcher_path(kind: str) -> Path:
    return _launcher_dir() / _LAUNCHER_NAME[kind]


def _write_launcher(spec: DatasetPlistSpec) -> Path:
    """Write a tiny shell launcher whose filename is the friendly name.

    macOS uses ``ProgramArguments[0]``'s basename as the System
    Settings display label, so a wrapper named "Schwab Indices
    Dataset" is the simplest way to control that string for an
    unsigned CLI without pulling in a full .app bundle.
    """
    path = _launcher_path(spec.kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    if spec.kind == "indices":
        cmd = (
            f'exec {_shquote(spec.binary_path)} dataset update --indices "$@"'
        )
    else:
        # market-data daily job — invokes the existing --group volatility
        # CLI path; the daily run iterates whichever products (ohlcv +
        # volatility) the dataset.json declares.
        cmd = (
            f'exec {_shquote(spec.binary_path)} '
            f'dataset update --group volatility "$@"'
        )
    body = (
        "#!/bin/sh\n"
        f"# {_LAUNCHER_NAME[spec.kind]} (auto-generated by schwab_cli)\n"
        f"{cmd}\n"
    )
    path.write_text(body)
    path.chmod(0o755)
    return path


def _shquote(s: str) -> str:
    """POSIX-shell-quote a path for embedding in the launcher script."""
    if not s or any(c in s for c in " '\"\\$`"):
        return "'" + s.replace("'", "'\\''") + "'"
    return s


@dataclass
class DatasetPlistSpec:
    binary_path: str
    cron:        str
    kind:        str  # 'indices' or 'market-data'
    log_file:    str | None = None

    def __post_init__(self) -> None:
        # 'volatility' accepted as deprecated alias for 'market-data'
        # during the rename window so callers compiled against the
        # old constant don't crash. Coerce to the canonical value.
        if self.kind == "volatility":
            object.__setattr__(self, "kind", "market-data")
        if self.kind not in ("indices", "market-data"):
            raise ValueError(
                f"unsupported plist kind: {self.kind!r} "
                f"(expected 'indices' or 'market-data')"
            )

    @property
    def label(self) -> str:
        return INDICES_LABEL if self.kind == "indices" else MARKET_DATA_LABEL

    @property
    def program_args(self) -> list[str]:
        """Direct-binary form, kept for callers / tests that want it.

        The installed plist references the friendly-named launcher
        script instead — see :func:`build_dataset_plist`.
        """
        if self.kind == "indices":
            return [self.binary_path, "dataset", "update", "--indices"]
        return [self.binary_path, "dataset", "update", "--group", "volatility"]

    @property
    def plist_path(self) -> Path:
        return _default_dir() / f"{self.label}.plist"


def build_dataset_plist(
    spec: DatasetPlistSpec,
    *,
    launcher_path: Path | None = None,
) -> bytes:
    """Render plist bytes. If ``launcher_path`` is given, its basename
    becomes the System Settings → Login Items display name."""
    program_args = (
        [str(launcher_path)] if launcher_path is not None
        else spec.program_args
    )
    plist: dict[str, Any] = {
        "Label":                 spec.label,
        "ProgramArguments":      program_args,
        "StartCalendarInterval": crontab_to_calendar_interval(spec.cron),
        # market-data fires once a day at a TZ-fixed local time. If
        # the laptop was off when that time passed, RunAtLoad lets the
        # job pick up on next boot — sleep_until_ny's catch-up branch
        # then either runs immediately (already past 17:00 ET) or waits
        # until target. Indices is weekly; RunAtLoad there would cause
        # a spurious sync every reload, so it stays off.
        "RunAtLoad":             spec.kind == "market-data",
        "KeepAlive":             False,
    }
    if spec.log_file:
        plist["StandardOutPath"] = spec.log_file
        plist["StandardErrorPath"] = spec.log_file
    return plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=True)


def install_plist(spec: DatasetPlistSpec) -> Path:
    """Write the launcher + plist and ``launchctl load`` (G13.2).

    Idempotent across schedule changes: any previously-loaded job at
    the same label is unloaded first. Without that, ``launchctl load``
    silently fails with ``Load failed: 5: Input/output error`` while
    returning exit 0 — so the plist on disk reflects the new schedule
    but launchd is still running the old one. We proactively unload
    so the reload always sticks.
    """
    launcher = _write_launcher(spec)
    spec.plist_path.parent.mkdir(parents=True, exist_ok=True)
    spec.plist_path.write_bytes(
        build_dataset_plist(spec, launcher_path=launcher)
    )
    # Best-effort unload — silent when nothing's loaded ("Could not
    # find specified service" on stderr is expected and harmless).
    subprocess.run(
        ["launchctl", "unload", str(spec.plist_path)],
        check=False, capture_output=True,
    )
    # macOS launchctl returns 0 even when load fails, so we have to
    # sniff stderr for "Load failed" instead of trusting the exit code.
    result = subprocess.run(
        ["launchctl", "load", "-w", str(spec.plist_path)],
        capture_output=True, text=True,
    )
    err = (result.stderr or "").strip()
    if result.returncode != 0 or "load failed" in err.lower():
        raise RuntimeError(
            f"launchctl load failed for {spec.plist_path}: "
            f"{err or 'exit ' + str(result.returncode)}"
        )
    return spec.plist_path


def uninstall_plist(kind: str) -> Path:
    """``launchctl unload`` then remove the plist + launcher."""
    if kind == "volatility":  # deprecated alias for back-compat callers
        kind = "market-data"
    label = INDICES_LABEL if kind == "indices" else MARKET_DATA_LABEL
    path = _default_dir() / f"{label}.plist"
    if path.exists():
        subprocess.run(
            ["launchctl", "unload", str(path)],
            check=False,  # already-unloaded is fine
        )
        path.unlink()
    launcher = _launcher_path(kind)
    if launcher.exists():
        launcher.unlink()
    return path


def uninstall_legacy_volatility_job() -> Path | None:
    """If the legacy ``com.schwab-cli.dataset.volatility`` job is still
    installed (pre-rename build), bootout + remove its plist. No-op
    when the plist isn't present. Returns the plist path that was
    removed, or ``None`` when the no-op branch hit.
    """
    import os
    plist = _default_dir() / f"{LEGACY_VOLATILITY_LABEL}.plist"
    if not plist.exists():
        return None
    subprocess.run(
        ["launchctl", "bootout",
         f"gui/{os.getuid()}/{LEGACY_VOLATILITY_LABEL}"],
        check=False, capture_output=True,
    )
    # ``launchctl unload`` is the pre-bootout-era alternative; try it
    # too so the cleanup works on older macOS releases.
    subprocess.run(
        ["launchctl", "unload", str(plist)],
        check=False, capture_output=True,
    )
    plist.unlink(missing_ok=True)
    return plist
