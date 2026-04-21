from __future__ import annotations

import typer

from schwab_cli.config import Config, ConfigError, config_path, load, mask_secret, save


def _prompt_required(label: str, existing: str | None, *, sensitive: bool) -> str:
    """Prompt until the user provides a non-empty value (or keeps existing)."""
    default_display = mask_secret(existing) if (existing and sensitive) else existing
    while True:
        entered = typer.prompt(label, default=default_display or "", show_default=bool(default_display))
        # If the user accepted the masked default, restore the real value.
        if sensitive and existing and entered == default_display:
            return existing
        if entered:
            return entered
        typer.secho(f"{label} is required.", fg=typer.colors.RED, err=True)


def _prompt_optional_credential(
    label: str,
    existing: str | None,
    *,
    sensitive: bool,
    hint: str | None = None,
) -> str:
    """Prompt for a value; empty is not allowed when auto-login is being set."""
    if hint:
        typer.echo(f"  ({hint})")
    default_display = mask_secret(existing) if (existing and sensitive) else existing
    while True:
        entered = typer.prompt(label, default=default_display or "", show_default=bool(default_display))
        if sensitive and existing and entered == default_display:
            return existing
        if entered:
            return entered
        typer.secho(f"{label} is required when auto-login is enabled.", fg=typer.colors.RED, err=True)


def run() -> None:
    """Interactive setup: capture credentials and persist to ~/.config/schwab_cli/config.json."""
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

    client_id = _prompt_required(
        "Client ID",
        existing.client_id if existing else None,
        sensitive=False,
    )
    client_secret = _prompt_required(
        "Client Secret",
        existing.client_secret if existing else None,
        sensitive=True,
    )

    auto_default = bool(existing and existing.auto_login_enabled)
    enable_auto = typer.confirm("Enable automatic login?", default=auto_default)

    username: str | None = None
    password: str | None = None
    if enable_auto:
        username = _prompt_optional_credential(
            "Username",
            existing.username if existing else None,
            sensitive=False,
        )
        password = _prompt_optional_credential(
            "Password",
            existing.password if existing else None,
            sensitive=True,
            hint="supports op:// 1Password references",
        )

    cfg = Config(
        client_id=client_id,
        client_secret=client_secret,
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
