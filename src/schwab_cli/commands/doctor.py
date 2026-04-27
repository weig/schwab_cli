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
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import typer


# ---- printing helpers --------------------------------------------------


def _ok(label: str, detail: str = "") -> None:
    typer.secho(f"  ✓ ", fg=typer.colors.GREEN, nl=False)
    typer.echo(f"{label:<32} {detail}")


def _bad(label: str, detail: str = "") -> None:
    typer.secho(f"  ✗ ", fg=typer.colors.RED, nl=False)
    typer.echo(f"{label:<32} {detail}")


def _info(label: str, detail: str = "") -> None:
    typer.echo(f"    {label:<32} {detail}")


def _hint(text: str) -> None:
    typer.secho(f"    → {text}", fg=typer.colors.YELLOW)


def _section(title: str) -> None:
    typer.echo("")
    typer.secho(title, fg=typer.colors.BRIGHT_BLUE, bold=True)


# ---- 1. Global install ------------------------------------------------


def _check_install() -> None:
    _section("Install")
    binary = shutil.which("schwab_cli")
    if binary:
        _ok("Global install", binary)
    else:
        _bad("Not on PATH")
        _hint("uv tool install --editable .")


# ---- 2. MCP server ----------------------------------------------------


_MCP_DEFAULT_URL = "http://127.0.0.1:7234"
_MCP_LAUNCHD_LABEL = "com.schwab-cli.mcp"
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
        _hint("schwab_cli mcp --sse  (or `mcp start-service` if installed)")

    plist_loaded = _launchctl_loaded(_MCP_LAUNCHD_LABEL)
    plist_present = _MCP_PLIST.exists()
    if plist_present and plist_loaded:
        _ok("Auto-start (launchd)", str(_MCP_PLIST))
    elif plist_present:
        _bad("launchd plist present but not loaded")
        _hint(f"launchctl load -w {_MCP_PLIST}")
    else:
        _bad("launchd plist not installed")
        _hint("schwab_cli mcp install-service")

    if _claude_code_has_schwab():
        _ok("Registered with Claude Code", "~/.claude/settings.json")
    else:
        _bad("Not registered with Claude Code")
        _hint("schwab_cli mcp install")


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

    if cfg.auto_login_enabled:
        _ok("Auto-login enabled", _redact_user(cfg.username))
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


def _redact_user(username: str | None) -> str:
    if not username:
        return ""
    if username.startswith("op://"):
        return f"1Password ref ({username})"
    if "@" in username:
        local, _, dom = username.partition("@")
        return f"{local[:2]}***@{dom}"
    return f"{username[:2]}***"


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


_DATASET_INDICES_PLIST = (
    Path.home() / "Library" / "LaunchAgents"
    / "com.schwab-cli.dataset.indices.plist"
)
_DATASET_VOL_PLIST = (
    Path.home() / "Library" / "LaunchAgents"
    / "com.schwab-cli.dataset.volatility.plist"
)


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
            last_capture = conn.execute(
                "SELECT MAX(captured_at_ms) FROM vol_snapshots"
            ).fetchone()[0]
            indices_intent = conn.execute(
                "SELECT COUNT(*) FROM index_subscriptions "
                "WHERE unsubscribed_at IS NULL"
            ).fetchone()[0]
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

    typer.echo("    Last run")
    if last_capture:
        ts = datetime.fromtimestamp(last_capture / 1000, tz=timezone.utc)
        delta_h = (time.time() - last_capture / 1000) / 3600
        marker = "✓" if delta_h < 36 else "⚠"
        color = (typer.colors.GREEN if delta_h < 36
                 else typer.colors.YELLOW)
        typer.secho(
            f"      {marker} volatility samples present  "
            f"latest {ts.isoformat(timespec='minutes')} "
            f"({delta_h:.1f}h ago)",
            fg=color,
        )
    else:
        typer.secho(
            "      ✗ no volatility samples written yet",
            fg=typer.colors.RED,
        )
        _hint("schwab_cli dataset update --group volatility")

    typer.echo("    Cron jobs")
    _print_dataset_cron(
        "indices (weekly)",
        plist=_DATASET_INDICES_PLIST,
        label="com.schwab-cli.dataset.indices",
        needed=indices_intent > 0,
        not_needed_msg="(no index subscriptions)",
        install_cmd="schwab_cli dataset cron install --indices",
    )
    _print_dataset_cron(
        "volatility (daily)",
        plist=_DATASET_VOL_PLIST,
        label="com.schwab-cli.dataset.volatility",
        needed=bool(sub_rows),
        not_needed_msg="(no subscriptions yet)",
        install_cmd="schwab_cli dataset cron install --group volatility",
    )


def _print_dataset_cron(
    label_text: str,
    *,
    plist: Path,
    label: str,
    needed: bool,
    not_needed_msg: str,
    install_cmd: str,
) -> None:
    """Render one cron-job row at the Dataset-subsection indent level."""
    if plist.exists():
        if _launchctl_loaded(label):
            typer.secho("      ✓ ", fg=typer.colors.GREEN, nl=False)
            typer.echo(f"{label_text:<28} {plist}")
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
    typer.echo("")
