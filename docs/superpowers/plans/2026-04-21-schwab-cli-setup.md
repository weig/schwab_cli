# Schwab CLI `setup` Command Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first milestone of the Schwab CLI — a `schwab_cli setup` interactive command that captures Schwab API credentials and optional auto-login credentials, persisting them to `~/.config/schwab_cli/config.json` with strict permissions.

**Architecture:** Python 3.11+ package using `typer` for the CLI and frozen dataclasses for config. CLI layer (`cli.py`, `commands/setup.py`) is kept thin; all I/O and data logic lives in `config.py` so it can be unit-tested without the CLI runner. Packaged with `uv` and `pyproject.toml`; installs as a console script `schwab_cli` and is also invocable via `python -m schwab_cli`.

**Tech Stack:** Python 3.11+, `typer`, `pytest`, `uv`, frozen `@dataclass`, standard-library JSON and `pathlib`.

**Spec:** [`docs/superpowers/specs/2026-04-21-schwab-cli-setup-design.md`](../specs/2026-04-21-schwab-cli-setup-design.md)

---

## File Structure

| Path | Responsibility |
|---|---|
| `pyproject.toml` | Package metadata, dependencies, console-script entry |
| `src/schwab_cli/__init__.py` | Package marker, `__version__` |
| `src/schwab_cli/__main__.py` | Enables `python -m schwab_cli` |
| `src/schwab_cli/cli.py` | Typer app; registers subcommands |
| `src/schwab_cli/config.py` | `Config` dataclass, path resolution, `load`, `save`, `mask_secret` |
| `src/schwab_cli/commands/__init__.py` | Package marker |
| `src/schwab_cli/commands/setup.py` | Interactive `setup` subcommand |
| `tests/__init__.py` | Test package marker |
| `tests/test_config.py` | Unit tests for `config.py` |
| `tests/test_setup.py` | CLI tests for `setup` command via `typer.testing.CliRunner` |
| `README.md` | Install + usage instructions |

---

## Task 1: Project Scaffolding

**Files:**
- Create: `pyproject.toml`
- Create: `src/schwab_cli/__init__.py`
- Create: `src/schwab_cli/__main__.py`
- Create: `src/schwab_cli/cli.py`
- Create: `src/schwab_cli/commands/__init__.py`
- Create: `tests/__init__.py`
- Create: `.gitignore`
- Create: `.python-version`

- [ ] **Step 1.1: Initialize git**

Run from `/Users/weig/Projects/finance/schwab_cli`:

```bash
git init
git branch -M main
```

Expected: "Initialized empty Git repository".

- [ ] **Step 1.2: Create `.python-version`**

```
3.11
```

- [ ] **Step 1.3: Create `.gitignore`**

```
__pycache__/
*.py[cod]
*.egg-info/
.venv/
.pytest_cache/
.coverage
htmlcov/
dist/
build/
.DS_Store
```

- [ ] **Step 1.4: Create `pyproject.toml`**

```toml
[project]
name = "schwab_cli"
version = "0.1.0"
description = "Charles Schwab CLI for API access"
requires-python = ">=3.11"
dependencies = [
    "typer>=0.12.0",
]

[project.scripts]
schwab_cli = "schwab_cli.cli:app"

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-cov>=5.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/schwab_cli"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-v --cov=schwab_cli --cov-report=term-missing"
pythonpath = ["src"]
```

- [ ] **Step 1.5: Create `src/schwab_cli/__init__.py`**

```python
__version__ = "0.1.0"
```

- [ ] **Step 1.6: Create `src/schwab_cli/cli.py` (stub)**

```python
import typer

app = typer.Typer(
    name="schwab_cli",
    help="Charles Schwab CLI.",
    no_args_is_help=True,
    add_completion=False,
)
```

- [ ] **Step 1.7: Create `src/schwab_cli/__main__.py`**

```python
from schwab_cli.cli import app

if __name__ == "__main__":
    app()
```

- [ ] **Step 1.8: Create `src/schwab_cli/commands/__init__.py`**

Empty file.

- [ ] **Step 1.9: Create `tests/__init__.py`**

Empty file.

- [ ] **Step 1.10: Install dependencies via uv**

Run:

```bash
uv sync --extra dev
```

Expected: creates `.venv/`, installs typer + pytest. No errors.

- [ ] **Step 1.11: Verify console script is registered**

Run:

```bash
uv run schwab_cli --help
```

Expected output contains `Usage: schwab_cli [OPTIONS] COMMAND [ARGS]...` and exits 0.

- [ ] **Step 1.12: Commit**

```bash
git add .gitignore .python-version pyproject.toml src/ tests/
git commit -m "chore: scaffold schwab_cli Python package with typer and uv"
```

---

## Task 2: Config Dataclass and `auto_login_enabled` Property

**Files:**
- Create: `src/schwab_cli/config.py`
- Create: `tests/test_config.py`

- [ ] **Step 2.1: Write failing tests for the `Config` dataclass**

Create `tests/test_config.py`:

```python
import pytest

from schwab_cli.config import Config


def test_config_defaults_username_password_to_none():
    cfg = Config(client_id="cid", client_secret="csec")
    assert cfg.username is None
    assert cfg.password is None
    assert cfg.version == 1


def test_config_is_frozen():
    cfg = Config(client_id="cid", client_secret="csec")
    with pytest.raises(Exception):
        cfg.client_id = "other"  # type: ignore[misc]


def test_auto_login_enabled_requires_both_fields():
    both = Config(client_id="cid", client_secret="csec", username="u", password="p")
    only_user = Config(client_id="cid", client_secret="csec", username="u")
    only_pass = Config(client_id="cid", client_secret="csec", password="p")
    neither = Config(client_id="cid", client_secret="csec")

    assert both.auto_login_enabled is True
    assert only_user.auto_login_enabled is False
    assert only_pass.auto_login_enabled is False
    assert neither.auto_login_enabled is False
```

- [ ] **Step 2.2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_config.py -v
```

Expected: `ImportError` / `ModuleNotFoundError` for `schwab_cli.config`.

- [ ] **Step 2.3: Implement `Config` in `src/schwab_cli/config.py`**

```python
from __future__ import annotations

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

- [ ] **Step 2.4: Run tests to verify they pass**

Run:

```bash
uv run pytest tests/test_config.py -v
```

Expected: 3 passed.

- [ ] **Step 2.5: Commit**

```bash
git add src/schwab_cli/config.py tests/test_config.py
git commit -m "feat(config): add frozen Config dataclass with auto_login_enabled"
```

---

## Task 3: `mask_secret` Helper

**Files:**
- Modify: `src/schwab_cli/config.py`
- Modify: `tests/test_config.py`

- [ ] **Step 3.1: Add failing tests for `mask_secret`**

Append to `tests/test_config.py`:

```python
from schwab_cli.config import mask_secret


def test_mask_secret_long_string_shows_last_four():
    assert mask_secret("abc123xyz") == "*****3xyz"


def test_mask_secret_exactly_four_chars_fully_masked():
    assert mask_secret("abcd") == "****"


def test_mask_secret_shorter_than_four_fully_masked():
    assert mask_secret("ab") == "**"
    assert mask_secret("") == ""
```

- [ ] **Step 3.2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_config.py::test_mask_secret_long_string_shows_last_four -v
```

Expected: `ImportError` for `mask_secret`.

- [ ] **Step 3.3: Implement `mask_secret` in `src/schwab_cli/config.py`**

Append to `src/schwab_cli/config.py`:

```python
def mask_secret(value: str) -> str:
    """Mask all but the last 4 characters.

    Strings of length <= 4 are fully masked so we never leak a partial short secret.
    """
    if len(value) <= 4:
        return "*" * len(value)
    return "*" * (len(value) - 4) + value[-4:]
```

- [ ] **Step 3.4: Run tests to verify they pass**

Run:

```bash
uv run pytest tests/test_config.py -v
```

Expected: 6 passed.

- [ ] **Step 3.5: Commit**

```bash
git add src/schwab_cli/config.py tests/test_config.py
git commit -m "feat(config): add mask_secret helper"
```

---

## Task 4: Config Path Resolution

**Files:**
- Modify: `src/schwab_cli/config.py`
- Modify: `tests/test_config.py`

- [ ] **Step 4.1: Add failing tests for `config_path()`**

Append to `tests/test_config.py`:

```python
from pathlib import Path

from schwab_cli.config import config_path


def test_config_path_defaults_to_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert config_path() == tmp_path / ".config" / "schwab_cli" / "config.json"


def test_config_path_honors_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config_path() == tmp_path / "xdg" / "schwab_cli" / "config.json"
```

- [ ] **Step 4.2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_config.py::test_config_path_defaults_to_home -v
```

Expected: `ImportError` for `config_path`.

- [ ] **Step 4.3: Implement `config_path()` in `src/schwab_cli/config.py`**

Add near the top of `src/schwab_cli/config.py` (after imports):

```python
import os
from pathlib import Path


def config_path() -> Path:
    """Return the absolute path to config.json.

    Honors XDG_CONFIG_HOME; falls back to ~/.config.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "schwab_cli" / "config.json"
```

- [ ] **Step 4.4: Run tests to verify they pass**

Run:

```bash
uv run pytest tests/test_config.py -v
```

Expected: 8 passed.

- [ ] **Step 4.5: Commit**

```bash
git add src/schwab_cli/config.py tests/test_config.py
git commit -m "feat(config): add config_path resolver with XDG support"
```

---

## Task 5: Config `load()`

**Files:**
- Modify: `src/schwab_cli/config.py`
- Modify: `tests/test_config.py`

- [ ] **Step 5.1: Add failing tests for `load()`**

Append to `tests/test_config.py`:

```python
import json

from schwab_cli.config import ConfigError, load


def _write_config(tmp_path, data):
    """Write a config file under tmp_path/.config/schwab_cli/config.json."""
    cfg_dir = tmp_path / ".config" / "schwab_cli"
    cfg_dir.mkdir(parents=True)
    cfg_file = cfg_dir / "config.json"
    cfg_file.write_text(json.dumps(data))
    return cfg_file


def test_load_returns_none_when_file_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert load() is None


def test_load_parses_full_config(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "username": "u",
        "password": "op://Personal/Schwab/password",
    })
    cfg = load()
    assert cfg == Config(
        client_id="cid",
        client_secret="csec",
        username="u",
        password="op://Personal/Schwab/password",
    )
    assert cfg.auto_login_enabled is True


def test_load_parses_minimal_config_without_auto_login(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
    })
    cfg = load()
    assert cfg.username is None
    assert cfg.password is None
    assert cfg.auto_login_enabled is False


def test_load_ignores_unknown_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    _write_config(tmp_path, {
        "version": 1,
        "client_id": "cid",
        "client_secret": "csec",
        "future_field": "ignore me",
    })
    cfg = load()
    assert cfg.client_id == "cid"


def test_load_raises_on_malformed_json(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    cfg_dir = tmp_path / ".config" / "schwab_cli"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text("{not valid json")
    with pytest.raises(ConfigError, match="malformed"):
        load()


def test_load_raises_on_unsupported_future_version(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    _write_config(tmp_path, {
        "version": 999,
        "client_id": "cid",
        "client_secret": "csec",
    })
    with pytest.raises(ConfigError, match="version"):
        load()


def test_load_raises_on_missing_required_field(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    _write_config(tmp_path, {"version": 1, "client_id": "cid"})  # no client_secret
    with pytest.raises(ConfigError, match="client_secret"):
        load()
```

- [ ] **Step 5.2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_config.py -v -k load
```

Expected: `ImportError` for `ConfigError`, `load`.

- [ ] **Step 5.3: Implement `load()` and `ConfigError` in `src/schwab_cli/config.py`**

Add to `src/schwab_cli/config.py`:

```python
import json

SUPPORTED_VERSION = 1
_REQUIRED_FIELDS = ("client_id", "client_secret")
_OPTIONAL_FIELDS = ("username", "password")


class ConfigError(Exception):
    """Raised when an existing config file cannot be used as-is."""


def load() -> Config | None:
    """Load config from disk.

    Returns None if the file does not exist. Raises ConfigError on malformed
    JSON, unsupported schema versions, or missing required fields.
    """
    path = config_path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ConfigError(f"malformed JSON in {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"expected object at top level of {path}")
    version = raw.get("version", 1)
    if version != SUPPORTED_VERSION:
        raise ConfigError(
            f"unsupported config version {version} in {path} "
            f"(this build supports version {SUPPORTED_VERSION})"
        )
    for field in _REQUIRED_FIELDS:
        if field not in raw:
            raise ConfigError(f"missing required field '{field}' in {path}")
    return Config(
        client_id=raw["client_id"],
        client_secret=raw["client_secret"],
        username=raw.get("username"),
        password=raw.get("password"),
        version=version,
    )
```

- [ ] **Step 5.4: Run tests to verify they pass**

Run:

```bash
uv run pytest tests/test_config.py -v
```

Expected: 15 passed.

- [ ] **Step 5.5: Commit**

```bash
git add src/schwab_cli/config.py tests/test_config.py
git commit -m "feat(config): add load() with schema validation"
```

---

## Task 6: Config `save()` with Atomic Write and Permissions

**Files:**
- Modify: `src/schwab_cli/config.py`
- Modify: `tests/test_config.py`

- [ ] **Step 6.1: Add failing tests for `save()`**

Append to `tests/test_config.py`:

```python
import os
import stat

from schwab_cli.config import save


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_save_writes_file_with_mode_0600(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    cfg = Config(client_id="cid", client_secret="csec")
    save(cfg)
    file = tmp_path / ".config" / "schwab_cli" / "config.json"
    assert file.exists()
    assert _mode(file) == 0o600


def test_save_creates_parent_dir_with_mode_0700(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save(Config(client_id="cid", client_secret="csec"))
    parent = tmp_path / ".config" / "schwab_cli"
    assert _mode(parent) == 0o700


def test_save_round_trips_through_load(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    original = Config(
        client_id="cid",
        client_secret="csec",
        username="u",
        password="op://Personal/Schwab/password",
    )
    save(original)
    assert load() == original


def test_save_omits_none_username_and_password(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save(Config(client_id="cid", client_secret="csec"))
    raw = json.loads((tmp_path / ".config" / "schwab_cli" / "config.json").read_text())
    assert "username" not in raw
    assert "password" not in raw


def test_save_disabling_auto_login_removes_prior_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save(Config(client_id="cid", client_secret="csec", username="u", password="p"))
    save(Config(client_id="cid", client_secret="csec"))  # disables auto-login
    raw = json.loads((tmp_path / ".config" / "schwab_cli" / "config.json").read_text())
    assert "username" not in raw
    assert "password" not in raw


def test_save_is_atomic_on_rename_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    # First write establishes a known-good file.
    original = Config(client_id="orig_id", client_secret="orig_secret")
    save(original)
    original_bytes = (tmp_path / ".config" / "schwab_cli" / "config.json").read_bytes()

    # Break os.replace to simulate a crash between temp-write and rename.
    def boom(*args, **kwargs):
        raise OSError("simulated failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        save(Config(client_id="new_id", client_secret="new_secret"))

    # Original file untouched.
    assert (tmp_path / ".config" / "schwab_cli" / "config.json").read_bytes() == original_bytes
    # No stray .tmp left behind.
    strays = list((tmp_path / ".config" / "schwab_cli").glob("*.tmp"))
    assert strays == []
```

- [ ] **Step 6.2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_config.py -v -k save
```

Expected: `ImportError` for `save`.

- [ ] **Step 6.3: Implement `save()` in `src/schwab_cli/config.py`**

Append to `src/schwab_cli/config.py`:

```python
def save(cfg: Config) -> None:
    """Persist a Config to disk atomically with strict permissions.

    Writes to a temp file in the same directory, chmods it 0600, then
    atomically renames it over the target. If the rename fails, cleans up
    the temp file and leaves any prior config untouched.
    """
    path = config_path()
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    # Only set 0700 when we just created it; don't re-tighten an existing dir.
    try:
        parent.chmod(0o700)
    except OSError:
        pass  # best effort; don't block save if chmod is not permitted
    payload: dict = {
        "version": cfg.version,
        "client_id": cfg.client_id,
        "client_secret": cfg.client_secret,
    }
    if cfg.username is not None:
        payload["username"] = cfg.username
    if cfg.password is not None:
        payload["password"] = cfg.password

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    try:
        tmp.chmod(0o600)
        os.replace(tmp, path)
    except OSError:
        # Clean up temp file so we don't leave stragglers.
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
```

Note: the previous `chmod(0o700)` on the parent dir is wrapped in a best-effort try so saving still succeeds when the user has deliberately relaxed dir perms. The test for `0o700` creates the dir fresh via `save()`, so it will still observe the expected mode.

- [ ] **Step 6.4: Run tests to verify they pass**

Run:

```bash
uv run pytest tests/test_config.py -v
```

Expected: 21 passed.

- [ ] **Step 6.5: Commit**

```bash
git add src/schwab_cli/config.py tests/test_config.py
git commit -m "feat(config): add atomic save() with 0600 file and 0700 dir"
```

---

## Task 7: `setup` Command — Fresh Install Flow

**Files:**
- Create: `src/schwab_cli/commands/setup.py`
- Modify: `src/schwab_cli/cli.py`
- Create: `tests/test_setup.py`

- [ ] **Step 7.1: Write failing test for fresh setup with auto-login disabled**

Create `tests/test_setup.py`:

```python
import json
from pathlib import Path

from typer.testing import CliRunner

from schwab_cli.cli import app
from schwab_cli.config import Config, load

runner = CliRunner()


def _run(inputs, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return runner.invoke(app, ["setup"], input=inputs)


def test_fresh_setup_without_auto_login(monkeypatch, tmp_path):
    # client_id, client_secret, decline auto-login
    result = _run("cid_value\ncsec_value\nn\n", monkeypatch, tmp_path)
    assert result.exit_code == 0, result.output
    cfg = load()
    assert cfg == Config(client_id="cid_value", client_secret="csec_value")
    assert not cfg.auto_login_enabled


def test_fresh_setup_with_auto_login(monkeypatch, tmp_path):
    # client_id, client_secret, accept auto-login, username, password
    result = _run(
        "cid_value\ncsec_value\ny\nuser@example.com\nop://Personal/Schwab/password\n",
        monkeypatch,
        tmp_path,
    )
    assert result.exit_code == 0, result.output
    cfg = load()
    assert cfg == Config(
        client_id="cid_value",
        client_secret="csec_value",
        username="user@example.com",
        password="op://Personal/Schwab/password",
    )
    assert cfg.auto_login_enabled
```

- [ ] **Step 7.2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_setup.py -v
```

Expected: `UsageError` / "No such command 'setup'".

- [ ] **Step 7.3: Implement the `setup` command**

Create `src/schwab_cli/commands/setup.py`:

```python
from __future__ import annotations

import typer

from schwab_cli.config import Config, ConfigError, config_path, load, mask_secret, save


def _prompt_required(label: str, existing: str | None, *, sensitive: bool) -> str:
    """Prompt until the user provides a non-empty value (or keeps existing)."""
    default_display = mask_secret(existing) if (existing and sensitive) else existing
    while True:
        entered = typer.prompt(label, default=default_display or "", show_default=bool(default_display))
        # If the user accepted the masked default, restore the real value.
        if sensitive and existing and entered == default_display:
            return existing
        if entered:
            return entered
        typer.secho(f"{label} is required.", fg=typer.colors.RED, err=True)


def _prompt_optional_credential(
    label: str,
    existing: str | None,
    *,
    sensitive: bool,
    hint: str | None = None,
) -> str:
    """Prompt for a value; empty is not allowed when auto-login is being set."""
    if hint:
        typer.echo(f"  ({hint})")
    default_display = mask_secret(existing) if (existing and sensitive) else existing
    while True:
        entered = typer.prompt(label, default=default_display or "", show_default=bool(default_display))
        if sensitive and existing and entered == default_display:
            return existing
        if entered:
            return entered
        typer.secho(f"{label} is required when auto-login is enabled.", fg=typer.colors.RED, err=True)


def run() -> None:
    """Interactive setup: capture credentials and persist to ~/.config/schwab_cli/config.json."""
    path = config_path()
    typer.echo("Schwab CLI Setup")
    typer.echo(f"Config: {path}")
    typer.echo("")

    try:
        existing = load()
    except ConfigError as e:
        typer.secho(f"Existing config is unusable: {e}", fg=typer.colors.YELLOW, err=True)
        overwrite = typer.confirm("Overwrite with new setup?", default=False)
        if not overwrite:
            raise typer.Exit(code=0)
        existing = None

    client_id = _prompt_required(
        "Client ID",
        existing.client_id if existing else None,
        sensitive=False,
    )
    client_secret = _prompt_required(
        "Client Secret",
        existing.client_secret if existing else None,
        sensitive=True,
    )

    auto_default = bool(existing and existing.auto_login_enabled)
    enable_auto = typer.confirm("Enable automatic login?", default=auto_default)

    username: str | None = None
    password: str | None = None
    if enable_auto:
        username = _prompt_optional_credential(
            "Username",
            existing.username if existing else None,
            sensitive=False,
        )
        password = _prompt_optional_credential(
            "Password",
            existing.password if existing else None,
            sensitive=True,
            hint="supports op:// 1Password references",
        )

    cfg = Config(
        client_id=client_id,
        client_secret=client_secret,
        username=username,
        password=password,
    )
    try:
        save(cfg)
    except OSError as e:
        typer.secho(f"Failed to write config: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from e

    typer.echo("")
    typer.secho(f"Saved to {path}.", fg=typer.colors.GREEN)
    typer.echo(f"Auto-login: {'enabled' if cfg.auto_login_enabled else 'disabled'}")
```

- [ ] **Step 7.4: Register the command in `src/schwab_cli/cli.py`**

Replace contents of `src/schwab_cli/cli.py` with:

```python
import typer

from schwab_cli.commands import setup as setup_cmd

app = typer.Typer(
    name="schwab_cli",
    help="Charles Schwab CLI.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command("setup", help="Configure Schwab CLI credentials.")
def setup() -> None:
    setup_cmd.run()
```

- [ ] **Step 7.5: Run tests to verify they pass**

Run:

```bash
uv run pytest tests/test_setup.py -v
```

Expected: 2 passed.

- [ ] **Step 7.6: Manual smoke test**

Run:

```bash
uv run schwab_cli setup
```

Type `test_id`, `test_secret`, `n`. Expected: "Saved to …/config.json." and file contains the credentials.

Clean up after the smoke test:

```bash
rm ~/.config/schwab_cli/config.json
```

- [ ] **Step 7.7: Commit**

```bash
git add src/schwab_cli/cli.py src/schwab_cli/commands/setup.py tests/test_setup.py
git commit -m "feat(setup): implement interactive setup command"
```

---

## Task 8: `setup` — Re-run Keeps Existing Values on Enter

**Files:**
- Modify: `tests/test_setup.py`

- [ ] **Step 8.1: Write failing test for re-run preserving values**

Append to `tests/test_setup.py`:

```python
from schwab_cli.config import save as save_cfg


def _seed(tmp_path, cfg):
    monkeypatch_home = tmp_path
    save_cfg(cfg)  # assumes caller already set HOME via monkeypatch
    return monkeypatch_home


def test_rerun_accepting_defaults_preserves_all_values(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save_cfg(
        Config(
            client_id="existing_id",
            client_secret="existing_secret_xyz",
            username="existing_user",
            password="op://Personal/Schwab/password",
        )
    )
    # Press Enter through every prompt: client_id, client_secret, auto-login confirm, username, password
    result = runner.invoke(app, ["setup"], input="\n\n\n\n\n")
    assert result.exit_code == 0, result.output
    cfg = load()
    assert cfg == Config(
        client_id="existing_id",
        client_secret="existing_secret_xyz",
        username="existing_user",
        password="op://Personal/Schwab/password",
    )


def test_rerun_disabling_auto_login_removes_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save_cfg(
        Config(
            client_id="existing_id",
            client_secret="existing_secret",
            username="existing_user",
            password="existing_pass",
        )
    )
    # Enter for client_id, Enter for client_secret, 'n' to disable auto-login.
    result = runner.invoke(app, ["setup"], input="\n\nn\n")
    assert result.exit_code == 0, result.output
    cfg = load()
    assert cfg.username is None
    assert cfg.password is None
    assert cfg.auto_login_enabled is False
```

- [ ] **Step 8.2: Run tests to verify they pass**

Run:

```bash
uv run pytest tests/test_setup.py -v
```

Expected: 4 passed. (Task 7's implementation already supports this flow; this task locks the behavior with tests.)

If any test fails, inspect `result.output` for the actual prompt sequence and adjust the implementation in `commands/setup.py` so the "accept default" path restores the underlying value for sensitive fields.

- [ ] **Step 8.3: Commit**

```bash
git add tests/test_setup.py
git commit -m "test(setup): lock re-run default-acceptance and auto-login disable"
```

---

## Task 9: `setup` — Required-Field Re-Prompt

**Files:**
- Modify: `tests/test_setup.py`

- [ ] **Step 9.1: Write failing test for re-prompt behavior**

Append to `tests/test_setup.py`:

```python
def test_fresh_setup_reprompts_on_empty_client_id(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    # First: empty client_id (should re-prompt), then valid one, client_secret, decline auto.
    result = runner.invoke(app, ["setup"], input="\ncid_value\ncsec_value\nn\n")
    assert result.exit_code == 0, result.output
    assert "Client ID is required" in result.output
    cfg = load()
    assert cfg.client_id == "cid_value"
```

- [ ] **Step 9.2: Run tests to verify they pass**

Run:

```bash
uv run pytest tests/test_setup.py::test_fresh_setup_reprompts_on_empty_client_id -v
```

Expected: PASS. (Task 7's `_prompt_required` already re-prompts.)

If this fails because typer's `prompt()` rejects empty input with a different default, adjust `_prompt_required` in `commands/setup.py` so it accepts empty input, shows the error, and loops.

- [ ] **Step 9.3: Commit**

```bash
git add tests/test_setup.py
git commit -m "test(setup): lock re-prompt behavior for empty required fields"
```

---

## Task 10: `setup` — Malformed and Future-Version Configs

**Files:**
- Modify: `tests/test_setup.py`

- [ ] **Step 10.1: Write failing tests for error handling of existing configs**

Append to `tests/test_setup.py`:

```python
def test_malformed_existing_config_decline_overwrite_leaves_file(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    cfg_dir = tmp_path / ".config" / "schwab_cli"
    cfg_dir.mkdir(parents=True)
    bad = cfg_dir / "config.json"
    bad.write_text("{not valid")
    original_bytes = bad.read_bytes()

    result = runner.invoke(app, ["setup"], input="n\n")  # decline overwrite
    assert result.exit_code == 0, result.output
    assert bad.read_bytes() == original_bytes


def test_malformed_existing_config_accept_overwrite_writes_new(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    cfg_dir = tmp_path / ".config" / "schwab_cli"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text("{not valid")

    # y = overwrite, then client_id, client_secret, decline auto-login
    result = runner.invoke(app, ["setup"], input="y\ncid\ncsec\nn\n")
    assert result.exit_code == 0, result.output
    cfg = load()
    assert cfg == Config(client_id="cid", client_secret="csec")
```

- [ ] **Step 10.2: Run tests to verify they pass**

Run:

```bash
uv run pytest tests/test_setup.py -v
```

Expected: 7 passed. (Task 7's implementation already handles `ConfigError` + confirm overwrite.)

- [ ] **Step 10.3: Commit**

```bash
git add tests/test_setup.py
git commit -m "test(setup): lock malformed-config decline and overwrite flows"
```

---

## Task 11: README

**Files:**
- Create: `README.md`

- [ ] **Step 11.1: Create `README.md`**

```markdown
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
```

- [ ] **Step 11.2: Commit**

```bash
git add README.md
git commit -m "docs: add README with install and setup instructions"
```

---

## Task 12: Final Verification

- [ ] **Step 12.1: Run full test suite with coverage**

```bash
uv run pytest
```

Expected: all tests pass. Coverage report should show ≥80% for both `config.py` and `commands/setup.py`.

If coverage on either module is below 80%, add targeted tests for the uncovered lines reported by `--cov-report=term-missing` before proceeding.

- [ ] **Step 12.2: Verify installed entry point works end-to-end**

```bash
uv run schwab_cli --help
uv run schwab_cli setup --help
uv run python -m schwab_cli setup --help
```

Expected: all three print help text and exit 0.

- [ ] **Step 12.3: Final manual smoke test**

Run `uv run schwab_cli setup`, enter test values, confirm:

```bash
ls -l ~/.config/schwab_cli/config.json
# -rw------- ...  (mode 0600)
cat ~/.config/schwab_cli/config.json
# valid JSON with version, client_id, client_secret [, username, password]
```

Re-run `schwab_cli setup`, press Enter through all prompts, confirm nothing changed:

```bash
schwab_cli setup   # press Enter for everything
diff <(cat ~/.config/schwab_cli/config.json) <(cat ~/.config/schwab_cli/config.json)
# (no diff — no changes)
```

Remove test config:

```bash
rm ~/.config/schwab_cli/config.json
```

- [ ] **Step 12.4: Final commit if anything changed**

```bash
git status
# If anything was added during verification:
git add -A
git commit -m "chore: polish after verification"
```
