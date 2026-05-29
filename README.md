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
| Server / MCP daemon | `server`, `server --enable-mcp`, `server install`, `server status`, `server register-claude` | [server](doc/server.md) · [mcp tools](doc/mcp.md) |
| Scheduled jobs | `jobs init`, `jobs list`, `jobs status`, `jobs reload`, `jobs run`, `jobs migrate` | [jobs](doc/jobs.md) |
| Cached dataset backend | `dataset subscribe`, `dataset sync`, `dataset update …` | [setup](doc/setup.md) |
| Health check | `doctor` | _(prints install / MCP / auth / dataset status)_ |

Output formats are uniform across commands:
- **Default**: human-readable rich table.
- `--json`: machine-readable for `| jq` and scripts.
- `--md`: GitHub-flavored markdown — drop into an LLM prompt and the
  agent can read prices, greeks, etc. without parsing tables.

---

## Architecture

`schwab_cli` is organized into three layers so the same Schwab
operations can be reached from the CLI, an MCP agent, or REST without
duplicating logic:

- **Layer 1 — `schwab_cli.api.*`**: a pure HTTP wrapper over the Schwab
  endpoints. It takes an authenticated client and does no auth or
  state-management of its own.
- **Layer 2 — `schwab_cli.service.*`**: the business layer. It **owns
  auth** — it reads `session.json`, transparently mints a fresh access
  token when the current one is stale, and raises `SessionExpired` when
  the refresh token itself is dead. It returns frozen dataclasses /
  plain payloads so every interface renders the same shape.
- **Layer 3 — interfaces**: thin adapters that parse input → call a
  service → render output. These are the CLI commands (`commands/*`),
  the MCP tools, and the REST PoC.

Because the service mints access tokens per call, every interface stays
fresh as long as the 7-day refresh token is alive. Keeping that refresh
token alive across its 7-day expiry is the job of [`schwab
server`](#auth-maintenance-server-schwab-server) — it is the *only*
component that proactively renews the refresh token (via headless
browser auto-login).

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
     `http://localhost`): a loopback HTTPS URL such as
     `https://127.0.0.1:19806/schwab/callback`. `schwab setup` suggests a
     default with a random port; register whatever you use here in the
     portal *exactly* (see [Login callback](#login-callback-local-server)).
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

On the human path, a paste fallback races alongside the local callback
server; with `auto_login_command` set, the subprocess drives the browser
while the local server captures the redirect. Full details in
[doc/auth.md](doc/auth.md).

**Credentials never live in `schwab_cli` config** — they belong in
webauto's encrypted env file. See `webauto-cli secrets keygen`.

### Login callback (local server)

Schwab requires a pre-registered **HTTPS** redirect URI and won't accept
`http://localhost`. `schwab_cli` gets the OAuth `code` back by running a tiny
local HTTPS server on `127.0.0.1` — Schwab redirects the browser straight
back to it. No public relay, no polling.

1. Register a loopback HTTPS callback in the developer portal, e.g.
   `https://127.0.0.1:19806/schwab/callback`. `schwab setup` defaults to one
   with a random port; whatever you pick must match the portal exactly.
2. Because the browser must trust an HTTPS server on `127.0.0.1`, a one-time
   local root CA is installed into the macOS System keychain. `schwab setup`
   does this for you (asking for your login/sudo password once) when the
   Callback URL is loopback HTTPS; you can also run it directly:

   ```bash
   schwab cert install      # trust the local CA (macOS; asks for sudo once)
   schwab cert status       # CA trusted? leaf present? valid-until
   schwab cert uninstall    # remove it cleanly
   ```

The resulting config is just:

```json
{
  "auth_flow": "local_server",
  "redirect_uri": "https://127.0.0.1:19806/schwab/callback"
}
```

At `auth` time the server captures `?code=…&state=…` with strict `state`
validation; a paste fallback remains for the human path. If the certificate
isn't installed, `auth` fails fast with "run `schwab cert install` first"
before opening the browser. macOS only for now. See
[doc/auth.md](doc/auth.md) and [doc/cert.md](doc/cert.md).

---

## MCP server

Expose `schwab_cli` as a Model Context Protocol server so AI agents
(Claude Code, Claude Desktop, custom tools) can call Schwab operations
as MCP tools. **The MCP server runs under the daemon** — there is no
separate `schwab mcp` command; use `schwab server --enable-mcp`.

```bash
schwab server --enable-mcp        # run the MCP server (single /mcp endpoint)
schwab server register-claude     # register /mcp in ~/.claude/settings.json
schwab server status              # launchd check + GET /health probe + snapshot
schwab server log                 # tail the JSONL logbook
schwab server restart             # bounce the daemon
schwab server logout              # graceful /admin/shutdown
```

Available MCP tools out of the box: `get_quote`, `get_chain`,
`stream_quote`, `dataset_status`, `dataset_history`, `dataset_iv_rank`,
`server_status`. The daemon speaks Streamable HTTP only — a single
`/mcp` endpoint. See [doc/mcp.md](doc/mcp.md) for the tool list and
Claude Code integration, and [doc/server.md](doc/server.md) for running
and managing the daemon.

---

## Auth-maintenance server (`schwab server`)

`schwab server` is a long-lived daemon whose core job is to **keep your
7-day refresh token alive** so the box never falls out of auth. It is
the single component that proactively renews the refresh token (via the
headless browser auto-login). Run it under launchd
(`com.schwab-cli.server`) and the rest of `schwab_cli` stays logged in
indefinitely.

```bash
schwab server                       # bare: auth-maintenance loop only
schwab server --enable-mcp          # + Streamable HTTP MCP server (single /mcp)
schwab server --enable-rest         # + unauthenticated REST PoC (GET /quote/{symbol}, /health)
schwab server install               # install + load the launchd LaunchAgent (bare)
schwab server install --enable-mcp  # bake --enable-mcp into the launchd plist
schwab server status                # launchd check + GET /health probe + snapshot
schwab server uninstall             # unload + remove the LaunchAgent
```

The `--enable-*` flags compose. When MCP runs under `schwab server` its
own auth monitor is disabled — the maintenance loop is the sole renewer,
so there is no competing rotation. `server install` bakes whichever mode
flags you pass into the plist's `ProgramArguments`. `--enable-rest` is a
proof-of-concept only and serves **unauthenticated**; don't expose it
beyond loopback. See [doc/server.md](doc/server.md) for the full picture.

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
- `gitleaks` (credential-leak scan; rules in [`.gitleaks.toml`](.gitleaks.toml))
- **PR checklist** — every `- [ ]` in the PR body must be ticked.
  Drop any line that doesn't apply rather than leaving it unchecked.

`ruff format` is not yet enforced; the existing tree predates the
formatter. Run `uv run ruff format path/to/file.py` opt-in.

### Local secret scan

`pre-commit install` arms gitleaks on every commit, but ad-hoc scans
are useful for periodic audits:

```bash
brew install gitleaks            # macOS; or see github.com/gitleaks/gitleaks
gitleaks detect --no-git         # scan working tree
gitleaks detect                  # scan full git history
gitleaks protect --staged        # scan what would be committed
```

If a leak fires on a known-safe placeholder, add an entry to
`.gitleaks.toml`'s `[allowlist]` — don't bypass with `--no-verify`.

Conventional-commits style for commit messages (`feat: …`, `fix: …`,
`refactor: …`, `docs: …`, `test: …`, `chore: …`).

---

## License

See `LICENSE`.
