"""`dataset` command group — subscriptions, status, update, cron.

Typer wrappers parse args and call handler functions in
:mod:`schwab_cli.dataset.update` and :mod:`schwab_cli.dataset.store`.
"""
from __future__ import annotations

import json

import typer

from datetime import datetime
from zoneinfo import ZoneInfo

from schwab_cli._doc import doc_option
from schwab_cli.dataset.scheduler import sleep_until_ny


_NY_TZ = ZoneInfo("America/New_York")
_TARGET_NY_HOUR = 17  # market-data cron anchor


def _make_notifier():
    """Indirection so tests can stub the notifier."""
    from schwab_cli.notify import Notifier
    return Notifier.from_file()


def _now_ny():
    """Indirection for clock stubbing in tests."""
    return datetime.now(tz=_NY_TZ)


def _check_fire_time_and_alert(notifier) -> bool:
    """Emit a drift alert when we fired at NY ≥ 17:00 ET. Returns
    True when the fire-time is OK (safe window), False on drift.

    On drift, additionally invoke the **Phase 4 auto-fix**:
    recompute the right local Hour for the current system TZ, rewrite
    the launchd plist, re-bootstrap via ``launchctl``, and emit a
    follow-up ``fire_time_autofixed`` notification. Auto-fix errors
    are captured and reported as ``fire_time_autofix_failed`` —
    never silent.

    Skips the sleep_until_ny call on drift (which would no-op anyway)
    and lets the cron run immediately so the operator at least gets
    *some* data point — partial data > no data.
    """
    now_ny = _now_ny()
    if now_ny.hour >= _TARGET_NY_HOUR:
        notifier.emit(
            "dataset.market_data.fire_time_drift",
            ny_time=now_ny.strftime("%H:%M %Z"),
            target_ny_time=f"{_TARGET_NY_HOUR:02d}:00 ET",
        )
        try:
            from schwab_cli.dataset.launchd import (
                _compute_safe_local_hour, reinstall_market_data_job,
            )
            system_tz = datetime.now().astimezone().tzinfo
            if system_tz is None:
                raise RuntimeError("system has no recognized timezone")
            new_hour = _compute_safe_local_hour(system_tz=system_tz)
            reinstall_market_data_job(local_hour=new_hour)
            notifier.emit(
                "dataset.market_data.fire_time_autofixed",
                new_local_hour=f"{new_hour:02d}:00",
                system_tz=str(system_tz),
            )
        except Exception as e:
            notifier.emit(
                "dataset.market_data.fire_time_autofix_failed",
                error=f"{type(e).__name__}: {e}",
            )
        return False
    return True
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
    group: str = typer.Option(
        "volatility", "--group",
        help="Data product(s). Comma-separated for multi-product subscribe, "
             "e.g. `--group=ohlcv,volatility` adds one row per product.",
    ),
    doc: bool = doc_option(),
) -> None:
    from schwab_cli.storage import vol_history
    from schwab_cli.storage.groups import ALL_GROUPS
    from schwab_cli.dataset.store import (
        subscribe_equity, subscribe_index,
    )
    from schwab_cli.dataset.config import (
        load_config_or_default, save_config,
    )

    # Parse the comma-separated --group flag into a list of products.
    # Empty / whitespace-only entries are dropped. Order is preserved
    # so the subscribe order matches the user's intent (mostly
    # cosmetic — the cron treats memberships as a set).
    group_list = [g.strip() for g in group.split(",") if g.strip()]
    if not group_list:
        typer.secho("--group requires at least one product name",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    unknown = [g for g in group_list if g not in ALL_GROUPS]
    if unknown:
        typer.secho(
            f"unknown group(s): {', '.join(unknown)} "
            f"(expected one of: {', '.join(ALL_GROUPS)})",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=2)

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
                for g in group_list:
                    subscribe_index(
                        conn, index_name=target_str.strip().upper(),
                        group_name=g,
                    )
        except ValueError as e:
            typer.secho(str(e), fg=typer.colors.RED)
            raise typer.Exit(code=2)
        typer.secho(
            f"subscribed index {target_str!r} → groups={','.join(group_list)}; "
            f"run `dataset update --indices` to populate members.",
            fg=typer.colors.GREEN,
        )
        return

    symbols = [s.strip().upper() for s in target_str.split(",") if s.strip()]
    with vol_history.connect() as conn:
        for sym in symbols:
            for g in group_list:
                subscribe_equity(conn, symbol=sym, group_name=g)
    typer.secho(
        f"subscribed: {', '.join(symbols)} → groups={','.join(group_list)}",
        fg=typer.colors.GREEN,
    )


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
    ohlcv_counts: dict[str, int] = {}
    with vol_history.connect() as conn:
        for g in groups:
            out_rows.extend(read_status_rows(
                conn, group_name=g, tier=tier, source=source, symbols=syms,
            ))
        # Per-symbol OHLCV cache size — shown alongside the existing
        # snapshot stats so the operator can see at a glance whether
        # the daily cron is actually populating ohlcv_daily.
        for r in conn.execute(
            "SELECT symbol, count(*) AS n FROM ohlcv_daily GROUP BY symbol"
        ).fetchall():
            ohlcv_counts[r["symbol"]] = r["n"]

    # Decorate each row with its cached OHLCV bar count so the JSON
    # consumer sees it too.
    for r in out_rows:
        r["ohlcv_rows"] = ohlcv_counts.get(r["symbol"], 0)

    if as_json:
        typer.echo(json.dumps(out_rows, indent=2))
        return

    if not out_rows:
        typer.echo("(no subscriptions)")
        return

    cols = ("SYMBOL", "GROUP", "TIER", "SOURCES",
            "FIRST", "LAST", "DAYS", "OHLCV")
    typer.echo(f"{'  '.join(cols)}")
    for r in out_rows:
        typer.echo(
            f"{r['symbol']:<6}  {r['group']:<10}  {r['tier']:<6}  "
            f"{','.join(r['sources']):<35}  "
            f"{r['first_date'] or '—':<10}  "
            f"{r['last_date'] or '—':<10}  "
            f"{r['n_days']:<6}  "
            f"{r['ohlcv_rows']}"
        )


@app.command("update", help="Run an indices or volatility update now.")
def update(
    indices: bool = typer.Option(False, "--indices"),
    group: str = typer.Option(None, "--group"),
    skip_wait: bool = typer.Option(
        False, "--skip-wait",
        help="Skip the NY-17:00-ET wait. For manual reruns. The cron "
             "normally fires at a fixed UTC+8 local time (earlier than "
             "NY 17:00 ET in either DST mode) and sleeps until target.",
    ),
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

    # Anchor the daily market-data run to NY 17:00 ET regardless of
    # what local time launchd fires us at. Indices runs unaffected
    # (weekly, no chain-snapshot timing concerns).
    if group:
        # Drift detection — if we fired at NY ≥ 17:00 (e.g. system
        # TZ changed after install), sleep_until_ny will no-op and
        # the run lands at an unexpected chain-snapshot moment.
        # Surface this as a Telegram alert so the operator notices
        # without having to run `doctor` manually.
        fire_ok = _check_fire_time_and_alert(_make_notifier())
        if not skip_wait and fire_ok:
            sleep_until_ny(17, 0)

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
        # --group volatility installs the unified market-data daily
        # job — the launchd plist's internal kind is "market-data" in
        # v4, even though the user-facing flag is unchanged.
        return "market-data"
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
        uninstall_legacy_volatility_job,
    )

    kind = _resolve_cron_kind(indices, group)
    cfg = load_config_or_default()
    if not config_path().exists():
        save_config(cfg)
    # v2: cron expressions live in code (installer-owned), not config.
    cron_expr = (INDICES_CRON_LOCAL if kind == "indices"
                 else MARKET_DATA_CRON_LOCAL)

    # Clean up the legacy `com.schwab-cli.dataset.volatility` plist
    # before installing under the new market-data label so users
    # upgrading from a pre-rename build don't end up with both
    # registered with launchd.
    if kind != "indices":
        legacy = uninstall_legacy_volatility_job()
        if legacy is not None:
            typer.secho(f"removed legacy plist → {legacy}",
                        fg=typer.colors.YELLOW)

    # Console-script renamed from schwab_cli → schwab in PR #6.
    # Look up the new name; fall back to `schwab_cli` on PATH for the
    # rare case someone still has the legacy binary installed.
    binary = (
        shutil.which("schwab")
        or shutil.which("schwab_cli")
        or "schwab"
    )
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
