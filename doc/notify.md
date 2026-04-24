# `notify`

Configure and test notification channels the MCP daemon uses to
alert on auth, streamer, and lifecycle events. MVP ships with
**Telegram**; Slack is deferred to Phase 2b.

## Usage

```
schwab_cli notify list                    # show configured channels
schwab_cli notify setup --channel telegram   # interactive
schwab_cli notify test  --channel telegram   # send a hello
```

## Config file

`~/.config/schwab_cli/notification.json` (mode 0600). Kept separate
from `config.json` so `schwab_cli setup` — which rewrites
`config.json` wholesale — can't clobber notification settings.

Shape:

```json
{
  "telegram": {
    "bot_token": "<from @BotFather>",
    "chat_id": "<your user or group id>",
    "events": [
      "auth.auto_login.failed",
      "auth.auto_login.succeeded",
      "auth.refresh_expiring",
      "streamer.crash"
    ],
    "rate_limit_seconds": 300
  },
  "slack": {
    "_tbd": "Slack channel not yet supported — see docs/plan/mcp-service.md (Phase 2b)."
  }
}
```

- **`bot_token`** — from [@BotFather](https://t.me/BotFather).
- **`chat_id`** — your user id (DMs) or a group's id. Start a
  chat with the bot, then hit
  `https://api.telegram.org/bot<TOKEN>/getUpdates` to see the
  numeric id.
- **`events`** — event names the channel subscribes to. Unknown
  names are ignored at dispatch time.
- **`rate_limit_seconds`** — per-event debounce. Default 300 (5 min).
  Prevents notification storms during transient issues.

## Event catalog

| Event | Level | Fires when |
| --- | --- | --- |
| `auth.auto_login.succeeded` | info | Proactive browser rotation finished successfully. |
| `auth.auto_login.failed` | error | Rotation subprocess returned non-zero. Manual `schwab_cli auth --force` required. |
| `auth.refresh_expiring` | warning | Refresh token has <15 min left and anti-thrash blocks another attempt. |
| `streamer.crash` | error | Schwab streamer WebSocket reader loop died unexpectedly. |
| `daemon.start` / `daemon.stop` | info | Daemon lifecycle events (opt-in — not in default subscription). |
| `test.hello` | info | Only fired by `notify test`. |

## Commands

### `schwab_cli notify setup`

Interactive Telegram configuration. Prompts for bot token + chat id,
writes `notification.json` with sensible event defaults, then fires
a test message to confirm the transport works.

### `schwab_cli notify test`

Bypasses the subscription-list filter and rate-limit — the command's
job is to probe the wire. Reports the transport outcome explicitly
(`✔ sent` or `✗ failed: <reason>`) so you know whether Telegram
actually accepted the POST.

### `schwab_cli notify list`

Dump the configured channels and their event subscriptions. Never
prints secrets.

## Security posture

- `notification.json` is 0600 — owner-only read/write, same as
  `config.json` and `session.json`.
- Bot tokens are never logged; the logbook redacts on the way out.
- Rate limiting caps Telegram API usage per event at
  `rate_limit_seconds` intervals.

## What's next (Phase 2b)

- **Slack channel**: webhook URL + mrkdwn templates, same
  `Channel` protocol. Config slot already reserved under
  `slack.*` in `notification.json`.
- **Desktop notifications** via `osascript` on macOS — nice for
  always-running laptops.
- **User-configurable event catalog** — adding new event types
  without touching code.
