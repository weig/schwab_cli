# schwab_cli

A CLI for Charles Schwab API access.

## Requirements

- Python 3.11+
- [`uv`](https://github.com/astral-sh/uv)

## Install (dev)

```bash
uv sync --extra dev
```

## Install (global)

```bash
uv tool install .
```

## First-time setup

Run `setup` to capture your Schwab API credentials and (optional) auto-login
credentials. The config is stored at `~/.config/schwab_cli/config.json` with
mode `0600`.

```bash
schwab_cli setup
```

The prompt walks through:

1. **Client ID** — your Schwab developer-portal client ID.
2. **Client Secret** — your Schwab developer-portal client secret.
3. **Enable automatic login?** — if yes, you'll be asked for a username and password.
4. **Username / Password** — either literal values, or 1Password Secret References
   (`op://<vault>/<item>/<field>`). `op://` values are resolved at login time by the
   future `login` command via the `op` CLI.

Re-running `setup` shows existing values as defaults; press **Enter** to keep
them or type a new value. Sensitive values are displayed masked (`****xxxx`).

## Run tests

```bash
uv run pytest
```
