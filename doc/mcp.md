# `mcp` — MCP tools reference

Expose `schwab_cli` as an MCP server so agents (Claude Code, Claude
Desktop, custom tools) can call Schwab operations as tools.

> **There is no `schwab mcp` command.** The MCP server runs under the
> auth-maintenance daemon: **`schwab server --enable-mcp`**. See
> [`server.md`](server.md) for how to run, install, probe, and manage
> the daemon. This page documents the **tools** the MCP server exposes
> and how to register it with Claude Code.

## Running the MCP server

```bash
# Run the daemon with the MCP server on top (single /mcp endpoint).
schwab server --enable-mcp                              # http://127.0.0.1:7234/mcp
schwab server --enable-mcp --mcp-host 0.0.0.0 --mcp-port 9000

# Or install it as a launchd LaunchAgent that bakes the flag in:
schwab server install --enable-mcp
```

The daemon is HTTP-only (Streamable HTTP). stdio cannot hold the
long-lived authenticated session the daemon requires, so it is not
supported. Run once; many agents connect as clients to the same process
over a single `/mcp` endpoint, sharing one Schwab WebSocket (one `SUBS`
per symbol, not N) and one background auth-keeper.

## Registering with Claude Code

```bash
# Streamable HTTP entry — registers the /mcp URL in ~/.claude/settings.json:
schwab server register-claude

# With a shared-secret bearer token for the HTTP daemon:
schwab server register-claude --token "s3cr3t"
```

This writes `{"type": "http", "url": "http://127.0.0.1:7234/mcp"}` into
`~/.claude/settings.json`. The installer merges into the file, preserves
other keys, and refuses to clobber an existing `schwab` entry without
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

Long-running tool. Subscribe to real-time level-1 equity quotes for one
or more symbols. Each update arrives as an MCP progress notification
whose `message` field is a JSON object with `bid` / `ask` / `last` /
`volume` / `quote_time` / etc.

```json
{"symbols": ["NVDA", "AAPL"]}
```

Implementation details:

- Refcounted at the Schwab side: two agents subscribing to `NVDA` share
  one `SUBS` wire command.
- Keepalive progress notifications every 60s during quiet periods so the
  client knows the stream is still live (useful after-hours).
- On client cancel: `UNSUBS` is sent to Schwab for any refcount that hit
  zero, queues are dropped, and an `unsubscribe` log entry is emitted.
- TCP close cleanup: if the MCP client disconnects without cancelling, a
  `session.drop` event runs the same cleanup path.

**After-hours behaviour**: Schwab sends one snapshot frame on `SUBS`
then delta frames only on actual trades. Outside market hours you'll see
the initial snapshot and then silence until the market moves. The
subscription stays live — the keepalives confirm that.

### `server_status`

No arguments. Returns PID, uptime, token expiries, transport, and
current subscription state — the same payload `schwab server status`
renders.

### Dataset tools

`dataset_status`, `dataset_history`, `dataset_iv_rank` — read from the
cached dataset backend. See [`setup.md`](setup.md).

## HTTP endpoints (when `--enable-mcp` is running)

| Path | Method | Purpose |
| --- | --- | --- |
| `/mcp` | (Streamable HTTP) | The MCP transport endpoint agents connect to. |
| `/health` | GET | Liveness probe; powers `schwab server status`. |
| `/admin/status` | GET | Snapshot; powers `server status` / `server_status`. |
| `/admin/shutdown` | POST | Graceful shutdown; powers `server logout`. |
| `/admin/flush` | POST | Clear subscription state (panic button). |

`/health` is unauthenticated. The `/admin/*` endpoints require the
bearer token when the daemon was started with one; otherwise loopback is
the default and no auth is enforced.

## Logging

Every tool call, auth event, and subscription change emits one JSON
line:

```
{"ts":"2026-04-24T14:32:17.123Z","level":"info","event":"tool.call","tool":"get_quote","args":{"symbols":["NVDA"]}}
```

Tail it live with `schwab server log -f` (see [`server.md`](server.md)).
Default path: `~/.config/schwab_cli/mcp.log`.

## Schwab streamer constraint

Schwab allows **one concurrent streamer session per account**. Run one
HTTP daemon and let agents share it rather than opening several streamer
connections — whichever logs in most recently kicks the others off.

## Related

- [`server`](server.md) — the daemon that hosts the MCP server
  (`--enable-mcp`), plus install / status / log / logout / restart /
  register-claude.
- [`stream`](stream.md) — `schwab stream` auto-routes through this MCP
  server's `stream_quote` tool when the daemon is reachable.
- [`notify`](notify.md) — Telegram alerts for auth / streamer events.
