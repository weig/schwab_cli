"""`schwab screener` — Options VRP screener command group.

Read commands (status/ranking/ledger) emit JSON for agent consumption; the
action commands (update/earnings/membership) print a short human summary.
The screener produces a candidate pool only — it never places an order.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import typer

from schwab_cli._doc import doc_option

app = typer.Typer(
    help="Options VRP screener — bid-side put-selling candidate pool.",
    no_args_is_help=True,
)


def _now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


@app.command("update", help="Run the daily screener pass (snapshot, rank, ledger).")
def update(doc: bool = doc_option()) -> None:
    from schwab_cli.screener.update import run_daily

    summary = run_daily()
    typer.secho(json.dumps(summary, indent=2, default=str))


@app.command("earnings", help="Refresh the earnings calendar for tracked symbols.")
def earnings(doc: bool = doc_option()) -> None:
    import httpx

    from schwab_cli.dataset.store import list_active_subscriptions
    from schwab_cli.screener.earnings import nasdaq_earnings_fetcher, refresh_earnings
    from schwab_cli.storage.vol_history import connect

    with connect() as conn:
        symbols = sorted(
            {r["symbol"] for r in list_active_subscriptions(conn, group_name="volatility")}
        )
        with httpx.Client() as client:
            summary = refresh_earnings(
                conn, symbols, nasdaq_earnings_fetcher(client), now_ms=_now_ms()
            )
    typer.secho(json.dumps(summary, indent=2))


@app.command("membership", help="Snapshot today's index membership (survivorship guard).")
def membership(doc: bool = doc_option()) -> None:
    from schwab_cli.screener.membership import record_membership_snapshot
    from schwab_cli.screener.update import ny_snapshot_date
    from schwab_cli.storage.vol_history import connect

    now = _now_ms()
    with connect() as conn:
        summary = record_membership_snapshot(
            conn, as_of_date=ny_snapshot_date(now), now_ms=now
        )
    typer.secho(json.dumps(summary, indent=2))


@app.command("ranking", help="Show the latest (or a given date's) ranking.")
def ranking(
    date: str = typer.Option(None, "--date", help="Ranking date (YYYY-MM-DD); default latest."),
    limit: int = typer.Option(10, "--limit", help="Number of rows (candidate pool = 10)."),
    doc: bool = doc_option(),
) -> None:
    from schwab_cli.storage import screener as store
    from schwab_cli.storage.vol_history import connect

    with connect() as conn:
        ranking_date = date or store.latest_ranking_date(conn)
        rows = (
            store.read_ranking(conn, ranking_date=ranking_date, limit=limit)
            if ranking_date else []
        )
    payload = {
        "ranking_date": ranking_date,
        "rows": [dict(r) for r in rows],
    }
    typer.secho(json.dumps(payload, indent=2, default=str))


@app.command("status", help="Screener status: latest ranking + filter/ledger counts.")
def status(doc: bool = doc_option()) -> None:
    typer.secho(json.dumps(_status_payload(), indent=2, default=str))


@app.command("ledger", help="Paper-ledger validation report (top vs bottom).")
def ledger(doc: bool = doc_option()) -> None:
    from schwab_cli.screener.report import ledger_report
    from schwab_cli.storage import screener as store
    from schwab_cli.storage.vol_history import connect

    with connect() as conn:
        rows = [dict(r) for r in store.read_ledger(conn)]
    typer.secho(json.dumps(ledger_report(rows), indent=2, default=str))


def _status_payload() -> dict:
    from schwab_cli.storage import screener as store
    from schwab_cli.storage.vol_history import connect

    with connect() as conn:
        ranking_date = store.latest_ranking_date(conn)
        top = store.read_ranking(conn, ranking_date=ranking_date, limit=10) if ranking_date else []
        ledger = store.read_ledger(conn)
    open_positions = sum(1 for r in ledger if r["settled_at"] is None)
    return {
        "latest_ranking_date": ranking_date,
        "candidate_pool": [
            {"rank": r["rank"], "symbol": r["symbol"],
             "executable_vrp": r["executable_vrp"]}
            for r in top
        ],
        "ledger": {
            "total": len(ledger),
            "open": open_positions,
            "settled": len(ledger) - open_positions,
        },
    }
