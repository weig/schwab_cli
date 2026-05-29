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
| `--dry-run` | Run the prompts but print the resulting JSON to stdout instead of writing the file. Useful for previewing a change or generating a config snippet without touching the real file. Skips the certificate-install step. |

## What it asks for

| Prompt | Notes |
| --- | --- |
| Client ID | From the Schwab developer portal for your app. |
| Client Secret | From the Schwab developer portal. Hidden on entry. |
| Callback URL | The redirect URI you registered in the developer portal, exactly as registered (trailing slash matters). Recommended: a loopback HTTPS callback like `https://127.0.0.1:PORT/schwab/callback`. Defaults to `https://127.0.0.1:<random port>/schwab/callback`. See [auth](auth.md). |
| Auto-login command | Optional — an external subprocess (e.g. [webauto-cli](https://github.com/weig/webauto-cli)) that drives the browser so `auth --force` runs hands-off. Parsed with shell quoting. |
| Auto-login timeout | Only when an auto-login command is configured. Seconds to wait for the subprocess (default `300`). |

There is no longer an "auth flow" menu or a "Code Relay URL" prompt — the
single supported flow is `local_server` and it is set automatically.

## Local callback certificate

When the Callback URL is a **loopback HTTPS** URL (host `127.0.0.1`,
`localhost`, or `::1`), `setup` prints a notice and runs the equivalent of
[`schwab cert install`](cert.md): it installs a one-time, name-constrained
local root CA into the **macOS System keychain** so the browser trusts the
local callback server at auth time.

- You will be asked for your **login (sudo) password** once, to add the CA
  to the System keychain.
- A **non-loopback** Callback URL skips this step (no local server is run).
- A **non-interactive** session (stdin is not a TTY) skips the install with a
  hint to run `schwab cert install` later before authenticating.
- If the certificate install **fails**, setup warns but still writes the
  config — you can run `schwab cert install` before your first `auth`.

macOS only for now. See [cert](cert.md) for the full trust model and how to
uninstall cleanly.

## Example session (sanitised)

```
Schwab CLI Setup
Config: /home/user/.config/schwab_cli/config.json

Client ID: your_client_id_here
Client Secret: ****

  (Recommended: a loopback HTTPS callback like https://127.0.0.1:PORT/schwab/callback — schwab_cli captures the redirect locally.)
Callback URL [https://127.0.0.1:19806/schwab/callback]:

Configure auto-login subprocess (e.g. webauto-cli)? [y/N]: n

This callback runs a local HTTPS server on 127.0.0.1; a one-time root certificate may be installed so the browser trusts it.

Auth uses a local callback: schwab_cli starts a tiny HTTPS server on 127.0.0.1 to receive the OAuth redirect.
This needs a one-time root certificate for 127.0.0.1 in your System keychain — you'll be asked for your login password next.
Password:

Saved to /home/user/.config/schwab_cli/config.json.
Auto-login: disabled
```

## `--dry-run`

Same prompts, different finish — and the certificate-install step is skipped:

```
--- dry-run: would write /home/user/.config/schwab_cli/config.json ---
{
  "version": 1,
  "client_id": "your_client_id_here",
  "client_secret": "your_secret_here",
  "redirect_uri": "https://127.0.0.1:19806/schwab/callback",
  "auth_flow": "local_server"
}
--- not saved ---
Auto-login: disabled (dry-run)
```

When auto-login is enabled, the payload also carries `auto_login_command`
(a list of argv tokens) and `auto_login_timeout_seconds`. Nothing is written
to disk under `--dry-run`.

## Scripted preview

Point at an isolated path with `SCHWAB_CLI_CONFIG` — never risks
overwriting the real config. With `--dry-run` no certificate is installed,
so this is safe to run unattended:

```bash
export SCHWAB_CLI_CONFIG=/tmp/preview.json
printf 'cid\ncsec\nhttps://127.0.0.1:19806/schwab/callback\nn\n' | schwab_cli setup --dry-run
unset SCHWAB_CLI_CONFIG
```
