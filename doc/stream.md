# `stream`

Watch live Schwab quotes in the terminal. Direct-streamer path for
now; the MCP client path (`--mcp`) is scaffolded but waits on the
`stream_quote` MCP tool landing in a follow-up commit.

## Usage

```
schwab_cli stream SYMBOL [SYMBOL ...] [--fields bid,ask,last] [--json]
                  [--direct | --mcp] [--mcp-url URL]
```

## Quick examples

```bash
# Live quotes for two names:
schwab_cli stream NVDA AAPL

# Narrow the payload to just bid/ask/last:
schwab_cli stream NVDA --fields bid,ask,last

# One JSON object per line (pipe into jq, save to file, etc.):
schwab_cli stream NVDA --json

# Explicitly bypass any running MCP daemon and connect directly:
schwab_cli stream NVDA --direct
```

Ctrl+C cancels cleanly — sends `UNSUBS` + `ADMIN LOGOUT` to Schwab
before exiting so your account's subscription count is released
immediately.

## Output format

Default human format, one line per update:

```
[14:32:17.123] NVDA    bid 250.1  ask 250.15  last 250.12  volume 1,234,567
[14:32:17.201] AAPL    bid 175.8  ask 175.82  last 175.81  volume 987,654
[14:32:17.405] NVDA    bid 250.12 ask 250.16  last 250.13  volume 1,234,789
```

With `--json`:

```json
{"symbol": "NVDA", "bid": 250.1, "ask": 250.15, "last": 250.12, "volume": 1234567, "quote_time": 1735654337123}
```

## Fields

`--fields` accepts either friendly names or raw numeric Schwab field
IDs. Friendly names recognised:

`symbol`, `bid`, `ask`, `last`, `bid_size`, `ask_size`, `volume`,
`last_size`, `high`, `low`, `close`, `open`, `net_change`,
`net_change_pct`, `mark`, `quote_time`, `trade_time`.

Unknown tokens are treated as raw numeric IDs and passed through.
The `symbol` (field 0) is always included so each update can be
routed by ticker.

## Transport modes

| Mode | Flag | Behaviour |
| --- | --- | --- |
| Auto | (default) | Probes for a running MCP daemon on `127.0.0.1:7234`; routes through it if found, else direct. |
| Direct | `--direct` | Opens its own Schwab streamer WebSocket, bypassing any running daemon. |
| MCP | `--mcp` | Forces the MCP client path; exits non-zero if no daemon is reachable. |

When the MCP daemon is running, using the default (auto) mode is
preferred: every subscribe / unsubscribe event shows up in the
daemon's log, `server status` reflects your client, and multiple
concurrent `stream` invocations share the one Schwab WebSocket.

## After-hours behaviour

Schwab's streamer sends one **snapshot** frame on each `SUBS` with
current prices, then pushes delta frames only when new trades or
quote updates actually happen. Outside market hours (4:00-20:00 ET
extended or 9:30-16:00 ET regular) there's typically no activity, so
you'll see one initial row per symbol and then silence. The
subscription stays live — data resumes as soon as the market moves.
This is correct behaviour, not a bug. Use `-f` mode of `server log` to
see heartbeats flow in the background during quiet periods.

## Schwab streamer constraint

Schwab allows **one streamer session per account**. Two
`schwab_cli stream --direct` processes running simultaneously will
clobber each other's connections (each new `ADMIN LOGIN` disconnects
the prior). Run the MCP daemon (`schwab server --enable-mcp`) and use
`schwab stream --mcp` for true concurrent multi-tool streaming through
its shared `stream_quote` tool.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Clean exit on Ctrl+C / normal termination. |
| 1 | Missing config or session, expired refresh token, Schwab error. |
| 2 | Usage error (conflicting flags, missing symbols). |
