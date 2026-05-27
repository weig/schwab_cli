# `mcp`

Run `schwab_cli` as an MCP server so agents (Claude Code, Claude
Desktop, custom tools) can call Schwab operations as tools.

## Feature summary

| Feature | Status |
| --- | --- |
| Stdio transport | ✅ shipped |
| Streamable HTTP transport (long-lived daemon, single `/mcp` endpoint) | ✅ shipped |
| REST tools: `get_quote`, `get_chain`, `server_status` | ✅ shipped |
| Streaming tool: `stream_quote` (refcounted, fan-out, progress notifications) | ✅ shipped |
| Structured JSONL logbook (stderr + file) with session / subscribe / unsubscribe / streamer events | ✅ shipped |
| `mcp status` / `mcp log` / `mcp logout` / `mcp restart` / `mcp install` | ✅ shipped |
| `schwab_cli stream` with auto-routing (MCP when daemon up, else direct) | ✅ shipped |
| Proactive browser auto-login at 1h expiry threshold | ✅ shipped |
| Telegram notifications for auth / streamer lifecycle events | ✅ shipped |
| launchd LaunchAgent install + start/stop/uninstall subcommands | ✅ shipped |
| `stream_option_quote` tool | 🚧 next |
| `reauth` tool | 🚧 next |
| Slack notification channel | 🚧 Phase 2b |

## Usage

```
# Start the daemon (default: stdio).
schwab_cli mcp [--stdio | --sse] [--host 127.0.0.1] [--port 7234]
               [--log-file PATH | --no-log-file]

# Subcommands operate on a running server.
schwab_cli mcp status   [--url URL] [--token T] [--json]
schwab_cli mcp log      [-f] [--session S] [--symbol X] [--level warning]
                        [--json] [--tail N] [--log-file PATH]
schwab_cli mcp logout   [--url URL] [--token T]
schwab_cli mcp restart  [--url URL] [--token T] [--stdio|--sse]
schwab_cli mcp install  [--stdio | --sse] [--url URL] [--token T]
                        [--claude-settings PATH] [--yes] [--force]
```

## Transport modes

### Stdio (default)

The daemon reads JSON-RPC from stdin and writes to stdout. Claude
Code spawns one stdio daemon per session. Zero service-management
overhead — the agent starts and stops it.

### Streamable HTTP (long-lived daemon)

Run once, many agents connect as clients to the same process over a
single `/mcp` endpoint (the `--sse` flag name is kept for back-compat
but now drives the modern Streamable HTTP transport):

```bash
schwab_cli mcp --sse                          # http://127.0.0.1:7234/mcp
schwab_cli mcp --sse --host 0.0.0.0 --port 9000   # remote bind
```

Benefits:

- **Shared Schwab WebSocket** across all connected agents (one
  SUBS per symbol, not N).
- **Auth-keeper for the machine** — the daemon rotates
  `session.json` tokens in the background, so every other
  `schwab_cli` command on the box sees a fresh session.
- Survives agent restarts; subscriptions persist.

Cost: you manage the lifecycle (tmux, launchd, systemd, or just a
terminal tab). See "Service management" below.

## Schwab streamer constraint

Schwab allows **one concurrent streamer session per account**. If
two `schwab_cli mcp` processes both try to stream, whichever logs in
most recently kicks the other off. The tool warns you on start when
it detects this pattern. Use one HTTP daemon and let agents share it.

## Registering with Claude Code

Either write the entry manually or use the installer:

```bash
# Streamable HTTP entry (recommended) — registers the /mcp URL:
schwab_cli mcp install

# Stdio variant:
schwab_cli mcp install --stdio

# With a shared-secret token for the HTTP daemon:
schwab_cli mcp install --token "s3cr3t"
```

The HTTP entry registers `{"type": "http", "url":
"http://127.0.0.1:7234/mcp"}` in `~/.claude/settings.json`.

The installer merges into `~/.claude/settings.json`, preserves other
keys, and refuses to clobber an existing `schwab` entry without
`--force`.

## Tools

### `get_quote`

```json
{"symbols": ["NVDA", "AAPL"]}
```

Returns the Schwab `/marketdata/v1/quotes` response as JSON.

### `get_chain`

```json
{"symbol": "AMZN", "expiry": "2026-05-01", "strike_count": 20}
```

Returns the flattened envelope the `option` command emits.

### `stream_quote`

Long-running tool. Subscribe to real-time level-1 equity quotes for
one or more symbols. Each update arrives as an MCP progress
notification whose `message` field is a JSON object with
`bid` / `ask` / `last` / `volume` / `quote_time` / etc.

```json
{"symbols": ["NVDA", "AAPL"]}
```

Implementation details:

- Refcounted at the Schwab side: two agents subscribing to `NVDA`
  share one `SUBS` wire command.
- Keepalive progress notifications every 60s during quiet periods
  so the client knows the stream is still live (useful
  after-hours — see off-hours note below).
- On client cancel: `UNSUBS` is sent to Schwab for any refcount
  that hit zero, queues are dropped, and a `unsubscribe` log
  entry is emitted with the reason.
- TCP close cleanup: if the MCP client disconnects without
  cancelling, a `session.drop` event runs the same cleanup path.

**After-hours behaviour**: Schwab sends one snapshot frame on
`SUBS` then delta frames only on actual trades. Outside market
hours you'll see the initial snapshot and then silence until the
market moves. The subscription stays live — the keepalives
confirm that.

### `server_status`

No arguments. Returns PID, uptime, token expiries, transport, and
current subscription state.

## Admin endpoints (HTTP mode only)

| Path | Method | Purpose |
| --- | --- | --- |
| `/admin/status` | GET | Powers `mcp status`. |
| `/admin/shutdown` | POST | Graceful shutdown; powers `mcp logout`. |
| `/admin/flush` | POST | Clear subscription state (panic button). |

Bearer token required if the daemon was started with `--token`.
Otherwise loopback-only is the default; no auth is enforced.

## Logging

Every tool call, auth event, and subscription change emits one JSON
line:

```
{"ts":"2026-04-24T14:32:17.123Z","level":"info","event":"tool.call","tool":"get_quote","args":{"symbols":["NVDA"]}}
```

**Tail live** (works from any terminal, even if the daemon is in
tmux or launchd):

```bash
schwab_cli mcp log -f                           # everything
schwab_cli mcp log -f --level warning           # warnings + errors
schwab_cli mcp log -f --session s_ab12          # one session only
schwab_cli mcp log -f --symbol NVDA             # events touching NVDA
schwab_cli mcp log -f --json | jq '.'           # raw for jq pipelines
```

Default log path: `~/.config/schwab_cli/mcp.log`. Append-mode, no
rotation in MVP — truncate manually if it grows large.

## Running as a macOS service (launchd)

For a set-and-forget daemon that starts on login and auto-restarts
on exit:

```bash
# Install + start:
schwab_cli mcp install-service

# Verify:
schwab_cli mcp status

# Stop without uninstalling (auto-restart pauses until next start):
schwab_cli mcp stop-service

# Fully remove the service:
schwab_cli mcp uninstall-service
```

The installed plist lives at
`~/Library/LaunchAgents/com.schwab-cli.mcp.plist` and uses
`KeepAlive=true` so any exit triggers a relaunch — which makes
`schwab_cli mcp restart` a no-op apart from calling `mcp logout`;
launchd takes it from there.

**Why LaunchAgent, not LaunchDaemon**: Agents run under your user,
so they can read `~/.config/schwab_cli/session.json` natively.
Daemons would need root + user-switching gymnastics.

## Proactive auto-login

With the daemon running as a service (or in HTTP mode generally), a
background task monitors the refresh-token expiry and proactively
rotates the 7-day token via `schwab_cli auth --force` at the 1h
threshold. Headless Chromium + saved credentials make it silent
in the common case.

- Default threshold: **1h** before `refresh_token_expires_at`.
- Anti-thrash: at most **one attempt per hour** on repeated
  failures.
- At **15m remaining**, an `auth.refresh_expiring` warning fires
  if rotation still hasn't succeeded — shown via
  `notifications/message` to all connected MCP sessions and
  pushed to any subscribed notification channels (see
  `doc/notify.md`).
- On success: bridge reconnects the Schwab streamer with the
  fresh access token; active subscriptions resume automatically.
- On failure: `auth.auto_login.failed` notification with the
  subprocess stderr tail so you know what to fix.

Opt out with a future `--no-auto-login` flag (the code already
scaffolds it); for now the monitor runs whenever the daemon is
in HTTP mode.

## Notifications

Configure Telegram to receive alerts on auth / streamer events:

```bash
schwab_cli notify setup --channel telegram
schwab_cli notify test  --channel telegram
```

Details in [`doc/notify.md`](notify.md).

## Code-update workflow

```bash
cd ~/src/schwab_cli && git pull
uv tool install --reinstall --from . schwab_cli
schwab_cli mcp restart
```

`mcp restart` tells the running server to shut down, then starts a
fresh foreground process in the same terminal. Connected agents see
~1-3s of downtime and re-subscribe on their next tool call.

## What's next

- **Streaming tools** (`stream_quote`, `stream_option_quote`): the
  subscription manager and streamer module are in place; they need
  an async bridge from the Schwab WebSocket to per-session MCP
  progress notifications.
- **Proactive browser auto-login** at the 1h refresh-token
  threshold so long-running daemons rotate through 7-day cycles
  without manual intervention.
- **`reauth` tool** for agent-triggered re-authentication.
- **`schwab_cli stream --mcp`** — connect to the daemon's
  `stream_quote` tool instead of opening a separate streamer
  connection.

See [`docs/plan/mcp-streaming.md`](../docs/plan/mcp-streaming.md)
for the full design and phase breakdown.
