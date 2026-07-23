"""`schwab backup` — encrypted incremental backups to Cloudflare R2."""
from __future__ import annotations

import json
from pathlib import Path

import typer

from schwab_cli._doc import doc_option

app = typer.Typer(
    help="Encrypted incremental backups of databases + config to R2.",
    no_args_is_help=True,
)


def _paths() -> tuple[Path, Path, Path, Path]:
    from schwab_cli.config import config_path
    from schwab_cli.storage.vol_history import storage_dir

    cdir = config_path().parent
    return (cdir, storage_dir(), cdir / "backup_passphrase",
            cdir / "backup_r2.env")


def _remote(env_file: Path):
    from schwab_cli.backup.remote import R2Remote

    if not env_file.exists():
        typer.secho(f"missing {env_file} — R2 credentials not configured",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    return R2Remote(env_file)


@app.command("run", help="Run a backup (differential; Saturday/migration/--full → full).")
def run(
    full: bool = typer.Option(False, "--full", help="Force a full backup."),
    doc: bool = doc_option(),
) -> None:
    from schwab_cli.backup.runner import run_backup

    cdir, sdir, passfile, env_file = _paths()
    summary = run_backup(_remote(env_file), config_dir=cdir, storage_dir=sdir,
                         passfile=passfile, force_full=full)
    typer.secho(json.dumps(summary, indent=1))
    if summary.get("passphrase_created"):
        typer.secho(
            "\nNEW passphrase generated. Store it in 1Password now:\n"
            "  op signin && op item create --category password "
            "--title 'schwab-backup passphrase' "
            f"password=\"$(cat {passfile})\"",
            fg=typer.colors.YELLOW, err=True)


@app.command("restore", help="Restore databases as of DATE (YYYYMMDD) into a directory.")
def restore(
    date: str = typer.Argument(..., help="Backup day to restore (YYYYMMDD)."),
    dest: Path = typer.Option(..., "--dest", help="Destination directory "
                              "(never the live storage dir)."),
    doc: bool = doc_option(),
) -> None:
    from schwab_cli.backup.runner import run_restore

    cdir, sdir, passfile, env_file = _paths()
    if Path(dest).resolve() == sdir.resolve():
        typer.secho("refusing to restore over the live storage dir; "
                    "pick another --dest", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    out = run_restore(_remote(env_file), day=date, dest=Path(dest),
                      passfile=passfile)
    typer.secho(json.dumps(out, indent=1))


@app.command("status", help="Show recent backups and the current base.")
def status(doc: bool = doc_option()) -> None:
    from schwab_cli.backup.runner import load_state

    cdir, _, _, env_file = _paths()
    r = _remote(env_file)
    payload = {
        "state": load_state(cdir),
        "data": r.list("data/")[-10:],
        "monthly": r.list("monthly/"),
        "config": r.list("config/")[-5:],
    }
    typer.secho(json.dumps(payload, indent=1))
