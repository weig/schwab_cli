import typer

from schwab_cli._doc import doc_option
from schwab_cli.commands import accounts as accounts_cmd
from schwab_cli.commands import auth as auth_cmd
from schwab_cli.commands import breadth as breadth_cmd
from schwab_cli.commands import dataset as dataset_cmd
from schwab_cli.commands import watch as watch_cmd
from schwab_cli.commands import dividends as dividends_cmd
from schwab_cli.commands import doctor as doctor_cmd
from schwab_cli.commands import fundamentals as fundamentals_cmd
from schwab_cli.commands import greeks as greeks_cmd
from schwab_cli.commands import history as history_cmd
from schwab_cli.commands import mcp as mcp_cmd
from schwab_cli.commands import notify as notify_cmd
from schwab_cli.commands import performance as performance_cmd
from schwab_cli.commands import option as option_cmd
from schwab_cli.commands import order as order_cmd
from schwab_cli.commands import profile as profile_cmd
from schwab_cli.commands import quote as quote_cmd
from schwab_cli.commands import server as server_cmd
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


@app.command("doctor", help="Health check: install, MCP, auth, dataset.")
def doctor(doc: bool = doc_option()) -> None:
    doctor_cmd.run()


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
            "Skip the auto-login subprocess (if configured) and drive the "
            "Schwab login yourself in the browser; paste the redirect URL "
            "when prompted. The CodeRelayHandler (when configured) still "
            "joins the race alongside the paste fallback."
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
    "performance",
    help=(
        "Time-weighted return (TWR) for account(s) over a date range "
        "with SPX / COMP / RUT comparison. Default range is YTD."
    ),
)
def performance(
    range_str: str = typer.Option(
        "ytd", "--range",
        help="Date range: '<start>..<end>' or one of: ytd, mtd, wtd. "
             "Endpoints: YYYYMMDD, -Nu (u in d/w/mo/y), or 'now'.",
    ),
    account: str = typer.Option(
        None, "--account",
        help="Filter to one account (full number or last-4 suffix).",
    ),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
    doc: bool = doc_option(),
) -> None:
    performance_cmd.run(
        range_str=range_str, account=account, as_json=as_json,
    )


@app.command(
    "breadth",
    help=(
        "Market breadth: % of index constituents above SMA at "
        "Bloomberg-style timeframes (5D … 2Y). Lazy-fills OHLCV cache."
    ),
)
def breadth(
    indices: str = typer.Option(
        "SPX,NQ,DJI", "--indices",
        help="Comma-separated index codes (SPX, NQ, DJI). "
             "NQ = Nasdaq 100, closest cleanly available proxy for COMP.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
    refresh_members: bool = typer.Option(
        False, "--refresh-members",
        help="(reserved) re-fetch constituent lists; currently always live.",
    ),
    doc: bool = doc_option(),
) -> None:
    breadth_cmd.run(
        indices=[s.strip().upper() for s in indices.split(",") if s.strip()],
        as_json=as_json,
        refresh_members=refresh_members,
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
app.add_typer(dataset_cmd.app, name="dataset")
app.add_typer(mcp_app, name="mcp")


watch_app = typer.Typer(
    help=(
        "Manual ticker watchlist. Subscribes to OHLCV + volatility "
        "automatically on `add`; demotes to GRACE on `remove`."
    ),
    no_args_is_help=True,
)
app.add_typer(watch_app, name="watch")


@watch_app.command("add", help="Add a ticker to the watchlist.")
def watch_add(
    symbol: str = typer.Argument(..., help="Ticker (e.g. NVDA)."),
    doc: bool = doc_option(),
) -> None:
    watch_cmd.run_add(symbol)


@watch_app.command("remove", help="Remove a ticker from the watchlist.")
def watch_remove(
    symbol: str = typer.Argument(..., help="Ticker (e.g. NVDA)."),
    doc: bool = doc_option(),
) -> None:
    watch_cmd.run_remove(symbol)


@watch_app.command("list", help="Snapshot table: bid/ask/sizes/vol/OHLC.")
def watch_list(
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
    doc: bool = doc_option(),
) -> None:
    watch_cmd.run_list(as_json=as_json)


@watch_app.command("show", help="Live streaming table. Ctrl-C to exit.")
def watch_show(doc: bool = doc_option()) -> None:
    watch_cmd.run_show()


@mcp_app.callback(invoke_without_command=True)
def mcp_root(
    ctx: typer.Context,
    host: str = typer.Option(
        "127.0.0.1", "--host",
        help="HTTP bind host. Loopback-only by default.",
    ),
    port: int = typer.Option(
        7234, "--port",
        help="HTTP bind port.",
    ),
    log_file: str = typer.Option(
        None, "--log-file",
        help="Path to the structured log file. Default: ~/.config/schwab_cli/mcp.log.",
    ),
    no_log_file: bool = typer.Option(
        False, "--no-log-file",
        help="Disable the disk log; events still go to stderr.",
    ),
    no_auto_login: bool = typer.Option(
        False, "--no-auto-login",
        help=(
            "Disable browser auto-login. If the refresh token has "
            "expired at startup, exit 1 instead of spawning "
            "`schwab_cli auth --force`. Also disables the proactive "
            "rotation task that runs at the 1h expiry threshold."
        ),
    ),
    doc: bool = doc_option(),
) -> None:
    """When no subcommand, run the daemon (Streamable HTTP only)."""
    if ctx.invoked_subcommand is not None:
        return
    mcp_cmd.run(
        host=host, port=port,
        log_file=log_file, no_log_file=no_log_file,
        no_auto_login=no_auto_login,
    )


@mcp_app.command("status", help="Print a snapshot of the running MCP server.")
def mcp_status(
    url: str = typer.Option(
        None, "--url",
        help="Base URL of the running server (default: http://127.0.0.1:7234).",
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
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(7234, "--port"),
) -> None:
    mcp_cmd.run_restart(
        url=url, token=token, host=host, port=port,
    )


@mcp_app.command(
    "install-service",
    help=(
        "Install a macOS launchd LaunchAgent so the Streamable HTTP "
        "daemon starts on login and restarts on exit."
    ),
)
def mcp_install_service(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(7234, "--port"),
    log_file: str = typer.Option(
        None, "--log-file",
        help="Captures stderr+stdout. Default: ~/Library/Logs/schwab_cli-mcp.log",
    ),
    admin_token: str = typer.Option(None, "--admin-token"),
    plist_path: str = typer.Option(None, "--plist-path"),
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    mcp_cmd.run_install_service(
        host=host, port=port, log_file=log_file,
        admin_token=admin_token, plist_path=plist_path, yes=yes,
    )


@mcp_app.command("start-service", help="launchctl load the installed plist.")
def mcp_start_service(
    plist_path: str = typer.Option(None, "--plist-path"),
) -> None:
    mcp_cmd.run_start_service(plist_path=plist_path)


@mcp_app.command("stop-service", help="launchctl unload — stops without auto-restart.")
def mcp_stop_service(
    plist_path: str = typer.Option(None, "--plist-path"),
) -> None:
    mcp_cmd.run_stop_service(plist_path=plist_path)


@mcp_app.command("uninstall-service", help="Unload and remove the launchd plist.")
def mcp_uninstall_service(
    plist_path: str = typer.Option(None, "--plist-path"),
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    mcp_cmd.run_uninstall_service(plist_path=plist_path, yes=yes)


@mcp_app.command("install", help="Register this MCP server in ~/.claude/settings.json.")
def mcp_install(
    url: str = typer.Option(
        "http://127.0.0.1:7234/mcp", "--url",
        help="Streamable HTTP URL of the daemon's /mcp endpoint.",
    ),
    token: str = typer.Option(
        None, "--token",
        help="Bearer token to include in the entry.",
    ),
    settings: str = typer.Option(
        None, "--claude-settings",
        help="Override path (default: ~/.claude/settings.json).",
    ),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation."),
    force: bool = typer.Option(False, "--force", help="Overwrite existing entry."),
) -> None:
    mcp_cmd.run_install(
        url=url, token=token, settings=settings,
        yes=yes, force=force,
    )


# ---- server sub-app ---------------------------------------------------

server_app = typer.Typer(
    help=(
        "Run and manage the auth-maintenance server. Bare `server` "
        "runs a long-lived loop that keeps the OAuth refresh token "
        "alive. Add `--enable-mcp` to ALSO run the Streamable HTTP MCP "
        "server on top of that always-running maintenance loop (the "
        "loop is the single proactive token renewer; the MCP server "
        "runs with auth monitoring disabled). Use subcommands to "
        "install / uninstall / check the launchd LaunchAgent."
    ),
    no_args_is_help=False,
    invoke_without_command=True,
)
app.add_typer(server_app, name="server")


@server_app.callback(invoke_without_command=True)
def server_root(
    ctx: typer.Context,
    interval_hours: float = typer.Option(
        8.0, "--interval-hours",
        help="Hours between maintenance ticks. Default: 8.",
    ),
    enable_mcp: bool = typer.Option(
        False, "--enable-mcp",
        help=(
            "Also run the Streamable HTTP MCP server on top of the "
            "always-running maintenance loop. The loop remains the "
            "single proactive refresh-token renewer; the MCP server "
            "runs with auth monitoring disabled to avoid competing "
            "rotation."
        ),
    ),
    mcp_host: str = typer.Option(
        "127.0.0.1", "--mcp-host",
        help="MCP HTTP bind host (only used with --enable-mcp). "
        "Loopback-only by default.",
    ),
    mcp_port: int = typer.Option(
        7234, "--mcp-port",
        help="MCP HTTP bind port (only used with --enable-mcp).",
    ),
    enable_rest: bool = typer.Option(
        False, "--enable-rest",
        help=(
            "Also serve the REST PoC. UNAUTHENTICATED proof of the "
            "REST -> service path (GET /quote/{symbol}); auth/"
            "allowlisting is a deliberate later step. Standalone it "
            "runs on --rest-host:--rest-port; combined with "
            "--enable-mcp its routes mount onto the MCP server's port."
        ),
    ),
    rest_host: str = typer.Option(
        "127.0.0.1", "--rest-host",
        help="REST PoC bind host (only used with --enable-rest, "
        "standalone). Loopback-only by default.",
    ),
    rest_port: int = typer.Option(
        8000, "--rest-port",
        help="REST PoC bind port (only used with --enable-rest, "
        "standalone).",
    ),
    log_file: str = typer.Option(
        None, "--log-file",
        help="Path to the MCP structured log file (only used with "
        "--enable-mcp). Default: ~/.config/schwab_cli/mcp.log.",
    ),
    no_log_file: bool = typer.Option(
        False, "--no-log-file",
        help="Disable the MCP disk log; events still go to stderr "
        "(only used with --enable-mcp).",
    ),
    no_auto_login: bool = typer.Option(
        False, "--no-auto-login",
        help=(
            "Disable browser auto-login at MCP startup (only used with "
            "--enable-mcp). If the refresh token has expired at "
            "startup, exit 1 instead of spawning `schwab_cli auth "
            "--force`."
        ),
    ),
) -> None:
    """Bare `schwab server` → maintenance loop; `--enable-mcp` adds MCP."""
    if ctx.invoked_subcommand is not None:
        return
    server_cmd.run(
        interval_s=int(interval_hours * 3600),
        enable_mcp=enable_mcp,
        mcp_host=mcp_host,
        mcp_port=mcp_port,
        enable_rest=enable_rest,
        rest_host=rest_host,
        rest_port=rest_port,
        log_file=log_file,
        no_log_file=no_log_file,
        no_auto_login=no_auto_login,
    )


@server_app.command("install", help="Install + load the launchd LaunchAgent.")
def server_install(
    plist_path: str = typer.Option(
        None, "--plist-path",
        help="Override plist path (default: ~/Library/LaunchAgents/"
        "com.schwab-cli.server.plist).",
    ),
    log_file: str = typer.Option(
        None, "--log-file",
        help="Capture stdout+stderr to this file.",
    ),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation."),
) -> None:
    server_cmd.run_install(plist_path=plist_path, log_file=log_file, yes=yes)


@server_app.command("uninstall", help="Unload and remove the launchd plist.")
def server_uninstall(
    plist_path: str = typer.Option(
        None, "--plist-path",
        help="Override plist path.",
    ),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation."),
) -> None:
    server_cmd.run_uninstall(plist_path=plist_path, yes=yes)


@server_app.command("status", help="Report whether the server job is loaded.")
def server_status() -> None:
    server_cmd.run_status()


@app.command(
    "strategy",
    help=(
        "Option-strategy probability + risk analysis. Pass one or more "
        "--leg tokens in OCC-style form: ±N@YYMMDD{C|P}STRIKE. See "
        "`schwab_cli strategy --doc` for the full grammar and examples."
    ),
)
def strategy(
    symbol: str = typer.Argument(..., help="Underlying ticker, e.g. AMZN."),
    leg: list[str] = typer.Option(
        ..., "--leg",
        help=(
            "Option leg in OCC form: ±N@YYMMDD{C|P}STRIKE. Repeat for "
            "multi-leg positions. Example: --leg +1@260501C255."
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
    force: bool = typer.Option(
        False, "--force",
        help=(
            "With --direct, proceed even when an MCP daemon is running "
            "(Schwab allows only one streamer session per account — the "
            "direct connection may disconnect the daemon's streamer)."
        ),
    ),
    mcp_url: str = typer.Option(
        "http://127.0.0.1:7234/mcp", "--mcp-url",
        help="Streamable HTTP URL of the MCP daemon (only used with --mcp).",
    ),
    doc: bool = doc_option(),
) -> None:
    stream_cmd.run(
        symbols,
        fields=fields,
        as_json=as_json,
        via_mcp=via_mcp,
        direct=direct,
        force=force,
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
    account: str = typer.Option(
        None, "--account", "-a",
        help="Account number or suffix to filter to (e.g. '0756'). "
             "Omit to show all accounts. When supplied, the Account column "
             "is dropped from human/MD output (redundant).",
    ),
    range_str: str = typer.Option(
        "-7d..now", "--range", "-r",
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
    refresh: bool = typer.Option(
        False, "--refresh",
        help="Bypass the local cache for this run and re-fetch from Schwab. "
             "Result still upserts into the cache.",
    ),
    doc: bool = doc_option(),
) -> None:
    transactions_cmd.run(
        account,
        range_str=range_str,
        type_filter=type_filter,
        as_json=as_json,
        as_md=as_md,
        refresh=refresh,
    )


# ---- order subcommand group ----------------------------------------------

order_app = typer.Typer(
    help=(
        "Place, preview, list, get, and cancel Schwab orders. Phase 1 "
        "supports equity (single leg) and option orders (single or "
        "multi-leg via --leg or --parse). Confirmation prompt requires "
        'typing "yes" unless --yes is passed.'
    ),
    no_args_is_help=True,
)
app.add_typer(order_app, name="order")


@order_app.command("place", help="Place an order. Always shows a confirmation panel.")
def order_place(
    symbol: str = typer.Argument(
        None,
        help="Underlying symbol for an equity order. Omit when using --leg or --parse.",
    ),
    account: str = typer.Option(
        None, "--account", "-a",
        help="Account number or trailing-digit suffix (required for place).",
    ),
    order_type: str = typer.Option(
        None, "--type",
        help="MARKET, LIMIT, NET_DEBIT, NET_CREDIT (Phase 1).",
    ),
    price: float = typer.Option(
        None, "--price",
        help="Limit / net price. Required for LIMIT, NET_DEBIT, NET_CREDIT.",
    ),
    quantity: int = typer.Option(
        None, "--quantity", "-q",
        help="Equity share count (default 1). Multi-leg uses qty in --leg.",
    ),
    side: str = typer.Option(
        None, "--side",
        help="Equity instruction: BUY, SELL, SELL_SHORT, BUY_TO_COVER (default BUY).",
    ),
    duration: str = typer.Option(
        None, "--duration",
        help="DAY, GTC (=GOOD_TILL_CANCEL), FOK, IOC, etc. (default DAY).",
    ),
    session: str = typer.Option(
        "NORMAL", "--session",
        help="NORMAL, AM, PM, SEAMLESS. AM/PM/SEAMLESS require LIMIT+DAY.",
    ),
    legs: list[str] = typer.Option(
        [], "--leg",
        help="Repeatable option leg spec: ±N@YYMMDD{C|P}STRIKE[o|c].",
    ),
    complex_strategy: str = typer.Option(
        None, "--complex",
        help="complexOrderStrategyType (NONE, VERTICAL, CALENDAR, ..., CUSTOM, AUTO).",
    ),
    special: str = typer.Option(
        None, "--special",
        help="specialInstruction (ALL_OR_NONE, DO_NOT_REDUCE, ...).",
    ),
    parse_string: str = typer.Option(
        None, "--parse",
        help='Schwab/TOS-style ticket, e.g. "BUY +1 VERTICAL AMZN 1 MAY 26 250/260 CALL @1.50 LMT".',
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Render the confirmation panel and exit without placing the order (same as `order preview`).",
    ),
    yes: bool = typer.Option(
        False, "--yes",
        help='Skip the "yes" confirmation prompt. Panel still renders for the record.',
    ),
    as_json: bool = typer.Option(
        False, "--json",
        help="Emit JSON on stdout (panel still goes to stderr).",
    ),
    profile: str = typer.Option(
        None, "--profile",
        help="Policy profile name (default: default; honours $SCHWAB_CLI_PROFILE).",
    ),
    override_reason: str = typer.Option(
        None, "--override",
        help=(
            "Bypass the policy gate. Reason 10-500 chars; logged "
            "verbatim. Requires --override-confirm. Tier-driven ceremony "
            "follows (cli / telegram_notify_then_cli / telegram_inbound)."
        ),
    ),
    override_confirm: bool = typer.Option(
        False, "--override-confirm",
        help="Required companion flag for --override. Without it the "
             "override has no effect (ceremony — see docs/plan/order.md).",
    ),
    stop_price: float = typer.Option(
        None, "--stop-price",
        help="Trigger price for STOP / STOP_LIMIT orders. Required when "
             "--type is STOP or STOP_LIMIT.",
    ),
    trailing_offset: float = typer.Option(
        None, "--trailing-offset",
        help="Distance the stop trails the basis. VALUE = absolute "
             "dollars (e.g. 1.50); PERCENT = whole-number percent "
             "(e.g. 5 for 5%). Required for TRAILING_STOP*.",
    ),
    trailing_basis: str = typer.Option(
        None, "--trailing-basis",
        help="What the trail anchors to: BID, ASK, LAST, MARK. "
             "Required for TRAILING_STOP*.",
    ),
    trailing_type: str = typer.Option(
        None, "--trailing-type",
        help="VALUE (absolute) or PERCENT (relative). "
             "Required for TRAILING_STOP*.",
    ),
    doc: bool = doc_option(),
) -> None:
    order_cmd.run_place(
        symbol=symbol, account=account,
        order_type=order_type, price=price, quantity=quantity, side=side,
        duration=duration, session=session,
        leg_specs=tuple(legs), complex_strategy=complex_strategy,
        special=special, parse_string=parse_string,
        dry_run=dry_run, yes=yes, as_json=as_json,
        profile=profile,
        override_reason=override_reason,
        override_confirm=override_confirm,
        stop_price=stop_price,
        trailing_offset=trailing_offset,
        trailing_basis=trailing_basis,
        trailing_type=trailing_type,
    )


@order_app.command("preview", help="Preview an order — same as `place --dry-run`.")
def order_preview(
    symbol: str = typer.Argument(None, help="Equity underlying."),
    account: str = typer.Option(
        None, "--account", "-a", help="Account number / suffix (required)."
    ),
    order_type: str = typer.Option(None, "--type"),
    price: float = typer.Option(None, "--price"),
    quantity: int = typer.Option(None, "--quantity", "-q"),
    side: str = typer.Option(None, "--side"),
    duration: str = typer.Option(None, "--duration"),
    session: str = typer.Option("NORMAL", "--session"),
    legs: list[str] = typer.Option([], "--leg"),
    complex_strategy: str = typer.Option(None, "--complex"),
    special: str = typer.Option(None, "--special"),
    parse_string: str = typer.Option(None, "--parse"),
    as_json: bool = typer.Option(False, "--json"),
    stop_price: float = typer.Option(None, "--stop-price"),
    trailing_offset: float = typer.Option(None, "--trailing-offset"),
    trailing_basis: str = typer.Option(None, "--trailing-basis"),
    trailing_type: str = typer.Option(None, "--trailing-type"),
    profile: str = typer.Option(
        None, "--profile",
        help="Policy profile name (default: default; honours $SCHWAB_CLI_PROFILE).",
    ),
    doc: bool = doc_option(),
) -> None:
    order_cmd.run_place(
        symbol=symbol, account=account,
        order_type=order_type, price=price, quantity=quantity, side=side,
        duration=duration, session=session,
        leg_specs=tuple(legs), complex_strategy=complex_strategy,
        special=special, parse_string=parse_string,
        dry_run=True, yes=False, as_json=as_json,
        profile=profile,
        stop_price=stop_price,
        trailing_offset=trailing_offset,
        trailing_basis=trailing_basis,
        trailing_type=trailing_type,
    )


@order_app.command("get", help="Fetch one order by id.")
def order_get(
    order_id: str = typer.Argument(..., help="Schwab order id."),
    account: str = typer.Option(
        None, "--account", "-a",
        help="Account number / suffix (recommended; warns if omitted).",
    ),
    as_json: bool = typer.Option(False, "--json"),
    doc: bool = doc_option(),
) -> None:
    order_cmd.run_get(order_id=order_id, account=account, as_json=as_json)


@order_app.command("list", help="List orders. Defaults: --status=ACTIVE → --range=ALL.")
def order_list(
    account: str = typer.Option(
        None, "--account", "-a",
        help="Account number / suffix. Omit to query across all accounts (warned).",
    ),
    status: str = typer.Option(
        "ACTIVE", "--status",
        help="ACTIVE | FILLED | CANCELED | REPLACED | REJECTED | EXPIRED | ALL "
             "or any raw Schwab status (e.g. WORKING).",
    ),
    range_str: str = typer.Option(
        None, "--range",
        help="Time range. Defaults to ALL when status=ACTIVE, else -7d..now. "
             "Accepts ytd/mtd/wtd or <start>..<end> tokens (-7d, -1mo, now, YYYYMMDD).",
    ),
    limit: int = typer.Option(None, "--limit", help="Maps to maxResults."),
    as_json: bool = typer.Option(False, "--json"),
    doc: bool = doc_option(),
) -> None:
    order_cmd.run_list(
        account=account, status=status,
        range_str=range_str, limit=limit, as_json=as_json,
    )


@order_app.command("cancel", help="Cancel one order by id.")
def order_cancel(
    order_id: str = typer.Argument(..., help="Schwab order id."),
    account: str = typer.Option(
        None, "--account", "-a",
        help="Account number / suffix. Omit to scan every account (warned).",
    ),
    yes: bool = typer.Option(
        False, "--yes",
        help='Skip the "yes" confirmation prompt.',
    ),
    as_json: bool = typer.Option(False, "--json"),
    doc: bool = doc_option(),
) -> None:
    order_cmd.run_cancel(
        order_id=order_id, account=account, yes=yes, as_json=as_json,
    )


@order_app.command(
    "replace",
    help="Replace an existing limit order — V1 supports price overrides only.",
)
def order_replace(
    order_id: str = typer.Argument(..., help="Schwab order id to replace."),
    account: str = typer.Option(
        None, "--account", "-a",
        help="Account number / suffix. Omit to scan every account (warned).",
    ),
    price: float = typer.Option(
        ..., "--price",
        help="New limit price. Required.",
    ),
    yes: bool = typer.Option(
        False, "--yes",
        help='Skip the "yes" confirmation prompt.',
    ),
    as_json: bool = typer.Option(False, "--json"),
    doc: bool = doc_option(),
) -> None:
    order_cmd.run_replace(
        order_id=order_id, account=account,
        new_price=price, yes=yes, as_json=as_json,
    )


# ---- profile subcommand group -------------------------------------------

profile_app = typer.Typer(
    help=(
        "Manage profiles for the policy engine. Profiles live as one "
        "JSON file each under ~/.config/schwab_cli/profiles/<type>/. "
        "Today only `--type=order` is supported (used by `order place` "
        "/ `order preview`); future types (notification, strategy, …) "
        "share the same group."
    ),
    no_args_is_help=True,
)
app.add_typer(profile_app, name="profile")


_PROFILE_TYPE_OPT = typer.Option(
    ...,
    "--type",
    help="Profile type. Required; only `order` is supported today.",
)


def _check_profile_type(t: str) -> None:
    """Validate ``--type``. Phase 2f only ships ``order``."""
    if t != "order":
        typer.secho(
            f"--type must be 'order' (got {t!r}). "
            f"Other types (notification, strategy, …) are reserved "
            "for future phases.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=2)


@profile_app.command(
    "new",
    help=(
        "Interactively create a new profile. Top-level questionnaire "
        "+ a vim-key list editor (j/k/c/d/u/s/q). TTY-only."
    ),
)
def profile_new(
    type_: str = _PROFILE_TYPE_OPT,
    doc: bool = doc_option(),
) -> None:
    _check_profile_type(type_)
    profile_cmd.run_new()


@profile_app.command("show", help="Print the resolved profile as JSON.")
def profile_show(
    type_: str = _PROFILE_TYPE_OPT,
    profile: str = typer.Option(
        None, "--profile",
        help="Profile name (default: default; honours $SCHWAB_CLI_PROFILE).",
    ),
    doc: bool = doc_option(),
) -> None:
    _check_profile_type(type_)
    profile_cmd.run_show(profile=profile)


@profile_app.command("lint", help="Validate one or every profile file.")
def profile_lint(
    type_: str = _PROFILE_TYPE_OPT,
    profile: str = typer.Option(
        None, "--profile",
        help="Profile name (default: default; honours $SCHWAB_CLI_PROFILE).",
    ),
    all_profiles: bool = typer.Option(
        False, "--all",
        help="Validate every profile file in the profiles directory.",
    ),
    doc: bool = doc_option(),
) -> None:
    _check_profile_type(type_)
    profile_cmd.run_lint(profile=profile, all_profiles=all_profiles)


@profile_app.command("test", help="Dry-run evaluate a JSON order body.")
def profile_test(
    order_path: str = typer.Argument(
        ...,
        help="Path to JSON order body (Schwab POST shape). Use '-' for stdin.",
    ),
    type_: str = _PROFILE_TYPE_OPT,
    profile: str = typer.Option(
        None, "--profile",
        help="Profile name (default: default; honours $SCHWAB_CLI_PROFILE).",
    ),
    account: str = typer.Option(
        None, "--account", "-a",
        help="Account number to use as the `account` field for matching.",
    ),
    doc: bool = doc_option(),
) -> None:
    _check_profile_type(type_)
    profile_cmd.run_test(
        order_json_path=order_path, profile=profile, account=account,
    )


@profile_app.command("counters", help="Show persisted order counters.")
def profile_counters(
    type_: str = _PROFILE_TYPE_OPT,
    account: str = typer.Option(
        None, "--account", "-a",
        help="Limit output to one account (matches the stored 8-digit number).",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON."),
    doc: bool = doc_option(),
) -> None:
    _check_profile_type(type_)
    profile_cmd.run_counters(account=account, as_json=as_json)


@profile_app.command("audit", help="Tail the order audit log.")
def profile_audit(
    type_: str = _PROFILE_TYPE_OPT,
    since: str = typer.Option(
        None, "--since",
        help="Range token (e.g. -1d..now, ytd, mtd, 20260420..20260425). "
             "Default: last 24h.",
    ),
    account: str = typer.Option(
        None, "--account", "-a",
        help="Filter to one account (exact match on the audit row's `account`).",
    ),
    decision: str = typer.Option(
        None, "--decision",
        help="Filter to approve or reject rows.",
    ),
    limit: int = typer.Option(
        None, "--limit", help="Keep only the last N matching rows.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON."),
    doc: bool = doc_option(),
) -> None:
    _check_profile_type(type_)
    profile_cmd.run_audit(
        since=since, account=account, decision=decision,
        limit=limit, as_json=as_json,
    )
