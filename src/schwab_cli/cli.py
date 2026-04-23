import typer

from schwab_cli.commands import accounts as accounts_cmd
from schwab_cli.commands import auth as auth_cmd
from schwab_cli.commands import greeks as greeks_cmd
from schwab_cli.commands import history as history_cmd
from schwab_cli.commands import option as option_cmd
from schwab_cli.commands import quote as quote_cmd
from schwab_cli.commands import setup as setup_cmd
from schwab_cli.commands import transactions as transactions_cmd

app = typer.Typer(
    name="schwab_cli",
    help="Charles Schwab CLI.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main() -> None:
    """Charles Schwab CLI."""


@app.command("setup", help="Configure Schwab CLI credentials.")
def setup(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the resulting config.json to stdout without saving it.",
    ),
) -> None:
    setup_cmd.run(dry_run=dry_run)


@app.command("auth", help="Authenticate with Schwab (refresh or full OAuth).")
def auth(
    force: bool = typer.Option(
        False,
        "--force",
        help="Skip session refresh and run the full OAuth flow.",
    ),
    manual: bool = typer.Option(
        False,
        "--manual",
        help=(
            "Skip saved-credential automation and drive the Schwab login "
            "yourself in a visible browser (forces HEADLESS=0 for this run)."
        ),
    ),
) -> None:
    auth_cmd.run(force=force, manual=manual)


@app.command("accounts", help="List Schwab accounts.")
def accounts(
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
    as_md: bool = typer.Option(False, "--md", help="Output GitHub-flavored markdown."),
) -> None:
    accounts_cmd.run_list(as_json=as_json, as_md=as_md)


@app.command("account", help="Show one Schwab account by number (or suffix).")
def account(
    account_number: str = typer.Argument(..., help="Full number or last-N-digit suffix."),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
    as_md: bool = typer.Option(False, "--md", help="Output GitHub-flavored markdown."),
) -> None:
    accounts_cmd.run_show(account_number, as_json=as_json, as_md=as_md)


@app.command("positions", help="List positions across accounts (or one account).")
def positions(
    account_number: str = typer.Argument(None, help="Optional account number or suffix."),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
    as_md: bool = typer.Option(False, "--md", help="Output GitHub-flavored markdown."),
) -> None:
    accounts_cmd.run_positions(account_number, as_json=as_json, as_md=as_md)


@app.command("quote", help="Get real-time quotes for one or more symbols.")
def quote(
    symbols: list[str] = typer.Argument(..., help="One or more ticker symbols."),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
    as_md: bool = typer.Option(False, "--md", help="Output GitHub-flavored markdown."),
) -> None:
    quote_cmd.run(symbols, as_json=as_json, as_md=as_md)


@app.command(
    "option",
    help=(
        "Look up an option chain. SPEC: YYMMDD[P|C]*[strike] — "
        "quote the spec in shells that glob `*`."
    ),
)
def option(
    symbol: str = typer.Argument(..., help="Underlying ticker (e.g. NVDA)."),
    spec: str = typer.Argument(..., help="Option spec, e.g. '270115*250' or '270115P*'."),
    strikes: int = typer.Option(
        10, "--strikes", help="Total strikes around ATM when no explicit strike."
    ),
    detail: int = typer.Option(
        0,
        "--detail",
        help="Detail level: 0=classic, 1=stacked+greeks, 2=stacked+sub-table.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
    as_md: bool = typer.Option(False, "--md", help="Output GitHub-flavored markdown."),
) -> None:
    option_cmd.run(
        symbol,
        spec,
        strikes=strikes,
        detail=detail,
        as_json=as_json,
        as_md=as_md,
    )


@app.command(
    "greeks",
    help=(
        "Show detailed greeks for a single option contract. "
        "Accepts any common form: NVDA260501C240, 'NVDA  260501C00240000', "
        "NVDA260501C240.0."
    ),
)
def greeks(
    ticker: str = typer.Argument(
        ..., help="Option ticker in any supported form (NVDA260501C240, …)."
    ),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
    as_md: bool = typer.Option(False, "--md", help="Output GitHub-flavored markdown."),
) -> None:
    greeks_cmd.run(ticker, as_json=as_json, as_md=as_md)


@app.command(
    "history",
    help=(
        "Fetch OHLCV price history for a stock or option. "
        "Option tickers accept any common form (NVDA260501C240, …)."
    ),
)
def history(
    symbol: str = typer.Argument(..., help="Ticker (e.g. NVDA)."),
    range_str: str = typer.Option(
        "-1y..now", "--range",
        help="Date range: '<start>..<end>' or one of: ytd, mtd, wtd. "
             "Endpoints: YYYYMMDD, -Nu (u in d/w/mo/y), or 'now'.",
    ),
    interval_str: str = typer.Option(
        "1day", "--interval",
        help="Candle interval: 1min, 5min, 10min, 15min, 30min, 1day, 1wk, 1mo.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
    as_md: bool = typer.Option(False, "--md", help="Output GitHub-flavored markdown."),
) -> None:
    history_cmd.run(
        symbol,
        range_str=range_str,
        interval_str=interval_str,
        as_json=as_json,
        as_md=as_md,
    )


@app.command(
    "transactions",
    help="List account transactions over a date range (default: last 7 days, TRADE only).",
)
def transactions(
    account: str = typer.Argument(
        None, help="Optional account number or suffix. Default: all accounts.",
    ),
    range_str: str = typer.Option(
        "-7d..now", "--range",
        help="Date range: '<start>..<end>' or one of: ytd, mtd, wtd. "
             "Endpoints: YYYYMMDD, -Nu (u in d/w/mo/y), or 'now'.",
    ),
    type_filter: str = typer.Option(
        "TRADE", "--type",
        help="Transaction type filter: TRADE, DIVIDEND_OR_INTEREST, JOURNAL, "
             "RECEIVE_AND_DELIVER, or ALL for no filter.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
    as_md: bool = typer.Option(False, "--md", help="Output GitHub-flavored markdown."),
) -> None:
    transactions_cmd.run(
        account,
        range_str=range_str,
        type_filter=type_filter,
        as_json=as_json,
        as_md=as_md,
    )
