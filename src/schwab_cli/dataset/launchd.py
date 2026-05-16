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
# The accounts snapshot also anchors to NY 17:00 ET via sleep_until_ny;
# launchd just needs to fire EARLIER than that target across both DST
# modes. 04:30 local fires after the market-data job so today's positions
# already include the day's settled trades.
ACCOUNTS_CRON_LOCAL    = "30 4 * * *"
# Unified scheduler — single cron expression for the daily fan-out.
# Fires earlier than NY 17:00 ET (the market-close anchor) under both
# DST modes; sub-jobs sleep_until_ny internally to the right minute.
# Indices is delayed inside its child to NY 18:00 ET for request
# spacing, so launchd just needs to fire before 17:00.
SCHEDULER_CRON_LOCAL   = "0 4 * * *"

# Launcher filenames are what macOS shows in
# System Settings → Login Items, since the displayed name is read
# from ``ProgramArguments[0]``. Using the bare ``schwab_cli``
# binary makes all three plists look identical there.
_LAUNCHER_NAME = {
    "indices":     "Schwab Indices Dataset",
    "market-data": "Schwab Market Data",
    "accounts":    "Schwab Accounts NAV",
    "scheduler":   "Schwab Data Sync Service",
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
    if spec.kind == "scheduler":
        cmd = (
            f'exec {_shquote(spec.binary_path)} dataset sync "$@"'
        )
    elif spec.kind == "indices":
        cmd = (
            f'exec {_shquote(spec.binary_path)} dataset update --indices "$@"'
        )
    elif spec.kind == "accounts":
        # accounts cron job — snapshots today's account NAV for every
        # subscribed account. Runs the dedicated `dataset accounts
        # snapshot` subcommand which writes ``account_nav_daily`` rows
        # after sleep_until_ny anchors to NY 17:00 ET.
        cmd = (
            f'exec {_shquote(spec.binary_path)} '
            f'dataset accounts snapshot "$@"'
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
        if self.kind not in (
            "indices", "market-data", "accounts", "scheduler",
        ):
            raise ValueError(
                f"unsupported plist kind: {self.kind!r} (expected "
                f"'indices', 'market-data', 'accounts', or 'scheduler')"
            )

    @property
    def label(self) -> str:
        if self.kind == "indices":
            return INDICES_LABEL
        if self.kind == "accounts":
            return ACCOUNTS_LABEL
        if self.kind == "scheduler":
            return SCHEDULER_LABEL
        return MARKET_DATA_LABEL

    @property
    def program_args(self) -> list[str]:
        """Direct-binary form, kept for callers / tests that want it.

        The installed plist references the friendly-named launcher
        script instead — see :func:`build_dataset_plist`.
        """
        if self.kind == "indices":
            return [self.binary_path, "dataset", "update", "--indices"]
        if self.kind == "accounts":
            return [self.binary_path, "dataset", "accounts", "snapshot"]
        if self.kind == "scheduler":
            return [self.binary_path, "dataset", "sync"]
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
        "RunAtLoad":             spec.kind in (
            "market-data", "accounts", "scheduler",
        ),
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
    if kind == "indices":
        label = INDICES_LABEL
    elif kind == "accounts":
        label = ACCOUNTS_LABEL
    elif kind == "scheduler":
        label = SCHEDULER_LABEL
    else:
        label = MARKET_DATA_LABEL
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


def uninstall_per_job_plists() -> list[Path]:
    """Bootout + remove the three pre-scheduler plists (indices,
    market-data, accounts). Used by ``cron install --scheduler`` so
    the unified job replaces the per-job plists in one step.
    Returns the paths that were actually removed."""
    removed: list[Path] = []
    for kind in ("indices", "market-data", "accounts"):
        label = (
            INDICES_LABEL if kind == "indices"
            else ACCOUNTS_LABEL if kind == "accounts"
            else MARKET_DATA_LABEL
        )
        path = _default_dir() / f"{label}.plist"
        if path.exists():
            uninstall_plist(kind)
            removed.append(path)
    return removed


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
