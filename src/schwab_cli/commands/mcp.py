"""`mcp` command — MCP server runner + admin subcommands.

Bare ``schwab_cli mcp`` starts the daemon over Streamable HTTP (the
only supported transport). Subcommands live under the ``mcp`` typer
group:

* ``mcp status`` — HTTP client for ``/admin/status``.
* ``mcp log [-f]`` — read / tail the structured log file.
* ``mcp logout`` — graceful shutdown via ``/admin/shutdown``.
* ``mcp restart`` — logout + start again in-place.
* ``mcp install`` — register the server in
  ``~/.claude/settings.json``.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import typer

from schwab_cli import config as config_module
from schwab_cli.api.client import SchwabClient
from schwab_cli.mcp_server.app import SchwabMcpServer
from schwab_cli.mcp_server.logbook import LogBook
from schwab_cli.session import load as load_session


DEFAULT_LOG_FILE = Path.home() / ".config" / "schwab_cli" / "mcp.log"
DEFAULT_SSE_URL = "http://127.0.0.1:7234"


# ---- daemon runner ----------------------------------------------------


def run(
    *,
    host: str,
    port: int,
    log_file: str | None,
    no_log_file: bool,
    no_auto_login: bool = False,
) -> None:
    """Entry point from :mod:`schwab_cli.cli` for the bare `mcp` call.

    Runs the daemon over Streamable HTTP until SIGINT or the admin
    shutdown endpoint is called.

    Startup sequence (before the server runs):

    1. Config + session present on disk.
    2. Refresh token still valid OR ``--no-auto-login`` absent AND
       startup auto-login (browser subprocess) succeeds.
    3. Logbook + notifier built so auto-login events have a
       destination.
    """
    cfg = config_module.load()
    if cfg is None:
        typer.secho(
            "No config found. Run `schwab_cli setup` first.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=1)
    session = load_session()
    if session is None:
        typer.secho(
            "No session found. Run `schwab_cli auth` first.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=1)

    # Logbook + notifier built here (before the refresh-expiry check)
    # so the startup auto-login path has both available. Passing the
    # notifier into the server afterwards avoids double-loading
    # notification.json.
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
                "then restart the MCP server.",
                fg=typer.colors.RED, err=True,
            )
            raise typer.Exit(code=1)
        logbook.warning(
            "daemon.startup_refresh_expired",
            attempting_autologin=True,
        )
        fresh = _attempt_startup_autologin(logbook, notifier)
        if fresh is None:
            typer.secho(
                "Startup auto-login failed. Check "
                "~/.config/schwab_cli/mcp.log for details, then run "
                "`schwab_cli auth --force` manually before restarting.",
                fg=typer.colors.RED, err=True,
            )
            raise typer.Exit(code=1)
        session = fresh
        logbook.info("daemon.startup_refresh_recovered")

    client = SchwabClient(cfg, session)
    server = SchwabMcpServer(
        client, logbook,
        notifier=notifier,
        auth_monitor_enabled=not no_auto_login,
    )

    logbook.info(
        "daemon.start",
        pid=os.getpid(),
        transport="http",
        bind=f"{host}:{port}",
        log_file=str(resolved_log_file) if resolved_log_file else None,
    )
    try:
        asyncio.run(server.run_http(host, port))
    except KeyboardInterrupt:
        logbook.info("daemon.stop", reason="SIGINT")
    except Exception as e:
        logbook.error("daemon.crash", error=f"{type(e).__name__}: {e}")
        raise


def _attempt_startup_autologin(
    logbook,
    notifier,
    *,
    monitor_cls=None,
    session_loader=None,
):
    """One-shot rotation at daemon startup when the refresh token
    is already dead. Returns the freshly-loaded Session on success,
    ``None`` on failure.

    Reuses ``AuthMonitor.run_once`` so the subprocess, env,
    anti-thrash, and notification code is identical to the
    steady-state rotation path. Runs synchronously via ``asyncio.run``
    because we're still in the setup phase — the server event loop
    hasn't started yet.

    Accepts ``monitor_cls`` / ``session_loader`` overrides for tests
    — the defaults import the real AuthMonitor and session loader.
    """
    if monitor_cls is None:
        from schwab_cli.mcp_server.auth_monitor import AuthMonitor
        monitor_cls = AuthMonitor
    if session_loader is None:
        from schwab_cli.session import load as load_session
        session_loader = load_session

    monitor = monitor_cls(logbook, notifier)
    result = asyncio.run(monitor.run_once(reason="startup"))
    if not result.ok:
        return None
    return session_loader()


def _resolve_log_file(log_file: str | None, no_log_file: bool) -> Path | None:
    if no_log_file:
        return None
    if log_file:
        return Path(log_file).expanduser()
    return DEFAULT_LOG_FILE


# ---- mcp status -------------------------------------------------------


def run_status(*, url: str | None, token: str | None, as_json: bool) -> None:
    base = (url or DEFAULT_SSE_URL).rstrip("/")
    try:
        r = httpx.get(
            f"{base}/admin/status",
            headers=_auth_headers(token),
            timeout=5.0,
        )
    except httpx.RequestError as e:
        typer.secho(
            f"Could not reach MCP server at {base}: {type(e).__name__}. "
            "Is the daemon running?",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=1)
    if r.status_code == 401:
        typer.secho(
            "401 unauthorized — pass --token.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=1)
    if r.status_code != 200:
        typer.secho(f"{r.status_code}: {r.text}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    data = r.json()
    if as_json:
        typer.echo(json.dumps(data, indent=2, default=str))
    else:
        typer.echo(_format_status(data))


def _format_status(data: dict[str, Any]) -> str:
    lines: list[str] = ["=== schwab_cli mcp server ==="]
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


def _auth_headers(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


# ---- mcp logout / restart --------------------------------------------


def run_logout(*, url: str | None, token: str | None) -> None:
    base = (url or DEFAULT_SSE_URL).rstrip("/")
    try:
        r = httpx.post(
            f"{base}/admin/shutdown",
            headers=_auth_headers(token),
            timeout=5.0,
        )
    except httpx.RequestError as e:
        typer.secho(
            f"Could not reach MCP server at {base}: {type(e).__name__}.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=1)
    if r.status_code != 200:
        typer.secho(f"{r.status_code}: {r.text}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.echo("shutdown signalled")


def _launchd_job_loaded(label: str) -> bool:
    """Return True when ``launchctl list`` recognizes ``label``.

    Covers both running and throttled-retry states — what we care about
    is whether launchd owns the daemon's lifecycle, not whether it's
    currently up. Returns False on non-Darwin or when launchctl is
    missing so the function is safe to call from tests.
    """
    try:
        r = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def run_restart(
    *, url: str | None, token: str | None,
    host: str, port: int,
) -> None:
    """Bounce the Streamable HTTP daemon.

    Two paths:

    1. **launchd-managed.** When ``com.schwab-cli.mcp`` is loaded with
       launchctl (the install-service path), ``launchctl kickstart -k``
       is the canonical bounce — it SIGTERMs the existing PID and
       lets ``KeepAlive=true`` respawn under the same job. The
       command returns immediately; the user's terminal stays free.
    2. **Manual foreground.** No launchd job loaded → fall back to
       logout-via-admin + ``os.execvp``, the original behavior. The
       restarted daemon takes over the user's terminal.

    Any non-default ``--host``/``--port`` are incompatible with launchd
    (the plist bakes in ``127.0.0.1:7234`` by default); mismatched
    host/port surface as a warning so the user can decide whether to
    ``mcp install-service`` to re-bake the plist.
    """
    from schwab_cli.mcp_server.launchd import LABEL

    if _launchd_job_loaded(LABEL):
        if (host, port) != ("127.0.0.1", 7234):
            typer.secho(
                f"warning: --host/--port flags ({host}:{port}) are ignored "
                f"in launchd mode; the plist's baked config wins. "
                f"Re-run `mcp install-service` to change the bound address.",
                fg=typer.colors.YELLOW, err=True,
            )
        import subprocess
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
            "daemon will respawn momentarily — `mcp status` to verify."
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
    args = [sys.argv[0], "mcp", "--host", host, "--port", str(port)]
    typer.echo(f"starting: {' '.join(args)}")
    os.execvp(sys.argv[0], args)


# ---- mcp log ----------------------------------------------------------


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

    # Follow mode — simple seek-to-end + sleep loop. Good enough
    # without adding an inotify dependency.
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
                    session=session, symbol=symbol, level=level, as_json=as_json,
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

    # Symbol filter — matches against either `symbol` key or a list in
    # `symbols`.
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
    # Summarize remaining fields.
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


# ---- mcp install ------------------------------------------------------


def run_install_service(
    *, host: str, port: int, log_file: str | None,
    admin_token: str | None, plist_path: str | None, yes: bool,
) -> None:
    """Install the launchd LaunchAgent plist for the Streamable HTTP daemon.

    After writing, runs ``launchctl load`` to start immediately and
    register for start-at-login. User must have GUI access because
    the daemon talks to Schwab's OAuth flow on rotation.
    """
    import shutil
    from schwab_cli.mcp_server.launchd import (
        DEFAULT_PLIST_PATH,
        LaunchdPlistSpec,
        write_plist,
    )

    # Resolve the absolute path to the binary so launchd doesn't
    # depend on $PATH at login time. PR #6 renamed schwab_cli → schwab;
    # legacy `schwab_cli` is checked second for back-compat.
    binary = shutil.which("schwab") or shutil.which("schwab_cli")
    if not binary:
        typer.secho(
            "schwab not found on PATH; install it with "
            "`uv tool install --from . schwab_cli` first.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=1)

    target = (
        Path(plist_path).expanduser() if plist_path else DEFAULT_PLIST_PATH
    )
    spec = LaunchdPlistSpec(
        binary_path=binary,
        host=host,
        port=port,
        log_file=log_file,
        admin_token=admin_token,
    )
    typer.echo("Proposed LaunchAgent:")
    typer.echo("  Label:     com.schwab-cli.mcp")
    typer.echo(f"  Binary:    {binary}")
    typer.echo(f"  Bind:      {host}:{port}")
    typer.echo(f"  Plist:     {target}")
    typer.echo("  KeepAlive: true  (exits → auto-restart)")
    if not yes:
        if not typer.confirm("Install and load now?", default=True):
            typer.echo("aborted")
            raise typer.Exit(code=0)

    write_plist(spec, target)
    # `launchctl load` is the canonical way to enable an Agent plist.
    # Success is quiet; failure surfaces on stderr with a non-zero
    # exit code that we propagate.
    result = subprocess.run(
        ["launchctl", "load", "-w", str(target)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        typer.secho(
            f"launchctl load failed: {result.stderr.strip() or result.stdout.strip()}",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=1)
    typer.echo(f"installed + loaded: {target}")
    typer.echo(
        "Daemon is now running and will restart on exit. "
        "Use `schwab_cli mcp status` to verify, "
        "`schwab_cli mcp uninstall-service` to remove."
    )


def run_start_service(*, plist_path: str | None) -> None:
    from schwab_cli.mcp_server.launchd import DEFAULT_PLIST_PATH

    target = (
        Path(plist_path).expanduser() if plist_path else DEFAULT_PLIST_PATH
    )
    if not target.exists():
        typer.secho(
            f"{target} not found. Run `schwab_cli mcp install-service` first.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=1)
    result = subprocess.run(
        ["launchctl", "load", "-w", str(target)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        # "service already loaded" is not a failure we should escalate.
        if "already loaded" in err.lower():
            typer.echo(f"already loaded: {target}")
            return
        typer.secho(f"launchctl load failed: {err}",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.echo(f"loaded: {target}")


def run_stop_service(*, plist_path: str | None) -> None:
    from schwab_cli.mcp_server.launchd import DEFAULT_PLIST_PATH

    target = (
        Path(plist_path).expanduser() if plist_path else DEFAULT_PLIST_PATH
    )
    if not target.exists():
        typer.secho(
            f"{target} not found (nothing to stop).",
            fg=typer.colors.YELLOW, err=True,
        )
        raise typer.Exit(code=0)
    result = subprocess.run(
        ["launchctl", "unload", "-w", str(target)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        typer.secho(f"launchctl unload failed: {err}",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.echo(f"unloaded: {target}")


def run_uninstall_service(*, plist_path: str | None, yes: bool) -> None:
    from schwab_cli.mcp_server.launchd import (
        DEFAULT_PLIST_PATH, remove_launcher,
    )

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
    # Best-effort unload (might not be loaded if user already ran stop).
    subprocess.run(
        ["launchctl", "unload", "-w", str(target)],
        capture_output=True, text=True,
    )
    target.unlink(missing_ok=True)
    remove_launcher()
    typer.echo(f"removed: {target}")


def run_install(
    *, url: str, token: str | None,
    settings: str | None, yes: bool, force: bool,
) -> None:
    """Merge a `schwab` MCP server entry into ~/.claude/settings.json."""
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
