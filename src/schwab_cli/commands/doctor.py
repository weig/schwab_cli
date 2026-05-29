"""`schwab_cli doctor` — health check and install hints.

Surfaces install / config / runtime state across:

  • Global install (binary on PATH)
  • MCP server (running, autostart, transport, claude-code, launchd)
  • Schwab auth (config, auto-login, session validity)
  • Dataset (subscriptions per source, tier counts, market-data stats)
  • Data Sync Service — scheduled jobs run in-process by `schwab server`,
    config in ``~/.config/schwab_cli/jobs/<id>.json`` (promoted to
    ``jobs/.current/``), run state in ``jobs/.current/state.json``

For each item that is missing or inactive, prints the exact command
you'd run to fix it. Pure read-only — never mutates state.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import typer


_NY_TZ = ZoneInfo("America/New_York")


# ---- printing helpers --------------------------------------------------


def _ok(label: str, detail: str = "") -> None:
    typer.secho("  ✓ ", fg=typer.colors.GREEN, nl=False)
    typer.echo(f"{label:<32} {detail}")


def _bad(label: str, detail: str = "") -> None:
    typer.secho("  ✗ ", fg=typer.colors.RED, nl=False)
    typer.echo(f"{label:<32} {detail}")


def _info(label: str, detail: str = "") -> None:
    typer.echo(f"    {label:<32} {detail}")


def _hint(text: str) -> None:
    typer.secho(f"    → {text}", fg=typer.colors.YELLOW)


def _section(title: str) -> None:
    typer.echo("")
    typer.secho(title, fg=typer.colors.BRIGHT_BLUE, bold=True)


# ---- relative-time + plist helpers ------------------------------------


def _format_relative_time(
    target: datetime | None, *, now: datetime | None = None,
) -> str:
    """Render ``target`` relative to ``now`` (default ``datetime.now``).

    Spec — relative form everywhere except deep history:
      * < 1 minute either side → ``"<1m"``
      * 1m – 59m past         → ``"NNm ago"``
      * 1m – 59m future       → ``"in NNm"``
      * 1h – 23h past         → ``"N.Nh ago"``
      * 1h – 23h future       → ``"in N.Nh"``
      * 1d – 29d past         → ``"N.Nd ago"`` (one decimal under a
                                  week, integer beyond)
      * 1d – 29d future       → ``"in N.Nd"``
      * ≥ 30d either side     → full local datetime
                                  ``YYYY-MM-DD HH:MM ZZZ``
    """
    if target is None:
        return "—"
    now = now or datetime.now(tz=timezone.utc)
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    delta_sec = (target - now).total_seconds()
    abs_sec = abs(delta_sec)
    in_future = delta_sec > 0

    if abs_sec < 60:
        return "<1m"
    if abs_sec < 3600:
        mins = int(abs_sec // 60)
        return f"in {mins}m" if in_future else f"{mins}m ago"
    if abs_sec < 86400:
        hrs = abs_sec / 3600
        return f"in {hrs:.1f}h" if in_future else f"{hrs:.1f}h ago"
    if abs_sec < 30 * 86400:
        days = abs_sec / 86400
        # Under a week: keep a decimal so "1.5d ago" still tells you
        # something. Past a week, integer is enough.
        fmt = f"{days:.1f}d" if days < 7 else f"{int(days)}d"
        return f"in {fmt}" if in_future else f"{fmt} ago"
    local = target.astimezone()
    return local.strftime("%Y-%m-%d %H:%M %Z").rstrip()


def _ms_to_dt(ms: int | None) -> datetime | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _format_ohlcv_day(
    day: str | None, *, now: datetime | None = None,
) -> str:
    """Render an OHLCV trading day relative to today's NY date.

    Daily bars are keyed by the America/New_York trading ``day``, so freshness
    is coverage-based: compare the stored ISO date to the current NY date and
    report ``today`` / ``N days ago`` rather than a wall-clock write time.
    A malformed/empty day renders ``"—"``.
    """
    if not day:
        return "—"
    now = now or datetime.now(tz=_NY_TZ)
    try:
        bar_date = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        return f"latest day {day}"
    today = now.astimezone(_NY_TZ).date()
    delta_days = (today - bar_date).days
    if delta_days <= 0:
        rel = "today"
    elif delta_days == 1:
        rel = "1 day ago"
    else:
        rel = f"{delta_days} days ago"
    return f"latest day {day} ({rel})"


# ---- 1. Global install ------------------------------------------------


def _check_install() -> None:
    _section("Install")
    # Console-script renamed schwab_cli → schwab in PR #6.
    # Surface either binary if found; warn only when neither is present.
    binary = shutil.which("schwab") or shutil.which("schwab_cli")
    if binary:
        _ok("Global install", binary)
    else:
        _bad("Not on PATH")
        _hint("uv tool install --editable .")


# ---- 2. Server --------------------------------------------------------


_MCP_HOST = "127.0.0.1"
_MCP_PORT = 7234
_MCP_DEFAULT_URL = f"http://{_MCP_HOST}:{_MCP_PORT}"
# `schwab server` is the only daemon now (PR #52); MCP runs under it via
# `--enable-mcp`, so the launchd job to look for is the server's.
_MCP_LAUNCHD_LABEL = "com.schwab-cli.server"
_MCP_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{_MCP_LAUNCHD_LABEL}.plist"


def _mcp_status() -> dict | None:
    """Hit the MCP daemon's /admin/status endpoint with a quick timeout."""
    try:
        resp = httpx.get(f"{_MCP_DEFAULT_URL}/admin/status", timeout=2.0)
        if resp.status_code == 200:
            return resp.json()
    except (httpx.HTTPError, json.JSONDecodeError):
        pass
    return None


def _health_ok() -> bool:
    """True iff the daemon's ``/health`` endpoint returns ``{"ok": true}``."""
    try:
        resp = httpx.get(f"{_MCP_DEFAULT_URL}/health", timeout=2.0)
        return resp.status_code == 200 and bool(resp.json().get("ok"))
    except (httpx.HTTPError, json.JSONDecodeError):
        return False


def _launchctl_loaded(label: str) -> bool:
    """True if launchctl reports the label loaded."""
    try:
        out = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True, text=True, timeout=3,
        )
        return out.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def _claude_code_has_schwab() -> bool:
    """True if ~/.claude/settings.json registers our MCP server."""
    settings_path = Path.home() / ".claude" / "settings.json"
    if not settings_path.exists():
        return False
    try:
        data = json.loads(settings_path.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(data, dict):
        return False
    return "schwab" in (data.get("mcpServers") or {})


def _check_mcp() -> None:
    _section("Server")
    status = _mcp_status()
    if status:
        uptime_h = (status.get("uptime_sec", 0) or 0) / 3600
        # Probe /health on top of /admin/status so the line reflects the
        # daemon's own liveness check: "healthy" or "gone".
        health = "healthy" if _health_ok() else "gone"
        _ok(
            "Running",
            f"pid={status.get('pid')} bind={_MCP_HOST}:{_MCP_PORT} "
            f"uptime={uptime_h:.1f}h transport={status.get('transport', '?')} "
            f"{health}",
        )
    else:
        _bad("Not running", f"(expected on {_MCP_HOST}:{_MCP_PORT})")
        _hint("schwab server --enable-mcp  (the MCP server runs under `server`)")

    plist_loaded = _launchctl_loaded(_MCP_LAUNCHD_LABEL)
    plist_present = _MCP_PLIST.exists()
    if plist_present and plist_loaded:
        _ok("Auto-start (launchd)", str(_MCP_PLIST))
    elif plist_present:
        _bad("launchd plist present but not loaded")
        _hint(f"launchctl load -w {_MCP_PLIST}")
    else:
        _bad("launchd plist not installed")
        _hint("schwab server install --enable-mcp")

    if _claude_code_has_schwab():
        _ok("Registered with Claude Code", "~/.claude/settings.json")
    else:
        _bad("Not registered with Claude Code")
        _hint("schwab server register-claude")


# ---- 3. Schwab auth ---------------------------------------------------


def _check_auth() -> None:
    _section("Schwab auth")
    from schwab_cli import config as config_module
    from schwab_cli import session as session_module

    try:
        cfg = config_module.load()
    except Exception as e:
        _bad("Config malformed", str(e))
        _hint("schwab_cli setup")
        return

    if cfg is None:
        _bad("Config missing")
        _hint("schwab_cli setup")
        return
    _ok("Config", f"~/.config/schwab_cli/config.json (auth_flow={cfg.auth_flow})")

    if cfg.auto_login_command is not None:
        _ok(
            "Auto-login enabled",
            f"command={cfg.auto_login_command[0]} ... "
            f"(timeout={cfg.auto_login_timeout_seconds}s)",
        )
    else:
        _info("Auto-login disabled", "(manual OAuth on every run)")

    sess = session_module.load()
    if sess is None:
        _bad("No session")
        _hint("schwab_cli auth")
        return

    now = int(time.time())
    access_in = sess.expires_at - now
    refresh_in = sess.refresh_token_expires_at - now
    access_state = (
        f"valid for {access_in // 60}m" if access_in > 0
        else "expired (auto-refreshes on next call)"
    )
    refresh_state = (
        f"valid for {refresh_in // 86400}d {(refresh_in % 86400) // 3600}h"
        if refresh_in > 0 else "EXPIRED"
    )
    _ok("Session present", f"access {access_state}")
    if refresh_in > 0:
        _info("Refresh token", refresh_state)
    else:
        _bad("Refresh token expired")
        _hint("schwab_cli auth --force")


# ---- 4. Telegram notifications ----------------------------------------


_TELEGRAM_API = "https://api.telegram.org"


def _telegram_get_me(bot_token: str) -> dict | None:
    """Hit Telegram's getMe with a short timeout. Returns the bot info
    on success, ``None`` on any failure (bad token, network, …)."""
    try:
        resp = httpx.get(
            f"{_TELEGRAM_API}/bot{bot_token}/getMe", timeout=3.0,
        )
        if resp.status_code != 200:
            return None
        body = resp.json()
        if not body.get("ok"):
            return None
        return body.get("result")
    except (httpx.HTTPError, json.JSONDecodeError):
        return None


def _redact_token(token: str) -> str:
    """Show only the bot ID portion (before the colon) so we don't
    leak the secret half. Telegram tokens look like ``<id>:<secret>``."""
    head, _, _ = token.partition(":")
    return f"{head}:***" if head else "***"


def _check_telegram() -> None:
    _section("Telegram notifications")
    from schwab_cli.notify.config import load as load_notify_config

    cfg = load_notify_config()
    tg = cfg.telegram

    if not tg.configured:
        _bad("Not configured")
        _hint("schwab_cli notify configure  (or edit "
              "~/.config/schwab_cli/notification.json directly)")
        return

    _ok("Configured", f"bot {_redact_token(tg.bot_token)}, "
                      f"chat {tg.chat_id}")

    bot = _telegram_get_me(tg.bot_token)
    if bot:
        _ok("Bot reachable", f"@{bot.get('username', '?')} "
                             f"({bot.get('first_name', '?')})")
    else:
        _bad("Bot unreachable")
        _hint("verify bot_token + network; try `schwab_cli notify test`")

    n_events = len(tg.events)
    if n_events > 0:
        _info("Events subscribed", f"{n_events} ({', '.join(tg.events[:3])}"
                                   + ("…" if n_events > 3 else "") + ")")
    else:
        _info("Events subscribed", "0  (channel configured but no events "
                                   "will fire — add to "
                                   "~/.config/schwab_cli/notification.json)")

    _info("Rate limit", f"{tg.rate_limit_seconds}s per (channel, event)")


# ---- 5. Dataset -------------------------------------------------------


_LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
_SCHEDULER_PLIST = _LAUNCH_AGENTS_DIR / "com.schwab-cli.scheduler.plist"


def _check_dataset() -> None:
    _section("Dataset")
    from schwab_cli.storage import vol_history

    try:
        with vol_history.connect() as conn:
            sub_rows = conn.execute(
                """
                SELECT source, source_key, COUNT(*) AS n
                FROM subscriptions
                WHERE unsubscribed_at IS NULL
                GROUP BY source, source_key
                ORDER BY source, source_key
                """,
            ).fetchall()
            tier_rows = conn.execute(
                """
                SELECT tier, COUNT(*) AS n
                FROM ticker_state
                GROUP BY tier
                """,
            ).fetchall()
            ohlcv_longest = conn.execute(
                """
                SELECT symbol, COUNT(*) AS n, MIN(day) AS d
                FROM ohlcv_daily
                GROUP BY symbol
                ORDER BY n DESC, symbol ASC
                LIMIT 1
                """,
            ).fetchone()
            vol_longest_rows = conn.execute(
                """
                SELECT source, symbol, n, first_ms FROM (
                    SELECT
                        source,
                        symbol,
                        COUNT(*) AS n,
                        MIN(captured_at_ms) AS first_ms,
                        ROW_NUMBER() OVER (
                            PARTITION BY source
                            ORDER BY COUNT(*) DESC, symbol ASC
                        ) AS rn
                    FROM vol_snapshots
                    GROUP BY source, symbol
                )
                WHERE rn = 1
                ORDER BY source
                """,
            ).fetchall()
            vol_longest_rows = [
                _vol_leader_with_unique_days(conn, r)
                for r in vol_longest_rows
            ]
    except Exception as e:
        _bad("Dataset DB unreachable", str(e))
        return

    typer.echo("    Subscriptions")
    if not sub_rows:
        _info("(none)", "")
    for r in sub_rows:
        label = (
            f"{r['source']}={r['source_key']}"
            if r["source_key"] else r["source"]
        )
        typer.echo(f"      {label:<24} {r['n']}")

    typer.echo("    Tiers")
    tier_counts = {r["tier"]: r["n"] for r in tier_rows}
    for tier in ("ACTIVE", "GRACE", "WATCH", "FROZEN"):
        typer.echo(f"      {tier:<24} {tier_counts.get(tier, 0)}")

    typer.echo("    Market Data Stat")
    _print_market_data_stat(ohlcv_longest, vol_longest_rows)


# Job last_status values that mean the run ended badly; plus the
# coarse display states that signal a problem. Surfaced loudly.
_JOBS_FAIL_STATUSES = frozenset(
    {"failed", "auth-failed", "timeout", "interrupted"}
)
_JOBS_FAIL_STATES = frozenset({"error"})


def _check_data_sync_service() -> None:
    """Scheduled jobs section.

    Jobs are config files under ``~/.config/schwab_cli/jobs`` that the
    ``schwab server`` daemon runs in-process at their own cron times
    (state in ``jobs/.current/state.json``). This renders: the runner
    (the server), any leftover legacy scheduler, the per-job status,
    and a data-freshness read straight off the DB. Read-only.
    """
    _section("Data Sync Service")

    _print_runner()
    _print_legacy_leftover()
    _print_jobs_block()
    _print_data_freshness()


def _print_runner() -> None:
    """Jobs fire inside the ``schwab server`` daemon — confirm it's up."""
    if _launchctl_loaded(_MCP_LAUNCHD_LABEL):
        _ok("Runner", "jobs run by `schwab server` (com.schwab-cli.server)")
    else:
        _bad("Runner", "`schwab server` not running — scheduled jobs will NOT fire")
        _hint("schwab server install --enable-mcp")


def _print_legacy_leftover() -> None:
    """Warn only if the retired launchd scheduler is still installed."""
    if _SCHEDULER_PLIST.exists() or _launchctl_loaded("com.schwab-cli.scheduler"):
        _bad(
            "Legacy scheduler still present",
            "com.schwab-cli.scheduler is deprecated — remove it to "
            "avoid double runs",
        )
        _hint("schwab dataset cron uninstall")


def _job_is_failing(job: dict) -> bool:
    """True when a job's last run or display state signals a problem."""
    return (
        job.get("last_status") in _JOBS_FAIL_STATUSES
        or job.get("state") in _JOBS_FAIL_STATES
    )


def _print_jobs_block(*, config_dir: Path | None = None) -> None:
    """Render one stanza per promoted job from :func:`status_payload`.

    Failing jobs (bad ``last_status`` or ``error`` state) get a loud
    ``_bad`` header; healthy ones a plain/``_ok`` header. All I/O is
    guarded — any failure simply yields no jobs.
    """
    from schwab_cli.server.jobs.runtime import status_payload

    try:
        payload = status_payload(config_dir=config_dir)
    except (OSError, ValueError):
        payload = {"jobs": [], "server_running": False}

    jobs = payload.get("jobs") or []
    typer.echo("    Jobs")
    if not jobs:
        _info("Jobs", "(none configured)")
        _hint("schwab jobs init")
        return

    now = datetime.now(tz=timezone.utc)
    # "unloaded" is the only state with no live config behind it, so it
    # carries no cron/timezone to show.
    for job in sorted(jobs, key=lambda j: str(j.get("id"))):
        job_id = str(job.get("id"))
        state = str(job.get("state"))
        detail = f"— {state}"
        if state != "unloaded" and job.get("cron"):
            detail += f' cron "{job["cron"]}" {job.get("timezone", "")}'.rstrip()

        if _job_is_failing(job):
            _bad(job_id, detail)
        elif state == "scheduled":
            _ok(job_id, detail)
        else:
            typer.echo(f"    {job_id:<32} {detail}")

        last_dt = _ms_to_dt_epoch(job.get("last_run_at"))
        last_status = job.get("last_status") or "never"
        typer.echo(
            f"        last run {_format_relative_time(last_dt, now=now)} "
            f"({last_status})"
        )
        if state == "scheduled":
            next_dt = _ms_to_dt_epoch(job.get("next_run_at"))
            typer.echo(
                f"        next run {_format_relative_time(next_dt, now=now)}"
            )
        if job.get("outdated"):
            typer.secho(
                f"        ⚠ staged edit invalid: {job.get('edit_error')}",
                fg=typer.colors.YELLOW,
            )


def _ms_to_dt_epoch(ts: float | None) -> datetime | None:
    """Convert an epoch-seconds float (job timestamps) to aware UTC."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _print_data_freshness() -> None:
    """Last write per DB table — a data-staleness signal independent of
    how sync runs. Drawn straight off ``vol_history``; guarded."""
    from schwab_cli.dataset import store
    from schwab_cli.storage import vol_history

    typer.echo("    Data Freshness")
    try:
        with vol_history.connect() as conn:
            freshness = store.read_dataset_freshness(conn)
    except Exception as e:
        _bad("Data Freshness: DB unreachable", str(e))
        return

    now = datetime.now(tz=timezone.utc)
    # OHLCV is coverage-based: daily bars are keyed by trading day, so report
    # the latest day present (today / N days ago) rather than the write time.
    typer.echo(
        f"      {'OHLCV':<13} {_format_ohlcv_day(freshness.ohlcv_latest_day)}"
    )
    # Volatility & Account are point-in-time samples — last write is correct.
    rows = (
        ("Volatility", _ms_to_dt(freshness.volatility_ms)),
        ("Account", _ms_to_dt(freshness.account_ms)),
    )
    for task, last in rows:
        typer.echo(
            f"      {task:<13} last write "
            f"{_format_relative_time(last, now=now)}"
        )


def _vol_leader_with_unique_days(conn, row):
    """Enrich a vol-snapshot leader row with the count of unique NY
    trading days. This matches the dedup that ``read_recent_per_day``
    applies before computing IVP, so doctor's "days" column tracks the
    same number ``vol`` shows.
    """
    ms_rows = conn.execute(
        "SELECT captured_at_ms FROM vol_snapshots "
        "WHERE source = ? AND symbol = ?",
        (row["source"], row["symbol"]),
    ).fetchall()
    unique_days = len({
        datetime.fromtimestamp(
            r["captured_at_ms"] / 1000, tz=timezone.utc
        ).astimezone(_NY_TZ).date()
        for r in ms_rows
    })
    return {
        "source": row["source"],
        "symbol": row["symbol"],
        "n": row["n"],
        "first_ms": row["first_ms"],
        "unique_days": unique_days,
    }


def _print_market_data_stat(ohlcv_row, vol_rows) -> None:
    """Render per-group longest-series stats.

    For each group/source we show: raw row count, unique NY-trading-day
    count (matches what ``vol`` consumes), earliest capture date, and
    the leader symbol — a quick read on cache depth and IVP-readiness
    without having to query every ticker.
    """
    any_row = False
    if ohlcv_row and ohlcv_row["n"]:
        any_row = True
        typer.echo(
            f"      {'OHLCV (1 day)':<18} "
            f"{ohlcv_row['n']:>5} since {ohlcv_row['d']}"
        )
    if vol_rows:
        for i, r in enumerate(vol_rows):
            any_row = True
            label = "volatility" if i == 0 else ""
            first_day = datetime.fromtimestamp(
                r["first_ms"] / 1000, tz=timezone.utc
            ).date().isoformat()
            counts = f"{r['n']} rows / {r['unique_days']} days"
            typer.echo(
                f"      {label:<18} "
                f"{counts:>16} since {first_day} "
                f"({r['source']}, {r['symbol']})"
            )
    if not any_row:
        _info("(no samples yet)", "")


# ---- entry point ------------------------------------------------------


def run() -> None:
    _check_install()
    _check_mcp()
    _check_auth()
    _check_telegram()
    _check_dataset()
    _check_data_sync_service()
    typer.echo("")
