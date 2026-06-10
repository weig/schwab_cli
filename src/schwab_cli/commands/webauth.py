"""``schwab webauth`` — inspect and debug the REST auth providers.

* ``schwab webauth list``           — providers, errors, disabled files.
* ``schwab webauth verify <token>`` — run a real token through the
  exact verification path the REST middleware uses; prints the
  resulting principal (provider, subject, email, scopes) or the
  rejection reason. Pass ``-`` to read the token from stdin so it
  doesn't land in shell history.
"""

from __future__ import annotations

import sys

import typer

from schwab_cli.webauth.config import load_providers
from schwab_cli.webauth.verify import (
    SubjectNotAllowed,
    TokenVerifier,
    WebAuthError,
)


def run_list() -> None:
    loaded = load_providers()
    if not loaded.providers and not loaded.errors and not loaded.disabled:
        typer.echo(
            "No providers configured "
            "(~/.config/schwab_cli/webauth/*.json). "
            "REST /api stays loopback-unauthenticated."
        )
        return
    for p in loaded.providers:
        subjects = (
            "any subject (*)" if p.allow_all_subjects
            else f"{len(p.subject_scopes)} subject(s)"
        )
        typer.secho(f"  ✓ {p.name}", fg=typer.colors.GREEN, nl=False)
        typer.echo(f"  {p.issuer} — aud={p.audience} — {subjects}")
    for e in loaded.errors:
        typer.secho(f"  ✗ {e.path}", fg=typer.colors.RED, nl=False)
        typer.echo(f"  {e.reason}")
    for name in loaded.disabled:
        typer.echo(f"  - {name}  disabled (enabled=false)")


def run_verify(token: str) -> None:
    if token == "-":
        token = sys.stdin.read().strip()
    if not token:
        typer.secho("empty token", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    loaded = load_providers()
    for e in loaded.errors:
        typer.secho(
            f"(provider disabled: {e.path}: {e.reason})",
            fg=typer.colors.YELLOW, err=True,
        )
    if not loaded.providers:
        typer.secho(
            "No usable providers configured — nothing can verify this token.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=1)

    verifier = TokenVerifier(loaded.providers)
    try:
        principal = verifier.verify(token)
    except SubjectNotAllowed as e:
        typer.secho(f"REJECTED (subject): {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    except WebAuthError as e:
        typer.secho(f"REJECTED: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    typer.secho("ACCEPTED", fg=typer.colors.GREEN)
    typer.echo(f"  provider: {principal.provider}")
    typer.echo(f"  subject:  {principal.subject}")
    if principal.email:
        typer.echo(f"  email:    {principal.email}")
    scopes = ", ".join(sorted(principal.scopes)) or "(none)"
    typer.echo(f"  scopes:   {scopes}")
