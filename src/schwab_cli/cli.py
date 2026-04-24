import typer

from schwab_cli._doc import doc_option
from schwab_cli.commands import accounts as accounts_cmd
from schwab_cli.commands import auth as auth_cmd
from schwab_cli.commands import dividends as dividends_cmd
from schwab_cli.commands import fundamentals as fundamentals_cmd
from schwab_cli.commands import greeks as greeks_cmd
from schwab_cli.commands import history as history_cmd
from schwab_cli.commands import mcp as mcp_cmd
from schwab_cli.commands import notify as notify_cmd
from schwab_cli.commands import option as option_cmd
from schwab_cli.commands import quote as quote_cmd
from schwab_cli.commands import setup as setup_cmd
from schwab_cli.commands import skew as skew_cmd
from schwab_cli.commands import strategy as strategy_cmd
from schwab_cli.commands import stream as stream_cmd
from schwab_cli.commands import transactions as transactions_cmd
from schwab_cli.commands import vol as vol_cmd

app = typer.Typer(
    name="schwab_cli",
    help="Charles Schwab CLI.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main(
    doc: bool = doc_option(),
) -> None:
    """Charles Schwab CLI."""


@app.command("setup", help="Configure Schwab CLI credentials.")
def setup(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the resulting config.json to stdout without saving it.",
    ),
    doc: bool = doc_option(),
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
    doc: bool = doc_option(),
) -> None:
    auth_cmd.run(force=force, manual=manual)


@app.command("accounts", help="List Schwab accounts.")
def accounts(
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
    as_md: bool = typer.Option(False, "--md", help="Output GitHub-flavored markdown."),
    doc: bool = doc_option(),
) -> None:
    accounts_cmd.run_list(as_json=as_json, as_md=as_md)


@app.command("account", help="Show one Schwab account by number (or suffix).")
def account(
    account_number: str = typer.Argument(..., help="Full number or last-N-digit suffix."),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
    as_md: bool = typer.Option(False, "--md", help="Output GitHub-flavored markdown."),
    doc: bool = doc_option(),
) -> None:
    accounts_cmd.run_show(account_number, as_json=as_json, as_md=as_md)


@app.command("positions", help="List positions across accounts (or one account).")
def positions(
    account_number: str = typer.Argument(None, help="Optional account number or suffix."),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
    as_md: bool = typer.Option(False, "--md", help="Output GitHub-flavored markdown."),
    doc: bool = doc_option(),
) -> None:
    accounts_cmd.run_positions(account_number, as_json=as_json, as_md=as_md)


@app.command("quote", help="Get real-time quotes for one or more symbols.")
def quote(
    symbols: list[str] = typer.Argument(..., help="One or more ticker symbols."),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
    as_md: bool = typer.Option(False, "--md", help="Output GitHub-flavored markdown."),
    doc: bool = doc_option(),
) -> None:
    quote_cmd.run(symbols, as_json=as_json, as_md=as_md)


@app.command(
    "fundamentals",
    help=(
        "Show company fundamentals (valuation, margins, balance sheet) for "
        "one or more symbols. One API call (quotes + fundamental fields)."
    ),
)
def fundamentals(
    symbols: list[str] = typer.Argument(..., help="One or more ticker symbols."),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
    as_md: bool = typer.Option(False, "--md", help="Output GitHub-flavored markdown."),
    doc: bool = doc_option(),
) -> None:
    fundamentals_cmd.run(symbols, as_json=as_json, as_md=as_md)


@app.command(
    "dividends",
    help=(
        "Show most-recent + next-upcoming dividend for one or more symbols. "
        "Schwab's API doesn't expose a historical series — use --upcoming to "
        "filter rows by next ex-date within a window."
    ),
)
def dividends(
    symbols: list[str] = typer.Argument(..., help="One or more ticker symbols."),
    upcoming: bool = typer.Option(
        False,
        "--upcoming",
        help="Keep only rows whose next ex-date is within --within-days.",
    ),
    within_days: int = typer.Option(
        30,
        "--within-days",
        help="Window (in days) for --upcoming; ignored otherwise.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
    as_md: bool = typer.Option(False, "--md", help="Output GitHub-flavored markdown."),
    doc: bool = doc_option(),
) -> None:
    dividends_cmd.run(
        symbols,
        upcoming=upcoming,
        within_days=within_days,
        as_json=as_json,
        as_md=as_md,
    )


@app.command("div", hidden=True, help="Alias for `dividends`.")
def div(
    symbols: list[str] = typer.Argument(..., help="One or more ticker symbols."),
    upcoming: bool = typer.Option(
        False, "--upcoming",
        help="Keep only rows whose next ex-date is within --within-days.",
    ),
    within_days: int = typer.Option(
        30, "--within-days",
        help="Window (in days) for --upcoming; ignored otherwise.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
    as_md: bool = typer.Option(False, "--md", help="Output GitHub-flavored markdown."),
    doc: bool = doc_option(),
) -> None:
    dividends_cmd.run(
        symbols,
        upcoming=upcoming,
        within_days=within_days,
        as_json=as_json,
        as_md=as_md,
    )


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
    doc: bool = doc_option(),
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
    doc: bool = doc_option(),
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
    doc: bool = doc_option(),
) -> None:
    history_cmd.run(
        symbol,
        range_str=range_str,
        interval_str=interval_str,
        as_json=as_json,
        as_md=as_md,
    )


@app.command(
    "vol",
    help=(
        "Show volatility context for a stock: IV, HV, HVP, P/C Ratio. "
        "Uses two API calls; no local storage in phase 1."
    ),
)
def vol(
    symbol: str = typer.Argument(..., help="Stock ticker, e.g. NVDA."),
    hv_window: int = typer.Option(
        30,
        "--hv-window",
        help="Rolling HV window in trading days (default 30).",
    ),
    hv_lookback: int = typer.Option(
        252,
        "--hv-lookback",
        help="HVP percentile lookback in trading days (default 252 ≈ 1 year).",
    ),
    ivp_lookback: int = typer.Option(
        252,
        "--ivp-lookback",
        help="IVP percentile lookback in trading days (default 252).",
    ),
    no_record: bool = typer.Option(
        False,
        "--no-record",
        help="Do not append today's ATM IV to the local store.",
    ),
    snapshot_only: bool = typer.Option(
        False,
        "--snapshot-only",
        help="Record today's ATM IV and exit silently (cron-friendly).",
    ),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
    as_md: bool = typer.Option(False, "--md", help="Output GitHub-flavored markdown."),
    doc: bool = doc_option(),
) -> None:
    vol_cmd.run(
        symbol,
        hv_window=hv_window,
        hv_lookback=hv_lookback,
        ivp_lookback=ivp_lookback,
        no_record=no_record,
        snapshot_only=snapshot_only,
        as_json=as_json,
        as_md=as_md,
    )


@app.command(
    "skew",
    help=(
        "Option skew / smile metrics at L1 (single chain), L2 (term "
        "structure via --term or --dtes), or L3 (cross-ticker via "
        "--cross). See `schwab_cli skew --doc` for full usage."
    ),
)
def skew(
    args: list[str] = typer.Argument(
        ...,
        help=(
            "L1: SYMBOL YYMMDD  |  L2 --term: SYMBOL YYMMDD [YYMMDD ...]  |  "
            "L2 --dtes: SYMBOL N [N ...]  |  L3 --cross: YYMMDD SYMBOL [SYMBOL ...]"
        ),
    ),
    term: bool = typer.Option(
        False, "--term",
        help="L2 term structure — remaining args are YYMMDD expiries.",
    ),
    dtes: bool = typer.Option(
        False, "--dtes",
        help="L2 term structure by target DTE — remaining args are integer DTEs.",
    ),
    cross: bool = typer.Option(
        False, "--cross",
        help="L3 cross-ticker — args[0] is YYMMDD, remaining are symbols.",
    ),
    strikes: int = typer.Option(
        40, "--strikes",
        help="Chain width per expiry (total strikes around ATM).",
    ),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
    as_md: bool = typer.Option(False, "--md", help="Output GitHub-flavored markdown."),
    doc: bool = doc_option(),
) -> None:
    skew_cmd.run(
        args,
        term=term,
        dtes=dtes,
        cross=cross,
        strikes=strikes,
        as_json=as_json,
        as_md=as_md,
    )


mcp_app = typer.Typer(
    help=(
        "Run and manage the Schwab MCP server. Bare `mcp` starts "
        "the daemon; use subcommands for status / log / logout / "
        "install."
    ),
    no_args_is_help=False,
    invoke_without_command=True,
)
app.add_typer(mcp_app, name="mcp")


@mcp_app.callback(invoke_without_command=True)
def mcp_root(
    ctx: typer.Context,
    stdio: bool = typer.Option(
        True, "--stdio/--sse",
        help="Transport. --sse runs a long-lived daemon on --host / --port.",
    ),
    host: str = typer.Option(
        "127.0.0.1", "--host",
        help="SSE bind host. Loopback-only by default.",
    ),
    port: int = typer.Option(
        7234, "--port",
        help="SSE bind port.",
    ),
    log_file: str = typer.Option(
        None, "--log-file",
        help="Path to the structured log file. Default: ~/.config/schwab_cli/mcp.log.",
    ),
    no_log_file: bool = typer.Option(
        False, "--no-log-file",
        help="Disable the disk log; events still go to stderr.",
    ),
    doc: bool = doc_option(),
) -> None:
    """When no subcommand, run the daemon."""
    if ctx.invoked_subcommand is not None:
        return
    mcp_cmd.run(
        stdio=stdio, host=host, port=port,
        log_file=log_file, no_log_file=no_log_file,
    )


@mcp_app.command("status", help="Print a snapshot of the running MCP server.")
def mcp_status(
    url: str = typer.Option(
        None, "--url",
        help="SSE URL of the running server (default: http://127.0.0.1:7234).",
    ),
    token: str = typer.Option(None, "--token", help="Bearer token if set at start."),
    as_json: bool = typer.Option(False, "--json", help="Raw JSON output."),
) -> None:
    mcp_cmd.run_status(url=url, token=token, as_json=as_json)


@mcp_app.command("log", help="Read or tail the MCP server's structured log.")
def mcp_log(
    follow: bool = typer.Option(False, "-f", "--follow", help="Tail the file."),
    log_file: str = typer.Option(
        None, "--log-file",
        help="Log file path (default: ~/.config/schwab_cli/mcp.log).",
    ),
    session: str = typer.Option(None, "--session", help="Filter by session id."),
    symbol: str = typer.Option(None, "--symbol", help="Filter by symbol."),
    level: str = typer.Option(
        None, "--level",
        help="Filter: info | warning | error (shows that level and above).",
    ),
    as_json: bool = typer.Option(False, "--json", help="Raw JSONL pass-through."),
    tail: int = typer.Option(None, "--tail", help="Show only the last N lines."),
) -> None:
    mcp_cmd.run_log(
        follow=follow, log_file=log_file,
        session=session, symbol=symbol, level=level,
        as_json=as_json, tail=tail,
    )


@mcp_app.command("logout", help="Gracefully shut down a running MCP server.")
def mcp_logout(
    url: str = typer.Option(None, "--url"),
    token: str = typer.Option(None, "--token"),
) -> None:
    mcp_cmd.run_logout(url=url, token=token)


@mcp_app.command("restart", help="Logout + start again in-place.")
def mcp_restart(
    url: str = typer.Option(None, "--url"),
    token: str = typer.Option(None, "--token"),
    stdio: bool = typer.Option(False, "--stdio/--sse"),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(7234, "--port"),
) -> None:
    mcp_cmd.run_restart(
        url=url, token=token, stdio=stdio, host=host, port=port,
    )


@mcp_app.command("install", help="Register this MCP server in ~/.claude/settings.json.")
def mcp_install(
    stdio: bool = typer.Option(
        False, "--stdio/--sse",
        help="Which entry to install. Default is SSE if a daemon is implied.",
    ),
    url: str = typer.Option(
        "http://127.0.0.1:7234/sse", "--url",
        help="SSE URL (ignored for --stdio).",
    ),
    token: str = typer.Option(
        None, "--token",
        help="Bearer token to include in the entry (SSE only).",
    ),
    settings: str = typer.Option(
        None, "--claude-settings",
        help="Override path (default: ~/.claude/settings.json).",
    ),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation."),
    force: bool = typer.Option(False, "--force", help="Overwrite existing entry."),
) -> None:
    mcp_cmd.run_install(
        stdio=stdio, url=url, token=token, settings=settings,
        yes=yes, force=force,
    )


@app.command(
    "strategy",
    help=(
        "Option-strategy probability + risk analysis. Pass one or more "
        "--leg tokens in OCC-style form: ±N@YYYYMMDD{C|P}STRIKE. See "
        "`schwab_cli strategy --doc` for the full grammar and examples."
    ),
)
def strategy(
    symbol: str = typer.Argument(..., help="Underlying ticker, e.g. AMZN."),
    leg: list[str] = typer.Option(
        ..., "--leg",
        help=(
            "Option leg in OCC form: ±N@YYYYMMDD{C|P}STRIKE. Repeat for "
            "multi-leg positions. Example: --leg +1@20260501C255."
        ),
    ),
    risk_free: float = typer.Option(
        0.0, "--risk-free",
        help="Annualised risk-free rate for the log-normal drift (default 0).",
    ),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
    as_md: bool = typer.Option(False, "--md", help="Output GitHub-flavored markdown."),
    doc: bool = doc_option(),
) -> None:
    strategy_cmd.run(
        symbol,
        leg,
        risk_free=risk_free,
        as_json=as_json,
        as_md=as_md,
    )


@app.command(
    "stream",
    help=(
        "Watch live Schwab quotes in the terminal. Connects to a "
        "running MCP daemon if one is reachable, else opens a direct "
        "Schwab streamer connection. Ctrl+C to stop cleanly."
    ),
)
def stream(
    symbols: list[str] = typer.Argument(
        ..., help="Ticker symbols (e.g. NVDA AAPL).",
    ),
    fields: str = typer.Option(
        None, "--fields",
        help="Comma-separated field subset (default: bid,ask,last,volume).",
    ),
    as_json: bool = typer.Option(False, "--json", help="One JSON object per line."),
    via_mcp: bool = typer.Option(
        False, "--mcp",
        help="Force MCP path; exit non-zero if no daemon is reachable.",
    ),
    direct: bool = typer.Option(
        False, "--direct",
        help="Bypass MCP, connect directly to Schwab streamer.",
    ),
    mcp_url: str = typer.Option(
        "http://127.0.0.1:7234/sse", "--mcp-url",
        help="SSE URL of the MCP daemon (only used with --mcp).",
    ),
    doc: bool = doc_option(),
) -> None:
    stream_cmd.run(
        symbols,
        fields=fields,
        as_json=as_json,
        via_mcp=via_mcp,
        direct=direct,
        mcp_url=mcp_url,
    )


notify_app = typer.Typer(
    help=(
        "Manage notification channels (Telegram). Config lives in "
        "~/.config/schwab_cli/notification.json — separate from "
        "config.json so `setup` doesn't clobber it."
    ),
    no_args_is_help=True,
)
app.add_typer(notify_app, name="notify")


@notify_app.command("list", help="Show configured notification channels.")
def notify_list(
    path: str = typer.Option(None, "--path", help="Override config path."),
) -> None:
    notify_cmd.run_list(path=path)


@notify_app.command("test", help="Fire a test notification through configured channels.")
def notify_test(
    channel: str = typer.Option(
        "all", "--channel",
        help="Channel to test: telegram | all.",
    ),
    path: str = typer.Option(None, "--path", help="Override config path."),
) -> None:
    notify_cmd.run_test(channel=channel, path=path)


@notify_app.command("setup", help="Interactive Telegram setup — writes notification.json.")
def notify_setup(
    channel: str = typer.Option(
        "telegram", "--channel",
        help="Channel to configure (telegram only for now).",
    ),
    path: str = typer.Option(None, "--path", help="Override config path."),
) -> None:
    notify_cmd.run_setup(channel=channel, path=path)


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
    doc: bool = doc_option(),
) -> None:
    transactions_cmd.run(
        account,
        range_str=range_str,
        type_filter=type_filter,
        as_json=as_json,
        as_md=as_md,
    )
