from __future__ import annotations

import json
import sys

import typer

from schwab_cli.config import (
    AUTH_FLOWS,
    Config,
    ConfigError,
    config_path,
    load,
    mask_secret,
    save,
)


def _prompt_value(
    label: str,
    existing: str | None,
    *,
    sensitive: bool,
    error_suffix: str = "is required.",
    hint: str | None = None,
) -> str:
    """Prompt until the user provides a non-empty value (or keeps existing on Enter).

    When `existing` is set, the current value (masked if sensitive) is shown as a hint
    and an empty response keeps it. When `existing` is None, empty re-prompts.
    """
    if hint:
        typer.echo(f"  ({hint})")
    if existing:
        shown = mask_secret(existing) if sensitive else existing
        typer.echo(f"  Current {label}: {shown}  (press Enter to keep)")
    # Hide echo only for fresh sensitive entry; when keeping an existing value,
    # showing nothing would leave the user wondering if input was captured.
    hide = sensitive and not existing
    while True:
        entered = typer.prompt(label, default="", show_default=False, hide_input=hide)
        if entered:
            return entered
        if existing:
            return existing
        typer.secho(f"{label} {error_suffix}", fg=typer.colors.RED, err=True)


_AUTH_FLOW_DESCRIPTIONS: dict[str, str] = {
    "client": (
        "Schwab redirects to your loopback redirect_uri (e.g. "
        "https://127.0.0.1:8443). The CLI reads the OAuth code straight "
        "from the browser's URL bar. No external server required."
    ),
    "code_relay": (
        "Your redirect_uri points to a pre-deployed public relay "
        "(e.g. a Cloudflare Worker). The relay catches the callback and "
        "the CLI polls it for the OAuth code. Use this when the loopback "
        "redirect isn't reachable (remote shells, mobile login, etc.)."
    ),
}


def _prompt_auth_flow(default: str) -> str:
    """Prompt for an auth_flow.

    In an interactive terminal, shows an arrow-key-navigable select menu
    (one choice per flow). When stdin is not a TTY — i.e. tests or pipes —
    falls back to a numbered text prompt that accepts either the flow
    name or its menu number.
    """
    if sys.stdin.isatty() and sys.stdout.isatty():
        return _prompt_auth_flow_tty(default)
    return _prompt_auth_flow_text(default)


def _prompt_auth_flow_tty(default: str) -> str:
    """Arrow-key auth_flow selector for interactive terminals."""
    import questionary

    choices = [
        questionary.Choice(
            title=f"{name}  —  {_AUTH_FLOW_DESCRIPTIONS[name].split('. ')[0]}.",
            value=name,
        )
        for name in AUTH_FLOWS
    ]
    answer = questionary.select(
        "Auth flow (how the CLI captures the OAuth `code`):",
        choices=choices,
        default=default if default in AUTH_FLOWS else None,
        instruction="(↑/↓ to move, Enter to select)",
        use_shortcuts=True,
    ).ask()
    if answer is None:
        # Ctrl-C inside questionary returns None.
        raise typer.Abort()
    return answer


def _prompt_auth_flow_text(default: str) -> str:
    """Numbered-menu fallback for non-TTY stdin (tests, pipes, CI)."""
    typer.echo("")
    typer.echo("Auth flow — how the CLI captures the OAuth `code`:")
    for idx, name in enumerate(AUTH_FLOWS, start=1):
        typer.echo("")
        typer.echo(f"  {idx}. {name}")
        for line in _AUTH_FLOW_DESCRIPTIONS[name].split(". "):
            line = line.strip().rstrip(".")
            if line:
                typer.echo(f"     {line}.")
    typer.echo("")

    while True:
        entered = typer.prompt(
            "Auth flow (name or number)",
            default=default,
            show_default=True,
        ).strip()
        if entered.isdigit():
            idx = int(entered)
            if 1 <= idx <= len(AUTH_FLOWS):
                return AUTH_FLOWS[idx - 1]
        if entered in AUTH_FLOWS:
            return entered
        typer.secho(
            f"Auth flow must be one of: {', '.join(AUTH_FLOWS)} "
            f"(or a number 1-{len(AUTH_FLOWS)}).",
            fg=typer.colors.RED,
            err=True,
        )


def run(*, dry_run: bool = False) -> None:
    """Interactive setup: capture credentials and persist to ~/.config/schwab_cli/config.json.

    When ``dry_run`` is true, the prompts run normally but the resulting
    config is printed to stdout instead of being written to disk.
    """
    try:
        _run(dry_run=dry_run)
    except (KeyboardInterrupt, typer.Abort):
        typer.echo("\nSetup cancelled.", err=True)
        raise typer.Exit(code=130)


def _run(*, dry_run: bool) -> None:
    path = config_path()
    typer.echo("Schwab CLI Setup")
    typer.echo(f"Config: {path}")
    if dry_run:
        typer.secho("(dry-run: nothing will be written)", fg=typer.colors.YELLOW)
    typer.echo("")

    try:
        existing = load()
    except ConfigError as e:
        typer.secho(f"Existing config is unusable: {e}", fg=typer.colors.YELLOW, err=True)
        overwrite = typer.confirm("Overwrite with new setup?", default=False)
        if not overwrite:
            raise typer.Exit(code=0)
        existing = None

    client_id = _prompt_value(
        "Client ID",
        existing.client_id if existing else None,
        sensitive=False,
    )
    client_secret = _prompt_value(
        "Client Secret",
        existing.client_secret if existing else None,
        sensitive=True,
    )
    redirect_uri = _prompt_value(
        "Redirect URI",
        existing.redirect_uri if existing else None,
        sensitive=False,
    )

    auth_flow = _prompt_auth_flow(existing.auth_flow if existing else "client")

    code_relay_url: str | None = None
    if auth_flow == "code_relay":
        code_relay_url = _prompt_value(
            "Code Relay URL",
            existing.code_relay_url if existing else None,
            sensitive=False,
            hint="the URL the CLI polls for the captured OAuth code",
        )

    auto_default = bool(existing and existing.auto_login_enabled)
    enable_auto = typer.confirm("Enable automatic login?", default=auto_default)

    username: str | None = None
    password: str | None = None
    if enable_auto:
        username = _prompt_value(
            "Username",
            existing.username if existing else None,
            sensitive=False,
            error_suffix="is required when auto-login is enabled.",
        )
        password = _prompt_value(
            "Password",
            existing.password if existing else None,
            sensitive=True,
            error_suffix="is required when auto-login is enabled.",
            hint="supports op:// 1Password references",
        )

    cfg = Config(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        auth_flow=auth_flow,
        code_relay_url=code_relay_url,
        username=username,
        password=password,
    )

    if dry_run:
        typer.echo("")
        typer.secho(
            f"--- dry-run: would write {path} ---",
            fg=typer.colors.YELLOW,
        )
        typer.echo(json.dumps(cfg.to_payload(), indent=2))
        typer.secho("--- not saved ---", fg=typer.colors.YELLOW)
        typer.echo(
            f"Auto-login: {'enabled' if cfg.auto_login_enabled else 'disabled'} "
            "(dry-run)"
        )
        return

    try:
        save(cfg)
    except OSError as e:
        typer.secho(f"Failed to write config: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from e

    typer.echo("")
    typer.secho(f"Saved to {path}.", fg=typer.colors.GREEN)
    typer.echo(f"Auto-login: {'enabled' if cfg.auto_login_enabled else 'disabled'}")
