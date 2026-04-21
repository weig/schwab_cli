# schwab_cli

A CLI for Charles Schwab API access.

## Requirements

- Python 3.11+
- [`uv`](https://github.com/astral-sh/uv)
- [Playwright Chromium](https://playwright.dev) (installed via `playwright install chromium`)
- [1Password CLI `op`](https://developer.1password.com/docs/cli/) (only required if you use `op://` references for username/password)

## Install (dev)

```bash
uv sync --extra dev
uv run playwright install chromium
```

## Install (global)

```bash
uv tool install --editable .
playwright install chromium
```

## First-time setup

```bash
schwab_cli setup
```

Interactive prompts capture your Schwab API credentials and (optionally) auto-login credentials. Saved to `~/.config/schwab_cli/config.json` (mode `0600`).

The auto-login `password` field accepts either a literal value or a 1Password Secret Reference (`op://<vault>/<item>/<field>`). `op://` values are resolved at auth time via the `op` CLI; nothing sensitive ever lands in your shell history.

## Authenticate

```bash
schwab_cli auth          # refresh existing session if present, else full OAuth
schwab_cli auth --force  # skip refresh; always run the full OAuth flow
```

Tokens are saved to `~/.config/schwab_cli/session.json` (mode `0600`).

The flow also handles Schwab's MFA when it shows. After login, if Schwab presents the device-verification page, `schwab_cli auth`:

1. Selects the **Schwab App** option. If the option isn't present, auth fails with a clear message telling you to re-run with `DEBUG=1` and inspect the dumped page source.
2. Prints `"Schwab App MFA: check your phone to approve (up to 30s)..."` to stderr and waits up to 30 seconds for approval.
3. If Schwab shows the **Trust this device** page afterwards, selects *Yes, trust this device* and clicks **Next**.
4. Proceeds to the consent page.

If the device is already trusted, the MFA and/or trust steps are skipped automatically.

**Persistent browser profile.** Auth uses a persistent Chromium profile at `~/.config/schwab_cli/chromium/` (mode `0700`) so cookies — including Schwab's Trust Device cookie — survive across runs. The first successful auth that goes through MFA + "trust this device" should be the only one that needs the phone-tap; subsequent runs reuse the trust cookie and skip MFA entirely. To force a fresh device (e.g., after revoking trust on Schwab's side), delete the directory:

```bash
rm -rf ~/.config/schwab_cli/chromium
```

**Headless mode:** Schwab's OAuth UI sits behind Akamai Bot Manager, which blocks vanilla Playwright headless at the TLS/HTTP fingerprint layer. Two backends live in the codebase:

| Mode | Backend | When |
|---|---|---|
| Default (visible) | Playwright | Fast, quiet, same first-run trust-device cookie reuse across runs |
| `HEADLESS=1` | SeleniumBase UC | Bypasses Akamai for true headless operation (CI / cron / servers) |

Both perform the same flow (login → MFA → trust → consent → accept → accounts → confirm → done → code → exchange). The persistent profile lives at `~/.config/schwab_cli/chromium/` (Playwright) or `~/.config/schwab_cli/chromium-uc/` (SeleniumBase) — both keep the Trust Device cookie so subsequent runs skip MFA.

The refresh path (used every run after the first, while the 7-day refresh token is valid) is pure HTTP via `httpx` and doesn't touch a browser at all — headless-safe regardless of which backend is wired for full auth.

Set `DEBUG=1` (or `true` / `yes`, case-insensitive) to:

- Slow down each Playwright action by 1 second so the flow is watchable
- Emit `[debug] <step>` progress logs to stderr at each phase (navigating, waiting for login, filling credentials, consent, account selection, redirect capture)
- Dump the HTML source of each page checkpoint to `~/.config/schwab_cli/auth-debug/<timestamp>/<NN>-<label>.html` — useful when Schwab changes their UI and you need to find the right selectors. Resolved username/password are stripped before writing.
- Hold the browser open for 60 seconds after the flow ends (success or failure) so you can inspect the final page — press Ctrl+C during the hold to close immediately

```bash
DEBUG=1 schwab_cli auth --force
```

Debug logs never contain credentials, resolved secrets, tokens, or the auth code — only phase names. If Schwab changes their UI, update `src/schwab_cli/browser/selectors.py`.

## Run tests

```bash
uv run pytest
```
