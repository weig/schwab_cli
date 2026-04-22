from __future__ import annotations

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


def _prompt_auth_flow(default: str) -> str:
    """Prompt for an auth_flow value; loop until input is one of AUTH_FLOWS."""
    options = "/".join(AUTH_FLOWS)
    typer.echo(f"  (one of: {options})")
    while True:
        entered = typer.prompt(
            "Auth flow",
            default=default,
            show_default=True,
        ).strip()
        if entered in AUTH_FLOWS:
            return entered
        typer.secho(
            f"Auth flow must be one of: {options}",
            fg=typer.colors.RED,
            err=True,
        )


def run() -> None:
    """Interactive setup: capture credentials and persist to ~/.config/schwab_cli/config.json."""
    try:
        _run()
    except (KeyboardInterrupt, typer.Abort):
        typer.echo("\nSetup cancelled.", err=True)
        raise typer.Exit(code=130)


def _run() -> None:
    path = config_path()
    typer.echo("Schwab CLI Setup")
    typer.echo(f"Config: {path}")
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

    auth_flow = _prompt_auth_flow(existing.auth_flow if existing else "local")

    code_relay_url: str | None = None
    if auth_flow == "code_relay":
        code_relay_url = _prompt_value(
            "Code relay URL",
            existing.code_relay_url if existing else None,
            sensitive=False,
            hint="e.g. https://oauth-relay.example.com/<uuid>/<secret>/wait",
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
    try:
        save(cfg)
    except OSError as e:
        typer.secho(f"Failed to write config: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from e

    typer.echo("")
    typer.secho(f"Saved to {path}.", fg=typer.colors.GREEN)
    typer.echo(f"Auto-login: {'enabled' if cfg.auto_login_enabled else 'disabled'}")
