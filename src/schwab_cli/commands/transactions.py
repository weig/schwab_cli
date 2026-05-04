from __future__ import annotations

import typer

from schwab_cli import config as config_module
from schwab_cli.api.client import ApiError, SchwabClient, SessionExpired
from schwab_cli.api.transactions import get_all_transactions  # noqa: F401 (legacy import surface)
from schwab_cli.api.transactions_cache import fetch_cached
from schwab_cli.history_spec import RangeSpecError, parse_range
from schwab_cli.output.format import FormatError, pick_format
from schwab_cli.output.transactions import render_transactions, shape_transactions
from schwab_cli.session import load as load_session


def _client() -> SchwabClient:
    cfg = config_module.load()
    if cfg is None:
        typer.secho(
            "No config found. Run `schwab_cli setup` first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    session = load_session()
    if session is None:
        typer.secho(
            "No session found. Run `schwab_cli auth` first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    return SchwabClient(cfg, session)


def _filter_by_type(rows: list[dict], type_filter: str) -> list[dict]:
    if not type_filter or type_filter == "ALL":
        return rows
    wanted = {t.strip() for t in type_filter.split(",") if t.strip()}
    return [r for r in rows if (r.get("type") or "") in wanted]


def run(
    account: str | None,
    *,
    range_str: str,
    type_filter: str,
    as_json: bool,
    as_md: bool,
    refresh: bool = False,
) -> None:
    try:
        fmt = pick_format(as_json, as_md)
    except FormatError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    try:
        start, end = parse_range(range_str)
    except RangeSpecError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        code = 2 if getattr(e, "kind", "invalid") == "invalid" else 1
        raise typer.Exit(code=code)

    client = _client()
    try:
        # Cache always fetches the full type set; apply the user's
        # filter locally on the way to the renderer.
        raw = fetch_cached(
            client, account,
            start=start, end=end,
            refresh=refresh,
        )
    except (ApiError, SessionExpired) as e:
        msg = str(e) if str(e) else type(e).__name__
        typer.secho(msg, fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    raw = _filter_by_type(raw, type_filter)
    rows = shape_transactions(raw)
    typer.echo(render_transactions(
        rows, fmt=fmt,
        # When the user filtered to a specific account, drop the
        # redundant Account column from human/MD output. JSON is
        # unaffected (stable shape for machine consumers).
        show_account=(account is None),
    ))
