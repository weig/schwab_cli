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
        # Persist the account intent so the daily cron picks it up
        # even if today's eager sync fails (no auth, network blip, …).
        cfg = load_config_or_default()
        # v2 schema: accounts live under a single "market_data" bucket
        # regardless of which product (ohlcv / volatility) drove the
        # subscribe. The group_name discriminator inside the DB still
        # tracks per-product subscriptions; this is just where the
        # account-hash list lives.
        cfg.setdefault("accounts", {}).setdefault("market_data", [])
        if account not in cfg["accounts"]["market_data"]:
            cfg["accounts"]["market_data"].append(account)
            save_config(cfg)
        # Eager-sync positions so `dataset status` shows them right away.
        # Falls back gracefully when auth isn't set up yet.
        added, closed, err = _eager_sync_account(account, group=group)
        suffix = account[-4:] if len(account) >= 4 else account
        if err is not None:
            typer.secho(
                f"subscribed account {suffix!r} → group={group}; "
                f"position sync deferred ({err}). "
                f"Run `dataset update --group {group}` after auth is set up.",
                fg=typer.colors.YELLOW,
            )
        else:
            typer.secho(
                f"subscribed account {suffix!r} → group={group}; "
                f"+{len(added)} symbols ({', '.join(added) or '—'})",
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


def _eager_sync_account(
    account: str, *, group: str,
) -> tuple[list[str], list[str], str | None]:
    """Materialize position rows for the just-subscribed account.

    Returns ``(added_symbols, closed_symbols, error_message)``. On any
    failure (no auth, expired session, API error) we return an error
    string instead of raising — the subscription intent is already
    persisted, so the daily cron will retry.
    """
    import time
    from schwab_cli.storage import vol_history
    from schwab_cli.dataset.update import sync_account_positions
    try:
        from schwab_cli.api.client import SchwabClient
        from schwab_cli import config as config_module
        from schwab_cli.session import load as load_session
        cfg_full = config_module.load()
        sess = load_session()
    except Exception as e:
        return [], [], f"config/session load failed: {e}"
    if cfg_full is None or sess is None:
        return [], [], "no session — run `schwab_cli auth`"
    try:
        client = SchwabClient(cfg_full, sess)
        with vol_history.connect() as conn:
            summary = sync_account_positions(
                conn, client=client, account_hash=account,
                group_name=group, now_ms=int(time.time() * 1000),
            )
    except Exception as e:
        return [], [], str(e)
    if summary.get("error"):
        return [], [], summary["error"]
    return summary.get("added", []), summary.get("closed", []), None


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
        accounts = cfg.get("accounts", {}).get("market_data", [])
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
                .get("accounts", {}).get("market_data", []))
    with vol_history.connect() as conn:
        summary = run_volatility_update(
            conn, client=client, group_name=group,
            now_ms=now_ms, accounts=accounts,
            progress=_print_volatility_progress,
        )
    typer.echo("")  # blank line before summary
    typer.echo(
        f"sampled={len(summary['sampled'])} "
        f"skipped={len(summary['skipped'])} "
        f"errors={len(summary['errors'])} "
        f"transitions={len(summary['transitions'])}"
    )
    for t in summary["transitions"]:
        typer.echo(f"  {t['symbol']}: {t['from']} → {t['to']}")


def _print_volatility_progress(evt: dict) -> None:
    """Per-symbol progress printer for ``dataset update --group …``.

    Emits one line per symbol — the *start* event prints "updating
    [N/T] SYM volatility for DATE" before the chain pull so the user
    can see exactly which symbol is in flight if it stalls. Skipped
    and errored symbols emit a status line in place of the start
    line. Successful samples are silent on the second tick (the next
    "updating" line is the natural progress signal); failures emit
    a follow-up red line.
    """
    width = len(str(evt["total"]))
    head = f"[{evt['index']:>{width}}/{evt['total']}]"
    sym = evt["symbol"]
    date = evt.get("archive_date", "")
    e = evt["event"]
    if e == "start":
        typer.echo(f"updating {head} {sym} volatility for {date}")
    elif e == "skipped":
        typer.secho(
            f"skipping {head} {sym} ({evt.get('reason', 'skip')})",
            fg=typer.colors.YELLOW,
        )
    elif e == "errored":
        typer.secho(
            f"  ↳ {sym}: error — {evt.get('error', 'unknown')}",
            fg=typer.colors.RED,
        )
    elif e == "sampled":
        # Tier transitions are interesting; same-tier samples stay quiet.
        if evt.get("tier_from") != evt.get("tier_to"):
            typer.secho(
                f"  ↳ {sym}: tier {evt['tier_from']} → {evt['tier_to']}",
                fg=typer.colors.CYAN,
            )


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

    from schwab_cli.dataset.launchd import (
        INDICES_CRON_LOCAL, MARKET_DATA_CRON_LOCAL,
    )

    kind = _resolve_cron_kind(indices, group)
    cfg = load_config_or_default()
    if not config_path().exists():
        save_config(cfg)
    # v2: cron expressions live in code (installer-owned), not config.
    cron_expr = (INDICES_CRON_LOCAL if kind == "indices"
                 else MARKET_DATA_CRON_LOCAL)

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
