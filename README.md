# schwab_cli

A CLI for Charles Schwab API access.

## Requirements

- Python 3.11+
- [`uv`](https://github.com/astral-sh/uv)

## Install (dev)

```bash
uv sync --extra dev
```

## Install (global)

```bash
uv tool install --editable .
```

## First-time setup

```bash
schwab_cli setup
```

Interactive prompts capture your Schwab API credentials and the code-relay
URL. Saved to `~/.config/schwab_cli/config.json` (mode `0600`, plain text —
keep this file out of git, cloud-sync, and shared backups).

## Authenticate

```bash
schwab_cli auth            # refresh existing session if present, else open browser
schwab_cli auth --force    # skip refresh; always open browser for fresh login
schwab_cli auth --manual   # skip the auto-login subprocess (if configured)
```

Tokens are saved to `~/.config/schwab_cli/session.json` (mode `0600`).

### How auth works

1. If you have a valid refresh token, `auth` refreshes it via HTTP — no
   browser involved. Quick path that runs on every invocation.
2. If refresh fails (or you passed `--force`):
   - The CLI prints the Schwab OAuth URL to stderr and asks your OS
     default browser to open it.
   - You complete login + MFA in your normal browser. Schwab redirects
     to your configured `redirect_uri`.
   - Up to **three handlers race concurrently** to capture the `code`:
     - **Paste fallback** — always on. The CLI shows a prompt; paste
       the code / querystring / full redirect URL into it.
     - **Auto-login subprocess** — when `auto_login_command` is set
       and `--manual` is not passed. schwab_cli spawns the configured
       command (typically a [webauto-cli](https://github.com/weig/webauto)
       invocation) with `stdin=DEVNULL` and the right flags for the
       active `auth_flow`. The subprocess drives the browser through
       Schwab on your behalf.
     - **Code-relay polling** — when `auth_flow="code_relay"`. schwab_cli
       long-polls `code_relay_url` for a code your remote relay captured.
   - First valid result wins; losing handlers are cancelled and the
     subprocess is terminated (SIGTERM → 5s → SIGKILL).
   - `oauth.resolve_auth_result` converts the result to a `TokenResponse`:
     `code` → calls Schwab's token endpoint; `token` → already exchanged,
     wrap and save; `error` → surfaces the OAuth error and exits 1.

### `auth_flow` config field

| Value | Meaning |
|---|---|
| `"code_relay"` | schwab_cli polls a remote relay URL. `redirect_uri` points at the relay; `code_relay_url` is the polling endpoint. |
| `"client"` | schwab_cli stands up a local HTTP listener; the auto-login subprocess (or any other client) POSTs the captured code there. |

### Auto-login (optional, via webauto)

schwab_cli can delegate browser driving to an external subprocess.
The reference implementation is the [`webauto`](https://github.com/weig/webauto)
framework — installed separately, with its own venv and browser deps.
schwab_cli stays browser-dep-free.

Set `auto_login_command` in your config to an argv list pointing at
`webauto-cli` (or any equivalent tool that respects the wire protocol below):

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

The credentials (Schwab username/password) live in webauto's `--env` file
— **never** in schwab_cli's config. Use `webauto-cli secrets keygen` +
`webauto-cli secrets encrypt` to keep that file encrypted at rest:

```bash
# One-time
webauto-cli secrets keygen
cat > /tmp/plain.env <<EOF
URL=https://api.schwabapi.com/v1/oauth/authorize?...
USERNAME=alice
PASSWORD=hunter2
EOF
webauto-cli secrets encrypt /tmp/plain.env \
    --out ~/.config/schwab_cli/auto_login.env
rm -P /tmp/plain.env
```

**Wire protocol** — schwab_cli appends per-run flags to your `auto_login_command`
before spawning:

| `auth_flow` | Appended flags |
|---|---|
| `client` | `--notification-endpoint http://127.0.0.1:<port>/oauth/<token>` + `--state <state>` + `-a URL=<auth URL>` |
| `code_relay` | `--no-notify` + `--state <state>` + `-a URL=<auth URL>` |

The action script is your own — typically a copy of one of webauto's
examples (`~/Projects/finance/webauto/examples/schwab_auth_code_relay.py`
for relay flow, `examples/schwab_auth.py` for client flow). It reads
credentials from the env webauto loaded via `--env` and either:

- POSTs `{"kind": "code", "code": ..., "state": ...}` to the listener
  (client flow), or
- Calls `done()` after the browser hits the relay (code_relay flow);
  the relay captures and schwab_cli polls for it.

OAuth errors flow through as `{"kind": "error", "error": ..., "error_description": ...}`
and schwab_cli surfaces them with exit code 1.

### Testing without touching your live config + session

Use `SCHWAB_CLI_CONFIG_DIR` to point both `config.json` and
`session.json` at an isolated directory. Unlike `XDG_CONFIG_HOME`, it
points **directly** at the schwab_cli dir (no `schwab_cli` suffix
appended) so you can use any folder name:

```bash
mkdir -p ./test-config
cp ~/.config/schwab_cli/config.json ./test-config/   # or hand-write one
SCHWAB_CLI_CONFIG_DIR=./test-config schwab_cli auth --force
SCHWAB_CLI_CONFIG_DIR=./test-config schwab_cli accounts
```

Your real `~/.config/schwab_cli/session.json` stays untouched.

## Data commands

Once authenticated, read-only data commands are available:

```bash
schwab_cli accounts                  # all accounts: number, type, liquidation value, cash, position count
schwab_cli account 1234              # one account (suffix or full number)
schwab_cli positions                 # positions across all accounts
schwab_cli positions 5678            # positions for one account
schwab_cli quote AAPL                # one quote
schwab_cli quote AAPL MSFT NVDA      # multi-symbol quote
```

```bash
schwab_cli option NVDA 270115                     # both calls & puts, 10 strikes around ATM
schwab_cli option NVDA '270115*250'               # strike 250 exactly (quote the `*` in bash/zsh)
schwab_cli option NVDA '270115P*' --strikes 4     # puts, 4 strikes around ATM
schwab_cli option NVDA 270115 --detail=1          # stacked layout with greeks
schwab_cli option NVDA 270115 --detail=2          # stacked layout + per-contract details
```

**Spec grammar:** `YYMMDD[P|C]*[strike]`. `YYMMDD` expands to `20YY-MM-DD`; `P` / `C` filter to one side; `*<strike>` pins an exact strike. Shell glob quoting is required whenever `*` appears in the spec.

**`--strikes N`** selects N total strikes around ATM. Even N splits evenly (`N/2` ITM + `N/2` OTM); odd N includes the ATM row (`(N-1)/2` ITM + 1 ATM + `(N-1)/2` OTM). Ignored when the spec names an exact strike.

**Detail levels:**

| `--detail` | Layout | Columns |
|------------|--------|---------|
| `0` (default) | Classic side-by-side | Bid / Ask / Last / Δ per side |
| `1` | One row per contract | + IV, Γ, Θ, 𝒱, Vol, OI |
| `2` | One row per contract + inline sub-table | + Mark, sizes, OHLC, ρ, time/intrinsic value, settlement type |

When the terminal is too narrow, the renderer drops columns from the right and prints a `note:` line to stderr telling you which. `--json` and `--md` never drop columns.

Output formats:

```bash
schwab_cli accounts --json           # JSON for scripting (| jq)
schwab_cli accounts --md             # GitHub-flavored markdown for LLM context
schwab_cli accounts                  # human-readable rich table (default)
```

`--json` and `--md` are mutually exclusive.

The first HTTP 401 from Schwab's API triggers an automatic token refresh and a
single retry — no user action needed as long as the 7-day refresh token is
still valid. After it expires, re-run `schwab_cli auth --force`.

## `dataset` — cached volatility data

Maintain a daily-refreshed series so `vol SYMBOL` returns fast,
tenor-consistent IVR / IVP. One-time setup:

```bash
# Subscribe individual tickers (always tracked).
schwab_cli dataset subscribe NVDA,AMZN,SPY

# Or follow an index — members auto-sync weekly.
schwab_cli dataset subscribe SPX --indices

# Or follow your account's option-bearing positions.
schwab_cli dataset subscribe --account <accountHash>

# Schedule the cron jobs (weekly index sync + daily vol sample).
schwab_cli dataset cron install --indices
schwab_cli dataset cron install --group volatility

# Inspect.
schwab_cli dataset status
```

After the first ~120 days of cron runs, `vol NVDA` shows IVR / IVP
sourced from the clean `atm_iv_30d` series. Until then, it falls
back to the legacy series (with a one-shot BS-reconstructed
backfill if needed).

To stop tracking:

```bash
schwab_cli dataset unsubscribe NVDA
schwab_cli dataset cron uninstall --indices
schwab_cli dataset cron uninstall --group volatility
```

Supported indices: SPX, DJI, NQ. RUT is recognized but not yet
populated by an upstream provider.

## Run tests

```bash
uv run pytest
```
