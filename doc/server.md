# `server`

Run and manage the long-lived **auth-maintenance server** — the
component that keeps your Schwab OAuth refresh token alive so the rest
of `schwab_cli` never falls out of auth.

Schwab's refresh token expires after **7 days**; once it dies a full
browser OAuth round-trip (login + MFA) is required to mint a new one.
`schwab server` runs a maintenance loop that proactively renews the
refresh token (via the headless browser auto-login) before it expires.
It is the **single proactive renewer** on the machine.

## Auth model

`schwab_cli` is layered so that auth lives in exactly one place:

- The **service layer** (`schwab_cli.service.*`) owns auth on a
  per-call basis: it reads `session.json`, mints a fresh **access
  token** when the current one is stale, and raises `SessionExpired`
  when the **refresh token** is dead. Every CLI command, MCP tool, and
  REST handler relies on this — so they all stay fresh as long as the
  refresh token is alive.
- `schwab server` is the only component that renews the **refresh
  token** itself. The per-call service handles short-lived access
  tokens; the server handles the 7-day boundary.

This split is why `--enable-mcp` disables the MCP server's own auth
monitor (see below): the maintenance loop is already the renewer, and
two renewers competing would thrash the OAuth flow.

## Usage

```
# Bare: auth-maintenance loop only (no network listeners).
schwab server [--interval-hours 8] [--no-auto-login]

# Also run the Streamable HTTP MCP server (single /mcp endpoint).
schwab server --enable-mcp [--mcp-host 127.0.0.1] [--mcp-port 7234]
              [--log-file PATH | --no-log-file]

# Also serve the unauthenticated REST PoC.
schwab server --enable-rest [--rest-host 127.0.0.1] [--rest-port 8000]

# launchd LaunchAgent management (macOS).
schwab server install   [--plist-path PATH] [--log-file PATH] [--yes]
schwab server status
schwab server uninstall [--plist-path PATH] [--yes]
```

The `--enable-*` flags **compose**: `schwab server --enable-mcp
--enable-rest` runs the maintenance loop, the MCP server, and the REST
routes together.

## Modes

### Bare — auth-maintenance daemon

```bash
schwab server
```

Loads config, installs SIGTERM/SIGINT handlers for graceful shutdown,
and drives the maintenance loop on an interval (default 8h). No ports
are opened. This is the minimal "keep the box logged in" daemon — pair
it with launchd so it survives reboots.

### `--enable-mcp` — also run the MCP server

```bash
schwab server --enable-mcp
```

Composes the **Streamable HTTP** MCP server (a single `/mcp` endpoint,
default `127.0.0.1:7234`) on top of the maintenance loop. The
maintenance loop runs in a daemon thread as the single proactive
refresh-token renewer; the MCP server runs on the main thread with its
**auth monitor disabled** so there's no competing rotation. After each
renewal the loop hands the fresh session to the in-memory MCP client so
its next call uses the rotated token.

This is the **integrated** way to run MCP. The standalone
[`schwab mcp`](mcp.md) daemon is the alternative when you don't want the
maintenance loop in the same process — there, the MCP server runs its
own auth monitor.

### `--enable-rest` — also serve the REST PoC

```bash
schwab server --enable-rest
```

Serves a small REST proof-of-concept exercising the REST → service
path:

| Path | Method | Purpose |
| --- | --- | --- |
| `/quote/{symbol}` | GET | Quote for one symbol via the service layer. |
| `/health` | GET | Liveness probe. |

> ⚠️ **The REST PoC is UNAUTHENTICATED.** It proves the REST →
> service wiring only; auth / allowlisting is a deliberate later step.
> Keep it on loopback (the default) and do not expose it publicly.

Standalone (`--enable-rest` without `--enable-mcp`) it runs via uvicorn
on `--rest-host:--rest-port` (default `127.0.0.1:8000`). Combined with
`--enable-mcp`, its routes mount onto the MCP server's Starlette app and
share that single port instead.

## Running as a macOS service (launchd)

```bash
schwab server install     # write the plist + launchctl load
schwab server status      # is com.schwab-cli.server loaded?
schwab server uninstall   # launchctl unload + remove the plist
```

`install` writes the LaunchAgent at
`~/Library/LaunchAgents/com.schwab-cli.server.plist` with
`KeepAlive=true`, so any exit triggers a relaunch, and loads it
immediately. The plist runs the **bare** maintenance loop by default;
edit it (or re-install) if you want the launchd job to add
`--enable-mcp` / `--enable-rest`.

**Why a LaunchAgent, not a LaunchDaemon**: it runs under your user, so
it can read `~/.config/schwab_cli/session.json` and drive the browser
auto-login natively. A root daemon would need user-switching gymnastics.

## Notifications

Maintenance ticks (renew succeeded / failed / skipped) are forwarded
through the `notify` subsystem, so a renewal failure can alert you via
Telegram. Configure with `schwab notify setup --channel telegram`; see
[`notify.md`](notify.md).

## Related

- [`mcp`](mcp.md) — the standalone MCP daemon + admin subcommands.
- [`auth`](auth.md) — the OAuth flow and browser auto-login the server
  drives on renewal.
- [`notify`](notify.md) — alert channels for maintenance events.
