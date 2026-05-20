"""`notify` command group — setup, test, list.

Manages notification.json — the per-channel Telegram/Slack config
the MCP daemon uses to alert on auth, streamer, or daemon-level
events. Kept as its own file, parallel to ``config.json``, so
``schwab_cli setup`` never touches it.
"""

from __future__ import annotations

from pathlib import Path

import typer

from schwab_cli.notify import Notifier
from schwab_cli.notify import config as notify_config


def run_list(*, path: str | None) -> None:
    cfg_path = Path(path).expanduser() if path else notify_config.DEFAULT_PATH
    cfg = notify_config.load(cfg_path)
    summary = Notifier(cfg).channels_summary()
    lines: list[str] = [f"Notification config: {cfg_path}"]
    lines.append("")
    for channel, info in summary.items():
        configured = info.get("configured", False)
        status = "✔ configured" if configured else "— not configured"
        lines.append(f"{channel:<10} {status}")
        for k, v in info.items():
            if k == "configured":
                continue
            lines.append(f"  {k}: {v}")
    typer.echo("\n".join(lines))


def run_test(*, channel: str, path: str | None) -> None:
    """Fire a test notification and report the transport outcome.

    Bypasses the Notifier's subscription-list filter + rate limit —
    the test command's job is to probe the wire, not to honour policy.
    """
    from schwab_cli.notify import telegram as telegram_channel
    from schwab_cli.notify.events import level_of, summary_of

    cfg_path = Path(path).expanduser() if path else notify_config.DEFAULT_PATH
    cfg = notify_config.load(cfg_path)

    if channel not in ("telegram", "all"):
        typer.secho(
            f"Channel {channel!r} is not supported. "
            "Use 'telegram' (or 'all' when more channels ship).",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=2)

    if not cfg.telegram.configured:
        typer.secho(
            f"Telegram is not configured in {cfg_path}. "
            "Run `schwab_cli notify setup` first.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=1)

    text = telegram_channel.format_message(
        "test.hello",
        level_of("test.hello"),
        summary_of("test.hello"),
        {"invoked_by": "schwab_cli notify test", "user": _current_user()},
    )
    typer.echo("Dispatching test → Telegram…")
    ok, detail = telegram_channel.send(
        bot_token=cfg.telegram.bot_token,  # type: ignore[arg-type]
        chat_id=cfg.telegram.chat_id,      # type: ignore[arg-type]
        text=text,
    )
    if ok:
        typer.secho(
            f"  ✔ sent to chat_id={cfg.telegram.chat_id}",
            fg=typer.colors.GREEN,
        )
    else:
        typer.secho(
            f"  ✗ failed: {detail}",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=1)


def run_setup(*, channel: str, path: str | None) -> None:
    """Interactive setup. Channel is required so users don't get a
    generic prompt sequence that's hard to script around."""
    if channel != "telegram":
        typer.secho(
            "Only --channel telegram is supported in this build; "
            "Slack is TBD (Phase 2b).",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=2)

    cfg_path = Path(path).expanduser() if path else notify_config.DEFAULT_PATH
    existing = notify_config.load(cfg_path)

    typer.echo(
        "Telegram bot setup. You'll need:\n"
        "  - A bot token (from @BotFather)\n"
        "  - A chat_id (your user or group id to message)\n"
    )
    bot_token = typer.prompt(
        "Bot token", default=existing.telegram.bot_token or "", show_default=False,
    ).strip()
    chat_id = typer.prompt(
        "Chat id", default=existing.telegram.chat_id or "", show_default=False,
    ).strip()
    if not bot_token or not chat_id:
        typer.secho("bot_token and chat_id are required.",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    existing.telegram.bot_token = bot_token
    existing.telegram.chat_id = chat_id
    if not existing.telegram.events:
        existing.telegram.events = [
            "auth.auto_login.failed",
            "auth.auto_login.succeeded",
            "auth.refresh_expiring",
            "streamer.crash",
        ]
    written = notify_config.save(existing, cfg_path)
    typer.echo(f"wrote {written}")
    typer.echo(
        "Dispatching a test notification… (check your Telegram chat)"
    )
    Notifier(existing).emit(
        "test.hello", invoked_by="schwab_cli notify setup",
        user=_current_user(),
    )


def _current_user() -> str:
    import getpass
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"
