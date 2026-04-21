# Schwab CLI `auth` Command Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `schwab_cli auth [--force]` command — exchange a stored refresh token for a fresh access token (fast path); on failure or `--force`, drive Schwab's OAuth2 flow in an automated Playwright browser, persist tokens to `~/.config/schwab_cli/session.json`.

**Architecture:** Hand-rolled OAuth via `httpx` (isolated in `oauth.py`, no SDK dependency). Playwright orchestration kept in `browser/` so other commands don't pull it in. 1Password secret resolution isolated in `secrets.py`. Selectors and error markers centralized in `browser/selectors.py` so Schwab UI changes mean editing one file.

**Tech Stack:** Python 3.11+, `typer`, `httpx`, `playwright`, `pytest`, `respx` (httpx mocks), frozen `@dataclass`.

**Spec:** [`docs/superpowers/specs/2026-04-21-schwab-cli-auth-design.md`](../specs/2026-04-21-schwab-cli-auth-design.md)

---

## File Structure

| Path | Responsibility |
|---|---|
| `pyproject.toml` | Add `playwright`, `httpx`, dev `respx` |
| `src/schwab_cli/session.py` | `Session` dataclass + load/save/path helpers |
| `src/schwab_cli/oauth.py` | `TokenResponse`, `build_auth_url`, `exchange_code`, `refresh`, `OAuthError` |
| `src/schwab_cli/secrets.py` | `resolve_secret`, `SecretError` |
| `src/schwab_cli/browser/__init__.py` | Empty package marker |
| `src/schwab_cli/browser/selectors.py` | All page selectors + error markers, plus `_is_debug_truthy` and `_summarize_error` helpers |
| `src/schwab_cli/browser/flow.py` | `run_full_auth`, `wait_any`, `AuthError` |
| `src/schwab_cli/commands/auth.py` | `auth` subcommand orchestration |
| `src/schwab_cli/cli.py` | Register `auth` |
| `tests/test_session.py` | Session module unit tests |
| `tests/test_oauth.py` | OAuth module unit tests |
| `tests/test_secrets.py` | Secrets resolution unit tests |
| `tests/test_browser_flow.py` | `run_full_auth` + `wait_any` tests via `FakePage` |
| `tests/test_auth_command.py` | `auth` CLI tests with oauth + browser.flow mocked |
| `README.md` | Add playwright install + auth usage |

---

## Task 1: Add Dependencies + Browser Package Skeleton

**Files:**
- Modify: `pyproject.toml`
- Create: `src/schwab_cli/browser/__init__.py`

- [ ] **Step 1.1: Add runtime + dev dependencies to `pyproject.toml`**

In the `[project]` table, replace `dependencies` with:

```toml
dependencies = [
    "typer>=0.12.0",
    "httpx>=0.27",
    "playwright>=1.45",
]
```

In `[project.optional-dependencies]`, replace `dev` with:

```toml
dev = [
    "pytest>=8.0",
    "pytest-cov>=5.0",
    "respx>=0.21",
]
```

- [ ] **Step 1.2: Sync deps**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv sync --extra dev
```

Expected: installs httpx, playwright, respx into `.venv` without errors. (Playwright Chromium binaries are NOT installed by `uv sync`; that's a separate `playwright install chromium` step documented in the README later.)

- [ ] **Step 1.3: Create `src/schwab_cli/browser/__init__.py`**

Empty file.

- [ ] **Step 1.4: Verify existing tests still pass**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest -q
```

Expected: 30 passed.

- [ ] **Step 1.5: Commit**

```bash
cd /Users/weig/Projects/finance/schwab_cli && git add pyproject.toml uv.lock src/schwab_cli/browser/__init__.py && git commit -m "chore: add httpx, playwright, respx deps; scaffold browser package"
```

---

## Task 2: `Session` Module

**Files:**
- Create: `src/schwab_cli/session.py`
- Create: `tests/test_session.py`

TDD: tests first.

- [ ] **Step 2.1: Create `tests/test_session.py` with failing tests**

```python
import json
import os
import stat

import pytest

from schwab_cli.session import Session, SessionError, load, save, session_path


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def _write(tmp_path, data):
    d = tmp_path / ".config" / "schwab_cli"
    d.mkdir(parents=True)
    f = d / "session.json"
    f.write_text(json.dumps(data))
    return f


def test_session_path_defaults_to_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert session_path() == tmp_path / ".config" / "schwab_cli" / "session.json"


def test_session_path_honors_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert session_path() == tmp_path / "xdg" / "schwab_cli" / "session.json"


def test_session_is_frozen():
    s = Session(access_token="a", refresh_token="r", expires_at=1, refresh_token_expires_at=2)
    with pytest.raises(Exception):
        s.access_token = "x"  # type: ignore[misc]


def test_load_returns_none_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert load() is None


def test_load_parses_full_session(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    _write(tmp_path, {
        "version": 1,
        "access_token": "a",
        "refresh_token": "r",
        "expires_at": 100,
        "refresh_token_expires_at": 200,
    })
    assert load() == Session(
        access_token="a", refresh_token="r", expires_at=100, refresh_token_expires_at=200
    )


def test_load_raises_on_malformed_json(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    d = tmp_path / ".config" / "schwab_cli"
    d.mkdir(parents=True)
    (d / "session.json").write_text("{not valid")
    with pytest.raises(SessionError, match="malformed"):
        load()


def test_load_raises_on_missing_required_field(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    _write(tmp_path, {
        "version": 1,
        "access_token": "a",
        "refresh_token": "r",
        "expires_at": 100,
    })  # missing refresh_token_expires_at
    with pytest.raises(SessionError, match="refresh_token_expires_at"):
        load()


def test_load_raises_on_unsupported_version(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    _write(tmp_path, {
        "version": 999,
        "access_token": "a",
        "refresh_token": "r",
        "expires_at": 1,
        "refresh_token_expires_at": 2,
    })
    with pytest.raises(SessionError, match="version"):
        load()


def test_save_writes_file_with_mode_0600(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save(Session(access_token="a", refresh_token="r", expires_at=1, refresh_token_expires_at=2))
    f = tmp_path / ".config" / "schwab_cli" / "session.json"
    assert f.exists()
    assert _mode(f) == 0o600


def test_save_creates_parent_dir_with_mode_0700(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save(Session(access_token="a", refresh_token="r", expires_at=1, refresh_token_expires_at=2))
    parent = tmp_path / ".config" / "schwab_cli"
    assert _mode(parent) == 0o700


def test_save_round_trips_through_load(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    original = Session(access_token="a", refresh_token="r", expires_at=100, refresh_token_expires_at=200)
    save(original)
    assert load() == original


def test_save_is_atomic_on_rename_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save(Session(access_token="orig_a", refresh_token="orig_r", expires_at=1, refresh_token_expires_at=2))
    f = tmp_path / ".config" / "schwab_cli" / "session.json"
    original_bytes = f.read_bytes()

    def boom(*a, **kw):
        raise OSError("boom")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        save(Session(access_token="new_a", refresh_token="new_r", expires_at=10, refresh_token_expires_at=20))
    assert f.read_bytes() == original_bytes
    assert list((tmp_path / ".config" / "schwab_cli").glob("*.tmp")) == []


def test_from_token_response_computes_expiries():
    from schwab_cli.oauth import TokenResponse  # placeholder; will exist after Task 3
    tr = TokenResponse(access_token="a", refresh_token="r", expires_in=1800)
    s = Session.from_token_response(tr, now=1_000_000)
    assert s.access_token == "a"
    assert s.refresh_token == "r"
    assert s.expires_at == 1_000_000 + 1800
    assert s.refresh_token_expires_at == 1_000_000 + 7 * 24 * 3600
```

NOTE: `test_from_token_response_computes_expiries` imports from `oauth` which doesn't exist yet — mark this test with `@pytest.mark.skip(reason="enabled after Task 3")` for now.

Update that one test:

```python
@pytest.mark.skip(reason="enabled after oauth.TokenResponse exists in Task 3")
def test_from_token_response_computes_expiries():
    ...
```

- [ ] **Step 2.2: Run tests to verify they fail**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest tests/test_session.py -v
```

Expected: ImportError for `schwab_cli.session`.

- [ ] **Step 2.3: Implement `src/schwab_cli/session.py`**

```python
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from schwab_cli.oauth import TokenResponse


SUPPORTED_VERSION = 1
_REQUIRED_FIELDS = (
    "access_token",
    "refresh_token",
    "expires_at",
    "refresh_token_expires_at",
)
REFRESH_TOKEN_LIFETIME_SECONDS = 7 * 24 * 3600


class SessionError(Exception):
    """Raised when an existing session file cannot be used as-is."""


def session_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "schwab_cli" / "session.json"


@dataclass(frozen=True)
class Session:
    access_token: str
    refresh_token: str
    expires_at: int
    refresh_token_expires_at: int
    version: int = 1

    @classmethod
    def from_token_response(cls, tr: "TokenResponse", now: int) -> "Session":
        return cls(
            access_token=tr.access_token,
            refresh_token=tr.refresh_token,
            expires_at=now + tr.expires_in,
            refresh_token_expires_at=now + REFRESH_TOKEN_LIFETIME_SECONDS,
        )


def load() -> Session | None:
    path = session_path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise SessionError(f"malformed JSON in {path}: {e}") from e
    if not isinstance(raw, dict):
        raise SessionError(f"expected object at top level of {path}")
    version = raw.get("version", 1)
    if version != SUPPORTED_VERSION:
        raise SessionError(
            f"unsupported session version {version} in {path} "
            f"(this build supports version {SUPPORTED_VERSION})"
        )
    for field in _REQUIRED_FIELDS:
        if field not in raw:
            raise SessionError(f"missing required field '{field}' in {path}")
    return Session(
        access_token=raw["access_token"],
        refresh_token=raw["refresh_token"],
        expires_at=int(raw["expires_at"]),
        refresh_token_expires_at=int(raw["refresh_token_expires_at"]),
        version=version,
    )


def save(s: Session) -> None:
    path = session_path()
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    try:
        parent.chmod(0o700)
    except OSError:
        pass
    payload = {
        "version": s.version,
        "access_token": s.access_token,
        "refresh_token": s.refresh_token,
        "expires_at": s.expires_at,
        "refresh_token_expires_at": s.refresh_token_expires_at,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    try:
        tmp.chmod(0o600)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
```

- [ ] **Step 2.4: Run tests to verify they pass**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest tests/test_session.py -v
```

Expected: 12 passed (one skipped: `test_from_token_response_computes_expiries`).

- [ ] **Step 2.5: Commit**

```bash
cd /Users/weig/Projects/finance/schwab_cli && git add src/schwab_cli/session.py tests/test_session.py && git commit -m "feat(session): add Session dataclass with atomic load/save"
```

---

## Task 3: `oauth` Module — `TokenResponse` + `build_auth_url`

**Files:**
- Create: `src/schwab_cli/oauth.py`
- Create: `tests/test_oauth.py`

- [ ] **Step 3.1: Create `tests/test_oauth.py` with failing tests**

```python
import pytest

from schwab_cli.config import Config
from schwab_cli.oauth import OAuthError, TokenResponse, build_auth_url


def _cfg(**kwargs):
    base = dict(
        client_id="cid",
        client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
    )
    base.update(kwargs)
    return Config(**base)


def test_build_auth_url_includes_required_params():
    url = build_auth_url(_cfg())
    assert url.startswith("https://api.schwabapi.com/v1/oauth/authorize?")
    assert "response_type=code" in url
    assert "client_id=cid" in url
    # redirect_uri must be URL-encoded
    assert "redirect_uri=https%3A%2F%2F127.0.0.1%3A8443" in url


def test_token_response_parse_accepts_full_payload():
    tr = TokenResponse.parse({
        "access_token": "a",
        "refresh_token": "r",
        "expires_in": 1800,
        "scope": "ignored",
    })
    assert tr == TokenResponse(access_token="a", refresh_token="r", expires_in=1800)


def test_token_response_parse_coerces_expires_in_to_int():
    tr = TokenResponse.parse({
        "access_token": "a",
        "refresh_token": "r",
        "expires_in": "1800",
    })
    assert tr.expires_in == 1800


@pytest.mark.parametrize("missing", ["access_token", "refresh_token", "expires_in"])
def test_token_response_parse_raises_on_missing_field(missing):
    full = {"access_token": "a", "refresh_token": "r", "expires_in": 1800}
    full.pop(missing)
    with pytest.raises(OAuthError, match=missing):
        TokenResponse.parse(full)


def test_token_response_is_frozen():
    tr = TokenResponse(access_token="a", refresh_token="r", expires_in=1)
    with pytest.raises(Exception):
        tr.access_token = "x"  # type: ignore[misc]
```

- [ ] **Step 3.2: Verify failing**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest tests/test_oauth.py -v
```

Expected: ImportError for `schwab_cli.oauth`.

- [ ] **Step 3.3: Implement minimal `src/schwab_cli/oauth.py`** (only `TokenResponse` + `build_auth_url` + `OAuthError`; the http functions come in Task 4)

```python
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

from schwab_cli.config import Config

AUTH_URL = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"


class OAuthError(Exception):
    """Raised on OAuth protocol failures (bad responses, missing fields)."""


@dataclass(frozen=True)
class TokenResponse:
    access_token: str
    refresh_token: str
    expires_in: int

    @classmethod
    def parse(cls, data: dict) -> "TokenResponse":
        for field in ("access_token", "refresh_token", "expires_in"):
            if field not in data:
                raise OAuthError(f"token response missing '{field}'")
        return cls(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_in=int(data["expires_in"]),
        )


def build_auth_url(cfg: Config) -> str:
    return f"{AUTH_URL}?" + urlencode({
        "response_type": "code",
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
    })
```

- [ ] **Step 3.4: Run tests**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest tests/test_oauth.py -v
```

Expected: 7 passed.

- [ ] **Step 3.5: Re-enable the previously-skipped Session test**

Edit `tests/test_session.py` and remove the `@pytest.mark.skip` decorator on `test_from_token_response_computes_expiries`. Run:

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest tests/test_session.py -v
```

Expected: 13 passed (no skips).

- [ ] **Step 3.6: Commit**

```bash
cd /Users/weig/Projects/finance/schwab_cli && git add src/schwab_cli/oauth.py tests/test_oauth.py tests/test_session.py && git commit -m "feat(oauth): add TokenResponse and build_auth_url"
```

---

## Task 4: `oauth.exchange_code` and `oauth.refresh`

**Files:**
- Modify: `src/schwab_cli/oauth.py` (add `exchange_code`, `refresh`)
- Modify: `tests/test_oauth.py` (add tests using `respx`)

- [ ] **Step 4.1: Append failing tests to `tests/test_oauth.py`**

```python
import httpx
import respx

from schwab_cli.oauth import TOKEN_URL, exchange_code, refresh


@respx.mock
def test_exchange_code_posts_basic_auth_and_form_body():
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={
            "access_token": "atok", "refresh_token": "rtok", "expires_in": 1800
        })
    )
    tr = exchange_code(_cfg(), code="ABC123")
    assert tr == TokenResponse(access_token="atok", refresh_token="rtok", expires_in=1800)
    req = route.calls.last.request
    # Basic auth header: base64("cid:csec")
    assert req.headers["Authorization"].startswith("Basic ")
    body = dict(httpx.QueryParams(req.content.decode()))
    assert body == {
        "grant_type": "authorization_code",
        "code": "ABC123",
        "redirect_uri": "https://127.0.0.1:8443",
    }


@respx.mock
def test_refresh_posts_refresh_token_grant():
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={
            "access_token": "new_a", "refresh_token": "new_r", "expires_in": 1800
        })
    )
    tr = refresh(_cfg(), refresh_token="old_r")
    assert tr.access_token == "new_a"
    body = dict(httpx.QueryParams(route.calls.last.request.content.decode()))
    assert body == {"grant_type": "refresh_token", "refresh_token": "old_r"}


@respx.mock
def test_exchange_code_raises_on_4xx():
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )
    with pytest.raises(httpx.HTTPStatusError):
        exchange_code(_cfg(), code="bad")


@respx.mock
def test_refresh_raises_on_4xx():
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(401, json={"error": "invalid_client"})
    )
    with pytest.raises(httpx.HTTPStatusError):
        refresh(_cfg(), refresh_token="r")


@respx.mock
def test_exchange_code_raises_oauth_error_on_missing_field_in_200_response():
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "a", "refresh_token": "r"})
    )
    with pytest.raises(OAuthError, match="expires_in"):
        exchange_code(_cfg(), code="ABC")


@respx.mock
def test_refresh_raises_on_network_error():
    respx.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("nope"))
    with pytest.raises(httpx.RequestError):
        refresh(_cfg(), refresh_token="r")
```

- [ ] **Step 4.2: Verify failing**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest tests/test_oauth.py -v
```

Expected: ImportError for `exchange_code`, `refresh`.

- [ ] **Step 4.3: Append `exchange_code` and `refresh` to `src/schwab_cli/oauth.py`**

Add `import httpx` to the imports. Then append:

```python
def exchange_code(cfg: Config, code: str) -> TokenResponse:
    resp = httpx.post(
        TOKEN_URL,
        auth=(cfg.client_id, cfg.client_secret),
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": cfg.redirect_uri,
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    return TokenResponse.parse(resp.json())


def refresh(cfg: Config, refresh_token: str) -> TokenResponse:
    resp = httpx.post(
        TOKEN_URL,
        auth=(cfg.client_id, cfg.client_secret),
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    return TokenResponse.parse(resp.json())
```

- [ ] **Step 4.4: Run tests**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest tests/test_oauth.py -v
```

Expected: 13 passed (7 from Task 3 + 6 new).

- [ ] **Step 4.5: Commit**

```bash
cd /Users/weig/Projects/finance/schwab_cli && git add src/schwab_cli/oauth.py tests/test_oauth.py && git commit -m "feat(oauth): add exchange_code and refresh"
```

---

## Task 5: `secrets` Module

**Files:**
- Create: `src/schwab_cli/secrets.py`
- Create: `tests/test_secrets.py`

- [ ] **Step 5.1: Create `tests/test_secrets.py`**

```python
import subprocess
from unittest.mock import patch

import pytest

from schwab_cli.secrets import SecretError, resolve_secret


def test_literal_value_returned_verbatim():
    assert resolve_secret("plain-text-password") == "plain-text-password"


def test_empty_value_returned_verbatim():
    # Empty is a literal too — caller decides what to do with empty.
    assert resolve_secret("") == ""


def test_op_reference_calls_op_read():
    fake = subprocess.CompletedProcess(
        args=["op", "read", "op://Personal/Schwab/password"],
        returncode=0,
        stdout="my_secret_password\n",
        stderr="",
    )
    with patch("schwab_cli.secrets.subprocess.run", return_value=fake) as run:
        result = resolve_secret("op://Personal/Schwab/password")
    assert result == "my_secret_password"
    args, kwargs = run.call_args
    assert args[0] == ["op", "read", "op://Personal/Schwab/password"]
    assert kwargs.get("capture_output") is True
    assert kwargs.get("text") is True
    assert kwargs.get("check") is True


def test_op_missing_raises_secret_error():
    with patch("schwab_cli.secrets.subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(SecretError, match="not found on PATH"):
            resolve_secret("op://X/Y/Z")


def test_op_failure_surfaces_stderr_in_secret_error():
    err = subprocess.CalledProcessError(
        returncode=1,
        cmd=["op", "read", "op://X/Y/Z"],
        output="",
        stderr="[ERROR] item X not found\n",
    )
    with patch("schwab_cli.secrets.subprocess.run", side_effect=err):
        with pytest.raises(SecretError, match="item X not found"):
            resolve_secret("op://X/Y/Z")


def test_op_failure_with_no_stderr_uses_generic_message():
    err = subprocess.CalledProcessError(
        returncode=1, cmd=["op", "read", "op://X/Y/Z"], output="", stderr=""
    )
    with patch("schwab_cli.secrets.subprocess.run", side_effect=err):
        with pytest.raises(SecretError, match="unknown error"):
            resolve_secret("op://X/Y/Z")
```

- [ ] **Step 5.2: Verify failing**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest tests/test_secrets.py -v
```

Expected: ImportError.

- [ ] **Step 5.3: Implement `src/schwab_cli/secrets.py`**

```python
from __future__ import annotations

import subprocess


class SecretError(Exception):
    """Raised when a secret reference cannot be resolved."""


def resolve_secret(value: str) -> str:
    """Resolve a secret reference.

    `op://...` values are passed to the 1Password CLI (`op read <ref>`); anything
    else is returned verbatim. The resolved value is never logged.
    """
    if not value.startswith("op://"):
        return value
    try:
        result = subprocess.run(
            ["op", "read", value],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as e:
        raise SecretError("1Password CLI (`op`) not found on PATH.") from e
    except subprocess.CalledProcessError as e:
        msg = (e.stderr or "").strip() or "unknown error"
        raise SecretError(f"op read failed: {msg}") from e
    return result.stdout.rstrip("\n")
```

- [ ] **Step 5.4: Run tests**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest tests/test_secrets.py -v
```

Expected: 6 passed.

- [ ] **Step 5.5: Commit**

```bash
cd /Users/weig/Projects/finance/schwab_cli && git add src/schwab_cli/secrets.py tests/test_secrets.py && git commit -m "feat(secrets): add resolve_secret with op:// support"
```

---

## Task 6: `browser/selectors.py` — Selectors + Helpers

**Files:**
- Create: `src/schwab_cli/browser/selectors.py`
- Create: `tests/test_browser_selectors.py`

- [ ] **Step 6.1: Create `tests/test_browser_selectors.py` with tests for the helpers (selectors themselves are constants, no test value)**

```python
import pytest

from schwab_cli.browser.selectors import _is_debug_truthy, _summarize_error


@pytest.mark.parametrize("value", ["true", "True", "TRUE", "yes", "Yes", "1"])
def test_is_debug_truthy_accepts_known_truthy(value):
    assert _is_debug_truthy(value) is True


@pytest.mark.parametrize("value", [None, "", "0", "no", "false", "off", "  true  "])
def test_is_debug_truthy_rejects_others(value):
    # Whitespace-padded "true" returns False — env vars should be exact.
    assert _is_debug_truthy(value) is False


def test_summarize_error_format_for_oauth_error():
    from schwab_cli.oauth import OAuthError
    assert _summarize_error(OAuthError("missing field")) == "missing field"


def test_summarize_error_format_for_request_error():
    import httpx
    err = httpx.ConnectError("dns failed")
    assert _summarize_error(err) == "network: ConnectError"


def test_summarize_error_format_for_status_error():
    import httpx
    req = httpx.Request("POST", "https://example/")
    resp = httpx.Response(401, request=req, json={"error": "invalid_grant"})
    err = httpx.HTTPStatusError("401", request=req, response=resp)
    summary = _summarize_error(err)
    assert summary.startswith("401 ")
    assert "invalid_grant" in summary
```

- [ ] **Step 6.2: Verify failing**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest tests/test_browser_selectors.py -v
```

Expected: ImportError.

- [ ] **Step 6.3: Create `src/schwab_cli/browser/selectors.py`**

```python
from __future__ import annotations

import httpx

# ---------------------------------------------------------------------------
# Selectors. Best-guess starting set; tighten after first manual run.
# ---------------------------------------------------------------------------

# Login page
LOGIN_USERNAME_SELECTOR = "input#loginIdInput"
LOGIN_PASSWORD_SELECTOR = "input#passwordInput"
LOGIN_SUBMIT_SELECTOR = "button#btnLogin"

# Consent / agree page
CONSENT_PAGE_SELECTOR = "text=Terms of Use"
ACCEPT_SELECTOR = 'button:has-text("Accept")'

# Account selection
ACCOUNT_SELECTION_SELECTOR = "text=Select accounts"
ACCOUNT_CHECKBOX_SELECTOR = 'input[type="checkbox"][name^="account"]'
CONTINUE_SELECTOR = 'button:has-text("Continue")'

# Confirmation page
CONFIRM_PAGE_SELECTOR = "text=You will now be redirected"
DONE_SELECTOR = 'button:has-text("Done")'

# ---------------------------------------------------------------------------
# Error markers (page-content substrings).
# ---------------------------------------------------------------------------
INVALID_CLIENT_MARKERS = ('"error": "invalid_client"',)
INVALID_CREDENTIALS_TEXT = "Invalid login ID or password."
REDIRECT_URI_MISMATCH_TEXT = "We are unable to complete your request."

# ---------------------------------------------------------------------------
# Helpers (kept here so they're easy to find when tweaking selectors).
# ---------------------------------------------------------------------------
_TRUTHY_DEBUG_VALUES = frozenset({"true", "yes", "1"})


def _is_debug_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.lower() in _TRUTHY_DEBUG_VALUES


def _summarize_error(e: BaseException) -> str:
    """One-line human-readable reason from common exception types."""
    if isinstance(e, httpx.HTTPStatusError):
        body = e.response.text or ""
        first_line = body.splitlines()[0] if body else ""
        return f"{e.response.status_code} {first_line}".strip()
    if isinstance(e, httpx.RequestError):
        return f"network: {type(e).__name__}"
    return str(e)
```

- [ ] **Step 6.4: Run tests**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest tests/test_browser_selectors.py -v
```

Expected: 11 passed (6 truthy + 6 falsy + 3 summarize tests via parametrize counted individually).

- [ ] **Step 6.5: Commit**

```bash
cd /Users/weig/Projects/finance/schwab_cli && git add src/schwab_cli/browser/selectors.py tests/test_browser_selectors.py && git commit -m "feat(browser): add selectors and helper utilities"
```

---

## Task 7: `browser/flow.py` — `wait_any` and `AuthError`

**Files:**
- Create: `src/schwab_cli/browser/flow.py`
- Create: `tests/test_browser_flow.py`

This task introduces the `FakePage` test fixture used here and in Task 8.

- [ ] **Step 7.1: Create `tests/test_browser_flow.py` with `FakePage` + `wait_any` tests**

```python
"""Tests for browser/flow.py.

`FakePage` is a minimal Playwright Page stand-in that scripts page.content()
and selector responses so we can drive the flow logic without a real browser.
"""

from __future__ import annotations

import pytest

from schwab_cli.browser.flow import AuthError, wait_any


class FakeLocator:
    def __init__(self, present: bool):
        self._present = present

    def wait_for(self, *, timeout: int) -> None:
        if not self._present:
            raise TimeoutError("not present")


class FakePage:
    """Scriptable Page double for flow tests.

    Configure:
        - content_sequence: list of strings; .content() returns each in order
          (last is repeated if calls exceed the list).
        - selector_present: dict[str, bool] — which selectors return present
          when wait_for_selector is called.
    """

    def __init__(self, *, content_sequence: list[str] | None = None,
                 selectors_present: dict[str, bool] | None = None):
        self._content = content_sequence or [""]
        self._content_idx = 0
        self._selectors = selectors_present or {}
        self.url = ""

    def content(self) -> str:
        c = self._content[min(self._content_idx, len(self._content) - 1)]
        self._content_idx += 1
        return c

    def wait_for_selector(self, selector: str, *, timeout: int) -> FakeLocator:
        if self._selectors.get(selector, False):
            return FakeLocator(True)
        raise TimeoutError(f"selector {selector!r} not found")


def test_wait_any_returns_when_expected_selector_appears():
    page = FakePage(selectors_present={"#ready": True})
    # Should not raise; returns None on success.
    wait_any(page, expected="#ready", known_errors={}, timeout_ms=200)


def test_wait_any_raises_friendly_error_on_known_marker():
    page = FakePage(content_sequence=["...Invalid login ID or password...."],
                    selectors_present={"#ready": False})
    with pytest.raises(AuthError, match="incorrect username/password"):
        wait_any(
            page,
            expected="#ready",
            known_errors={"Invalid login ID or password.": "Login failed — incorrect username/password."},
            timeout_ms=200,
        )


def test_wait_any_times_out_with_ui_change_message():
    page = FakePage(content_sequence=[""], selectors_present={"#ready": False})
    with pytest.raises(AuthError, match="Schwab may have changed"):
        wait_any(page, expected="#ready", known_errors={}, timeout_ms=200)


def test_wait_any_known_marker_takes_precedence_over_selector():
    page = FakePage(
        content_sequence=["Invalid login ID or password."],
        selectors_present={"#ready": True},  # selector is also present
    )
    # Marker should be reported because it indicates a definite failure.
    with pytest.raises(AuthError, match="incorrect"):
        wait_any(
            page,
            expected="#ready",
            known_errors={"Invalid login ID or password.": "Login failed — incorrect username/password."},
            timeout_ms=200,
        )
```

- [ ] **Step 7.2: Verify failing**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest tests/test_browser_flow.py -v
```

Expected: ImportError for `wait_any`, `AuthError`.

- [ ] **Step 7.3: Implement `src/schwab_cli/browser/flow.py`** (just `wait_any` + `AuthError` for now; `run_full_auth` comes in Task 8)

```python
from __future__ import annotations

import time
from typing import Protocol


class AuthError(Exception):
    """Raised on any failure during the browser-driven auth flow."""


class _PageLike(Protocol):
    def content(self) -> str: ...
    def wait_for_selector(self, selector: str, *, timeout: int): ...


_POLL_INTERVAL_SECONDS = 0.2

_UI_CHANGED_MESSAGE = (
    "Auth step timed out — Schwab may have changed. "
    "Selectors live at src/schwab_cli/browser/selectors.py. "
    "Auth incomplete."
)


def wait_any(
    page: _PageLike,
    *,
    expected: str,
    known_errors: dict[str, str],
    timeout_ms: int = 15_000,
) -> None:
    """Wait for the expected selector or a known error marker, whichever comes first.

    On a known error marker → raise AuthError with the mapped user-facing message.
    On the expected selector appearing → return None.
    On timeout with neither matched → raise AuthError("...Schwab may have changed...").
    """
    deadline = time.monotonic() + timeout_ms / 1000.0
    while True:
        # Check known error markers FIRST so a definite failure is not masked by
        # a coincidentally-present expected element.
        try:
            content = page.content()
        except Exception:
            content = ""
        for marker, user_message in known_errors.items():
            if marker in content:
                raise AuthError(user_message)

        # Try the expected selector with a short per-iteration timeout.
        try:
            page.wait_for_selector(expected, timeout=int(_POLL_INTERVAL_SECONDS * 1000))
            return
        except Exception:
            pass

        if time.monotonic() >= deadline:
            raise AuthError(_UI_CHANGED_MESSAGE)
```

- [ ] **Step 7.4: Run tests**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest tests/test_browser_flow.py -v
```

Expected: 4 passed.

- [ ] **Step 7.5: Commit**

```bash
cd /Users/weig/Projects/finance/schwab_cli && git add src/schwab_cli/browser/flow.py tests/test_browser_flow.py && git commit -m "feat(browser): add wait_any and AuthError"
```

---

## Task 8: `browser.flow.run_full_auth`

**Files:**
- Modify: `src/schwab_cli/browser/flow.py` (append `run_full_auth`)
- Modify: `tests/test_browser_flow.py` (append tests with extended FakePage)

- [ ] **Step 8.1: Append failing tests**

Append to `tests/test_browser_flow.py`:

```python
from urllib.parse import urlparse, parse_qs

from schwab_cli.config import Config
from schwab_cli.browser.flow import run_full_auth


def _cfg():
    return Config(
        client_id="cid",
        client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
        username="user@example.com",
        password="op://X/Y/Z",
    )


class FakeCheckbox:
    def __init__(self, checked: bool = False):
        self._checked = checked

    def is_checked(self) -> bool:
        return self._checked

    def check(self) -> None:
        self._checked = True


class FullFakePage(FakePage):
    """FakePage extended for run_full_auth: scripts goto, fill, click, navigation, checkboxes."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.fills: list[tuple[str, str]] = []
        self.clicks: list[str] = []
        self.gotos: list[str] = []
        self._final_redirect_url: str | None = kwargs.pop("final_redirect_url", None)
        self._checkboxes: list[FakeCheckbox] = kwargs.pop("checkboxes", [FakeCheckbox()])
        self._closed = False

    def goto(self, url: str) -> None:
        self.gotos.append(url)

    def fill(self, selector: str, value: str) -> None:
        self.fills.append((selector, value))

    def click(self, selector: str) -> None:
        self.clicks.append(selector)

    def query_selector_all(self, selector: str):
        return list(self._checkboxes)

    def evaluate(self, script: str) -> None:
        # Used to scroll to bottom on consent page; no-op for tests.
        pass

    def wait_for_url(self, predicate, *, timeout: int) -> None:
        if self._final_redirect_url and predicate(self._final_redirect_url):
            self.url = self._final_redirect_url
            return
        raise TimeoutError("redirect did not happen")


class FakeBrowser:
    def __init__(self, page):
        self._page = page
        self.closed = False

    def new_page(self):
        return self._page

    def close(self):
        self.closed = True


def _happy_browser(page):
    return FakeBrowser(page)


def test_run_full_auth_happy_path(monkeypatch):
    page = FullFakePage(
        selectors_present={
            "input#loginIdInput": True,
            "text=Terms of Use": True,
            "text=Select accounts": True,
            "text=You will now be redirected": True,
        },
        final_redirect_url="https://127.0.0.1:8443/?code=AUTH_CODE_123&session=abc",
        checkboxes=[FakeCheckbox(False), FakeCheckbox(True)],
    )
    browser = _happy_browser(page)

    captured = {}
    monkeypatch.setattr(
        "schwab_cli.browser.flow._launch_browser",
        lambda headless: browser,
    )
    monkeypatch.setattr(
        "schwab_cli.browser.flow.resolve_secret",
        lambda v: f"resolved({v})",
    )

    code = run_full_auth(_cfg())

    assert code == "AUTH_CODE_123"
    assert browser.closed is True
    assert page.gotos[0].startswith("https://api.schwabapi.com/v1/oauth/authorize?")
    # Both username and password fields filled, with resolved (op://) values.
    assert ("input#loginIdInput", "resolved(user@example.com)") in page.fills
    assert ("input#passwordInput", "resolved(op://X/Y/Z)") in page.fills
    # Both checkboxes ended up checked (one was already, one we toggled).
    assert all(cb.is_checked() for cb in page._checkboxes)


def test_run_full_auth_invalid_client_marker(monkeypatch):
    page = FullFakePage(
        content_sequence=['{"error": "invalid_client"}'],
        selectors_present={"input#loginIdInput": False},
    )
    browser = _happy_browser(page)
    monkeypatch.setattr("schwab_cli.browser.flow._launch_browser", lambda headless: browser)
    monkeypatch.setattr("schwab_cli.browser.flow.resolve_secret", lambda v: v)

    with pytest.raises(AuthError, match="rejected client_id/secret"):
        run_full_auth(_cfg())
    assert browser.closed is True


def test_run_full_auth_bad_credentials(monkeypatch):
    page = FullFakePage(
        # First content() check (after goto) returns empty, allowing login page.
        # Second content() check (after click login) finds the credentials error.
        content_sequence=["", "Invalid login ID or password."],
        selectors_present={
            "input#loginIdInput": True,
            "text=Terms of Use": False,
        },
    )
    browser = _happy_browser(page)
    monkeypatch.setattr("schwab_cli.browser.flow._launch_browser", lambda headless: browser)
    monkeypatch.setattr("schwab_cli.browser.flow.resolve_secret", lambda v: v)

    with pytest.raises(AuthError, match="incorrect username/password"):
        run_full_auth(_cfg())
    assert browser.closed is True


def test_run_full_auth_redirect_uri_mismatch(monkeypatch):
    page = FullFakePage(
        content_sequence=["", "We are unable to complete your request."],
        selectors_present={
            "input#loginIdInput": True,
            "text=Terms of Use": False,
        },
    )
    browser = _happy_browser(page)
    monkeypatch.setattr("schwab_cli.browser.flow._launch_browser", lambda headless: browser)
    monkeypatch.setattr("schwab_cli.browser.flow.resolve_secret", lambda v: v)

    with pytest.raises(AuthError, match="Redirect URI mismatch"):
        run_full_auth(_cfg())


def test_run_full_auth_no_accounts(monkeypatch):
    page = FullFakePage(
        selectors_present={
            "input#loginIdInput": True,
            "text=Terms of Use": True,
            "text=Select accounts": True,
        },
        checkboxes=[],  # no accounts shown
    )
    browser = _happy_browser(page)
    monkeypatch.setattr("schwab_cli.browser.flow._launch_browser", lambda headless: browser)
    monkeypatch.setattr("schwab_cli.browser.flow.resolve_secret", lambda v: v)

    with pytest.raises(AuthError, match="No accounts available"):
        run_full_auth(_cfg())
    assert browser.closed is True


def test_run_full_auth_redirect_without_code(monkeypatch):
    page = FullFakePage(
        selectors_present={
            "input#loginIdInput": True,
            "text=Terms of Use": True,
            "text=Select accounts": True,
            "text=You will now be redirected": True,
        },
        final_redirect_url="https://127.0.0.1:8443/?session=abc",  # no code
        checkboxes=[FakeCheckbox()],
    )
    browser = _happy_browser(page)
    monkeypatch.setattr("schwab_cli.browser.flow._launch_browser", lambda headless: browser)
    monkeypatch.setattr("schwab_cli.browser.flow.resolve_secret", lambda v: v)

    with pytest.raises(AuthError, match="no `code` param"):
        run_full_auth(_cfg())


def test_run_full_auth_chromium_missing_message(monkeypatch):
    def boom(headless):
        raise RuntimeError("Executable doesn't exist at .../chromium")

    monkeypatch.setattr("schwab_cli.browser.flow._launch_browser", boom)
    monkeypatch.setattr("schwab_cli.browser.flow.resolve_secret", lambda v: v)

    with pytest.raises(AuthError, match="playwright install chromium"):
        run_full_auth(_cfg())
```

- [ ] **Step 8.2: Verify failing**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest tests/test_browser_flow.py -v
```

Expected: ImportError for `run_full_auth`.

- [ ] **Step 8.3: Append `run_full_auth` to `src/schwab_cli/browser/flow.py`**

Add these imports near the top (after existing imports):

```python
import os
from urllib.parse import parse_qs, urlparse

from schwab_cli.browser.selectors import (
    ACCEPT_SELECTOR,
    ACCOUNT_CHECKBOX_SELECTOR,
    ACCOUNT_SELECTION_SELECTOR,
    CONFIRM_PAGE_SELECTOR,
    CONSENT_PAGE_SELECTOR,
    CONTINUE_SELECTOR,
    DONE_SELECTOR,
    INVALID_CLIENT_MARKERS,
    INVALID_CREDENTIALS_TEXT,
    LOGIN_PASSWORD_SELECTOR,
    LOGIN_SUBMIT_SELECTOR,
    LOGIN_USERNAME_SELECTOR,
    REDIRECT_URI_MISMATCH_TEXT,
    _is_debug_truthy,
)
from schwab_cli.config import Config
from schwab_cli.oauth import build_auth_url
from schwab_cli.secrets import resolve_secret
```

Then append:

```python
def _launch_browser(headless: bool):
    """Real Playwright launch. Pulled out so tests can monkeypatch this single seam."""
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=headless)
    # Stash the playwright handle on the browser so we can stop it on close.
    browser._pw = pw  # type: ignore[attr-defined]

    original_close = browser.close

    def close_with_pw():
        try:
            original_close()
        finally:
            pw.stop()

    browser.close = close_with_pw  # type: ignore[method-assign]
    return browser


def run_full_auth(cfg: Config) -> str:
    """Drive the OAuth browser flow end-to-end.

    Returns the authorization `code` extracted from the redirect URI.
    Raises AuthError on any documented failure; the browser is always closed
    before raising.
    """
    username = resolve_secret(cfg.username or "")
    password = resolve_secret(cfg.password or "")
    headless = not _is_debug_truthy(os.environ.get("DEBUG"))

    try:
        browser = _launch_browser(headless)
    except Exception as e:
        msg = str(e)
        if "Executable doesn't exist" in msg or "playwright install" in msg.lower():
            raise AuthError(
                "Chromium not found. Run: `uv run playwright install chromium`"
            ) from e
        raise AuthError(f"Failed to launch browser: {msg}") from e

    try:
        page = browser.new_page()
        page.goto(build_auth_url(cfg))

        wait_any(
            page,
            expected=LOGIN_USERNAME_SELECTOR,
            known_errors={
                marker: "Schwab rejected client_id/secret — verify setup."
                for marker in INVALID_CLIENT_MARKERS
            },
        )

        page.fill(LOGIN_USERNAME_SELECTOR, username)
        page.fill(LOGIN_PASSWORD_SELECTOR, password)
        page.click(LOGIN_SUBMIT_SELECTOR)

        wait_any(
            page,
            expected=CONSENT_PAGE_SELECTOR,
            known_errors={
                INVALID_CREDENTIALS_TEXT: "Login failed — incorrect username/password.",
                REDIRECT_URI_MISMATCH_TEXT: "Redirect URI mismatch — re-check setup.",
            },
        )

        # Scroll to bottom so the Accept button is in view.
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.click(ACCEPT_SELECTOR)

        wait_any(
            page,
            expected=ACCOUNT_SELECTION_SELECTOR,
            known_errors={},
        )

        checkboxes = page.query_selector_all(ACCOUNT_CHECKBOX_SELECTOR)
        if not checkboxes:
            raise AuthError("No accounts available on this login.")
        for cb in checkboxes:
            if not cb.is_checked():
                cb.check()
        page.click(CONTINUE_SELECTOR)

        wait_any(
            page,
            expected=CONFIRM_PAGE_SELECTOR,
            known_errors={},
        )

        page.click(DONE_SELECTOR)

        try:
            page.wait_for_url(
                lambda u: u.startswith(cfg.redirect_uri),
                timeout=15_000,
            )
        except Exception as e:
            raise AuthError(
                "Redirect didn't happen — auth incomplete."
            ) from e

        parsed = urlparse(page.url)
        code = parse_qs(parsed.query).get("code", [None])[0]
        if not code:
            raise AuthError("Redirect reached but no `code` param present.")
        return code
    finally:
        try:
            browser.close()
        except Exception:
            pass
```

- [ ] **Step 8.4: Run tests**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest tests/test_browser_flow.py -v
```

Expected: 11 passed (4 wait_any + 7 run_full_auth).

- [ ] **Step 8.5: Commit**

```bash
cd /Users/weig/Projects/finance/schwab_cli && git add src/schwab_cli/browser/flow.py tests/test_browser_flow.py && git commit -m "feat(browser): add run_full_auth orchestration"
```

---

## Task 9: `auth` Command + CLI Wiring

**Files:**
- Create: `src/schwab_cli/commands/auth.py`
- Modify: `src/schwab_cli/cli.py`
- Create: `tests/test_auth_command.py`

- [ ] **Step 9.1: Create `tests/test_auth_command.py` with failing tests**

```python
from unittest.mock import patch

import httpx
import pytest
from typer.testing import CliRunner

from schwab_cli.cli import app
from schwab_cli.config import Config
from schwab_cli.config import save as save_config
from schwab_cli.oauth import OAuthError, TokenResponse
from schwab_cli.session import Session, load as load_session, save as save_session

runner = CliRunner()


def _setup_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


def _seed_config(username="user@example.com", password="op://X/Y/Z"):
    save_config(Config(
        client_id="cid",
        client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
        username=username,
        password=password,
    ))


def _seed_session():
    save_session(Session(
        access_token="old_a", refresh_token="old_r",
        expires_at=100, refresh_token_expires_at=200,
    ))


def test_auth_errors_when_no_config(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    result = runner.invoke(app, ["auth"])
    assert result.exit_code == 1
    assert "Run `schwab_cli setup` first" in result.output


def test_auth_refreshes_when_session_present(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    _seed_config()
    _seed_session()

    fake_tr = TokenResponse(access_token="new_a", refresh_token="new_r", expires_in=1800)
    with patch("schwab_cli.commands.auth.oauth.refresh", return_value=fake_tr):
        with patch("schwab_cli.commands.auth.time.time", return_value=1_000_000):
            result = runner.invoke(app, ["auth"])

    assert result.exit_code == 0, result.output
    assert "Already logged in" in result.output
    s = load_session()
    assert s.access_token == "new_a"
    assert s.refresh_token == "new_r"
    assert s.expires_at == 1_000_000 + 1800


def test_auth_falls_back_to_full_auth_on_refresh_failure(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    _seed_config()
    _seed_session()

    fake_tr = TokenResponse(access_token="full_a", refresh_token="full_r", expires_in=1800)

    req = httpx.Request("POST", "https://example/")
    resp = httpx.Response(401, request=req, json={"error": "invalid_grant"})
    refresh_err = httpx.HTTPStatusError("401", request=req, response=resp)

    with patch("schwab_cli.commands.auth.oauth.refresh", side_effect=refresh_err), \
         patch("schwab_cli.commands.auth.run_full_auth", return_value="CODE"), \
         patch("schwab_cli.commands.auth.oauth.exchange_code", return_value=fake_tr), \
         patch("schwab_cli.commands.auth.time.time", return_value=2_000_000):
        result = runner.invoke(app, ["auth"])

    assert result.exit_code == 0, result.output
    assert "Refresh token rejected" in result.output
    assert "Authenticated" in result.output
    s = load_session()
    assert s.access_token == "full_a"
    assert s.expires_at == 2_000_000 + 1800


def test_auth_force_skips_refresh(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    _seed_config()
    _seed_session()

    fake_tr = TokenResponse(access_token="full_a", refresh_token="full_r", expires_in=1800)

    with patch("schwab_cli.commands.auth.oauth.refresh") as refresh_mock, \
         patch("schwab_cli.commands.auth.run_full_auth", return_value="CODE"), \
         patch("schwab_cli.commands.auth.oauth.exchange_code", return_value=fake_tr):
        result = runner.invoke(app, ["auth", "--force"])

    assert result.exit_code == 0, result.output
    refresh_mock.assert_not_called()
    s = load_session()
    assert s.access_token == "full_a"


def test_auth_full_auth_failure_exits_1_no_session_written(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    _seed_config()
    # No prior session.

    from schwab_cli.browser.flow import AuthError

    with patch("schwab_cli.commands.auth.run_full_auth",
               side_effect=AuthError("Login failed — incorrect username/password.")):
        result = runner.invoke(app, ["auth"])

    assert result.exit_code == 1
    assert "Login failed" in result.output
    assert load_session() is None


def test_auth_full_auth_runs_when_no_session(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    _seed_config()

    fake_tr = TokenResponse(access_token="full_a", refresh_token="full_r", expires_in=1800)

    with patch("schwab_cli.commands.auth.run_full_auth", return_value="CODE") as full, \
         patch("schwab_cli.commands.auth.oauth.exchange_code", return_value=fake_tr) as ex:
        result = runner.invoke(app, ["auth"])

    assert result.exit_code == 0, result.output
    full.assert_called_once()
    ex.assert_called_once()
    assert "Authenticated" in result.output


def test_auth_token_exchange_failure_after_full_auth(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    _seed_config()

    req = httpx.Request("POST", "https://example/")
    resp = httpx.Response(400, request=req, json={"error": "invalid_grant"})
    err = httpx.HTTPStatusError("400", request=req, response=resp)

    with patch("schwab_cli.commands.auth.run_full_auth", return_value="CODE"), \
         patch("schwab_cli.commands.auth.oauth.exchange_code", side_effect=err):
        result = runner.invoke(app, ["auth"])

    assert result.exit_code == 1
    assert "Token exchange failed" in result.output
    assert load_session() is None
```

- [ ] **Step 9.2: Verify failing**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest tests/test_auth_command.py -v
```

Expected: ImportError for the `auth` command (not registered yet).

- [ ] **Step 9.3: Implement `src/schwab_cli/commands/auth.py`**

```python
from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx
import typer

from schwab_cli import config as config_module
from schwab_cli import oauth
from schwab_cli.browser.flow import AuthError, run_full_auth
from schwab_cli.browser.selectors import _summarize_error
from schwab_cli.secrets import SecretError
from schwab_cli.session import Session
from schwab_cli.session import save as save_session
from schwab_cli.session import load as load_session


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def run(force: bool) -> None:
    cfg = config_module.load()
    if cfg is None:
        typer.secho(
            "No config found. Run `schwab_cli setup` first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    if not force:
        session = load_session()
        if session is not None:
            try:
                tr = oauth.refresh(cfg, session.refresh_token)
                new_session = Session.from_token_response(tr, now=int(time.time()))
                save_session(new_session)
                typer.secho(
                    f"Already logged in. Access token valid until {_iso(new_session.expires_at)}.",
                    fg=typer.colors.GREEN,
                )
                raise typer.Exit(code=0)
            except (httpx.HTTPStatusError, httpx.RequestError, oauth.OAuthError) as e:
                typer.echo(
                    f"Refresh token rejected ({_summarize_error(e)}); doing full auth."
                )
                # fall through

    try:
        code = run_full_auth(cfg)
    except (AuthError, SecretError) as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    try:
        tr = oauth.exchange_code(cfg, code)
    except (httpx.HTTPStatusError, httpx.RequestError, oauth.OAuthError) as e:
        typer.secho(
            f"Token exchange failed: {_summarize_error(e)}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    new_session = Session.from_token_response(tr, now=int(time.time()))
    save_session(new_session)
    typer.secho(
        f"Authenticated. Access token expires at {_iso(new_session.expires_at)}.",
        fg=typer.colors.GREEN,
    )
```

- [ ] **Step 9.4: Register `auth` in `src/schwab_cli/cli.py`**

Replace the contents with:

```python
import typer

from schwab_cli.commands import auth as auth_cmd
from schwab_cli.commands import setup as setup_cmd

app = typer.Typer(
    name="schwab_cli",
    help="Charles Schwab CLI.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main() -> None:
    """Charles Schwab CLI."""


@app.command("setup", help="Configure Schwab CLI credentials.")
def setup() -> None:
    setup_cmd.run()


@app.command("auth", help="Authenticate with Schwab (refresh or full OAuth).")
def auth(
    force: bool = typer.Option(
        False,
        "--force",
        help="Skip session refresh and run the full OAuth flow.",
    ),
) -> None:
    auth_cmd.run(force=force)
```

- [ ] **Step 9.5: Run all tests**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest -v
```

Expected: full suite passes. New count: 30 (existing) + 13 (session) + 13 (oauth) + 6 (secrets) + 11 (selectors) + 11 (browser_flow) + 7 (auth_command) ≈ 91.

(Don't worry if the parametrize-counts shift the total slightly; the goal is "all passing".)

- [ ] **Step 9.6: Commit**

```bash
cd /Users/weig/Projects/finance/schwab_cli && git add src/schwab_cli/commands/auth.py src/schwab_cli/cli.py tests/test_auth_command.py && git commit -m "feat(auth): implement auth command with refresh + full-auth fallback"
```

---

## Task 10: README Update + Final Verification

**Files:**
- Modify: `README.md`

- [ ] **Step 10.1: Update `README.md`**

Replace its full contents with:

````markdown
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
````

- [ ] **Step 10.2: Run full test suite with coverage**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest
```

Expected: all tests pass. Coverage target ≥80% on each new module:
- `session.py` — should be ~95% (atomicity test exercises most branches)
- `oauth.py` — should be ~95%
- `secrets.py` — should be 100%
- `browser/selectors.py` — should be ~95% (constants + helpers)
- `browser/flow.py` — should be ≥80% (Playwright wrappers contribute uncovered lines; that's fine)
- `commands/auth.py` — should be ≥85%

If a new module is below 80%, identify the uncovered lines (`--cov-report=term-missing`). If they're real gaps (not defensive paths), add a targeted test before continuing.

- [ ] **Step 10.3: Verify CLI surface**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run schwab_cli --help
cd /Users/weig/Projects/finance/schwab_cli && uv run schwab_cli auth --help
cd /Users/weig/Projects/finance/schwab_cli && uv run schwab_cli setup --help
```

Expected: each prints help and exits 0. The top-level `--help` should list both `setup` and `auth` subcommands.

- [ ] **Step 10.4: Manual smoke test — refresh path with no session**

```bash
cd /Users/weig/Projects/finance/schwab_cli
TMPHOME=$(mktemp -d)
HOME=$TMPHOME printf 'cid\ncsec\nhttps://127.0.0.1:8443\nn\n' | uv run schwab_cli setup
HOME=$TMPHOME uv run schwab_cli auth || echo "expected to fail at full-auth (no real Schwab)"
rm -rf $TMPHOME
```

Expected: `setup` succeeds; `auth` proceeds straight to full auth (no session present), tries to launch Chromium, then either:
- Reaches the Schwab login page and times out at "Auth step timed out" (selectors will need tightening on the first real attempt), OR
- Errors out at the chromium-not-found message if Playwright browsers aren't installed.

Either outcome confirms the wiring is correct without requiring real Schwab credentials.

- [ ] **Step 10.5: Final commit**

```bash
cd /Users/weig/Projects/finance/schwab_cli && git add README.md && git commit -m "docs: document playwright install and auth usage"
```

If anything else changed during verification (formatting, etc.):

```bash
cd /Users/weig/Projects/finance/schwab_cli && git status
git add -A
git commit -m "chore: post-verification polish"
```
