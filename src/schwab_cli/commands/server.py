"""`server` command — long-lived auth-maintenance daemon + launchd install.

Bare ``schwab server`` runs the maintenance loop (keeps the OAuth refresh
token alive). Subcommands manage the macOS launchd LaunchAgent:

* ``server install`` — write the plist + ``launchctl load``.
* ``server uninstall`` — ``launchctl unload`` + remove the plist.
* ``server status`` — report whether the job is loaded.
"""

from __future__ import annotations

import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

import typer

from schwab_cli import config as config_module
from schwab_cli.server import maintenance
from schwab_cli.server.launchd import (
    DEFAULT_PLIST_PATH,
    LABEL,
    ServerPlistSpec,
    write_plist,
)
from schwab_cli.server.maintenance import DEFAULT_INTERVAL_S


# ---- bare `server` runner --------------------------------------------


def run(*, interval_s: int = DEFAULT_INTERVAL_S) -> int | None:
    """Entry point for the bare ``schwab server`` call.

    Loads config, installs SIGTERM/SIGINT handlers that flip a stop flag,
    then drives :func:`maintenance.run_loop` until stopped. Returns 0 on
    graceful exit.
    """
    cfg = config_module.load()
    if cfg is None:
        typer.secho(
            "No config found. Run `schwab_cli setup` first.",
            fg=typer.colors.RED, err=True,
        )
        raise SystemExit(1)

    # A threading.Event is the stop signal AND the interruptible sleep.
    # `Event.wait(interval)` returns immediately when the event is set,
    # so a SIGTERM during the multi-hour idle wakes the loop at once —
    # critical for graceful `launchctl unload` (plain `time.sleep` is
    # NOT interruptible: per PEP 475 it resumes after the handler runs,
    # which would make launchd SIGKILL us after its ~20s ExitTimeOut).
    stop_event = threading.Event()

    def _handle_signal(signum, _frame) -> None:
        stop_event.set()
        typer.secho(
            f"server: received signal {signum}, stopping gracefully...",
            fg=typer.colors.YELLOW, err=True,
        )

    _install_signal_handlers(_handle_signal)

    notifier = _build_notifier()

    typer.secho(
        f"server: starting auth-maintenance loop "
        f"(interval {interval_s}s)",
        fg=typer.colors.CYAN, err=True,
    )
    maintenance.run_loop(
        cfg,
        stop=stop_event.is_set,
        interval_s=interval_s,
        # Event.wait(timeout) is the interruptible sleep — returns early
        # the instant a signal handler sets the event.
        sleep=stop_event.wait,
        now=lambda: int(time.time()),
        notifier=notifier,
    )
    typer.secho("server: stopped.", fg=typer.colors.CYAN, err=True)
    return 0


def _install_signal_handlers(handler) -> None:
    """Best-effort SIGTERM/SIGINT install (no-op off the main thread)."""
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            # Not on the main thread (e.g. under a test runner) — skip.
            pass


def _build_notifier():
    """Build a notifier that forwards ticks to the Notifier infra.

    Returns ``None`` if the notification stack can't be constructed, so
    the loop degrades to silent maintenance rather than crashing.
    """
    try:
        from schwab_cli.notify import Notifier

        notifier = Notifier.from_file()
    except Exception:  # noqa: BLE001 — notifications are optional
        return None

    _event_for = {
        "renewed": "scheduler.proactive_auth_succeeded",
        "renew_failed": "scheduler.proactive_auth_failed",
        "token_ensured": "scheduler.proactive_auth_skipped",
        "token_failed": "scheduler.proactive_auth_failed",
    }

    def _forward(tick) -> None:
        event = _event_for.get(tick.action)
        if event is None:
            return
        try:
            notifier.emit(event, detail=tick.detail)
        except Exception:  # noqa: BLE001 — never break a tick
            pass

    return _forward


# ---- server install --------------------------------------------------


def run_install(
    *,
    plist_path: str | None = None,
    log_file: str | None = None,
    yes: bool = False,
) -> None:
    """Write the LaunchAgent plist and ``launchctl load`` it."""
    binary = shutil.which("schwab") or shutil.which("schwab_cli")
    if not binary:
        typer.secho(
            "schwab not found on PATH; install it with "
            "`uv tool install --from . schwab_cli` first.",
            fg=typer.colors.RED, err=True,
        )
        raise SystemExit(1)

    target = (
        Path(plist_path).expanduser() if plist_path else DEFAULT_PLIST_PATH
    )

    typer.echo("Proposed LaunchAgent:")
    typer.echo(f"  Label:     {LABEL}")
    typer.echo(f"  Binary:    {binary}")
    typer.echo(f"  Plist:     {target}")
    if log_file:
        typer.echo(f"  Log:       {log_file}")
    typer.echo("  KeepAlive: true  (exits → auto-restart)")
    if not yes:
        if not typer.confirm("Install and load now?", default=True):
            typer.echo("aborted")
            raise typer.Exit(code=0)

    write_plist(ServerPlistSpec(binary_path=binary, log_file=log_file), target)
    result = subprocess.run(
        ["launchctl", "load", "-w", str(target)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        typer.secho(
            f"launchctl load failed: {err}",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=1)
    typer.echo(f"installed + loaded: {target}")
    typer.echo(
        f"Label: {LABEL} — running now and restarting on exit. "
        "Use `schwab server status` to verify, "
        "`schwab server uninstall` to remove."
    )


# ---- server uninstall ------------------------------------------------


def run_uninstall(
    *,
    plist_path: str | None = None,
    yes: bool = False,
) -> None:
    """``launchctl unload`` then remove the plist file."""
    target = (
        Path(plist_path).expanduser() if plist_path else DEFAULT_PLIST_PATH
    )
    if not target.exists():
        typer.echo(f"{target} not found — nothing to uninstall.")
        return
    if not yes:
        if not typer.confirm(
            f"Unload and remove {target}?", default=True,
        ):
            typer.echo("aborted")
            raise typer.Exit(code=0)
    subprocess.run(
        ["launchctl", "unload", "-w", str(target)],
        capture_output=True, text=True,
    )
    target.unlink(missing_ok=True)
    typer.echo(f"removed: {target}")


# ---- server status ---------------------------------------------------


def run_status() -> None:
    """Report whether the launchd job is loaded via ``launchctl list``."""
    result = subprocess.run(
        ["launchctl", "list"],
        capture_output=True, text=True,
    )
    stdout = result.stdout or ""
    loaded = result.returncode == 0 and LABEL in stdout
    if loaded:
        typer.secho(
            f"{LABEL}: loaded (launchd is managing the server)",
            fg=typer.colors.GREEN,
        )
    else:
        typer.secho(
            f"{LABEL}: not loaded. "
            "Run `schwab server install` to register it.",
            fg=typer.colors.YELLOW,
        )
