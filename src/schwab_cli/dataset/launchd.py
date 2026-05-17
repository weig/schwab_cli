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
from zoneinfo import ZoneInfo


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
ACCOUNTS_LABEL          = "com.schwab-cli.dataset.accounts"
# Unified scheduler — replaces the three labels above. Fires once per
# day and pspawns market-data + accounts + indices as parallel
# children, each anchoring to its own NY hour internally.
SCHEDULER_LABEL         = "com.schwab-cli.scheduler"
# Pre-rename volatility plist label. Still referenced by tests and
# kept as a back-compat alias for ``VOLATILITY_LABEL``; the sweep
# uninstall picks up its plist via the ``_PLIST_PREFIXES`` glob, so
# no per-label cleanup code is needed.
LEGACY_VOLATILITY_LABEL = "com.schwab-cli.dataset.volatility"
VOLATILITY_LABEL        = LEGACY_VOLATILITY_LABEL  # back-compat alias

# Unified scheduler cron expression. Fires earlier than NY 17:00 ET
# (the market-close anchor) under both DST modes; sub-jobs
# sleep_until_ny internally to the right minute. Indices is delayed
# inside its child to NY 18:00 ET for request spacing, so launchd
# only needs to fire before 17:00.
SCHEDULER_CRON_LOCAL   = "0 4 * * *"


# Plist basenames the sweep is allowed to remove. Narrower than
# ``com.schwab-cli.`` on purpose — the MCP server installs itself as
# ``com.schwab-cli.mcp.plist`` and is NOT a dataset cron job, so a
# too-broad sweep would silently uninstall it.
#
# Everything we install for the dataset/scheduler subsystem lives
# under ``com.schwab-cli.dataset.<kind>.plist`` (legacy per-job) or
# ``com.schwab-cli.scheduler.plist`` (unified). Both hyphen and
# underscore variants are listed so legacy pre-rename installs are
# still picked up.
_PLIST_PREFIXES = (
    "com.schwab-cli.dataset.",
    "com.schwab_cli.dataset.",
    "com.schwab-cli.scheduler",
    "com.schwab_cli.scheduler",
)


@dataclass(frozen=True)
class _KindInfo:
    """Single source of truth for per-kind metadata. Adding a new
    plist kind = adding one entry to :data:`_KIND_INFO` below.

    ``cli_args`` is a tuple (not list) so the frozen-dataclass
    semantics extend to its contents — ``_KIND_INFO[k].cli_args.append``
    can't mutate global state.
    """
    label:           str
    cli_args:        tuple[str, ...]   # appended after the binary path
    # The basename macOS shows in System Settings → Login Items
    # (read from ``ProgramArguments[0]``).
    launcher_name:   str
    run_at_load:     bool


# ``scheduler`` is the only kind ``cron install`` actually writes.
# The legacy entries (``indices`` / ``market-data`` / ``accounts``)
# exist so :func:`_resolve_kind` can still name pre-scheduler plists
# for inspection and so tests can construct a :class:`DatasetPlistSpec`
# with a legacy kind without raising.
_KIND_INFO: dict[str, _KindInfo] = {
    "scheduler": _KindInfo(
        label=SCHEDULER_LABEL,
        cli_args=("dataset", "sync"),
        launcher_name="Schwab Data Sync Service",
        run_at_load=True,  # ensures missed runs catch up on next boot
    ),
    "indices": _KindInfo(
        label=INDICES_LABEL,
        cli_args=("dataset", "update", "--indices"),
        launcher_name="Schwab Indices Dataset",
        run_at_load=False,
    ),
    "market-data": _KindInfo(
        label=MARKET_DATA_LABEL,
        cli_args=("dataset", "update", "--group", "volatility"),
        launcher_name="Schwab Market Data",
        run_at_load=True,
    ),
    "accounts": _KindInfo(
        label=ACCOUNTS_LABEL,
        cli_args=("dataset", "accounts", "snapshot"),
        launcher_name="Schwab Accounts NAV",
        run_at_load=True,
    ),
}

# Cross-check: every label in :data:`_KIND_INFO` must be a plist
# basename our sweep recognises, otherwise a future kind added with a
# typo'd label would silently escape ``uninstall_all_schwab_plists``.
# Evaluated at import — keeps :data:`_KIND_INFO` self-validating.
for _info in _KIND_INFO.values():
    assert any(_info.label.startswith(p) for p in _PLIST_PREFIXES), (
        f"_KIND_INFO label {_info.label!r} doesn't match _PLIST_PREFIXES; "
        "uninstall_all_schwab_plists won't pick it up"
    )


# Legacy alias mapping — historical kind names that should resolve to
# their canonical entry without polluting :data:`_KIND_INFO`.
_KIND_ALIASES: dict[str, str] = {
    "volatility": "market-data",
}


def _resolve_kind(kind: str) -> _KindInfo:
    """Look up a ``_KindInfo`` row, normalising legacy aliases."""
    canonical = _KIND_ALIASES.get(kind, kind)
    try:
        return _KIND_INFO[canonical]
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
    from pathlib import Path as _P
    bin_dir = str(_P(spec.binary_path).parent)
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
    kind:        str  # 'indices' or 'market-data'
    log_file:    str | None = None

    def __post_init__(self) -> None:
        # Normalise legacy aliases (e.g. 'volatility' → 'market-data')
        # so the rest of the code only sees canonical kinds.
        canonical = _KIND_ALIASES.get(self.kind, self.kind)
        if canonical != self.kind:
            object.__setattr__(self, "kind", canonical)
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
        # already past target or waits until target. Per-kind in
        # :data:`_KIND_INFO`.
        "RunAtLoad":             _resolve_kind(spec.kind).run_at_load,
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


# (moved to module top — see :data:`_PLIST_PREFIXES`)


def uninstall_all_schwab_plists() -> list[Path]:
    """Unload + remove every Schwab-CLI plist in ``LaunchAgents``,
    plus the launcher scripts we own.

    Called by both ``cron install`` (clean slate before re-installing
    the scheduler) and ``cron uninstall`` (full teardown). Sweeping by
    filename prefix means we don't need a per-kind enumeration and
    legacy installs from earlier builds (e.g.
    ``com.schwab-cli.dataset.volatility``) are picked up
    automatically.

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


# ---- Phase 4: auto-fix plist on fire-time drift ------------------------
#
# When the system's timezone changes after install, the launchd plist's
# fixed `Hour=H_local` ends up firing at a different NY-clock moment.
# `sleep_until_ny` is robust to "fire early, wait longer"; it can't
# recover from "fire AFTER target" (it just no-ops). The cron detects
# that and emits a Telegram alert (`fire_time_drift`). Phase 4 also
# auto-fixes the plist:
#
#   * `_compute_safe_local_hour(system_tz)` — pick a local Hour that
#     fires at NY <= 16:00 under either DST mode (we anchor on EST,
#     UTC-5; EDT is automatically safe since it's an hour closer to
#     UTC, so the fire moves earlier).
#   * `reinstall_market_data_job(local_hour)` — rewrite the plist
#     in-place and re-bootstrap via launchctl. Idempotent when the
#     existing plist already has the right Hour.
from datetime import datetime, timezone


def _compute_safe_local_hour(*, system_tz: ZoneInfo) -> int:
    """Pick a local Hour that, given the system's TZ, fires at NY-clock
    ≤ 16:00 in either DST mode.

    Strategy: anchor on **December 15** (NY is in EST, most northern
    systems are also in standard time). 16:00 NY EST = 21:00 UTC →
    convert to system TZ → take Hour. The launchd plist's fixed Hour
    is then DST-invariant on the NY side: when NY flips EST→EDT, the
    same Hour fires an hour EARLIER in NY clock (still ≤ 17 ET, so
    sleep_until_ny just waits longer).

    Systems with their own DST flip (e.g. NY itself, EU) are still
    safe within North-America-style synchronized DST; opposite-
    hemisphere DST (e.g. Sydney) can land in a small drift window
    twice a year — those users may need a one-off reinstall.
    """
    # Year is irrelevant for the offset extraction; pick a recent one.
    anchor_utc = datetime(2026, 12, 15, 21, 0, tzinfo=timezone.utc)
    target_local = anchor_utc.astimezone(system_tz)
    return target_local.hour


def _market_data_plist_path() -> Path:
    return _default_dir() / f"{MARKET_DATA_LABEL}.plist"


def reinstall_market_data_job(*, local_hour: int) -> None:
    """Rewrite the market-data plist with ``Hour=local_hour`` and
    re-load it via launchctl bootout + bootstrap. Idempotent — if the
    plist already has the same Hour, no-op.

    Used by the cron's drift-detection branch to self-heal after a
    system TZ change. The legacy `install_plist` is the path the
    operator uses for a fresh install; this one is the "rewrite the
    fixed Hour and reload" specialization.
    """
    import os
    import plistlib as _plistlib

    plist_path = _market_data_plist_path()
    if plist_path.exists():
        try:
            existing = _plistlib.loads(plist_path.read_bytes())
            intervals = existing.get("StartCalendarInterval")
            if isinstance(intervals, dict):
                intervals = [intervals]
            if (intervals
                    and isinstance(intervals, list)
                    and intervals[0].get("Hour") == local_hour):
                return  # already correct — nothing to do
        except Exception:
            # Corrupt plist? Fall through and overwrite.
            pass

    # Preserve everything else about the plist; only flip the Hour.
    plist_dict: dict
    if plist_path.exists():
        try:
            plist_dict = _plistlib.loads(plist_path.read_bytes())
        except Exception:
            plist_dict = {}
    else:
        plist_dict = {}
    plist_dict.setdefault("Label", MARKET_DATA_LABEL)
    plist_dict["StartCalendarInterval"] = [
        {"Hour": local_hour, "Minute": 0},
    ]
    # Keep RunAtLoad on — it's the Phase 3 catch-up safety net.
    plist_dict.setdefault("RunAtLoad", True)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_bytes(_plistlib.dumps(plist_dict))

    # bootout + bootstrap are the modern equivalents of unload/load.
    subprocess.run(
        ["launchctl", "bootout",
         f"gui/{os.getuid()}/{MARKET_DATA_LABEL}"],
        check=False, capture_output=True,
    )
    subprocess.run(
        ["launchctl", "bootstrap",
         f"gui/{os.getuid()}", str(plist_path)],
        check=False, capture_output=True,
    )
