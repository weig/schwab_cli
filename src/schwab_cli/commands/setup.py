from __future__ import annotations

import json
import shlex

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
    hide = sensitive and not existing
    while True:
        entered = typer.prompt(label, default="", show_default=False, hide_input=hide)
        if entered:
            return entered
        if existing:
            return existing
        typer.secho(f"{label} {error_suffix}", fg=typer.colors.RED, err=True)


def _prompt_auth_flow(default: str) -> str:
    """Prompt for ``auth_flow`` ∈ AUTH_FLOWS, re-prompting on invalid input."""
    typer.echo("")
    typer.echo("Auth flow (how schwab_cli captures the OAuth code):")
    typer.echo("  code_relay  — schwab_cli polls a remote relay URL")
    typer.echo("  client      — schwab_cli stands up a local HTTP listener")
    while True:
        entered = typer.prompt(
            "Auth flow",
            default=default,
            show_default=True,
        ).strip()
        if entered in AUTH_FLOWS:
            return entered
        typer.secho(
            f"Must be one of: {', '.join(AUTH_FLOWS)}.",
            fg=typer.colors.RED, err=True,
        )


def _prompt_auto_login_command(
    existing: tuple[str, ...] | None,
) -> tuple[str, ...] | None:
    """Prompt for the optional auto-login command (parsed via ``shlex.split``).

    Returns:
        * ``None`` when the user declines auto-login.
        * Tuple of argv tokens otherwise.
    """
    auto_default = existing is not None
    enable_auto = typer.confirm(
        "Configure auto-login subprocess (e.g. webauto-cli)?",
        default=auto_default,
    )
    if not enable_auto:
        return None

    if existing:
        typer.echo(
            f"  Current command: {shlex.join(existing)}  "
            "(press Enter to keep)"
        )
    typer.echo(
        "  Examples:\n"
        "    webauto-cli ~/.config/schwab_cli/scripts/auth_automation.py "
        "--env ~/.config/schwab_cli/auto_login.env"
    )
    while True:
        entered = typer.prompt(
            "Auto-login command", default="", show_default=False,
        ).strip()
        if entered:
            try:
                tokens = shlex.split(entered)
            except ValueError as e:
                typer.secho(
                    f"Could not parse: {e}", fg=typer.colors.RED, err=True,
                )
                continue
            if not tokens:
                typer.secho(
                    "Command is empty.", fg=typer.colors.RED, err=True,
                )
                continue
            return tuple(tokens)
        if existing:
            return existing
        typer.secho(
            "Auto-login command is required when this section is enabled.",
            fg=typer.colors.RED, err=True,
        )


def _prompt_timeout(existing: int) -> int:
    """Prompt for ``auto_login_timeout_seconds`` (positive int)."""
    while True:
        entered = typer.prompt(
            "Auto-login timeout in seconds",
            default=str(existing),
            show_default=True,
        ).strip()
        if not entered:
            return existing
        try:
            value = int(entered)
        except ValueError:
            typer.secho(
                "Must be a positive integer.",
                fg=typer.colors.RED, err=True,
            )
            continue
        if value <= 0:
            typer.secho(
                "Must be a positive integer.",
                fg=typer.colors.RED, err=True,
            )
            continue
        return value


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
        typer.secho(
            f"Existing config is unusable: {e}",
            fg=typer.colors.YELLOW, err=True,
        )
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

    auth_flow = _prompt_auth_flow(
        existing.auth_flow if existing else "code_relay",
    )

    code_relay_url: str | None = None
    if auth_flow == "code_relay":
        code_relay_url = _prompt_value(
            "Code Relay URL",
            existing.code_relay_url if existing else None,
            sensitive=False,
            hint="the URL the CLI polls for the captured OAuth code",
        )

    auto_login_command = _prompt_auto_login_command(
        existing.auto_login_command if existing else None,
    )

    if auto_login_command is not None:
        auto_login_timeout_seconds = _prompt_timeout(
            existing.auto_login_timeout_seconds if existing else 300,
        )
    else:
        auto_login_timeout_seconds = 300

    cfg = Config(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        auth_flow=auth_flow,
        code_relay_url=code_relay_url,
        auto_login_command=auto_login_command,
        auto_login_timeout_seconds=auto_login_timeout_seconds,
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
            f"Auto-login: {'enabled' if auto_login_command else 'disabled'} "
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
    typer.echo(
        f"Auto-login: {'enabled' if auto_login_command else 'disabled'}"
    )
