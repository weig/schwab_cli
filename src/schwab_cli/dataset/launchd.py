"""Crontab string → launchd plist generator for the unified scheduler.

We only support the standard 5-field grammar with literal integers
or ``*``. No steps (``*/15``), no ranges (``9-17``), no name lists
(``MON,FRI``), no named shorthand (``@daily``). The error is
explicit so the user knows to rewrite their crontab into the simple
form rather than wonder why their job didn't fire.

Only one launchd job is installed today — the unified scheduler at
``com.schwab-cli.scheduler``. It fires once per day and pspawns the
market-data / accounts / indices children inline.
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
            f"named shorthand (@daily, @weekly, …) into launchd "
            f"StartCalendarInterval"
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


# Unified scheduler — single launchd job. Fires once per day and
# pspawns market-data + accounts + indices as parallel children, each
# anchoring to its own NY hour internally.
SCHEDULER_LABEL       = "com.schwab-cli.scheduler"

# Cron expression. Fires before NY 17:00 ET under both DST modes;
# sub-jobs sleep_until_ny internally to the right minute.
SCHEDULER_CRON_LOCAL  = "0 4 * * *"


# Plist basenames the sweep is allowed to remove. Narrow on purpose:
# the server daemon installs itself as ``com.schwab-cli.server.plist``
# and must not be touched. Only the scheduler plist is ours to sweep.
_PLIST_PREFIXES = (
    "com.schwab-cli.scheduler",
)


@dataclass(frozen=True)
class _KindInfo:
    """Single source of truth for per-kind metadata.

    ``cli_args`` is a tuple (not list) so the frozen-dataclass
    semantics extend to its contents.
    """
    label:           str
    cli_args:        tuple[str, ...]   # appended after the binary path
    # The basename macOS shows in System Settings → Login Items
    # (read from ``ProgramArguments[0]``).
    launcher_name:   str
    run_at_load:     bool


_KIND_INFO: dict[str, _KindInfo] = {
    "scheduler": _KindInfo(
        label=SCHEDULER_LABEL,
        cli_args=("dataset", "sync"),
        launcher_name="Schwab Data Sync Service",
        run_at_load=True,  # ensures missed runs catch up on next boot
    ),
}

# Cross-check: every label in :data:`_KIND_INFO` must be a plist
# basename our sweep recognises, otherwise the sweep would miss it.
for _info in _KIND_INFO.values():
    assert any(_info.label.startswith(p) for p in _PLIST_PREFIXES), (
        f"_KIND_INFO label {_info.label!r} doesn't match _PLIST_PREFIXES"
    )


def _resolve_kind(kind: str) -> _KindInfo:
    """Look up a ``_KindInfo`` row."""
    try:
        return _KIND_INFO[kind]
    except KeyError:
        raise ValueError(
            f"unsupported plist kind: {kind!r} "
            f"(expected one of: {', '.join(sorted(_KIND_INFO))})"
        ) from None


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
    return _launcher_dir() / _resolve_kind(kind).launcher_name


def _write_launcher(spec: DatasetPlistSpec) -> Path:
    """Write a tiny shell launcher whose filename is the friendly name.

    macOS uses ``ProgramArguments[0]``'s basename as the System
    Settings display label, so a wrapper named "Schwab Data Sync
    Service" is the simplest way to control that string for an
    unsigned CLI without pulling in a full .app bundle. The command
    body is built from :data:`_KIND_INFO` — single source of truth
    shared with ``DatasetPlistSpec.program_args``.
    """
    info = _resolve_kind(spec.kind)
    path = _launcher_dir() / info.launcher_name
    path.parent.mkdir(parents=True, exist_ok=True)
    # Launchd sets a minimal PATH (``/usr/bin:/bin:/usr/sbin:/sbin``)
    # that doesn't include uv-tool's ``~/.local/bin``. The scheduler
    # parent runs fine via the absolute ``binary_path`` below, but the
    # CHILD subprocesses it pspawns re-resolve the binary via
    # ``shutil.which("schwab")`` — that lookup fails under the minimal
    # PATH and crashes the whole sync. Prepend the binary's directory
    # so the lookup succeeds in every descendant.
    bin_dir = str(Path(spec.binary_path).parent)
    cmd = (
        f"exec {_shquote(spec.binary_path)} "
        f"{' '.join(info.cli_args)} \"$@\""
    )
    body = (
        "#!/bin/sh\n"
        f"# {info.launcher_name} (auto-generated by schwab_cli)\n"
        f"PATH={_shquote(bin_dir)}:$PATH\n"
        f"export PATH\n"
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
    kind:        str  # currently only 'scheduler'
    log_file:    str | None = None

    def __post_init__(self) -> None:
        # Validate up front — a bogus kind on construction beats a
        # KeyError two layers deep at install time.
        _resolve_kind(self.kind)

    @property
    def label(self) -> str:
        return _resolve_kind(self.kind).label

    @property
    def program_args(self) -> list[str]:
        """Direct-binary form, kept for callers / tests that want it.

        The installed plist references the friendly-named launcher
        script instead — see :func:`build_dataset_plist`.
        """
        return [self.binary_path, *_resolve_kind(self.kind).cli_args]

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
        # RunAtLoad lets a daily job catch up after a missed fire
        # (laptop closed at the scheduled minute) on next boot — the
        # entry point's sleep_until_ny either runs immediately if
        # already past target or waits until target.
        "RunAtLoad":             _resolve_kind(spec.kind).run_at_load,
        "KeepAlive":             False,
    }
    if spec.log_file:
        plist["StandardOutPath"] = spec.log_file
        plist["StandardErrorPath"] = spec.log_file
    return plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=True)


def install_plist(spec: DatasetPlistSpec) -> Path:
    """Write the launcher + plist and ``launchctl load``.

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


def uninstall_all_schwab_plists() -> list[Path]:
    """Unload + remove every Schwab-CLI scheduler plist in
    ``LaunchAgents``, plus the launcher scripts we own.

    Called by both ``cron install`` (clean slate before re-installing
    the scheduler) and ``cron uninstall`` (full teardown). Sweeping by
    filename prefix means we don't need a per-kind enumeration.

    Launcher cleanup is gated by the known ``launcher_name`` values
    in :data:`_KIND_INFO` — anything else in the launcher directory
    is left alone (it isn't ours to delete).

    Raises :class:`RuntimeError` if ``launchctl`` reports a real
    failure unloading any plist — the on-disk file is left in place
    so the caller can investigate without ending up with a registered
    launchd job and no plist to manage it.
    """
    removed: list[Path] = []
    plist_dir = _default_dir()
    if plist_dir.exists():
        for path in sorted(plist_dir.glob("*.plist")):
            if not any(path.name.startswith(p) for p in _PLIST_PREFIXES):
                continue
            _unload_or_raise(path)
            path.unlink(missing_ok=True)
            removed.append(path)

    # Launcher scripts: only remove files whose basenames we
    # explicitly own. Avoids accidentally nuking unrelated files a
    # user dropped in the Application Support directory.
    launcher_dir = _launcher_dir()
    known_names = {info.launcher_name for info in _KIND_INFO.values()}
    if launcher_dir.exists():
        for name in known_names:
            launcher = launcher_dir / name
            if not launcher.exists():
                continue
            try:
                launcher.unlink()
            except FileNotFoundError:
                # Race with concurrent removal — fine.
                pass
            # Note: any other OSError (permission denied, busy)
            # propagates intentionally. A real launcher failure is
            # operationally significant and should not be silent.
    return removed


# Strings macOS launchctl uses to indicate "no such loaded service"
# across the versions we've observed. Both spellings appear in the
# wild; anything else in stderr is treated as a real failure.
_LAUNCHCTL_NOT_LOADED_HINTS = (
    "could not find specified service",
    "no such file or directory",
)


def _unload_or_raise(plist_path: Path) -> None:
    """``launchctl unload`` one plist. Tolerates "service not loaded"
    (already inactive); raises on any other failure.

    macOS ``launchctl`` is inconsistent across versions: some exit 0
    for unload-when-not-loaded, others exit non-zero with one of the
    hint strings. We require BOTH conditions (zero exit AND empty or
    hint-matching stderr) to treat the call as success — the prior
    "exit 0 alone is fine" rule masked SIP / sandbox failures that
    exit 0 with a real diagnostic in stderr.
    """
    result = subprocess.run(
        ["launchctl", "unload", str(plist_path)],
        check=False, capture_output=True, text=True,
    )
    err = (result.stderr or "").strip().lower()
    if result.returncode == 0 and (
        not err or any(h in err for h in _LAUNCHCTL_NOT_LOADED_HINTS)
    ):
        return
    if any(h in err for h in _LAUNCHCTL_NOT_LOADED_HINTS):
        return
    raise RuntimeError(
        f"launchctl unload failed for {plist_path}: "
        f"{err or 'exit ' + str(result.returncode)}"
    )
