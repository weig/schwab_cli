"""`schwab_cli doctor` — health check and install hints.

Surfaces install / config / runtime state across:

  • Global install (binary on PATH)
  • MCP server (running, autostart, transport, claude-code, launchd)
  • Schwab auth (config, auto-login, session validity)
  • Dataset (subscriptions per source, tier counts, last run, cron jobs)

For each item that is missing or inactive, prints the exact command
you'd run to fix it. Pure read-only — never mutates state.
"""
from __future__ import annotations

import json
import plistlib
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import typer


_NY_TZ = ZoneInfo("America/New_York")
_TARGET_NY_HOUR = 17  # market-data cron anchor — see sleep_until_ny


def _check_market_data_fire_time(plist_path: Path) -> tuple[bool, str]:
    """Verify the plist's next fire lands before NY 17:00 so that
    ``sleep_until_ny`` actually waits. Returns ``(ok, message)``.

    Drift after a system-TZ change is the main thing this catches.
    """
    nxt_utc = _next_calendar_interval_run(plist_path)
    if nxt_utc is None:
        return True, "(no upcoming fire — plist absent or unparseable)"
    nxt_ny = nxt_utc.astimezone(_NY_TZ)
    if nxt_ny.hour >= _TARGET_NY_HOUR:
        return False, (
            f"next fire is {nxt_ny.strftime('%H:%M %Z on %Y-%m-%d')} "
            f"— AFTER {_TARGET_NY_HOUR:02d}:00 ET; sleep_until_ny "
            f"will NO-OP and the cron will run immediately at the "
            f"wrong chain-snapshot moment. Likely cause: system "
            f"timezone changed since `dataset cron install` was run. "
            f"Fix: schwab_cli dataset cron install --group volatility"
        )
    return True, (
        f"next fire at {nxt_ny.strftime('%H:%M %Z on %Y-%m-%d')} "
        f"(before 17:00 ET ✓)"
    )


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


def _next_calendar_interval_run(
    plist_path: Path, *, now: datetime | None = None,
) -> datetime | None:
    """Parse a launchd plist and return the next firing datetime in
    UTC, or ``None`` if the plist is missing / unparseable / contains
    no future match within 7 days.
    """
    if not plist_path.exists():
        return None
    try:
        spec = plistlib.loads(plist_path.read_bytes())
    except Exception:
        return None
    intervals = spec.get("StartCalendarInterval")
    if not intervals:
        return None
    if isinstance(intervals, dict):       # single-entry sugar form
        intervals = [intervals]

    now = (now or datetime.now(tz=timezone.utc)).astimezone()
    candidates: list[datetime] = []
    for entry in intervals:
        nxt = _next_match_for_entry(entry, now)
        if nxt is not None:
            candidates.append(nxt)
    if not candidates:
        return None
    return min(candidates).astimezone(timezone.utc)


def _next_match_for_entry(
    entry: dict, now: datetime,
) -> datetime | None:
    """Walk minute-by-minute up to 7 days; return first match.

    launchd's Weekday convention: 0 = Sunday, 6 = Saturday. Python's
    ``isoweekday()`` returns 1=Mon … 7=Sun, so ``isoweekday() % 7``
    matches launchd's numbering.
    """
    cur = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(7 * 24 * 60):
        if _matches_calendar_entry(cur, entry):
            return cur
        cur += timedelta(minutes=1)
    return None


def _matches_calendar_entry(dt: datetime, entry: dict) -> bool:
    if "Minute" in entry and dt.minute != entry["Minute"]:
        return False
    if "Hour" in entry and dt.hour != entry["Hour"]:
        return False
    if "Day" in entry and dt.day != entry["Day"]:
        return False
    if "Month" in entry and dt.month != entry["Month"]:
        return False
    if "Weekday" in entry:
        if (dt.isoweekday() % 7) != entry["Weekday"]:
            return False
    return True


def _last_market_data_run_at(conn) -> datetime | None:
    """Latest captured_at_ms across vol_snapshots — proxy for "last
    successful volatility cron run". A live ``vol`` invocation also
    counts, which is fine: both are proof the writer side works."""
    row = conn.execute(
        "SELECT MAX(captured_at_ms) FROM vol_snapshots"
    ).fetchone()
    if not row or row[0] is None:
        return None
    return datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc)


import re as _re


_INDICES_DELTA_RE = _re.compile(
    r"^(?P<ts>\S+)\s+\[indices\]\s+(?P<sym>[A-Za-z0-9._-]+):\s+"
    r"total=(?P<total>\d+)\s+\+(?P<added>\d+)\s+-(?P<removed>\d+)\s*$"
)
_INDICES_FINISHED_RE = _re.compile(
    r"^(?P<ts>\S+)\s+\[indices\]\s+finished,\s+(?P<processed>\d+)\s+"
    r"indices processed,\s+(?P<errored>\d+)\s+errored"
    r"(?:\s+\((?P<note>[^)]+)\))?"
)
_INDICES_START_RE = _re.compile(r"^\S+\s+\[indices\]\s+start\b")


def _parse_last_indices_run() -> dict | None:
    """Find the most recent ``[indices] finished`` block in the audit
    log and return its timestamp + per-index deltas.

    Returns ``{"finished_at": datetime, "errored": int,
               "deltas": [(symbol, added, removed, total), ...]}``
    or ``None`` when no completed indices run is on file.

    Authoritative source for "last indices run" — supersedes the old
    ``MAX(subscribed_at) WHERE source='indices'`` proxy that stayed
    stale when the upstream member set was unchanged across runs.
    """
    from schwab_cli.dataset.audit_log import audit_log_path
    # RotatingFileHandler rolls scheduler.log → .1 → .2 → .3. If the
    # active file has no [indices] finished line (e.g. just rotated),
    # fall through to the backups newest-first so we still surface a
    # stale-but-real last run instead of showing "—".
    active = audit_log_path()
    candidates = [active, *(active.with_name(active.name + f".{i}")
                            for i in range(1, 4))]

    finished_idx = -1
    finished_match = None
    lines: list[str] = []
    for path in candidates:
        try:
            lines = path.read_text().splitlines()
        except (FileNotFoundError, OSError):
            continue
        for i in range(len(lines) - 1, -1, -1):
            m = _INDICES_FINISHED_RE.match(lines[i])
            if m:
                finished_idx = i
                finished_match = m
                break
        if finished_match is not None:
            break
    if finished_match is None:
        return None

    finished_at = datetime.strptime(
        finished_match.group("ts"), "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=timezone.utc)
    errored = int(finished_match.group("errored"))
    processed = int(finished_match.group("processed"))
    note = finished_match.group("note")  # e.g. "skipped: within max-age threshold"

    deltas: list[tuple[str, int, int, int]] = []
    for j in range(finished_idx - 1, -1, -1):
        if _INDICES_START_RE.match(lines[j]):
            break  # crossed into the previous run's block
        m = _INDICES_DELTA_RE.match(lines[j])
        if m:
            deltas.append((
                m.group("sym"),
                int(m.group("added")),
                int(m.group("removed")),
                int(m.group("total")),
            ))
            if len(deltas) >= processed:
                break
    deltas.reverse()
    return {
        "finished_at": finished_at,
        "errored":     errored,
        "deltas":      deltas,
        "note":        note,
    }


def _last_indices_run_at(conn=None) -> datetime | None:
    """Datetime of the most recent successful indices run.

    Backed by the audit log so it advances on every run, not just on
    constituent changes. The ``conn`` parameter is kept for legacy
    callers but ignored.
    """
    info = _parse_last_indices_run()
    return info["finished_at"] if info else None


def _format_indices_deltas(deltas: list[tuple[str, int, int, int]]) -> str:
    """Render the per-index delta summary as a git-style colored
    ``[SPX: +2 -2, NQ: 0, DJI: +1]`` string.

    ``+N`` green, ``-N`` red, ``0`` (no change either way) dim. When
    both ``added`` and ``removed`` are zero, collapse to ``0`` instead
    of ``+0 -0`` so the eye lands on the changes.
    """
    if not deltas:
        return ""
    parts: list[str] = []
    for sym, added, removed, _total in deltas:
        if added == 0 and removed == 0:
            chunk = typer.style("0", dim=True)
        else:
            pieces: list[str] = []
            if added:
                pieces.append(typer.style(f"+{added}", fg=typer.colors.GREEN))
            if removed:
                pieces.append(typer.style(f"-{removed}", fg=typer.colors.RED))
            chunk = " ".join(pieces)
        parts.append(f"{sym}: {chunk}")
    return "[" + ", ".join(parts) + "]"


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


# ---- 2. MCP server ----------------------------------------------------


_MCP_DEFAULT_URL = "http://127.0.0.1:7234"
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
    _section("MCP server")
    status = _mcp_status()
    if status:
        uptime_h = (status.get("uptime_sec", 0) or 0) / 3600
        _ok(
            "Running",
            f"pid={status.get('pid')} uptime={uptime_h:.1f}h "
            f"transport={status.get('transport', '?')}",
        )
    else:
        _bad("Not running")
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


def _check_data_sync_service() -> None:
    """Top-level section showing the unified scheduler plist + each
    child task's last/next run. Replaces the prior per-task cron
    rows; the tasks aren't independent cron jobs anymore, they're
    children pspawned by the one scheduler."""
    _section("Data Sync Service")

    typer.echo("    Scheduler")
    _print_scheduler_block(_SCHEDULER_PLIST)

    typer.echo("    Sync Scope")
    _print_sync_scope()

    # Last-run marker from the unified scheduler. ``last_run.json``
    # captures per-job exit codes so a Telegram-down failure is
    # still visible offline. Surface whatever the most recent run
    # wrote.
    _print_last_run_marker()


def _print_scheduler_block(plist: Path) -> None:
    """Render the scheduler row: plist status + next-fire time +
    drift check inline. No per-task data here — the tasks are
    listed separately under Sync Scope."""
    if not plist.exists():
        typer.secho("      ✗ ", fg=typer.colors.RED, nl=False)
        typer.echo("not installed")
        _hint("schwab dataset cron install")
        return
    if not _launchctl_loaded("com.schwab-cli.scheduler"):
        typer.secho("      ✗ ", fg=typer.colors.RED, nl=False)
        typer.echo("plist present but not loaded")
        typer.secho(f"        → launchctl load -w {plist}",
                    fg=typer.colors.YELLOW)
        return

    typer.secho("      ✓ ", fg=typer.colors.GREEN, nl=False)
    typer.echo(f"{plist.name}")
    now = datetime.now(tz=timezone.utc)
    next_run = _next_calendar_interval_run(plist, now=now)
    typer.echo(
        f"          next fire   "
        f"{_format_relative_time(next_run, now=now)}"
    )
    # Inline fire-time drift check — when the system TZ changes
    # after install, the plist's UTC+old-tz fire hour ends up firing
    # at a different NY-clock moment. sleep_until_ny can recover
    # from "fire early" but not "fire AFTER target" (silent no-op).
    md_ok, md_msg = _check_market_data_fire_time(plist)
    if md_ok:
        typer.secho(f"                      ✓ {md_msg}",
                    fg=typer.colors.GREEN)
    else:
        typer.secho(f"                      ✗ {md_msg}",
                    fg=typer.colors.RED)


# Each scheduler child anchors to a NY hour internally via
# sleep_until_ny. ``next run`` for the child is the next time that
# hour passes — not when launchd fires the scheduler.
_TASK_ANCHOR_HOUR = {
    "OHLCV":      17,
    "Volatility": 17,
    "Indices":    18,
    "Account":    17,
}


def _print_sync_scope() -> None:
    """One row per data-sync task: last write to the relevant table
    + next NY-anchored run time."""
    from schwab_cli.storage import vol_history

    last_by_task: dict[str, datetime | None] = {
        "OHLCV": None, "Volatility": None,
        "Indices": None, "Account": None,
    }
    try:
        with vol_history.connect() as conn:
            ohlcv_ms = conn.execute(
                "SELECT MAX(captured_at_ms) FROM ohlcv_daily"
            ).fetchone()[0]
            vol_ms = conn.execute(
                "SELECT MAX(captured_at_ms) FROM vol_snapshots"
            ).fetchone()[0]
            acct_ms = conn.execute(
                "SELECT MAX(captured_at_ms) FROM account_nav_daily"
            ).fetchone()[0]
            last_by_task["OHLCV"] = _ms_to_dt(ohlcv_ms)
            last_by_task["Volatility"] = _ms_to_dt(vol_ms)
            last_by_task["Account"] = _ms_to_dt(acct_ms)
    except Exception as e:
        _bad("Sync Scope: DB unreachable", str(e))
        return

    indices_run = _parse_last_indices_run()
    last_by_task["Indices"] = (
        indices_run["finished_at"] if indices_run else None
    )

    now = datetime.now(tz=timezone.utc)
    for task, hour in _TASK_ANCHOR_HOUR.items():
        last = last_by_task[task]
        next_run = _next_ny_hour(hour, now=now)
        suffix = ""
        if task == "Indices" and indices_run:
            if indices_run.get("note"):
                # Skip sentinel — no deltas to render. Show the reason
                # in dim so it's clear the run completed without
                # hitting the upstream provider.
                suffix = ", " + typer.style(
                    indices_run["note"], dim=True,
                )
            else:
                deltas_str = _format_indices_deltas(indices_run["deltas"])
                if deltas_str:
                    suffix = f", {deltas_str}"
                if indices_run["errored"]:
                    suffix += typer.style(
                        f" ({indices_run['errored']} errored)",
                        fg=typer.colors.RED,
                    )
        typer.echo(
            f"      {task:<13} last run "
            f"{_format_relative_time(last, now=now)}{suffix}"
        )
        typer.echo(
            f"                    next run {_format_relative_time(next_run, now=now)}"
        )


def _ms_to_dt(ms: int | None) -> datetime | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _next_ny_hour(hour: int, *, now: datetime) -> datetime:
    """Next occurrence of NY hour ``H:00`` strictly after ``now``.
    Used for per-task next-run estimates — each scheduler child
    sleep_until_ny to its anchor hour, so the actual work time is
    independent of when launchd fires the scheduler."""
    from zoneinfo import ZoneInfo
    ny = ZoneInfo("America/New_York")
    now_ny = now.astimezone(ny)
    target = now_ny.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now_ny:
        target += timedelta(days=1)
    return target.astimezone(timezone.utc)


def _print_last_run_marker() -> None:
    """Read the offline failure marker the scheduler writes after
    each run and surface its per-job status. Quiet on success, loud
    on failure — this is the backstop when Telegram alerts didn't
    land."""
    import json as _json

    from schwab_cli.dataset.sync_scheduler import _last_run_path
    try:
        path = _last_run_path()
    except Exception:
        return
    if not path.exists():
        return
    try:
        payload = _json.loads(path.read_text())
    except (OSError, _json.JSONDecodeError):
        return
    if payload.get("overall_succeeded"):
        return  # quiet path — failure marker is the interesting one
    failed = [
        j for j in (payload.get("jobs") or [])
        if j.get("returncode") not in (0, None) or j.get("timed_out")
    ]
    if not failed:
        return
    finished = payload.get("finished_at", "")
    _bad(
        f"last scheduler run had failures ({finished})",
        "see ~/.config/schwab_cli/last_run.json for per-job tails",
    )
    for j in failed:
        outcome = "timeout" if j.get("timed_out") else f"exit {j['returncode']}"
        typer.secho(f"        ✗ {j['name']} — {outcome}",
                    fg=typer.colors.RED)


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


def _print_dataset_cron(
    label_text: str,
    *,
    plist: Path,
    label: str,
    needed: bool,
    not_needed_msg: str,
    install_cmd: str,
    last_run: datetime | None = None,
    products: str | None = None,
) -> None:
    """Render one cron-job row at the Dataset-subsection indent level.

    When ``products`` is provided (market-data row), it renders as a
    separate ``group`` sub-line below the title so the plist filename
    column stays vertically aligned with the indices row above.
    """
    if plist.exists():
        if _launchctl_loaded(label):
            typer.secho("      ✓ ", fg=typer.colors.GREEN, nl=False)
            typer.echo(f"{label_text:<28} {plist.name}")
            if products:
                typer.echo(f"          group     {products}")
            now = datetime.now(tz=timezone.utc)
            next_run = _next_calendar_interval_run(plist, now=now)
            typer.echo(
                f"          last run  "
                f"{_format_relative_time(last_run, now=now)}"
            )
            typer.echo(
                f"          next run  "
                f"{_format_relative_time(next_run, now=now)}"
            )
        else:
            typer.secho("      ✗ ", fg=typer.colors.RED, nl=False)
            typer.echo(f"{label_text:<28} plist present but not loaded")
            typer.secho(f"        → launchctl load -w {plist}",
                        fg=typer.colors.YELLOW)
    elif needed:
        typer.secho("      ✗ ", fg=typer.colors.RED, nl=False)
        typer.echo(f"{label_text:<28} not installed")
        typer.secho(f"        → {install_cmd}", fg=typer.colors.YELLOW)
    else:
        typer.echo(f"      · {label_text:<28} {not_needed_msg}")


# ---- entry point ------------------------------------------------------


def run() -> None:
    _check_install()
    _check_mcp()
    _check_auth()
    _check_telegram()
    _check_dataset()
    _check_data_sync_service()
    typer.echo("")
