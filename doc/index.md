# schwab_cli

Command-line access to your Charles Schwab brokerage account: quotes,
option chains, account positions, transactions, price history, and greeks —
rendered as human tables, JSON, or GitHub-flavored markdown.

Built against the public Schwab developer API (individual-developer tier).
All market data is streamed through your own authenticated session, so there
are no third-party dependencies and no data-sharing concerns.

## Install

```bash
# One-time: build the isolated tool venv and put `schwab_cli` on your PATH.
uv tool install --from . schwab_cli
```

After a pull or a local edit, update the installed binary:

```bash
uv tool install --reinstall --from . schwab_cli
```

Or run from the project directory without installing:

```bash
uv run schwab_cli <command> ...
```

## First-time setup

Three steps, in order:

```bash
schwab_cli setup      # interactively capture client_id / secret / redirect_uri / auth_flow
schwab_cli auth       # OAuth browser dance; writes ~/.config/schwab_cli/session.json
schwab_cli quote NVDA # smoke test
```

See [setup](setup.md) and [auth](auth.md) for the detail on each.

## Commands

| Command | Purpose |
| --- | --- |
| [`setup`](setup.md) | Capture API credentials and choose an auth flow. |
| [`auth`](auth.md) | Refresh or perform a full OAuth flow, save session. |
| [`accounts`](accounts.md) | List every brokerage account the session has access to. |
| [`account`](account.md) | Show one account by number or last-N-digit suffix. |
| [`positions`](positions.md) | List positions across one or all accounts. |
| [`quote`](quote.md) | Real-time quote for one or more symbols. |
| [`fundamentals`](fundamentals.md) | Company valuation / profitability / balance-sheet snapshot. |
| [`dividends`](dividends.md) | Most-recent + next-upcoming dividend. `div` alias. |
| [`option`](option.md) | Option chain for an underlying at a given expiry + strike window. |
| [`greeks`](greeks.md) | Detailed greeks for one specific option contract. |
| [`history`](history.md) | OHLCV candles for a stock or option ticker. |
| [`vol`](vol.md) | Volatility context for a stock: IV, HV, HVP, P/C Ratio. |
| [`skew`](skew.md) | Option skew / smile (25Δ RR, wing skew, butterfly, ATM slope) — L1 single chain, L2 term structure, L3 cross-ticker. |
| [`strategy`](strategy.md) | Multi-leg option strategy probability + risk: POP, EV, breakevens, max P/L, plus a Schwab-copy-paste order ticket. |
| [`server`](server.md) | The daemon: keeps the 7-day refresh token alive, and `--enable-mcp` runs the Streamable HTTP MCP server on top (`--enable-rest` adds a REST PoC). Subcommands: `install` / `uninstall` / `status` / `log` / `logout` / `restart` / `register-claude`. |
| [`mcp`](mcp.md) | MCP **tools** reference (`get_quote`, `get_chain`, `stream_quote`, …) + Claude Code integration. The MCP server itself runs as `schwab server --enable-mcp`. |
| [`stream`](stream.md) | Watch live Schwab quotes in the terminal (direct-streamer path). Ctrl+C to stop. |
| [`notify`](notify.md) | Configure Telegram notifications for daemon events (auth rotation, streamer crashes). |
| [`transactions`](transactions.md) | Account activity (trades, dividends, journals) over a date range. |

Cross-cutting references:

- [Ticker format](ticker.md) — stocks vs. options, all accepted option forms.
- [Time and range syntax](time.md) — `--range` / `--interval` syntax used by `history` and `transactions`.

## Global environment variables

| Variable | Purpose |
| --- | --- |
| `SCHWAB_CLI_CONFIG` | Absolute path override for `config.json`. Bypasses `HOME` / `XDG_CONFIG_HOME`. Use this for scripting or previewing without touching the real file. |
| `XDG_CONFIG_HOME` | Standard XDG config dir. Falls back to `~/.config` when unset. |
| `HEADLESS` | `1` = run the auth browser headlessly; anything else = visible window. The `auth --manual` flag forces `HEADLESS=0`. |
| `DEBUG` | `1` = emit `[debug] …` trace lines during `auth`. Also dumps the browser's page source at each step to `~/.config/schwab_cli/auth-debug/<timestamp>/` so you can inspect Schwab UI changes. |

## File layout

```
~/.config/schwab_cli/
├── config.json          # 0600 — client_id, secret, redirect_uri, auth_flow, creds
├── session.json         # 0600 — access_token + refresh_token (7-day lifetime)
├── chromium-uc/         # persistent browser profile (Trust-Device cookie lives here)
└── auth-debug/<ts>/     # DEBUG=1 page dumps; redacted for secrets
```

## Output formats

Every data-producing command accepts `--json` and `--md`:

- **HUMAN** (default): Rich-formatted tables with colour and Unicode glyphs.
- `--json`: machine-readable JSON envelope. Numbers are floats, dates are ISO strings.
- `--md`: GitHub-flavoured markdown. Safe to paste into PRs / tickets / Slack.

The three are mutually exclusive; passing both `--json` and `--md` exits with a usage error.
