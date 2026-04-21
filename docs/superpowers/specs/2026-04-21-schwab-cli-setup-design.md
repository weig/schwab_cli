# Schwab CLI — `setup` Command Design

**Date:** 2026-04-21
**Status:** Approved (pending user spec review)
**Scope:** First milestone of the Schwab CLI — the `setup` subcommand only. OAuth flow, login, and API access are out of scope for this spec.

## Goal

Provide a `schwab_cli setup` interactive command that captures the user's Schwab API credentials (`client_id`, `client_secret`) and optional auto-login credentials (`username`, `password`), and persists them to `~/.config/schwab_cli/config.json` with strict file permissions.

The command is idempotent: re-running it shows current values as defaults so the user can press Enter to keep them or type new values.

## Non-Goals

- Performing the OAuth token exchange.
- Validating credentials against Schwab's API.
- Driving Playwright auto-login (handled by a future `login` command).
- Checking for `op` CLI presence (the future `login` command resolves `op://` references at use time).
- Encrypting secrets at rest beyond filesystem permissions.

## Decisions Summary

| Decision | Choice |
|---|---|
| Language / runtime | Python (3.11+) |
| Package manager | `uv` |
| CLI framework | `typer` |
| Invocation | Both `schwab_cli setup` (console script) and `python -m schwab_cli setup` |
| Config file location | `~/.config/schwab_cli/config.json` |
| Secret storage at rest | Plain JSON, `chmod 0600`, parent dir `chmod 0700` |
| 1Password integration | Detect `op://` prefix in `password` field; resolve at login time (out of scope here) |
| Sensitive value display | Masked default showing last 4 chars (e.g. `****3xyz`) |
| Auto-login signal | Presence of both `username` and `password` (no separate `enabled` flag) |

## Project Layout

```
schwab_cli/
├── pyproject.toml          # uv-managed; declares console script + python -m entry
├── README.md
├── src/
│   └── schwab_cli/
│       ├── __init__.py
│       ├── __main__.py     # enables `python -m schwab_cli`
│       ├── cli.py          # typer app, registers subcommands
│       ├── config.py       # Config dataclass, load/save, path resolution
│       └── commands/
│           ├── __init__.py
│           └── setup.py    # `setup` subcommand implementation
└── tests/
    ├── test_config.py
    └── test_setup.py
```

`pyproject.toml` declares:

```toml
[project.scripts]
schwab_cli = "schwab_cli.cli:app"
```

`src/schwab_cli/__main__.py`:

```python
from schwab_cli.cli import app
app()
```

Rationale: `config.py` stays free of CLI concerns so it can be unit-tested without a typer runner. Subcommands live in their own files under `commands/` so future commands (`login`, `quote`, etc.) can be added without crowding `cli.py`.

## Config Schema

File: `~/.config/schwab_cli/config.json`

```json
{
  "version": 1,
  "client_id": "ABCD1234...",
  "client_secret": "xyz...",
  "username": "user@example.com",
  "password": "op://Personal/Schwab/password"
}
```

Field rules:

- `version` (int, required) — schema version. Always `1` for this milestone. A future version mismatch causes setup to refuse to overwrite without an explicit upgrade path.
- `client_id` (string, required) — Schwab API client ID.
- `client_secret` (string, required) — Schwab API client secret.
- `username` (string, optional) — Schwab login username (or `op://...` reference).
- `password` (string, optional) — Schwab login password (or `op://...` reference).
- Both `username` and `password` present → auto-login enabled.
- Both absent → auto-login disabled.
- Exactly one set is invalid; setup will not produce such a file.

File / dir permissions:

- File written with mode `0o600`.
- Parent dir created with mode `0o700` if missing.
- Existing dir permissions are not modified (documented as user's responsibility to tighten if desired).

### Python model

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class Config:
    client_id: str
    client_secret: str
    username: str | None = None
    password: str | None = None
    version: int = 1

    @property
    def auto_login_enabled(self) -> bool:
        return self.username is not None and self.password is not None
```

`frozen=True` enforces the project's immutability principle: `setup` builds a new `Config` and writes it; never mutates a loaded one.

## `setup` Command Flow

```
1. Load existing config from ~/.config/schwab_cli/config.json (or None if missing).
2. Print header: "Schwab CLI Setup" + the config path being written.
3. Prompt: client_id      [default: existing or empty]
4. Prompt: client_secret  [default: masked existing (e.g. "****3xyz") or empty]
5. Confirm: "Enable automatic login? [Y/n]"
       default = True if existing config has BOTH username and password set, else False
6. If yes:
       Prompt: username  [default: existing or empty]
       Prompt: password  [default: masked existing or empty]
                         hint: "supports op:// 1Password references"
   If no:
       username = None, password = None
7. Build new Config(...).
8. Write atomically: write to <path>.tmp, chmod 0600, rename to <path>.
       (mkdir -p the parent with 0700 if missing.)
9. Print summary: "Saved to <path>. Auto-login: enabled/disabled."
```

UX details:

- **Default-on-Enter:** typer's `prompt(default=...)` returns the default when the user presses Enter. For sensitive fields, the displayed default is the masked form, but pressing Enter restores the unmasked stored value internally.
- **Required fields:** `client_id` and `client_secret` are required. If no existing value AND user enters empty, re-prompt with a clear error.
- **Hidden input for new passwords only:** `password` prompt uses `hide_input=True` only when no existing value is present. When there *is* an existing value, the masked default is visible and Enter keeps it; typing a new value echoes (acceptable trade-off for editability).
- **Disabling auto-login on re-run:** answering "n" at the auto-login confirm clears `username` and `password` from the saved config — no stale credentials left behind.
- **op:// hint:** shown next to the password prompt so the user knows the option exists without needing docs.

### Masking helper

```python
def mask_secret(value: str) -> str:
    """Mask all but the last 4 characters. Strings shorter than 4 chars are fully masked."""
    if len(value) <= 4:
        return "*" * len(value)
    return "*" * (len(value) - 4) + value[-4:]
```

## Error Handling

| Scenario | Behavior |
|---|---|
| Config dir/file write fails (permission denied, disk full) | Catch `OSError`, print clear message including the path, exit 1 |
| Existing config is malformed JSON | Warn, show parse error, prompt "overwrite with new setup? [y/N]". On `n` → exit 0 with no changes |
| Existing config has unknown fields | Ignore with a warning (forward compatibility) |
| Existing config has `version` newer than current | Print upgrade-required message, exit 1 — do not overwrite |
| User Ctrl+C during prompts | typer raises `Abort`; top-level handler catches it, exits 130, no partial writes |
| `HOME` unset | Use `pathlib.Path.home()`; if it raises, surface a clear error |
| Existing dir has loose permissions (e.g. `0o755`) | Do not modify; document `chmod 700 ~/.config/schwab_cli` as a user step |
| Rename over file owned by another user | `OSError` from rename surfaces with the path |

## Testing Strategy

Framework: `pytest`. Coverage target: ≥80% per project rules.

### `tests/test_config.py` — pure logic, no CLI

- Round-trip: `Config` → JSON → `Config` equality.
- `auto_login_enabled` property: both set, neither set, only username, only password.
- `load()` returns `None` when file missing.
- `load()` raises a clear error on malformed JSON.
- `save()` writes file with `0o600` and dir with `0o700` (using `tmp_path` fixture and a patched home dir).
- `save()` is atomic: monkeypatch `os.rename` to raise after the temp file is written; verify the original file is untouched.
- `save()` round-trips a config containing an `op://` reference verbatim.

### `tests/test_setup.py` — CLI behavior via typer's `CliRunner`

- Fresh setup with all fields → file is written; auto-login disabled when user declines.
- Fresh setup with auto-login enabled → file contains `username` and `password`.
- Re-run with existing config: pressing Enter at every prompt keeps all existing values (verified by reading the saved file).
- Re-run and disable auto-login → saved file omits `username` and `password`.
- Sensitive masking: `mask_secret("abc123xyz")` → `"****3xyz"`; values ≤4 chars → fully masked.
- Required-field re-prompt: empty `client_id` on fresh setup re-prompts until a value is given.
- Malformed existing config: declining the overwrite leaves file unchanged on disk.
- Ctrl+C during prompt → no partial file written (verify temp file is also cleaned up or never committed).

## Open Questions / Future Work

These are explicitly deferred to later milestones:

- `login` command: OAuth token exchange + Playwright auto-login flow + `op://` resolution.
- Token refresh and storage (likely `~/.config/schwab_cli/tokens.json` with same `0o600` rules).
- Additional commands: `quote`, `accounts`, `orders`, etc.
- Optional macOS Keychain backend for secret storage.
