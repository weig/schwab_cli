"""`dataset` command group — subscriptions, status, update, cron.

Typer wrappers parse args and call handler functions in
:mod:`schwab_cli.dataset.update` and :mod:`schwab_cli.dataset.store`.
"""
from __future__ import annotations

import json

import typer

from schwab_cli._doc import doc_option
from schwab_cli.dataset.update import (
    run_indices_update, run_volatility_update,
)
from schwab_cli.dataset.launchd import (
    DatasetPlistSpec, install_plist, uninstall_plist,
)


app = typer.Typer(
    help="Manage cached volatility datasets — subscriptions, status, "
         "manual updates, cron lifecycle.",
    no_args_is_help=True,
)


@app.command("subscribe", help="Add subscription(s) for a group.")
def subscribe(
    targets: list[str] = typer.Argument(None),
    indices: bool = typer.Option(False, "--indices"),
    account: str = typer.Option(None, "--account"),
    group: str = typer.Option("volatility", "--group"),
    doc: bool = doc_option(),
) -> None:
    from schwab_cli.storage import vol_history
    from schwab_cli.dataset.store import (
        subscribe_equity, subscribe_index,
    )
    from schwab_cli.dataset.config import (
        load_config_or_default, save_config,
    )

    target_str = ",".join(targets) if targets else ""
    if account is not None:
        if not account:
            typer.secho("--account requires a non-empty value",
                        fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2)
        cfg = load_config_or_default()
        cfg.setdefault("accounts", {}).setdefault(group, [])
        if account not in cfg["accounts"][group]:
            cfg["accounts"][group].append(account)
            save_config(cfg)
        typer.secho(
            f"subscribed account {account[-4:]!r} → group={group}",
            fg=typer.colors.GREEN,
        )
        return

    if not target_str:
        typer.secho("subscribe needs SYMBOLS, INDEX --indices, or --account",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    if indices:
        if "," in target_str:
            typer.secho("--indices accepts one index at a time",
                        fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2)
        try:
            with vol_history.connect() as conn:
                subscribe_index(conn, index_name=target_str.strip().upper(),
                                group_name=group)
        except ValueError as e:
            typer.secho(str(e), fg=typer.colors.RED)
            raise typer.Exit(code=2)
        typer.secho(
            f"subscribed index {target_str!r} → group={group}; "
            f"run `dataset update --indices` to populate members.",
            fg=typer.colors.GREEN,
        )
        return

    symbols = [s.strip().upper() for s in target_str.split(",") if s.strip()]
    with vol_history.connect() as conn:
        for sym in symbols:
            subscribe_equity(conn, symbol=sym, group_name=group)
    typer.secho(f"subscribed: {', '.join(symbols)}", fg=typer.colors.GREEN)


@app.command("unsubscribe", help="Remove subscription(s) (soft-delete).")
def unsubscribe(
    targets: list[str] = typer.Argument(None),
    indices: bool = typer.Option(False, "--indices"),
    account: str = typer.Option(None, "--account"),
    group: str = typer.Option("volatility", "--group"),
    doc: bool = doc_option(),
) -> None:
    from schwab_cli.storage import vol_history
    from schwab_cli.dataset.store import (
        unsubscribe_equity, unsubscribe_index,
    )
    from schwab_cli.dataset.config import (
        load_config_or_default, save_config,
    )

    if account is not None:
        cfg = load_config_or_default()
        accounts = cfg.get("accounts", {}).get(group, [])
        if account in accounts:
            accounts.remove(account)
            save_config(cfg)
        typer.secho(f"unsubscribed account {account[-4:]!r}",
                    fg=typer.colors.GREEN)
        return

    target_str = ",".join(targets) if targets else ""
    if not target_str:
        typer.secho("unsubscribe needs SYMBOLS, INDEX --indices, or --account",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    if indices:
        with vol_history.connect() as conn:
            unsubscribe_index(conn,
                              index_name=target_str.strip().upper(),
                              group_name=group)
        typer.secho(f"unsubscribed index {target_str!r}", fg=typer.colors.GREEN)
        return

    symbols = [s.strip().upper() for s in target_str.split(",") if s.strip()]
    with vol_history.connect() as conn:
        for sym in symbols:
            unsubscribe_equity(conn, symbol=sym, group_name=group)
    typer.secho(f"unsubscribed: {', '.join(symbols)}", fg=typer.colors.GREEN)


@app.command("status", help="Show current dataset tracking state.")
def status(
    group: str = typer.Option(None, "--group"),
    tier: str = typer.Option(None, "--tier"),
    source: str = typer.Option(None, "--source"),
    symbol: str = typer.Option(None, "--symbol"),
    as_json: bool = typer.Option(False, "--json"),
    doc: bool = doc_option(),
) -> None:
    from schwab_cli.storage import vol_history
    from schwab_cli.dataset.store import read_status_rows

    syms = (
        [s.strip().upper() for s in symbol.split(",") if s.strip()]
        if symbol else None
    )
    groups = [group] if group else ["volatility"]
    out_rows: list[dict] = []
    with vol_history.connect() as conn:
        for g in groups:
            out_rows.extend(read_status_rows(
                conn, group_name=g, tier=tier, source=source, symbols=syms,
            ))

    if as_json:
        typer.echo(json.dumps(out_rows, indent=2))
        return

    if not out_rows:
        typer.echo("(no subscriptions)")
        return

    cols = ("SYMBOL", "GROUP", "TIER", "SOURCES",
            "FIRST", "LAST", "DAYS")
    typer.echo(f"{'  '.join(cols)}")
    for r in out_rows:
        typer.echo(
            f"{r['symbol']:<6}  {r['group']:<10}  {r['tier']:<6}  "
            f"{','.join(r['sources']):<35}  "
            f"{r['first_date'] or '—':<10}  "
            f"{r['last_date'] or '—':<10}  "
            f"{r['n_days']}"
        )


@app.command("update", help="Run an indices or volatility update now.")
def update(
    indices: bool = typer.Option(False, "--indices"),
    group: str = typer.Option(None, "--group"),
    doc: bool = doc_option(),
) -> None:
    from schwab_cli.storage import vol_history
    from schwab_cli.api.client import SchwabClient
    from schwab_cli import config as config_module
    from schwab_cli.session import load as load_session
    from schwab_cli.dataset.config import load_config_or_default
    import httpx
    import time

    if not indices and not group:
        typer.secho("update requires --indices or --group <name>",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    now_ms = int(time.time() * 1000)

    if indices:
        with httpx.Client(timeout=30.0) as http_client:
            with vol_history.connect() as conn:
                summary = run_indices_update(
                    conn, http_client=http_client,
                    group_name="volatility", now_ms=now_ms,
                )
        for idx, info in summary.items():
            if "error" in info:
                typer.secho(f"{idx}: ERROR {info['error']}",
                            fg=typer.colors.RED)
            else:
                typer.echo(
                    f"{idx}: total={info['total']} "
                    f"+{len(info['added'])} −{len(info['removed'])}"
                )
        return

    cfg_full = config_module.load()
    sess = load_session()
    if cfg_full is None or sess is None:
        typer.secho("Run `schwab_cli setup` and `schwab_cli auth` first.",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    client = SchwabClient(cfg_full, sess)
    accounts = (load_config_or_default()
                .get("accounts", {}).get(group, []))
    with vol_history.connect() as conn:
        summary = run_volatility_update(
            conn, client=client, group_name=group,
            now_ms=now_ms, accounts=accounts,
        )
    typer.echo(
        f"sampled={len(summary['sampled'])} "
        f"skipped={len(summary['skipped'])} "
        f"errors={len(summary['errors'])} "
        f"transitions={len(summary['transitions'])}"
    )
    for t in summary["transitions"]:
        typer.echo(f"  {t['symbol']}: {t['from']} → {t['to']}")


cron_app = typer.Typer(help="Install / uninstall launchd scheduled jobs.")
app.add_typer(cron_app, name="cron")


def _resolve_cron_kind(indices: bool, group: str | None) -> str:
    if indices and group:
        typer.secho("pass --indices OR --group, not both",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    if indices:
        return "indices"
    if group:
        if group != "volatility":
            typer.secho(f"unknown group {group!r} (only 'volatility' supported)",
                        fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2)
        return "volatility"
    typer.secho("must pass --indices or --group <name>",
                fg=typer.colors.RED, err=True)
    raise typer.Exit(code=2)


@cron_app.command("install", help="Write the plist and load it.")
def cron_install(
    indices: bool = typer.Option(False, "--indices"),
    group: str = typer.Option(None, "--group"),
    doc: bool = doc_option(),
) -> None:
    import shutil
    from schwab_cli.dataset.config import (
        load_config_or_default, save_config, config_path,
    )

    kind = _resolve_cron_kind(indices, group)
    cfg = load_config_or_default()
    if not config_path().exists():
        save_config(cfg)
    cron_expr = (cfg["cron"]["indices"] if kind == "indices"
                 else cfg["cron"]["groups"][group or "volatility"])

    binary = shutil.which("schwab_cli") or "schwab_cli"
    log_file = str(config_path().parent / "dataset.log")
    spec = DatasetPlistSpec(
        binary_path=binary, cron=cron_expr,
        kind=kind, log_file=log_file,
    )
    path = install_plist(spec)
    typer.secho(f"installed → {path}", fg=typer.colors.GREEN)


@cron_app.command("uninstall", help="Unload and remove the plist.")
def cron_uninstall(
    indices: bool = typer.Option(False, "--indices"),
    group: str = typer.Option(None, "--group"),
    doc: bool = doc_option(),
) -> None:
    kind = _resolve_cron_kind(indices, group)
    path = uninstall_plist(kind)
    typer.secho(f"removed → {path}", fg=typer.colors.GREEN)
