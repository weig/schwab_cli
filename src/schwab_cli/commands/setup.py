from __future__ import annotations

import json
import random
import shlex
import sys

import typer

from schwab_cli.config import (
    Config,
    ConfigError,
    config_path,
    load,
    mask_secret,
    save,
)
from schwab_cli.redirect_uri import is_loopback_https


def _default_callback_url() -> str:
    """Return the default loopback-HTTPS callback URL with a random port."""
    port = random.randint(15000, 20000)  # noqa: S311 — not security-sensitive
    return f"https://127.0.0.1:{port}/schwab/callback"


def _maybe_install_cert(url: str) -> None:
    """For a loopback-HTTPS callback URL, print a notice and install the
    local root certificate so the browser accepts the loopback redirect.

    No-op for non-loopback-HTTPS URLs. Skips (with a hint) when stdin is
    not a TTY so non-interactive setup never blocks on a sudo prompt.
    A keychain failure is surfaced as a warning rather than aborting setup.
    """
    if not is_loopback_https(url):
        return

    typer.echo("")
    typer.echo(
        "Auth uses a local callback: schwab_cli starts a tiny HTTPS server on "
        "127.0.0.1 to receive the OAuth redirect.\n"
        "This needs a one-time root certificate for 127.0.0.1 in your System "
        "keychain — you'll be asked for your login password next."
    )

    if not sys.stdin.isatty():
        typer.secho(
            "Non-interactive session — skipping certificate install. "
            "Run `schwab cert install` later before authenticating.",
            fg=typer.colors.YELLOW,
        )
        return

    from schwab_cli.cert.keychain import KeychainError
    from schwab_cli.commands.cert import _build_manager

    try:
        _build_manager().install()
    except (KeychainError, OSError) as e:
        # A cert failure must NOT abort setup — the config still gets written;
        # the user can `schwab cert install` before their first auth.
        typer.secho(
            f"Certificate install failed: {e}\n"
            "Run `schwab cert install` later before authenticating.",
            fg=typer.colors.YELLOW, err=True,
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
    typer.echo("")
    typer.echo(
        "  (Recommended: a loopback HTTPS callback like "
        "https://127.0.0.1:PORT/schwab/callback — schwab_cli captures the "
        "redirect locally.)"
    )
    redirect_uri = _prompt_value(
        "Callback URL",
        existing.redirect_uri if existing else _default_callback_url(),
        sensitive=False,
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
        auth_flow="local_server",
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

    if is_loopback_https(redirect_uri):
        typer.echo("")
        typer.echo(
            "This callback runs a local HTTPS server on 127.0.0.1; a one-time "
            "root certificate may be installed so the browser trusts it."
        )
        _maybe_install_cert(redirect_uri)

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
