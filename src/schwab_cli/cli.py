import typer

from schwab_cli.commands import accounts as accounts_cmd
from schwab_cli.commands import auth as auth_cmd
from schwab_cli.commands import option as option_cmd
from schwab_cli.commands import quote as quote_cmd
from schwab_cli.commands import setup as setup_cmd

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
def setup() -> None:
    setup_cmd.run()


@app.command("auth", help="Authenticate with Schwab (refresh or full OAuth).")
def auth(
    force: bool = typer.Option(
        False,
        "--force",
        help="Skip session refresh and run the full OAuth flow.",
    ),
) -> None:
    auth_cmd.run(force=force)


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
