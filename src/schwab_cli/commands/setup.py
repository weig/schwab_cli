from __future__ import annotations

import json

import typer

from schwab_cli.config import (
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


# Only one auth flow is supported today (``code_relay``). The previous
# multi-choice prompt is removed because the menu would have a single
# option. Future additions (e.g. AuthServerHandler) will reintroduce a
# selector at the same insertion point in ``_run`` below.


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

    # Only one auth flow today; setup hardcodes ``code_relay`` and prompts
    # for its required ``code_relay_url``. When more handlers ship, this
    # block grows back into a selector — see the comment above.
    auth_flow = "code_relay"
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
            hint="stored in plain text at ~/.config/schwab_cli/config.json (mode 0600)",
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
