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

By default, the OAuth browser runs **headless**. Set `DEBUG=1` (or `true` / `yes`, case-insensitive) to see the browser:

```bash
DEBUG=1 schwab_cli auth --force
```

When DEBUG is enabled, a screenshot is also written to `~/.config/schwab_cli/auth-error-<timestamp>.png` if any step fails — useful when Schwab changes their UI and selectors need updating (`src/schwab_cli/browser/selectors.py`).

## Run tests

```bash
uv run pytest
```
