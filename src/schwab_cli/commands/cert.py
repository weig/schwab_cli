"""`cert` command group — install / uninstall / status of the local CA.

Manages the local callback TLS certificate used by the OAuth redirect
listener on ``https://127.0.0.1``. Installing trusts a one-time root CA in
the macOS System keychain so the browser accepts the loopback redirect
without a security warning.

The CA private key is transient by default (generated in memory, never
written) — pass ``--persist-ca-key`` to keep it on disk for leaf renewal.
"""

from __future__ import annotations

import sys
from typing import NoReturn

import typer

from schwab_cli.cert.keychain import KeychainError, MacTrustStore
from schwab_cli.cert.manager import CertManager
from schwab_cli.cert.store import ManifestCorruptError

app = typer.Typer(
    help="Manage the local callback TLS certificate (127.0.0.1).",
    no_args_is_help=True,
)


def _build_manager() -> CertManager:
    """Factory seam — patched in tests to avoid touching the real keychain."""
    return CertManager(MacTrustStore())


def _fail(prefix: str, e: Exception) -> NoReturn:
    """Print an actionable error (appending keychain stderr when present) and exit 1."""
    detail = ""
    stderr = getattr(e, "stderr", "")
    if stderr and stderr.strip():
        detail = f"\n{stderr.rstrip()}"
    typer.secho(f"{prefix}: {e}{detail}", fg=typer.colors.RED, err=True)
    raise typer.Exit(1) from e


def _require_darwin() -> None:
    """Exit non-zero with a helpful message when not running on macOS."""
    if sys.platform != "darwin":
        typer.secho(
            "This command is macOS only — it relies on the macOS (darwin) "
            "System keychain and the `security` tool.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)


def _confirm_or_abort(yes: bool, action: str) -> None:
    """Gate a destructive keychain action behind explicit consent.

    With ``--yes`` we proceed immediately. Otherwise we prompt for
    confirmation. A real TTY answers the prompt directly; when stdin is not a
    TTY and no answer is piped in, ``typer.confirm`` aborts (EOF) — running
    ``sudo`` non-interactively without ``--yes`` is ambiguous, so we surface a
    hint and refuse. An explicit "no" aborts cleanly without calling the
    manager.
    """
    if yes:
        return
    try:
        confirmed = typer.confirm(f"Proceed to {action}?")
    except typer.Abort:
        typer.secho(
            f"Refusing to {action} non-interactively. Re-run with --yes, "
            "or run this command in an interactive terminal.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1) from None
    if not confirmed:
        typer.echo("Aborted.")
        raise typer.Exit(0)


@app.command("install")
def install(
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt."
    ),
    persist_ca_key: bool = typer.Option(
        False,
        "--persist-ca-key",
        help="Keep the CA private key on disk (enables leaf renewal).",
    ),
) -> None:
    """Install and trust the local CA + leaf certificate for 127.0.0.1."""
    _require_darwin()

    typer.echo(
        "Installing a one-time root certificate for 127.0.0.1 into the "
        "System keychain.\nYou may be asked for your login (sudo) password "
        "(only when the CA is not already trusted)."
    )
    if persist_ca_key:
        typer.secho(
            "Note: --persist-ca-key keeps the CA private key on disk. The "
            "default (transient) is more secure; only persist it if you need "
            "automatic leaf renewal.",
            fg=typer.colors.YELLOW,
        )

    _confirm_or_abort(yes, "install the local CA")

    manager = _build_manager()
    try:
        leaf = manager.install(persist_ca_key=persist_ca_key)
    except (KeychainError, ManifestCorruptError) as e:
        _fail("Failed to install the local CA", e)

    typer.secho(
        f"Success — the local CA is now trusted.\n"
        f"Leaf certificate: {leaf.cert}\n"
        f"Leaf key:         {leaf.key}",
        fg=typer.colors.GREEN,
    )


@app.command("uninstall")
def uninstall(
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt."
    ),
    by_label: bool = typer.Option(
        False,
        "--by-label",
        help="Remove by certificate label when no manifest is found.",
    ),
) -> None:
    """Remove the local CA from the trust store and delete on-disk artifacts."""
    _require_darwin()

    typer.echo(
        "This removes the Schwab CLI Local CA from the macOS System keychain "
        "and deletes the on-disk certificate files.\nYou may be asked for your "
        "login (sudo) password."
    )

    _confirm_or_abort(yes, "uninstall the local CA")

    manager = _build_manager()
    try:
        msg = manager.uninstall(by_label=by_label)
    except (KeychainError, ManifestCorruptError) as e:
        _fail("Failed to uninstall the local CA", e)

    typer.echo(msg)


@app.command("status")
def status() -> None:
    """Show the current install state of the local CA + leaf certificate."""
    try:
        st = _build_manager().status()
    except (KeychainError, ManifestCorruptError) as e:
        _fail("Failed to read certificate status", e)

    valid_until = st.leaf_valid_until if st.leaf_valid_until is not None else "—"
    trusted = "trusted" if st.ca_trusted else "not trusted"
    typer.echo(
        "Local callback certificate status:\n"
        f"  CA:                {trusted}\n"
        f"  Leaf cert present: {_yn(st.leaf_cert_present)}\n"
        f"  Leaf key present:  {_yn(st.leaf_key_present)}\n"
        f"  Leaf valid until:  {valid_until}\n"
        f"  Manifest present:  {_yn(st.manifest_present)}"
    )


def _yn(value: bool) -> str:
    return "yes" if value else "no"
