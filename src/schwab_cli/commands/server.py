"""`server` command — long-lived daemon + launchd install + diagnostics.

``schwab server`` is the **only** daemon. Bare ``schwab server`` runs the
TokenManager (the single owner of the OAuth token pair — access-token
half-life exchange + refresh-token renewal checkpoints); ``--enable-mcp``
ALSO runs the Streamable HTTP MCP server on top of it; ``--enable-rest``
adds the REST PoC.

Subcommands manage the macOS launchd LaunchAgent and talk to a running
daemon's HTTP admin/health endpoints (exposed with ``--enable-mcp``):

* ``server install`` — write the plist + ``launchctl load``; bakes the
  ``--enable-mcp`` / ``--enable-rest`` / host / port / log-file flags
  into the plist's ProgramArguments.
* ``server uninstall`` — ``launchctl unload`` + remove the plist.
* ``server status`` — launchd-label check AND a real ``GET /health``
  probe against the daemon, with the ``/admin/status`` snapshot.
* ``server log`` — read / tail the structured log file.
* ``server logout`` — graceful shutdown via ``/admin/shutdown``.
* ``server restart`` — kickstart the ``com.schwab-cli.server`` launchd
  job.
* ``server register-claude`` — register the server in
  ``~/.claude/settings.json``.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import typer

from schwab_cli import config as config_module
from schwab_cli.commands._daemon import (
    DEFAULT_LOG_FILE,
    attempt_startup_autologin as _attempt_startup_autologin,
    resolve_log_file as _resolve_log_file,
)
from schwab_cli.server import token_runtime
from schwab_cli.server.jobs import runner as jobs_runner
from schwab_cli.server.jobs import runtime as jobs_runtime
from schwab_cli.server.jobs.scheduler import JobScheduler
from schwab_cli.server.jobs.state import load_state, reconcile_orphans, save_state
from schwab_cli.server.launchd import (
    DEFAULT_PLIST_PATH,
    LABEL,
    ServerPlistSpec,
    write_plist,
)


# Legacy `--interval-hours` default. The TokenManager self-schedules from
# token expiries, so the flag is accepted for plist compatibility but no
# longer drives any loop cadence.
DEFAULT_INTERVAL_S = 8 * 3600

DEFAULT_MCP_URL = "http://127.0.0.1:7234"


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

    Without ``--enable-mcp`` this loads config, installs SIGTERM/SIGINT
    handlers that flip a stop flag, starts the TokenManager's two tracks
    as daemon threads, and parks until stopped. Returns 0 on graceful
    exit.

    With ``enable_mcp=True`` it ALSO composes the Streamable HTTP MCP
    server on top: the TokenManager remains the single owner of token
    renewal, the MCP server exposes its ``/auth/*`` surface and receives
    session handoffs (in-memory rebind + streamer reconnect after a full
    rotation). ``enable_rest=True`` additionally mounts the REST PoC
    routes onto that same MCP Starlette app (one shared port).

    With ``enable_rest=True`` but WITHOUT ``--enable-mcp`` it serves the
    standalone REST PoC app via uvicorn on ``rest_host:rest_port`` with
    the TokenManager underneath in daemon threads.

    ``interval_s`` is legacy (the old maintenance-loop cadence): accepted
    for installed-plist compatibility, no longer drives any schedule.
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

    # Three coordinating events, all set by main-thread-only signal handlers:
    #   stop_event  — SIGTERM/SIGINT → graceful shutdown
    #   reload_event — SIGHUP → reload job config (no restart)
    #   _wake       — wakes the jobs loop's interruptible sleep at once
    # `Event.wait(timeout)` is the interruptible sleep — returns the instant a
    # handler sets the event, so a SIGTERM during a long idle wakes us
    # immediately (critical for graceful `launchctl unload`; plain time.sleep
    # is NOT interruptible per PEP 475).
    stop_event = threading.Event()
    reload_event = threading.Event()
    wake = threading.Event()

    def _handle_signal(_signum, _frame) -> None:
        # Async-signal-safe: ONLY flip events here. The user-facing
        # "stopping gracefully" message is emitted from the main loop's
        # shutdown path below, never from inside the handler.
        stop_event.set()
        wake.set()

    def _handle_hup(_signum, _frame) -> None:
        reload_event.set()
        wake.set()

    _install_signal_handlers(_handle_signal, hup_handler=_handle_hup)

    notifier = _load_notifier()

    # The TokenManager owns both tokens: its two tracks run as daemon
    # threads; the main thread just parks on stop_event so a SIGTERM/SIGINT
    # (which sets it) drives the whole shutdown.
    token_manager = token_runtime.build_token_manager(cfg, notifier=notifier)
    token_threads = token_runtime.start_token_threads(token_manager, stop_event)

    # Initialise to None BEFORE the try so the finally's _stop_jobs is safe
    # (and never raises UnboundLocalError) if _start_jobs itself raises.
    scheduler = None
    jobs_thread = None
    curr = None
    try:
        scheduler, jobs_thread, curr = _start_jobs(
            cfg,
            stop_event=stop_event,
            reload_event=reload_event,
            wake=wake,
            renew=_jobs_renew(token_manager),
            notify=_jobs_notify(),
        )

        typer.secho(
            "server: starting token manager + job scheduler",
            fg=typer.colors.CYAN, err=True,
        )
        # Event.wait is interruptible — returns the instant a signal
        # handler sets stop_event.
        stop_event.wait()
    finally:
        token_runtime.stop_token_threads(
            token_manager, stop_event, token_threads,
        )
        _stop_jobs(
            scheduler, jobs_thread,
            stop_event=stop_event, wake=wake, current=curr,
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
    """``schwab server --enable-mcp`` — TokenManager + MCP HTTP server.

    Startup checks cfg + session presence, builds logbook + notifier,
    and recovers an expired refresh token via startup auto-login. The
    TokenManager's two tracks run as daemon threads (the single owner of
    token renewal); the Streamable HTTP MCP server runs on the main
    thread with the manager attached (enables ``/auth/refresh`` +
    ``/auth/status`` and the session-handoff bridge). uvicorn owns
    SIGINT/SIGTERM, so we install NO signal handlers here.
    """
    from schwab_cli.api.client import SchwabClient
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
        fresh = _attempt_startup_autologin(cfg, logbook, notifier)
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

    client = SchwabClient(cfg, session)
    server = SchwabMcpServer(client, logbook, notifier=notifier)

    # The TokenManager is the single owner of both tokens. Its
    # on_session_replaced handoff rebinds the persistent client's
    # in-memory session (so in-flight REST calls pick up the fresh
    # token) and reconnects the shared streamer after a full rotation;
    # attaching it to the server enables the /auth/* routes.
    stop_event = threading.Event()
    reload_event = threading.Event()
    wake = threading.Event()
    token_manager = token_runtime.build_token_manager(
        cfg,
        notifier=notifier,
        on_session_replaced=server.schedule_session_replaced,
    )
    server.attach_token_manager(token_manager)
    token_threads = token_runtime.start_token_threads(token_manager, stop_event)

    # Jobs scheduler thread. uvicorn owns SIGINT/SIGTERM (installed inside
    # asyncio.run); we install ONLY SIGHUP on the main thread BEFORE asyncio.run
    # so it survives uvicorn's handler install and drives a reload.
    def _handle_hup(_signum, _frame) -> None:
        reload_event.set()
        wake.set()

    _install_sighup(_handle_hup)

    # Initialise to None BEFORE the try so the finally's _stop_jobs is safe
    # (never UnboundLocalError) if _start_jobs itself raises.
    scheduler = None
    jobs_thread = None
    curr = None
    crashed = False
    try:
        scheduler, jobs_thread, curr = _start_jobs(
            cfg,
            stop_event=stop_event,
            reload_event=reload_event,
            wake=wake,
            renew=_jobs_renew(token_manager),
            notify=_jobs_notify(notifier),
        )

        logbook.info(
            "daemon.start",
            pid=os.getpid(),
            transport="http",
            bind=f"{mcp_host}:{mcp_port}",
            token_manager=True,
            log_file=str(resolved_log_file) if resolved_log_file else None,
        )
        typer.secho(
            f"server: starting MCP HTTP server on {mcp_host}:{mcp_port} "
            f"+ token manager + job scheduler",
            fg=typer.colors.CYAN, err=True,
        )
        # --enable-rest mounts the REST PoC routes onto the MCP server's
        # Starlette app so both share this single port. The /admin/jobs route
        # is always added so the control plane is reachable in --enable-mcp.
        extra_routes = [_jobs_admin_route(scheduler)]
        if enable_rest:
            from schwab_cli.server.rest import rest_routes

            extra_routes = list(rest_routes()) + extra_routes

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
        token_runtime.stop_token_threads(
            token_manager, stop_event, token_threads,
        )
        if any(t.is_alive() for t in token_threads):
            logbook.warning("daemon.token_threads_stop_timeout")
        elif not crashed:
            logbook.info("daemon.stop", reason="token_threads_stopped")
        _stop_jobs(
            scheduler, jobs_thread,
            stop_event=stop_event, wake=wake, current=curr,
        )
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
    ``rest_host:rest_port`` with the TokenManager running underneath
    in daemon threads (the single owner of token renewal). Mirrors :func:`_run_with_mcp`'s startup (cfg + session
    presence, logbook + notifier, refresh-expiry startup auto-login) and
    its thread + ``asyncio.run`` + ``stop_event`` + join structure;
    uvicorn owns SIGINT/SIGTERM, so we install NO signal handlers here.

    The REST app is UNAUTHENTICATED — it is a proof of the REST ->
    service path only (auth/allowlisting is a deliberate later step).
    """
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
        fresh = _attempt_startup_autologin(cfg, logbook, base_notifier)
        if fresh is None:
            typer.secho(
                "Startup auto-login failed. Check the log for details, "
                "then run `schwab_cli auth --force` manually before "
                "restarting.",
                fg=typer.colors.RED, err=True,
            )
            raise SystemExit(1)
        logbook.info("daemon.startup_refresh_recovered")

    # The TokenManager owns ongoing renewal of both tokens. REST calls
    # the service per-request, so there is no persistent client needing
    # an in-memory session handoff.
    stop_event = threading.Event()
    reload_event = threading.Event()
    wake = threading.Event()
    token_manager = token_runtime.build_token_manager(
        cfg, notifier=base_notifier,
    )
    token_threads = token_runtime.start_token_threads(token_manager, stop_event)

    # uvicorn owns SIGINT/SIGTERM; install ONLY SIGHUP on the main thread
    # BEFORE asyncio.run so it survives and drives a job-config reload.
    def _handle_hup(_signum, _frame) -> None:
        reload_event.set()
        wake.set()

    _install_sighup(_handle_hup)

    # Initialise to None BEFORE the try so the finally's _stop_jobs is safe
    # (never UnboundLocalError) if _start_jobs itself raises.
    scheduler = None
    jobs_thread = None
    curr = None
    try:
        scheduler, jobs_thread, curr = _start_jobs(
            cfg,
            stop_event=stop_event,
            reload_event=reload_event,
            wake=wake,
            renew=_jobs_renew(token_manager),
            notify=_jobs_notify(base_notifier),
        )

        typer.secho(
            f"server: starting REST PoC (unauthenticated) on "
            f"{rest_host}:{rest_port} + token manager + job scheduler",
            fg=typer.colors.CYAN, err=True,
        )

        async def _serve() -> None:
            import uvicorn
            from starlette.applications import Starlette
            from schwab_cli.server.rest import rest_routes

            app = Starlette(
                routes=list(rest_routes()) + [_jobs_admin_route(scheduler)]
            )
            config = uvicorn.Config(
                app,
                host=rest_host,
                port=rest_port,
                log_level="warning",
                loop="asyncio",
            )
            await uvicorn.Server(config).serve()

        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass
    finally:
        token_runtime.stop_token_threads(
            token_manager, stop_event, token_threads,
        )
        # Mirror _run_with_mcp's post-join observability check.
        if any(t.is_alive() for t in token_threads):
            logbook.warning("daemon.token_threads_stop_timeout")
        _stop_jobs(
            scheduler, jobs_thread,
            stop_event=stop_event, wake=wake, current=curr,
        )
    typer.secho("server: stopped.", fg=typer.colors.CYAN, err=True)
    return 0


def _install_signal_handlers(handler, *, hup_handler=None) -> None:
    """Best-effort SIGTERM/SIGINT/SIGHUP install (no-op off the main thread).

    Signals can only be installed on the main thread; off-thread (e.g. under a
    test runner) :func:`signal.signal` raises ``ValueError`` which we swallow.
    SIGTERM/SIGINT get ``handler`` (graceful stop); SIGHUP gets ``hup_handler``
    if provided, otherwise ``handler`` too — the caller wires SIGHUP to a reload
    so the daemon can pick up new job config without restarting.
    """
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            # Not on the main thread (e.g. under a test runner) — skip.
            pass
    try:
        signal.signal(signal.SIGHUP, hup_handler or handler)
    except (ValueError, OSError, AttributeError):
        # SIGHUP is absent on some platforms (Windows); off-main-thread raises
        # ValueError — both are non-fatal.
        pass


def _install_sighup(hup_handler) -> None:
    """Best-effort SIGHUP-only install (used by the asyncio modes).

    The MCP / REST modes let uvicorn own SIGINT/SIGTERM; we only add SIGHUP on
    the main thread BEFORE ``asyncio.run`` so it survives uvicorn's own handler
    install and can drive a job-config reload.
    """
    try:
        signal.signal(signal.SIGHUP, hup_handler)
    except (ValueError, OSError, AttributeError):
        pass


def _load_notifier():
    """Best-effort Notifier construction for the bare server mode.

    Returns ``None`` if the notification stack can't be built — the
    TokenManager degrades to silent operation rather than crashing.
    """
    try:
        from schwab_cli.notify import Notifier

        return Notifier.from_file()
    except Exception:  # noqa: BLE001 — notifications are optional
        return None


# ---- jobs scheduler wiring -------------------------------------------


def _spawn_worker(cfg, log_path: Path):
    """Adapter: the scheduler calls ``spawn(cfg, log_path)`` positionally, but
    :func:`runner.spawn_worker` takes ``log_path`` keyword-only."""
    return jobs_runner.spawn_worker(cfg, log_path=log_path)


def _pid_alive(pid: int) -> bool:
    """Return True if ``pid`` is a live process (``os.kill(pid, 0)`` probe)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by another user — still "alive" for our purposes.
        return True
    except OSError:
        return False
    return True


def _proc_start_unknown(_pid: int) -> float | None:
    """Conservative, psutil-free process-start probe: always unknown (None).

    With an unknown start time, :func:`reconcile_orphans` can never confirm a
    pid/start-time match, so a stale recorded pid is marked interrupted WITHOUT
    killing — the safe default (we never SIGTERM a reused, unrelated pid).
    """
    return None


def _start_jobs(
    cfg,
    *,
    stop_event: threading.Event,
    reload_event: threading.Event,
    wake: threading.Event,
    renew,
    notify,
) -> tuple[JobScheduler, threading.Thread, Path]:
    """Build the job scheduler once and start its poll loop in a daemon thread.

    Reconciles any orphaned children left by a prior daemon, promotes + loads
    the staged job configs, writes the server pidfile, and returns the live
    ``(scheduler, thread, current_dir)``. The thread runs
    :func:`runtime.run_scheduler_loop` driven by the shared ``stop_event`` /
    ``reload_event`` / ``wake`` events. The returned ``current_dir`` is threaded
    into :func:`_stop_jobs` so shutdown removes the same pidfile this resolved.
    """
    curr = jobs_runtime.current_dir()
    stg = jobs_runtime.jobs_dir()

    # Recover any children a previous daemon left marked-running.
    state = load_state(curr)
    state = reconcile_orphans(
        state,
        alive=_pid_alive,
        proc_start=_proc_start_unknown,
        killpg=lambda pgid: os.killpg(pgid, signal.SIGTERM),
    )
    save_state(curr, state)

    scheduler = JobScheduler(
        current_dir=curr,
        jobs=[],
        now=time.time,
        spawn=_spawn_worker,
        renew=renew,
        notify=notify,
        initial_states=state.jobs,
    )
    # Promote + load + schedule the staged configs.
    jobs_runtime.apply_reload(stg, curr, scheduler)
    # Persist initial state so it's visible immediately (no tick needed yet).
    save_state(curr, scheduler.snapshot())

    jobs_runtime.write_pidfile(curr)

    def _wait(timeout: float) -> None:
        wake.wait(timeout)
        wake.clear()

    def _reload_requested() -> bool:
        if reload_event.is_set():
            reload_event.clear()
            return True
        return False

    thread = threading.Thread(
        target=jobs_runtime.run_scheduler_loop,
        args=(scheduler, stg, curr),
        kwargs=dict(
            stop=stop_event.is_set,
            reload_requested=_reload_requested,
            wait=_wait,
            notify=notify,
        ),
        daemon=True,
        name="schwab-server-jobs",
    )
    thread.start()
    return scheduler, thread, curr


def _stop_jobs(
    scheduler: JobScheduler | None,
    thread: threading.Thread | None,
    *,
    stop_event: threading.Event,
    wake: threading.Event,
    current: Path | None = None,
) -> None:
    """Stop the job scheduler thread, terminate children, remove the pidfile.

    ``current`` is the ``.current`` dir resolved by :func:`_start_jobs`; pass it
    so shutdown removes the same pidfile rather than re-deriving it. Falls back
    to :func:`runtime.current_dir` when not provided (e.g. None-safe callers).
    """
    stop_event.set()
    wake.set()
    if thread is not None:
        thread.join(timeout=5)
    if scheduler is not None:
        scheduler.terminate_children()
    jobs_runtime.remove_pidfile(
        current if current is not None else jobs_runtime.current_dir()
    )


def _jobs_notify(notifier=None):
    """Build a scheduler ``notify(event, **fields)`` callable over a Notifier.

    The scheduler emits ``notify("scheduler.job_failed", job_id=..., ...)`` —
    a different shape from the maintenance-tick forwarder — so it needs a
    notifier whose ``emit`` is called directly. Returns ``None`` (silent) when
    no notification stack can be constructed.
    """
    if notifier is None:
        try:
            from schwab_cli.notify import Notifier

            notifier = Notifier.from_file()
        except Exception:  # noqa: BLE001 — notifications are optional
            return None

    def _emit(event: str, **fields) -> None:
        try:
            notifier.emit(event, **fields)
        except Exception:  # noqa: BLE001 — never break a tick
            pass

    return _emit


def _jobs_admin_route(scheduler: JobScheduler):
    """Build a ``GET /admin/jobs`` Starlette route returning the jobs snapshot."""
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def _admin_jobs(_request):
        return JSONResponse(jobs_runtime.jobs_admin_payload(scheduler))

    return Route("/admin/jobs", _admin_jobs, methods=["GET"])


def _jobs_renew(token_manager):
    """Best-effort pre-dispatch token freshen used as the scheduler ``renew``.

    Delegates to the TokenManager's single-flight exchange so a job tick
    never races the access track's own refresh.
    """
    def _renew() -> None:
        try:
            token_manager.force_exchange()
        except Exception:  # noqa: BLE001 — renew must never break a tick
            pass
    return _renew


# ---- server install --------------------------------------------------


def run_install(
    *,
    plist_path: str | None = None,
    log_file: str | None = None,
    enable_mcp: bool = False,
    enable_rest: bool = False,
    host: str | None = None,
    port: int | None = None,
    mcp_log_file: str | None = None,
    yes: bool = False,
) -> None:
    """Write the LaunchAgent plist and ``launchctl load`` it.

    With no mode flags the plist runs the bare token-manager daemon. The
    ``enable_mcp`` / ``enable_rest`` / ``host`` / ``port`` /
    ``mcp_log_file`` options are baked into the plist's
    ProgramArguments so launchd starts e.g.
    ``schwab server --enable-mcp --mcp-host 127.0.0.1 --mcp-port 7234``.
    """
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

    modes = []
    if enable_mcp:
        modes.append("--enable-mcp")
    if enable_rest:
        modes.append("--enable-rest")

    typer.echo("Proposed LaunchAgent:")
    typer.echo(f"  Label:     {LABEL}")
    typer.echo(f"  Binary:    {binary}")
    typer.echo(f"  Modes:     {' '.join(modes) if modes else 'bare token manager'}")
    if enable_mcp:
        typer.echo(f"  MCP bind:  {host or '127.0.0.1'}:{port or 7234}")
    typer.echo(f"  Plist:     {target}")
    if log_file:
        typer.echo(f"  Log:       {log_file}")
    typer.echo("  KeepAlive: true  (exits → auto-restart)")
    if not yes:
        if not typer.confirm("Install and load now?", default=True):
            typer.echo("aborted")
            raise typer.Exit(code=0)

    write_plist(
        ServerPlistSpec(
            binary_path=binary,
            log_file=log_file,
            enable_mcp=enable_mcp,
            enable_rest=enable_rest,
            host=host,
            port=port,
            mcp_log_file=mcp_log_file,
        ),
        target,
    )
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
    # `launchctl load -w` registers the job, but RunAtLoad only fires the
    # spawn reliably when loaded into the GUI/Aqua session. When `install`
    # runs from a non-GUI context (SSH, an automation shell, a subprocess),
    # the job loads with runs=0 and never starts until the next login.
    # kickstart it explicitly so the daemon comes up immediately regardless
    # of context. Best-effort: a failure here still leaves a loaded job that
    # RunAtLoad will start at the next login.
    kick = subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LABEL}"],
        capture_output=True, text=True,
    )
    if kick.returncode != 0:
        typer.secho(
            "note: launchctl kickstart did not start the job now "
            f"({(kick.stderr or kick.stdout or '').strip()}); it will start "
            "at next login. Run `schwab server status` to check.",
            fg=typer.colors.YELLOW, err=True,
        )
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


def _auth_headers(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _launchd_job_loaded(label: str) -> bool:
    """Return True when ``launchctl list`` recognizes ``label``.

    Covers both running and throttled-retry states — what we care about
    is whether launchd owns the daemon's lifecycle, not whether it's
    currently up. Returns False on non-Darwin / missing launchctl so the
    helper is safe to call from tests.
    """
    try:
        r = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def run_status(
    *,
    url: str | None = None,
    port: int | None = None,
    token: str | None = None,
    as_json: bool = False,
) -> None:
    """Report server health: launchd-label check + a real ``/health`` probe.

    Two checks, both reported:

    1. **launchd** — is ``com.schwab-cli.server`` loaded (``launchctl
       list``)?
    2. **HTTP** — a real ``GET /health`` against the daemon's bound
       address (default ``http://127.0.0.1:7234``; override with
       ``--url`` or ``--port``). When ``--enable-mcp`` is running this
       returns ``{"ok": true}``; the ``/admin/status`` snapshot is then
       fetched and rendered too.
    """
    # 1. launchd label check.
    list_result = subprocess.run(
        ["launchctl", "list"],
        capture_output=True, text=True,
    )
    stdout = list_result.stdout or ""
    loaded = list_result.returncode == 0 and LABEL in stdout

    # 2. Real /health probe against the bound address.
    base = (url or DEFAULT_MCP_URL).rstrip("/")
    if port is not None and url is None:
        base = f"http://127.0.0.1:{port}"
    health_ok = False
    health_err: str | None = None
    snapshot: dict[str, Any] | None = None
    try:
        hr = httpx.get(
            f"{base}/health",
            headers=_auth_headers(token),
            timeout=5.0,
        )
        health_ok = hr.status_code == 200
        if not health_ok:
            health_err = f"{hr.status_code}: {hr.text[:200]}"
    except httpx.RequestError as e:
        health_err = f"{type(e).__name__}"

    # Pull the /admin/status snapshot when the daemon is reachable.
    if health_ok:
        try:
            sr = httpx.get(
                f"{base}/admin/status",
                headers=_auth_headers(token),
                timeout=5.0,
            )
            if sr.status_code == 200:
                snapshot = sr.json()
        except (httpx.RequestError, json.JSONDecodeError):
            snapshot = None

    if as_json:
        typer.echo(json.dumps(
            {
                "launchd_label": LABEL,
                "launchd_loaded": loaded,
                "health_url": f"{base}/health",
                "health_reachable": health_ok,
                "health_error": health_err,
                "status": snapshot,
            },
            indent=2, default=str,
        ))
        return

    # launchd line.
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

    # /health line.
    if health_ok:
        typer.secho(
            f"health: reachable at {base}/health",
            fg=typer.colors.GREEN,
        )
        if snapshot is not None:
            typer.echo(_format_status(snapshot))
        else:
            typer.secho(
                "  (/admin/status unavailable — daemon may be running "
                "without --enable-mcp)",
                fg=typer.colors.YELLOW,
            )
    else:
        typer.secho(
            f"health: NOT reachable at {base}/health "
            f"({health_err or 'unknown error'}). "
            "Is the daemon running with --enable-mcp?",
            fg=typer.colors.YELLOW,
        )


# ---- status formatting (shared with the snapshot render) -------------


def _format_status(data: dict[str, Any]) -> str:
    lines: list[str] = ["=== schwab_cli server ==="]
    lines.append(f"PID:              {data.get('pid', '—')}")
    lines.append(f"Uptime:           {_fmt_duration(data.get('uptime_sec'))}")
    lines.append(f"Transport:        {data.get('transport', '—')}")
    lines.append("")
    auth = data.get("auth") or {}
    lines.append("Auth:")
    lines.append(f"  Access expires:  {auth.get('access_expires_at', '—')}")
    lines.append(f"  Refresh expires: {auth.get('refresh_expires_at', '—')}")
    lines.append("")
    stream = data.get("streamer") or {}
    lines.append("Schwab streamer:")
    lines.append(f"  State:           {stream.get('state', '—')}")
    lines.append(f"  Reconnects:      {stream.get('reconnects', 0)}")
    lines.append("")
    summary = data.get("subscription_summary") or {}
    lines.append(f"Active sessions:  {summary.get('session_count', 0)}")
    sessions = summary.get("sessions") or {}
    for sid, sess in sessions.items():
        lines.append(
            f"  {sid}  subs: {', '.join(sess.get('symbols') or []) or '—'}  "
            f"streams: {sess.get('progress_stream_count', 0)}"
        )
    lines.append("")
    subs = summary.get("subscriptions") or []
    lines.append(f"Subscriptions (refcounted): {len(subs)}")
    for s in subs:
        sessions_str = ", ".join(s.get("sessions") or [])
        lines.append(
            f"  {s.get('service', '?'):20} {s.get('symbol', '?'):10} "
            f"x{s.get('refcount', 0)}   ({sessions_str})"
        )
    return "\n".join(lines)


def _fmt_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "—"
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return "—"
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m, sec = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h or d:
        parts.append(f"{h}h")
    parts.append(f"{m}m {sec}s")
    return " ".join(parts)


# ---- server logout ---------------------------------------------------


def run_logout(*, url: str | None = None, token: str | None = None) -> None:
    """Gracefully shut down the running daemon via ``/admin/shutdown``."""
    base = (url or DEFAULT_MCP_URL).rstrip("/")
    try:
        r = httpx.post(
            f"{base}/admin/shutdown",
            headers=_auth_headers(token),
            timeout=5.0,
        )
    except httpx.RequestError as e:
        typer.secho(
            f"Could not reach server at {base}: {type(e).__name__}.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=1)
    if r.status_code != 200:
        typer.secho(f"{r.status_code}: {r.text}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.echo("shutdown signalled")


# ---- server restart --------------------------------------------------


def run_restart(
    *,
    url: str | None = None,
    token: str | None = None,
    host: str = "127.0.0.1",
    port: int = 7234,
) -> None:
    """Bounce the daemon.

    Two paths:

    1. **launchd-managed.** When ``com.schwab-cli.server`` is loaded,
       ``launchctl kickstart -k`` is the canonical bounce — it SIGTERMs
       the existing PID and lets ``KeepAlive=true`` respawn under the
       same job. Returns immediately; the terminal stays free.
    2. **Manual foreground.** No launchd job loaded → fall back to
       logout-via-admin + ``os.execvp`` a fresh bare ``schwab server``.

    Any non-default ``--host``/``--port`` are incompatible with launchd
    (the plist bakes the bound config); a mismatch surfaces as a warning.
    """
    if _launchd_job_loaded(LABEL):
        if (host, port) != ("127.0.0.1", 7234):
            typer.secho(
                f"warning: --host/--port flags ({host}:{port}) are ignored "
                f"in launchd mode; the plist's baked config wins. "
                f"Re-run `schwab server install` to change the bound "
                f"address.",
                fg=typer.colors.YELLOW, err=True,
            )
        target = f"gui/{os.getuid()}/{LABEL}"
        typer.echo(f"kickstarting launchd job: {target}")
        result = subprocess.run(
            ["launchctl", "kickstart", "-k", target],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            typer.secho(
                f"launchctl kickstart failed: {err}",
                fg=typer.colors.RED, err=True,
            )
            raise typer.Exit(code=1)
        typer.echo(
            "server will respawn momentarily — `schwab server status` to "
            "verify."
        )
        return

    try:
        run_logout(url=url, token=token)
    except typer.Exit:
        typer.secho(
            "(no running server to stop, starting fresh)",
            fg=typer.colors.YELLOW, err=True,
        )
    # Give the old server a moment to release the port.
    time.sleep(1.5)
    args = [sys.argv[0], "server"]
    typer.echo(f"starting: {' '.join(args)}")
    os.execvp(sys.argv[0], args)


# ---- server log ------------------------------------------------------


def run_log(
    *,
    follow: bool,
    log_file: str | None,
    session: str | None,
    symbol: str | None,
    level: str | None,
    as_json: bool,
    tail: int | None,
) -> None:
    """Reader / follower for the daemon's structured log file."""
    path = Path(log_file).expanduser() if log_file else DEFAULT_LOG_FILE
    if not path.exists():
        typer.secho(
            f"Log file not found: {path}. "
            "Has the daemon run at least once?",
            fg=typer.colors.YELLOW, err=True,
        )
        raise typer.Exit(code=0)

    # Historical portion.
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if tail is not None and tail > 0:
        lines = lines[-tail:]
    for line in lines:
        rendered = _render_log_line(
            line, session=session, symbol=symbol, level=level, as_json=as_json,
        )
        if rendered is not None:
            typer.echo(rendered)

    if not follow:
        return

    # Follow mode — simple seek-to-end + sleep loop. Good enough without
    # an inotify dependency.
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)  # end
            while True:
                chunk = f.readline()
                if not chunk:
                    time.sleep(0.25)
                    continue
                rendered = _render_log_line(
                    chunk.rstrip("\n"),
                    session=session, symbol=symbol, level=level,
                    as_json=as_json,
                )
                if rendered is not None:
                    typer.echo(rendered)
    except KeyboardInterrupt:
        return


_LEVEL_ORDER = {"debug": 0, "info": 1, "warning": 2, "error": 3}


def _render_log_line(
    raw: str,
    *,
    session: str | None,
    symbol: str | None,
    level: str | None,
    as_json: bool,
) -> str | None:
    """Apply filters; return the rendered line or None to skip."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        entry = json.loads(raw)
    except json.JSONDecodeError:
        return raw if as_json else None

    # Level filter (accept threshold and above).
    if level is not None:
        threshold = _LEVEL_ORDER.get(level.lower(), 0)
        cur = _LEVEL_ORDER.get(str(entry.get("level", "info")).lower(), 1)
        if cur < threshold:
            return None

    # Session filter.
    if session is not None:
        if entry.get("session") != session:
            return None

    # Symbol filter — matches `symbol` key or a list in `symbols`.
    if symbol is not None:
        syms = entry.get("symbols")
        if isinstance(syms, list):
            if symbol not in syms:
                return None
        elif entry.get("symbol") != symbol:
            return None

    if as_json:
        return raw

    # Pretty-print one-line format.
    ts = entry.get("ts", "")[-12:-1] if isinstance(entry.get("ts"), str) else "?"
    lvl = str(entry.get("level", "info")).upper()[:4]
    event = entry.get("event", "?")
    extras = {
        k: v for k, v in entry.items()
        if k not in {"ts", "level", "event"}
    }
    extra_str = " ".join(f"{k}={_short(v)}" for k, v in extras.items())
    return f"{ts} {lvl:5} {event:24} {extra_str}"


def _short(v: Any) -> str:
    """Compact repr for log-line extras — avoid huge blobs."""
    s = json.dumps(v, default=str)
    if len(s) > 80:
        return s[:77] + "..."
    return s


# ---- server register-claude ------------------------------------------


def run_register_claude(
    *, url: str, token: str | None,
    settings: str | None, yes: bool, force: bool,
) -> None:
    """Merge a `schwab` MCP server entry into ~/.claude/settings.json.

    Points Claude Code at the daemon's ``/mcp`` endpoint (run the daemon
    with ``schwab server --enable-mcp``).
    """
    settings_path = (
        Path(settings).expanduser() if settings
        else Path.home() / ".claude" / "settings.json"
    )
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict[str, Any] = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                typer.secho(
                    f"{settings_path} is not a JSON object; refusing to edit.",
                    fg=typer.colors.RED, err=True,
                )
                raise typer.Exit(code=1)
        except json.JSONDecodeError as e:
            typer.secho(
                f"{settings_path} is not valid JSON: {e}",
                fg=typer.colors.RED, err=True,
            )
            raise typer.Exit(code=1)

    mcp_servers = existing.setdefault("mcpServers", {})
    if "schwab" in mcp_servers and not force:
        typer.secho(
            f"schwab entry already exists in {settings_path}. "
            "Pass --force to overwrite.",
            fg=typer.colors.YELLOW, err=True,
        )
        raise typer.Exit(code=1)

    http_url = url.rstrip("/")
    if not http_url.endswith("/mcp"):
        http_url = http_url + "/mcp"
    entry: dict[str, Any] = {"type": "http", "url": http_url}
    if token:
        entry["headers"] = {"Authorization": f"Bearer {token}"}

    mcp_servers["schwab"] = entry

    typer.echo("Proposed write:")
    typer.echo(json.dumps({"mcpServers": {"schwab": entry}}, indent=2))
    if not yes:
        if not typer.confirm(f"Apply to {settings_path}?", default=True):
            typer.echo("aborted")
            raise typer.Exit(code=0)

    tmp = settings_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, settings_path)
    typer.echo(f"wrote {settings_path}")
