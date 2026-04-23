# `auth`

Authenticate with Schwab. Refreshes an existing session if one is valid;
otherwise runs the full OAuth flow and writes a new
`~/.config/schwab_cli/session.json`.

## Usage

```
schwab_cli auth [--force] [--manual]
```

## Flags

| Flag | Purpose |
| --- | --- |
| `--force` | Skip the refresh attempt and go straight to a full OAuth flow. |
| `--manual` | Open the browser visibly and let you drive the login yourself. Sets `HEADLESS=0` for this invocation and skips saved-credential automation. |

## Decision matrix

| State | Behaviour |
| --- | --- |
| No config | Error (`Run setup first`), exit 1. |
| Config is unreadable | Error (`Config is unusable: …`), exit 1. |
| Valid session exists | Refresh via `refresh_token`. On success: exit 0 with "Already logged in". |
| Refresh fails | Fall through to full OAuth. |
| `--force` given | Skip refresh attempt. |
| `--manual` given | Skip saved-credential automation. User drives the login. |

## Auth flows

Selected during [setup](setup.md); this command honours whichever is
active in your config.

### `client`

Schwab redirects to a loopback URL (e.g. `https://127.0.0.1:8443`). The
CLI reads the OAuth code directly from the browser's address bar after
Schwab navigates there. No external server required.

### `code_relay`

Your configured `redirect_uri` is a pre-deployed public relay (e.g. a
Cloudflare Worker). The relay catches Schwab's callback and the CLI
long-polls `code_relay_url` to retrieve the code. Use this when the
loopback isn't reachable — remote shells, mobile-first flows, etc.

## Environment variables

| Variable | Effect |
| --- | --- |
| `HEADLESS=1` | Chrome runs without a window. Fast, but you can't see what's happening. `--manual` forces this to `0`. |
| `HEADLESS=0` (default) | Visible Chromium window. Required for `--manual`. |
| `DEBUG=1` | Verbose trace logs plus sanitised page-source dumps to `~/.config/schwab_cli/auth-debug/<timestamp>/` at every UI step. |

## Example: refresh (happy path)

```
$ schwab_cli auth
Already logged in. Access token valid until 2026-04-23T16:15:42+00:00.
```

## Example: full auth with saved credentials

```
$ DEBUG=1 schwab_cli auth --force
[debug] resolving secrets
[debug] launching seleniumbase UC chromium (headless=False, ...)
[debug] driver ready (Chromium launched, UC patches applied)
[debug] navigated via driver.default_get (visible mode)
[debug] waiting for login page
[debug] filling credentials and submitting login
[debug] waiting for MFA picker / waiting / consent (whichever appears first)
[debug] checking consent agreement and clicking continue
[debug] waiting for Informed Consent modal Accept
[debug] waiting for account selection page
[debug] found N account checkbox(es)
[debug] waiting for confirmation page
[debug] clicking Done
Authenticated. Access token expires at 2026-04-23T16:15:42+00:00.
```

## Example: manual override

```
$ schwab_cli auth --manual
```

Visible browser opens on the Schwab login page. You log in yourself;
the CLI waits up to 5 minutes for the redirect and captures the code.

## Troubleshooting

- **"Refresh token rejected; doing full auth"** — Normal after ~7 days
  (the refresh-token lifetime). A full auth follows automatically.
- **"SeleniumBase UC was blocked by Akamai (Access Denied)"** —
  Schwab's bot protection flagged the session. Re-run with
  `HEADLESS=0` (visible browser) or delete `~/.config/schwab_cli/chromium-uc/`
  to start with a clean profile.
- **Browser opens but no Schwab page appears** — Chromium took the
  automation session on a different window handle. The CLI handles
  this automatically now; if you still see it, delete the persistent
  profile at `~/.config/schwab_cli/chromium-uc/` and retry.
- **Port 9222 already in use** — The SeleniumBase UC driver picks an
  ephemeral port automatically, so this isn't a problem anymore. If
  you see the error anyway, check `lsof -i :9222` and kill the stale
  process.
