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

## Auth flow

There is a single auth flow, `local_server`, configured automatically by
[setup](setup.md). (Legacy configs with the retired `client` / `code_relay`
flows still load so non-auth commands keep working, but `schwab auth`
refuses them and tells you to re-run `schwab setup`.)

### `local_server`

`schwab_cli` starts a small **local HTTPS callback server** bound to the
host/port/path of your `redirect_uri` (e.g.
`https://127.0.0.1:19806/schwab/callback`), using the leaf certificate
installed by [`schwab cert install`](cert.md). Schwab redirects the browser
straight back to that loopback URL with `?code=…&state=…`, and the local
server captures the code. No external relay, no polling — the callback lands
directly on your machine.

- **Strict state validation.** A fresh OAuth `state` token is generated per
  run and the callback must echo it exactly; a missing or mismatched `state`
  is rejected (CSRF / stale-callback protection).
- **Paste fallback.** On the human path (no auto-login, or `--manual`) a
  paste handler races alongside the server: if the redirect can't reach the
  loopback for some reason, paste the full redirected URL when prompted.
- **Fail fast on a missing cert.** If the leaf certificate isn't installed,
  `auth` fails immediately with "run `schwab cert install` first" — *before*
  opening the browser — so you never burn a one-shot `state` token. The
  callback port is also bound up front, so a port-in-use error surfaces
  before login too.

When `auto_login_command` is configured (and `--manual` is not passed),
[webauto-cli](https://github.com/weig/webauto-cli) drives the browser through
the Schwab login while the local server captures the redirect.

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

- **"local callback server needs a TLS certificate — run `schwab cert
  install` first"** — The leaf certificate for the loopback callback isn't on
  disk. Run [`schwab cert install`](cert.md) (macOS), then retry `auth`.
- **"port … in use; close the other process or re-run setup"** — Another
  process is already bound to your callback port. Find it (e.g.
  `lsof -i :<port>`) and stop it, or re-run `schwab setup` to pick a new port.
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
