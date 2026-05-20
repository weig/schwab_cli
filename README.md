# schwab_cli

A terminal-first CLI and MCP server for the Charles Schwab developer API.

> Quotes, option chains, accounts, positions, history, and a cached
> volatility/IVP dataset — rendered as human tables, JSON, or
> GitHub-flavored markdown, with an optional MCP server so AI agents
> can call the same operations as tools.

All market data is streamed through your own authenticated Schwab session.
No third-party intermediaries, no data sharing, no shared API keys.

> **Personal / individual-developer use only.** Schwab does not issue
> shared API keys; every user needs their own developer account and
> their own app's `client_id` / `client_secret`. Register one at
> [developer.schwab.com](https://developer.schwab.com) (free,
> individual-developer tier). This project ships zero credentials —
> you supply yours during `schwab setup`.

---

## What it can do

| Area | Commands | Reference |
|---|---|---|
| Accounts & positions | `accounts`, `account`, `positions`, `transactions` | [accounts](doc/accounts.md) · [positions](doc/positions.md) · [transactions](doc/transactions.md) |
| Quotes & history | `quote`, `history` | [quote](doc/quote.md) · [history](doc/history.md) |
| Options | `option`, `greeks`, `skew`, `strategy` | [option](doc/option.md) · [greeks](doc/greeks.md) · [skew](doc/skew.md) · [strategy](doc/strategy.md) |
| Volatility / IVR / IVP | `vol`, `dataset` | [vol](doc/vol.md) |
| Fundamentals & dividends | `fundamentals`, `div` | [fundamentals](doc/fundamentals.md) · [dividends](doc/dividends.md) |
| Streaming | `stream`, `watch` | [stream](doc/stream.md) |
| Orders (place / preview / cancel / replace) | `order` | [order](doc/order.md) |
| Notifications | `notify` (Telegram) | [notify](doc/notify.md) |
| MCP server | `mcp install`, `mcp status`, `mcp log`, … | [mcp](doc/mcp.md) |
| Cached dataset backend | `dataset subscribe`, `dataset sync`, `dataset cron …` | [setup](doc/setup.md) |
| Health check | `doctor` | _(prints install / MCP / auth / dataset status)_ |

Output formats are uniform across commands:
- **Default**: human-readable rich table.
- `--json`: machine-readable for `| jq` and scripts.
- `--md`: GitHub-flavored markdown — drop into an LLM prompt and the
  agent can read prices, greeks, etc. without parsing tables.

---

## Install

### One-liner (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/weig/schwab_cli/main/install.sh | sh
```

The installer fetches [`uv`](https://github.com/astral-sh/uv) if missing,
then `uv tool install`s `schwab_cli` from GitHub. Binary lands at
`~/.local/bin/schwab`.

> Review before running anything piped from the internet:
> [`install.sh`](install.sh).

### From a local checkout

Requires **Python 3.11+** and [`uv`](https://github.com/astral-sh/uv).

```bash
# One-time global install (creates an isolated tool venv, puts `schwab` on PATH)
uv tool install --from . schwab_cli

# After a pull or a local edit
uv tool install --reinstall --from . schwab_cli

# Or run from the project directory without installing
uv run schwab <command> ...
```

### Get your `client_id` and `client_secret`

You need a Schwab developer account — the CLI cannot share keys with
anyone else.

1. Sign up at [developer.schwab.com](https://developer.schwab.com)
   (free, individual-developer tier).
2. **Create an app**. Suggested settings:
   - **Type**: Individual developer.
   - **API products**: enable both *Accounts and Trading Production*
     and *Market Data Production*.
   - **Callback URL** (must be HTTPS — Schwab won't accept
     `http://localhost`):
     - `https://127.0.0.1:8443` if you'll use the local `client`
       auth flow, **or**
     - `https://oauth-relay.<you>.workers.dev/<uuid>/schwab_callback`
       if you'll use the `code_relay` flow (see
       [Login callback](#login-callback-oauth_relay)).
3. Wait for the app to be **approved** (usually same-day; status
   shows on the dashboard). Approval issues `App Key` + `Secret`.
4. Run `schwab setup` and paste the App Key as `client_id` and the
   Secret as `client_secret`. They're written to
   `~/.config/schwab_cli/config.json` (mode `0600`).

> 🔒 **`client_secret` is yours alone.** It stays on your machine in
> `~/.config/schwab_cli/config.json` (`0600`, plain text). Never share
> it, never commit it, never paste it into chat / issues / screenshots,
> and keep it out of cloud sync. Anyone with your `client_id` +
> `client_secret` + an OAuth `code` from your account can act as your
> Schwab app. If it leaks, rotate it from the Schwab developer portal
> immediately and re-run `schwab setup`.

Developer setup:

```bash
uv sync --extra dev
uv run pytest
```

---

## Authentication

```bash
schwab setup          # one-time: capture credentials → ~/.config/schwab_cli/config.json
schwab auth           # refresh existing session, else open browser
schwab auth --force   # skip refresh; full OAuth round-trip
schwab auth --manual  # skip the auto-login subprocess (if configured)
```

Config and session files live at `~/.config/schwab_cli/{config,session}.json`
(mode `0600`, plain text — keep these out of git and cloud sync).

**Session lifecycle:**

- The short-lived access token (~30 min) is refreshed transparently
  from your saved refresh token on the first HTTP 401 — no user action
  while the refresh token is still valid.
- Schwab's refresh token expires after **7 days**. After that, a full
  OAuth round-trip (browser login + MFA) is required to get a fresh
  one.
- **Fully hands-off operation past the 7-day boundary requires
  [auto-login](#browser-auto-login-optional)**. Without it, you'll
  need to re-run `schwab auth --force` once a week and complete the
  browser flow manually.

### Browser auto-login (optional)

`schwab_cli` can delegate the browser leg of OAuth to an external tool
so the CLI stays browser-dep-free. The reference implementation is
**[webauto-cli](https://github.com/weig/webauto-cli)** — a Playwright-based
automation runner with encrypted credential storage.

Set `auto_login_command` in your config to invoke it:

```json
{
  "auto_login_command": [
    "webauto-cli",
    "~/.config/schwab_cli/scripts/auth_automation.py",
    "--env", "~/.config/schwab_cli/auto_login.env"
  ],
  "auto_login_timeout_seconds": 300
}
```

Three handlers race concurrently to capture the OAuth code (paste
fallback / auto-login subprocess / code-relay polling); first valid
result wins, losers are cancelled. Full wire protocol and `auth_flow`
options in [doc/auth.md](doc/auth.md).

**Credentials never live in `schwab_cli` config** — they belong in
webauto's encrypted env file. See `webauto-cli secrets keygen`.

### Login callback (oauth_relay)

Schwab requires a pre-registered **HTTPS** redirect URI and won't accept
`http://localhost`. To get the OAuth `code` back to your CLI without
shipping certificates, point Schwab at a public relay.

The reference implementation is
**[oauth-relay](https://github.com/weig/oauth_relay)** — a tiny
Cloudflare Worker that:

1. Accepts Schwab's redirect at a fixed public URL
   (`https://<your-worker>.workers.dev/<uuid>/schwab_callback?code=…`).
2. Holds the `code` for a few seconds.
3. Hands it to whichever `schwab auth` invocation is currently
   long-polling it.

Configure once:

```json
{
  "auth_flow": "code_relay",
  "redirect_uri": "https://oauth-relay.<you>.workers.dev/<uuid>/schwab_callback",
  "code_relay_url": "https://oauth-relay.<you>.workers.dev/<uuid>/wait"
}
```

Alternative `auth_flow="client"` stands up a local HTTP listener
instead — pick this if you'd rather not run a worker. See
[doc/auth.md](doc/auth.md) for the trade-offs.

---

## MCP server

Expose `schwab_cli` as a Model Context Protocol server so AI agents
(Claude Code, Claude Desktop, custom tools) can call Schwab operations
as MCP tools.

```bash
schwab mcp install         # install + load the launchd agent (macOS)
schwab mcp status          # daemon status + last 10 logbook events
schwab mcp log             # tail the JSONL logbook
schwab mcp restart         # rolling restart
schwab mcp logout          # forget the session (force re-auth on next call)
```

Available MCP tools out of the box: `get_quote`, `get_chain`,
`stream_quote`, `dataset_status`, `dataset_history`, `dataset_iv_rank`,
`server_status`. See [doc/mcp.md](doc/mcp.md) for the full list,
stdio vs SSE transport, and Claude Code integration.

---

## Dataset backend (cached volatility, IVR / IVP)

The `dataset` subsystem maintains a daily-refreshed, tenor-consistent
ATM IV series locally so `vol SYMBOL` returns fast and IVP percentiles
are deterministic.

```bash
# Subscribe — individual tickers, an index's members, or your account.
schwab dataset subscribe NVDA,AMZN,SPY
schwab dataset subscribe SPX --indices
schwab dataset subscribe --account <accountHash>

# Install the unified daily scheduler (one launchd plist, three children).
schwab dataset cron install

# Inspect.
schwab dataset status
schwab doctor
```

`cron install` is idempotent — it sweeps any pre-existing schwab_cli
launchd plists first and installs the single unified scheduler
(`com.schwab-cli.scheduler`). The scheduler fires once per day in the
early local morning and pspawns three children that internally
`sleep_until_ny`:

- **market-data** — 17:00 ET ATM IV snapshot + OHLCV close.
- **accounts** — 17:00 ET portfolio NAV snapshot.
- **indices** — 18:00 ET membership refresh (rate-limited to ~weekly via `--max-age-days`).

Every transition is captured in `~/.config/schwab_cli/scheduler.log`
(rotating, 10MB × 3 backups) and the latest per-job exit status lives
in `~/.config/schwab_cli/last_run.json`. Job failures emit Telegram
alerts via the `notify` subsystem (configure with `schwab notify install`).

Stop tracking:

```bash
schwab dataset unsubscribe NVDA
schwab dataset cron uninstall
```

Supported indices: SPX, DJI, NQ. RUT is recognised but not yet
populated upstream.

---

## Contributing

PRs welcome.

- **Open an issue first** for non-trivial changes — design discussion
  is cheaper than rebases.
- **Stay terminal-first.** New features should render legibly in a
  default-width terminal and also expose `--json` / `--md` for
  scripting / LLM consumption.
- **Tests required** for parsers, formatters, and any new command.
  `uv run pytest` should be green before opening a PR.
- **Avoid hardcoded paths and secrets.** Honour `SCHWAB_CLI_CONFIG_DIR`
  for anything that touches `~/.config/schwab_cli/`.
- **Don't add browser/Playwright deps** — that lives in
  [webauto-cli](https://github.com/weig/webauto-cli). The CLI stays
  pure-Python with HTTP only.

### Local dev setup

```bash
uv sync --extra dev
uv run pre-commit install        # one-time: arm the lint/format hook
uv run ruff check .              # lint
uv run ruff format .             # format
uv run pytest                    # run the suite
```

### CI gates (PR will be blocked unless green)

- `ruff check` (lint — bugs, dead code, unused imports)
- `pytest` (streaming WebSocket suite excluded — hangs CI runners)
- **PR checklist** — every `- [ ]` in the PR body must be ticked.
  Drop any line that doesn't apply rather than leaving it unchecked.

`ruff format` is not yet enforced; the existing tree predates the
formatter. Run `uv run ruff format path/to/file.py` opt-in.

Conventional-commits style for commit messages (`feat: …`, `fix: …`,
`refactor: …`, `docs: …`, `test: …`, `chore: …`).

---

## License

See `LICENSE`.
