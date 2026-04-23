# `setup`

Interactive configuration — captures your Schwab developer-app credentials
and writes them to `~/.config/schwab_cli/config.json` with `0600`
permissions.

## Usage

```
schwab_cli setup [--dry-run]
```

## Flags

| Flag | Purpose |
| --- | --- |
| `--dry-run` | Run the prompts but print the resulting JSON to stdout instead of writing the file. Useful for previewing a change or generating a config snippet without touching the real file. |

## What it asks for

| Prompt | Notes |
| --- | --- |
| Client ID | From the Schwab developer portal for your app. |
| Client Secret | From the Schwab developer portal. Hidden on entry. |
| Redirect URI | Exactly as registered in the developer portal (trailing slash matters). |
| Auth flow | `client` (loopback) or `code_relay` (public relay). See [auth](auth.md). |
| Code Relay URL | Only when `auth_flow=code_relay`. The relay's `/wait` endpoint. |
| Auto-login credentials | Optional — enables `auth --force` without manual browser input. Password supports `op://` 1Password references. |

## Auth flow selection

The prompt is arrow-key-navigable in an interactive terminal, or a
numbered menu when stdin is piped (tests, scripts). Each option shows a
one-paragraph description so you don't need to dig through docs.

## Example session (sanitised)

```
Schwab CLI Setup
Config: /home/user/.config/schwab_cli/config.json

Client ID: your_client_id_here
Client Secret: ****

Redirect URI: https://127.0.0.1:8443

Auth flow — how the CLI captures the OAuth `code`:

  1. client
     Schwab redirects to your loopback redirect_uri.
     The CLI reads the OAuth code straight from the browser's URL bar.
     No external server required.

  2. code_relay
     Your redirect_uri points to a pre-deployed public relay.
     The relay catches the callback and the CLI polls it for the OAuth code.
     Use this when the loopback redirect isn't reachable (remote shells,
     mobile login, etc.).

Auth flow (name or number) [client]: 1

Enable automatic login? [y/N]: y
Username: demo@example.com
Password: ****

Saved to /home/user/.config/schwab_cli/config.json.
Auto-login: enabled
```

## `--dry-run`

Same prompts, different finish:

```
--- dry-run: would write /home/user/.config/schwab_cli/config.json ---
{
  "version": 1,
  "client_id": "your_client_id_here",
  "client_secret": "your_secret_here",
  "redirect_uri": "https://127.0.0.1:8443",
  "auth_flow": "client",
  "username": "demo@example.com",
  "password": "op://Personal/Schwab/password"
}
--- not saved ---
Auto-login: enabled (dry-run)
```

Nothing is written to disk.

## Scripted preview

Point at an isolated path with `SCHWAB_CLI_CONFIG` — never risks
overwriting the real config:

```bash
export SCHWAB_CLI_CONFIG=/tmp/preview.json
printf 'cid\ncsec\nhttps://127.0.0.1:8443\n1\nn\n' | schwab_cli setup --dry-run
unset SCHWAB_CLI_CONFIG
```
