"""`server` command — long-lived auth-maintenance daemon + launchd install.

Bare ``schwab server`` runs the maintenance loop (keeps the OAuth refresh
token alive). Subcommands manage the macOS launchd LaunchAgent:

* ``server install`` — write the plist + ``launchctl load``.
* ``server uninstall`` — ``launchctl unload`` + remove the plist.
* ``server status`` — report whether the job is loaded.
"""

from __future__ import annotations

import asyncio
import os
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


def run(
    *,
    interval_s: int = DEFAULT_INTERVAL_S,
    enable_mcp: bool = False,
    mcp_host: str = "127.0.0.1",
    mcp_port: int = 7234,
    enable_rest: bool = False,
    rest_host: str = "127.0.0.1",
    rest_port: int = 8000,
    log_file: str | None = None,
    no_log_file: bool = False,
    no_auto_login: bool = False,
) -> int | None:
    """Entry point for the bare ``schwab server`` call.

    Without ``--enable-mcp`` (the Phase 2 default) this loads config,
    installs SIGTERM/SIGINT handlers that flip a stop flag, then drives
    :func:`maintenance.run_loop` until stopped. Returns 0 on graceful
    exit.

    With ``enable_mcp=True`` it ALSO composes the Streamable HTTP MCP
    server on top of the maintenance loop: the maintenance loop runs in
    a daemon thread as the single proactive refresh-token renewer, and
    the MCP server runs on the main thread with
    ``auth_monitor_enabled=False`` so there is no competing rotation.
    ``enable_rest=True`` additionally mounts the REST PoC routes onto
    that same MCP Starlette app (one shared port).

    With ``enable_rest=True`` but WITHOUT ``--enable-mcp`` it serves the
    standalone REST PoC app via uvicorn on ``rest_host:rest_port`` with
    the maintenance loop underneath in a daemon thread.
    """
    if enable_mcp:
        return _run_with_mcp(
            interval_s=interval_s,
            mcp_host=mcp_host,
            mcp_port=mcp_port,
            enable_rest=enable_rest,
            log_file=log_file,
            no_log_file=no_log_file,
            no_auto_login=no_auto_login,
        )

    if enable_rest:
        return _run_with_rest(
            interval_s=interval_s,
            rest_host=rest_host,
            rest_port=rest_port,
            log_file=log_file,
            no_log_file=no_log_file,
            no_auto_login=no_auto_login,
        )

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


def _run_with_mcp(
    *,
    interval_s: int,
    mcp_host: str,
    mcp_port: int,
    enable_rest: bool = False,
    log_file: str | None,
    no_log_file: bool,
    no_auto_login: bool,
) -> int | None:
    """``schwab server --enable-mcp`` — maintenance loop + MCP HTTP server.

    Mirrors :func:`schwab_cli.commands.mcp.run`'s startup (cfg + session
    presence, logbook + notifier, refresh-expiry startup auto-login),
    then runs the maintenance loop in a daemon thread (the single
    refresh-token renewer) and the Streamable HTTP MCP server on the
    main thread with ``auth_monitor_enabled=False``. uvicorn owns
    SIGINT/SIGTERM, so we install NO signal handlers here.
    """
    from schwab_cli.api.client import SchwabClient
    from schwab_cli.commands.mcp import (
        _attempt_startup_autologin,
        _resolve_log_file,
    )
    from schwab_cli.mcp_server.app import SchwabMcpServer
    from schwab_cli.mcp_server.logbook import LogBook
    from schwab_cli.session import load as load_session

    cfg = config_module.load()
    if cfg is None:
        typer.secho(
            "No config found. Run `schwab_cli setup` first.",
            fg=typer.colors.RED, err=True,
        )
        raise SystemExit(1)
    session = load_session()
    if session is None:
        typer.secho(
            "No session found. Run `schwab_cli auth` first.",
            fg=typer.colors.RED, err=True,
        )
        raise SystemExit(1)

    # Logbook + notifier built before the refresh-expiry check so the
    # startup auto-login path has both available.
    resolved_log_file = _resolve_log_file(log_file, no_log_file)
    logbook = LogBook(log_file=resolved_log_file)
    from schwab_cli.notify import Notifier
    notifier = Notifier.from_file(logbook=logbook)

    now = int(time.time())
    if session.refresh_token_expires_at <= now:
        if no_auto_login:
            typer.secho(
                "Refresh token expired and --no-auto-login is set. "
                "Run `schwab_cli auth --force` to re-authenticate, "
                "then restart the server.",
                fg=typer.colors.RED, err=True,
            )
            raise SystemExit(1)
        logbook.warning(
            "daemon.startup_refresh_expired",
            attempting_autologin=True,
        )
        fresh = _attempt_startup_autologin(logbook, notifier)
        if fresh is None:
            typer.secho(
                "Startup auto-login failed. Check the log for details, "
                "then run `schwab_cli auth --force` manually before "
                "restarting.",
                fg=typer.colors.RED, err=True,
            )
            raise SystemExit(1)
        session = fresh
        logbook.info("daemon.startup_refresh_recovered")

    # The MCP server runs with auth_monitor_enabled=False: the
    # maintenance loop is the single proactive renewer, so the MCP
    # server must not run a competing rotation.
    client = SchwabClient(cfg, session)
    server = SchwabMcpServer(
        client, logbook,
        notifier=notifier,
        auth_monitor_enabled=False,
    )

    # Maintenance notifier: forwards tick events through the SAME notifier
    # the MCP server uses (no second notification.json load) AND hands the
    # freshly-renewed session to the persistent client's in-memory state.
    # Without this handoff, after the loop rotates the refresh token the
    # client would keep its boot-time refresh token in memory and fail its
    # next 401 refresh against a token Schwab already invalidated — even
    # though session.json on disk is valid. Mirrors the auth_monitor's
    # `_on_rotation_success` handoff (which is disabled here).
    _event_for = {
        "renewed": "scheduler.proactive_auth_succeeded",
        "renew_failed": "scheduler.proactive_auth_failed",
        "token_ensured": "scheduler.proactive_auth_skipped",
        "token_failed": "scheduler.proactive_auth_failed",
    }

    def _maint_notify(tick) -> None:
        if tick.action in ("renewed", "token_ensured"):
            fresh = load_session()
            if fresh is not None:
                # Atomic attribute rebind — the same in-memory handoff
                # `_on_rotation_success` performs on `_client._session`.
                client._session = fresh
                logbook.info("daemon.session_handoff", action=tick.action)
        event = _event_for.get(tick.action)
        if event is not None:
            try:
                notifier.emit(event, detail=tick.detail)
            except Exception:  # noqa: BLE001 — never break a tick
                pass

    # The maintenance loop owns ongoing refresh-token renewal — start it
    # in a daemon thread.
    stop_event = threading.Event()
    maint = threading.Thread(
        target=maintenance.run_loop,
        args=(cfg,),
        kwargs=dict(
            stop=stop_event.is_set,
            sleep=stop_event.wait,
            now=lambda: int(time.time()),
            interval_s=interval_s,
            notifier=_maint_notify,
        ),
        daemon=True,
        name="schwab-server-maintenance",
    )
    maint.start()

    logbook.info(
        "daemon.start",
        pid=os.getpid(),
        transport="http",
        bind=f"{mcp_host}:{mcp_port}",
        maintenance=True,
        interval_s=interval_s,
        log_file=str(resolved_log_file) if resolved_log_file else None,
    )
    typer.secho(
        f"server: starting MCP HTTP server on {mcp_host}:{mcp_port} "
        f"+ auth-maintenance loop (interval {interval_s}s)",
        fg=typer.colors.CYAN, err=True,
    )
    # --enable-rest mounts the REST PoC routes onto the MCP server's
    # Starlette app so both share this single port.
    extra_routes = None
    if enable_rest:
        from schwab_cli.server.rest import rest_routes

        extra_routes = rest_routes()

    crashed = False
    try:
        asyncio.run(
            server.run_http(mcp_host, mcp_port, extra_routes=extra_routes)
        )
    except KeyboardInterrupt:
        logbook.info("daemon.stop", reason="SIGINT")
    except Exception as e:
        crashed = True
        logbook.error("daemon.crash", error=f"{type(e).__name__}: {e}")
        raise
    finally:
        stop_event.set()
        maint.join(timeout=5)
        if maint.is_alive():
            logbook.warning("daemon.maintenance_stop_timeout")
        elif not crashed:
            logbook.info("daemon.stop", reason="maintenance_stopped")
    typer.secho("server: stopped.", fg=typer.colors.CYAN, err=True)
    return 0


def _run_with_rest(
    *,
    interval_s: int,
    rest_host: str,
    rest_port: int,
    log_file: str | None,
    no_log_file: bool,
    no_auto_login: bool,
) -> int | None:
    """``schwab server --enable-rest`` (without ``--enable-mcp``).

    Serves the standalone REST PoC Starlette app via uvicorn on
    ``rest_host:rest_port`` with the auth-maintenance loop running
    underneath in a daemon thread (the single proactive refresh-token
    renewer). Mirrors :func:`_run_with_mcp`'s startup (cfg + session
    presence, logbook + notifier, refresh-expiry startup auto-login) and
    its thread + ``asyncio.run`` + ``stop_event`` + join structure;
    uvicorn owns SIGINT/SIGTERM, so we install NO signal handlers here.

    The REST app is UNAUTHENTICATED — it is a proof of the REST ->
    service path only (auth/allowlisting is a deliberate later step).
    """
    from schwab_cli.commands.mcp import (
        _attempt_startup_autologin,
        _resolve_log_file,
    )
    from schwab_cli.mcp_server.logbook import LogBook
    from schwab_cli.server.rest import build_rest_app
    from schwab_cli.session import load as load_session

    cfg = config_module.load()
    if cfg is None:
        typer.secho(
            "No config found. Run `schwab_cli setup` first.",
            fg=typer.colors.RED, err=True,
        )
        raise SystemExit(1)
    session = load_session()
    if session is None:
        typer.secho(
            "No session found. Run `schwab_cli auth` first.",
            fg=typer.colors.RED, err=True,
        )
        raise SystemExit(1)

    resolved_log_file = _resolve_log_file(log_file, no_log_file)
    logbook = LogBook(log_file=resolved_log_file)
    from schwab_cli.notify import Notifier
    base_notifier = Notifier.from_file(logbook=logbook)

    now = int(time.time())
    if session.refresh_token_expires_at <= now:
        if no_auto_login:
            typer.secho(
                "Refresh token expired and --no-auto-login is set. "
                "Run `schwab_cli auth --force` to re-authenticate, "
                "then restart the server.",
                fg=typer.colors.RED, err=True,
            )
            raise SystemExit(1)
        logbook.warning("daemon.startup_refresh_expired", attempting_autologin=True)
        fresh = _attempt_startup_autologin(logbook, base_notifier)
        if fresh is None:
            typer.secho(
                "Startup auto-login failed. Check the log for details, "
                "then run `schwab_cli auth --force` manually before "
                "restarting.",
                fg=typer.colors.RED, err=True,
            )
            raise SystemExit(1)
        logbook.info("daemon.startup_refresh_recovered")

    # Forward maintenance ticks through the logbook-aware notifier (no
    # second notification.json load). REST calls the service per-request,
    # so there is no persistent client needing an in-memory session handoff.
    notifier = _build_notifier(base_notifier)

    # The maintenance loop owns ongoing refresh-token renewal — start it
    # in a daemon thread, exactly as the --enable-mcp path does.
    stop_event = threading.Event()
    maint = threading.Thread(
        target=maintenance.run_loop,
        args=(cfg,),
        kwargs=dict(
            stop=stop_event.is_set,
            sleep=stop_event.wait,
            now=lambda: int(time.time()),
            interval_s=interval_s,
            notifier=notifier,
        ),
        daemon=True,
        name="schwab-server-maintenance",
    )
    maint.start()

    typer.secho(
        f"server: starting REST PoC (unauthenticated) on "
        f"{rest_host}:{rest_port} + auth-maintenance loop "
        f"(interval {interval_s}s)",
        fg=typer.colors.CYAN, err=True,
    )

    async def _serve() -> None:
        import uvicorn

        config = uvicorn.Config(
            build_rest_app(),
            host=rest_host,
            port=rest_port,
            log_level="warning",
            loop="asyncio",
        )
        await uvicorn.Server(config).serve()

    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        maint.join(timeout=5)
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


def _build_notifier(notifier=None):
    """Build a maintenance-tick forwarder over the Notifier infra.

    Pass an already-constructed ``notifier`` (e.g. a logbook-aware one) to
    reuse it and avoid a second ``notification.json`` load; omit it to build
    a fresh one. Returns ``None`` if the notification stack can't be
    constructed, so the loop degrades to silent maintenance rather than
    crashing.
    """
    if notifier is None:
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
