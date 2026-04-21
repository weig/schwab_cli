# Phase 3 — Account + Market Data Commands Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add read-only account and market-data commands (`accounts`, `account`, `positions`, `quote`) on top of the Phase 2 auth infrastructure. Default human-readable output with opt-in `--json` / `--md` flags.

**Architecture:** Hand-rolled `httpx` against Schwab's REST API, matching the Phase 1/2 pattern. A small `SchwabClient` handles Bearer auth + auto-refresh on 401. Endpoint wrappers are one-function-per-request modules. Output is a pure-function formatter layer with three representations. Commands are thin glue between API and output.

**Tech Stack:** Python 3.11+, `typer`, `httpx`, `rich`, `pytest`, `respx`, frozen dataclasses.

**Spec:** [`docs/superpowers/specs/2026-04-21-schwab-cli-data-commands-design.md`](../specs/2026-04-21-schwab-cli-data-commands-design.md)

---

## File Structure

| Path | Responsibility |
|---|---|
| `pyproject.toml` | Add `rich>=13.7` |
| `src/schwab_cli/api/__init__.py` | Empty marker |
| `src/schwab_cli/api/client.py` | `SchwabClient`, `ApiError`, `SessionExpired` — httpx + auto-refresh |
| `src/schwab_cli/api/accounts.py` | `list_accounts`, `get_account`, `get_positions` |
| `src/schwab_cli/api/quotes.py` | `get_quotes` |
| `src/schwab_cli/output/__init__.py` | Empty marker |
| `src/schwab_cli/output/format.py` | `Format` enum, `FormatError`, `pick_format` |
| `src/schwab_cli/output/accounts.py` | `render_accounts`, `render_account`, `render_positions` |
| `src/schwab_cli/output/quotes.py` | `render_quotes` |
| `src/schwab_cli/commands/accounts.py` | `accounts`, `account`, `positions` subcommands |
| `src/schwab_cli/commands/quote.py` | `quote` subcommand |
| `src/schwab_cli/cli.py` | Register the 4 new subcommands |
| `tests/test_api_client.py` | Client auth / refresh / error tests |
| `tests/test_api_accounts.py` | Endpoint wrappers |
| `tests/test_api_quotes.py` | Quote endpoint |
| `tests/test_output_format.py` | Format picker + mutex |
| `tests/test_output_accounts.py` | Accounts renderers |
| `tests/test_output_quotes.py` | Quote renderers |
| `tests/test_commands_accounts.py` | CLI end-to-end with mocked client |
| `tests/test_commands_quote.py` | CLI end-to-end with mocked client |

---

## Task 1: Add `rich` dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1.1: Add rich to runtime deps**

Edit `pyproject.toml`'s `[project]` `dependencies` list to add `"rich>=13.7"`. The existing list should now include `typer>=0.12.0`, `httpx>=0.27`, `playwright>=1.45`, `playwright-stealth` (already there), `seleniumbase` (already there), and the new `rich>=13.7`.

- [ ] **Step 1.2: Sync**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv sync --extra dev
```

Expected: installs `rich` and its transitive deps (`markdown-it-py`, `pygments`). No other changes.

- [ ] **Step 1.3: Verify existing tests still pass**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest -q
```

Expected: 100 passed, 20 skipped. (Matches Phase 2 state.)

- [ ] **Step 1.4: Commit**

```bash
cd /Users/weig/Projects/finance/schwab_cli && git add pyproject.toml uv.lock && git commit -m "chore: add rich>=13.7 for terminal table rendering"
```

---

## Task 2: `output/format.py` — Format enum + mutex

**Files:**
- Create: `src/schwab_cli/output/__init__.py`
- Create: `src/schwab_cli/output/format.py`
- Create: `tests/test_output_format.py`

TDD: tests first.

- [ ] **Step 2.1: Create `src/schwab_cli/output/__init__.py`** — empty file.

- [ ] **Step 2.2: Create `tests/test_output_format.py`**

```python
import pytest

from schwab_cli.output.format import Format, FormatError, pick_format


def test_default_is_human():
    assert pick_format(False, False) is Format.HUMAN


def test_json_flag_picks_json():
    assert pick_format(True, False) is Format.JSON


def test_md_flag_picks_md():
    assert pick_format(False, True) is Format.MD


def test_both_flags_raise():
    with pytest.raises(FormatError, match="mutually exclusive"):
        pick_format(True, True)


def test_format_enum_has_three_variants():
    assert {f.name for f in Format} == {"HUMAN", "JSON", "MD"}
```

- [ ] **Step 2.3: Run to verify fail**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest tests/test_output_format.py -v
```

Expected: ImportError for `schwab_cli.output.format`.

- [ ] **Step 2.4: Create `src/schwab_cli/output/format.py`**

```python
from __future__ import annotations

from enum import Enum


class Format(Enum):
    HUMAN = "human"
    JSON = "json"
    MD = "md"


class FormatError(Exception):
    """Raised when incompatible format flags are combined."""


def pick_format(json: bool, md: bool) -> Format:
    if json and md:
        raise FormatError("--json and --md are mutually exclusive.")
    if json:
        return Format.JSON
    if md:
        return Format.MD
    return Format.HUMAN
```

- [ ] **Step 2.5: Run tests**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest tests/test_output_format.py -v
```

Expected: 5 passed.

- [ ] **Step 2.6: Commit**

```bash
cd /Users/weig/Projects/finance/schwab_cli && git add src/schwab_cli/output/__init__.py src/schwab_cli/output/format.py tests/test_output_format.py && git commit -m "feat(output): add Format enum and pick_format helper"
```

---

## Task 3: `api/client.py` — SchwabClient skeleton + `get()` with auth

**Files:**
- Create: `src/schwab_cli/api/__init__.py`
- Create: `src/schwab_cli/api/client.py`
- Create: `tests/test_api_client.py`

TDD: tests first.

- [ ] **Step 3.1: Create `src/schwab_cli/api/__init__.py`** — empty file.

- [ ] **Step 3.2: Create `tests/test_api_client.py`**

```python
import httpx
import pytest
import respx

from schwab_cli.api.client import ApiError, SchwabClient, SessionExpired
from schwab_cli.config import Config
from schwab_cli.session import Session


def _cfg() -> Config:
    return Config(
        client_id="cid",
        client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
    )


def _session(access="atok", refresh="rtok") -> Session:
    return Session(
        access_token=access,
        refresh_token=refresh,
        expires_at=1_000_000,
        refresh_token_expires_at=2_000_000,
    )


@respx.mock
def test_get_sends_bearer_auth_and_returns_json():
    route = respx.get("https://api.schwabapi.com/trader/v1/accounts").mock(
        return_value=httpx.Response(200, json=[{"accountNumber": "123"}]),
    )
    client = SchwabClient(_cfg(), _session(access="atok"))
    body = client.get("https://api.schwabapi.com/trader/v1/accounts")
    assert body == [{"accountNumber": "123"}]
    assert route.calls.last.request.headers["Authorization"] == "Bearer atok"


@respx.mock
def test_get_with_params_encodes_query():
    route = respx.get("https://api.schwabapi.com/marketdata/v1/quotes").mock(
        return_value=httpx.Response(200, json={"AAPL": {"symbol": "AAPL"}}),
    )
    client = SchwabClient(_cfg(), _session())
    client.get(
        "https://api.schwabapi.com/marketdata/v1/quotes",
        params={"symbols": "AAPL,MSFT"},
    )
    assert "symbols=AAPL%2CMSFT" in str(route.calls.last.request.url)


@respx.mock
def test_500_raises_api_error():
    respx.get("https://api.schwabapi.com/trader/v1/accounts").mock(
        return_value=httpx.Response(500, text="internal server error"),
    )
    client = SchwabClient(_cfg(), _session())
    with pytest.raises(ApiError, match="500"):
        client.get("https://api.schwabapi.com/trader/v1/accounts")


@respx.mock
def test_network_error_raises_api_error():
    respx.get("https://api.schwabapi.com/trader/v1/accounts").mock(
        side_effect=httpx.ConnectError("dns failed"),
    )
    client = SchwabClient(_cfg(), _session())
    with pytest.raises(ApiError, match="network"):
        client.get("https://api.schwabapi.com/trader/v1/accounts")


@respx.mock
def test_401_triggers_refresh_and_retry(tmp_path, monkeypatch):
    """401 → oauth.refresh → save new session → retry → success."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    # 401 first, 200 second for the accounts call
    respx.get("https://api.schwabapi.com/trader/v1/accounts").mock(
        side_effect=[
            httpx.Response(401, json={"error": "invalid_token"}),
            httpx.Response(200, json=[{"accountNumber": "123"}]),
        ]
    )
    # oauth.refresh hits the token endpoint
    respx.post("https://api.schwabapi.com/v1/oauth/token").mock(
        return_value=httpx.Response(200, json={
            "access_token": "fresh_atok",
            "refresh_token": "fresh_rtok",
            "expires_in": 1800,
        }),
    )

    client = SchwabClient(_cfg(), _session(access="old_atok"))
    body = client.get("https://api.schwabapi.com/trader/v1/accounts")
    assert body == [{"accountNumber": "123"}]
    # Second call must have used the fresh access token
    last_call = respx.routes[0].calls.last
    assert last_call.request.headers["Authorization"] == "Bearer fresh_atok"


@respx.mock
def test_401_then_refresh_fails_raises_session_expired(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    respx.get("https://api.schwabapi.com/trader/v1/accounts").mock(
        return_value=httpx.Response(401, json={"error": "invalid_token"}),
    )
    respx.post("https://api.schwabapi.com/v1/oauth/token").mock(
        return_value=httpx.Response(401, json={"error": "invalid_grant"}),
    )

    client = SchwabClient(_cfg(), _session())
    with pytest.raises(SessionExpired, match="Session expired"):
        client.get("https://api.schwabapi.com/trader/v1/accounts")


@respx.mock
def test_401_twice_after_refresh_raises_session_expired(tmp_path, monkeypatch):
    """Edge: token endpoint accepts the refresh but the follow-up API call still 401s."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    respx.get("https://api.schwabapi.com/trader/v1/accounts").mock(
        side_effect=[
            httpx.Response(401),
            httpx.Response(401),
        ]
    )
    respx.post("https://api.schwabapi.com/v1/oauth/token").mock(
        return_value=httpx.Response(200, json={
            "access_token": "fresh_atok",
            "refresh_token": "fresh_rtok",
            "expires_in": 1800,
        }),
    )

    client = SchwabClient(_cfg(), _session())
    with pytest.raises(SessionExpired):
        client.get("https://api.schwabapi.com/trader/v1/accounts")
```

- [ ] **Step 3.3: Verify failing**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest tests/test_api_client.py -v
```

Expected: ImportError for `schwab_cli.api.client`.

- [ ] **Step 3.4: Implement `src/schwab_cli/api/client.py`**

```python
from __future__ import annotations

import time

import httpx

from schwab_cli import oauth
from schwab_cli.config import Config
from schwab_cli.session import Session, save as save_session


class ApiError(Exception):
    """Raised on any Schwab API failure that isn't an auth-refresh case."""


class SessionExpired(ApiError):
    """Raised when the refresh token is rejected or the API still 401s after refresh.

    User must re-auth interactively (`schwab_cli auth --force`).
    """


class SchwabClient:
    """Minimal auth-aware HTTP client for Schwab REST APIs.

    Handles Bearer-token injection, a single automatic refresh on 401, and
    mapping of HTTP / network errors to `ApiError` / `SessionExpired`.
    """

    def __init__(self, cfg: Config, session: Session) -> None:
        self._cfg = cfg
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    def get(self, url: str, *, params: dict | None = None) -> dict | list:
        """Authed GET. Returns parsed JSON body. Raises ApiError/SessionExpired."""
        try:
            resp = self._request("GET", url, params=params)
        except httpx.RequestError as e:
            raise ApiError(f"network: {type(e).__name__}") from e

        if resp.status_code == 401:
            self._refresh_or_expire()
            try:
                resp = self._request("GET", url, params=params)
            except httpx.RequestError as e:
                raise ApiError(f"network: {type(e).__name__}") from e
            if resp.status_code == 401:
                raise SessionExpired(
                    "Session expired. Run `schwab_cli auth --force`."
                )

        if resp.status_code >= 400:
            body = (resp.text or "").splitlines()[0] if resp.text else ""
            raise ApiError(f"{resp.status_code} {body}".strip())

        return resp.json()

    def _request(self, method: str, url: str, *, params: dict | None = None) -> httpx.Response:
        return httpx.request(
            method,
            url,
            params=params,
            headers={"Authorization": f"Bearer {self._session.access_token}"},
            timeout=30.0,
        )

    def _refresh_or_expire(self) -> None:
        try:
            tr = oauth.refresh(self._cfg, self._session.refresh_token)
        except (httpx.HTTPStatusError, httpx.RequestError, oauth.OAuthError) as e:
            raise SessionExpired(
                "Session expired. Run `schwab_cli auth --force`."
            ) from e
        self._session = Session.from_token_response(tr, now=int(time.time()))
        save_session(self._session)
```

- [ ] **Step 3.5: Run tests**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest tests/test_api_client.py -v
```

Expected: 7 passed.

- [ ] **Step 3.6: Commit**

```bash
cd /Users/weig/Projects/finance/schwab_cli && git add src/schwab_cli/api/__init__.py src/schwab_cli/api/client.py tests/test_api_client.py && git commit -m "feat(api): add SchwabClient with bearer auth + auto-refresh on 401"
```

---

## Task 4: `api/client.py` — account-number → hashValue resolution

**Files:**
- Modify: `src/schwab_cli/api/client.py` (add `resolve_account`)
- Modify: `tests/test_api_client.py` (append tests)

- [ ] **Step 4.1: Append failing tests to `tests/test_api_client.py`**

```python
@respx.mock
def test_resolve_account_exact_match():
    respx.get("https://api.schwabapi.com/trader/v1/accounts/accountNumbers").mock(
        return_value=httpx.Response(200, json=[
            {"accountNumber": "12345678", "hashValue": "HASH_A"},
            {"accountNumber": "87654321", "hashValue": "HASH_B"},
        ]),
    )
    client = SchwabClient(_cfg(), _session())
    ids = client.resolve_account("12345678")
    assert ids.account_number == "12345678"
    assert ids.hash_value == "HASH_A"


@respx.mock
def test_resolve_account_by_suffix():
    respx.get("https://api.schwabapi.com/trader/v1/accounts/accountNumbers").mock(
        return_value=httpx.Response(200, json=[
            {"accountNumber": "12345678", "hashValue": "HASH_A"},
            {"accountNumber": "87654321", "hashValue": "HASH_B"},
        ]),
    )
    client = SchwabClient(_cfg(), _session())
    ids = client.resolve_account("5678")
    assert ids.account_number == "12345678"


@respx.mock
def test_resolve_account_ambiguous_raises():
    respx.get("https://api.schwabapi.com/trader/v1/accounts/accountNumbers").mock(
        return_value=httpx.Response(200, json=[
            {"accountNumber": "11115678", "hashValue": "HASH_A"},
            {"accountNumber": "22225678", "hashValue": "HASH_B"},
        ]),
    )
    client = SchwabClient(_cfg(), _session())
    with pytest.raises(ApiError, match="Multiple accounts match"):
        client.resolve_account("5678")


@respx.mock
def test_resolve_account_unknown_raises():
    respx.get("https://api.schwabapi.com/trader/v1/accounts/accountNumbers").mock(
        return_value=httpx.Response(200, json=[
            {"accountNumber": "12345678", "hashValue": "HASH_A"},
        ]),
    )
    client = SchwabClient(_cfg(), _session())
    with pytest.raises(ApiError, match="not found"):
        client.resolve_account("99999999")


@respx.mock
def test_resolve_account_caches_result():
    route = respx.get("https://api.schwabapi.com/trader/v1/accounts/accountNumbers").mock(
        return_value=httpx.Response(200, json=[
            {"accountNumber": "12345678", "hashValue": "HASH_A"},
        ]),
    )
    client = SchwabClient(_cfg(), _session())
    client.resolve_account("12345678")
    client.resolve_account("5678")
    assert route.call_count == 1  # second call used the cache
```

- [ ] **Step 4.2: Verify failing**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest tests/test_api_client.py -v -k resolve_account
```

Expected: AttributeError: no `resolve_account`.

- [ ] **Step 4.3: Add to `src/schwab_cli/api/client.py`**

At the top of the file, add the import and the dataclass:

```python
from dataclasses import dataclass
```

Add this frozen dataclass below `SessionExpired`:

```python
@dataclass(frozen=True)
class AccountIds:
    """User-facing account_number ↔ Schwab hashValue."""
    account_number: str
    hash_value: str
```

Inside `SchwabClient.__init__`, add:

```python
        self._account_ids_cache: list[AccountIds] | None = None
```

Add these methods to `SchwabClient`:

```python
    TRADER_BASE = "https://api.schwabapi.com/trader/v1"

    def _load_account_ids(self) -> list[AccountIds]:
        if self._account_ids_cache is None:
            raw = self.get(f"{self.TRADER_BASE}/accounts/accountNumbers")
            self._account_ids_cache = [
                AccountIds(account_number=item["accountNumber"], hash_value=item["hashValue"])
                for item in raw
            ]
        return self._account_ids_cache

    def resolve_account(self, user_input: str) -> AccountIds:
        """Match user input against account_number (exact or suffix).

        Raises ApiError on 0 matches ("not found") or 2+ matches ("Multiple accounts match").
        """
        ids = self._load_account_ids()
        matches = [i for i in ids if i.account_number == user_input or i.account_number.endswith(user_input)]
        if not matches:
            available = ", ".join(f"...{i.account_number[-4:]}" for i in ids)
            raise ApiError(
                f"Account {user_input!r} not found. Available: {available}."
            )
        if len(matches) > 1:
            listing = ", ".join(m.account_number for m in matches)
            raise ApiError(
                f"Multiple accounts match {user_input!r}: {listing}. Specify more digits."
            )
        return matches[0]
```

- [ ] **Step 4.4: Run tests**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest tests/test_api_client.py -v
```

Expected: 12 passed (7 prior + 5 new).

- [ ] **Step 4.5: Commit**

```bash
cd /Users/weig/Projects/finance/schwab_cli && git add src/schwab_cli/api/client.py tests/test_api_client.py && git commit -m "feat(api): resolve_account maps user input (full or suffix) to Schwab hashValue"
```

---

## Task 5: `api/accounts.py` — endpoint wrappers

**Files:**
- Create: `src/schwab_cli/api/accounts.py`
- Create: `tests/test_api_accounts.py`

- [ ] **Step 5.1: Create `tests/test_api_accounts.py`**

```python
import httpx
import respx

from schwab_cli.api.accounts import get_account, get_positions, list_accounts
from schwab_cli.api.client import SchwabClient
from schwab_cli.config import Config
from schwab_cli.session import Session


def _client() -> SchwabClient:
    cfg = Config(client_id="cid", client_secret="csec", redirect_uri="https://127.0.0.1:8443")
    session = Session(
        access_token="atok", refresh_token="rtok",
        expires_at=1_000_000, refresh_token_expires_at=2_000_000,
    )
    return SchwabClient(cfg, session)


@respx.mock
def test_list_accounts_returns_all_with_positions():
    route = respx.get("https://api.schwabapi.com/trader/v1/accounts").mock(
        return_value=httpx.Response(200, json=[
            {"securitiesAccount": {"accountNumber": "12345678", "type": "MARGIN"}},
            {"securitiesAccount": {"accountNumber": "87654321", "type": "CASH"}},
        ]),
    )
    got = list_accounts(_client())
    assert len(got) == 2
    assert got[0]["securitiesAccount"]["accountNumber"] == "12345678"
    assert route.calls.last.request.url.params["fields"] == "positions"


@respx.mock
def test_get_account_resolves_and_fetches_one():
    respx.get("https://api.schwabapi.com/trader/v1/accounts/accountNumbers").mock(
        return_value=httpx.Response(200, json=[
            {"accountNumber": "12345678", "hashValue": "HASH_A"},
        ]),
    )
    detail = {"securitiesAccount": {"accountNumber": "12345678", "type": "MARGIN"}}
    route = respx.get("https://api.schwabapi.com/trader/v1/accounts/HASH_A").mock(
        return_value=httpx.Response(200, json=detail),
    )
    got = get_account(_client(), "12345678")
    assert got == detail
    assert route.calls.last.request.url.params["fields"] == "positions"


@respx.mock
def test_get_positions_all_accounts_aggregates():
    respx.get("https://api.schwabapi.com/trader/v1/accounts").mock(
        return_value=httpx.Response(200, json=[
            {"securitiesAccount": {
                "accountNumber": "12345678",
                "positions": [{"instrument": {"symbol": "AAPL"}, "longQuantity": 10}],
            }},
            {"securitiesAccount": {
                "accountNumber": "87654321",
                "positions": [{"instrument": {"symbol": "MSFT"}, "longQuantity": 5}],
            }},
        ]),
    )
    rows = get_positions(_client(), None)
    assert len(rows) == 2
    symbols = {(r["_account"], r["instrument"]["symbol"]) for r in rows}
    assert symbols == {("12345678", "AAPL"), ("87654321", "MSFT")}


@respx.mock
def test_get_positions_filtered_by_account():
    respx.get("https://api.schwabapi.com/trader/v1/accounts/accountNumbers").mock(
        return_value=httpx.Response(200, json=[
            {"accountNumber": "12345678", "hashValue": "HASH_A"},
        ]),
    )
    respx.get("https://api.schwabapi.com/trader/v1/accounts/HASH_A").mock(
        return_value=httpx.Response(200, json={"securitiesAccount": {
            "accountNumber": "12345678",
            "positions": [{"instrument": {"symbol": "AAPL"}, "longQuantity": 10}],
        }}),
    )
    rows = get_positions(_client(), "12345678")
    assert len(rows) == 1
    assert rows[0]["instrument"]["symbol"] == "AAPL"
    assert rows[0]["_account"] == "12345678"


@respx.mock
def test_get_positions_handles_account_without_positions_key():
    respx.get("https://api.schwabapi.com/trader/v1/accounts").mock(
        return_value=httpx.Response(200, json=[
            {"securitiesAccount": {"accountNumber": "12345678"}},  # no "positions"
        ]),
    )
    rows = get_positions(_client(), None)
    assert rows == []
```

- [ ] **Step 5.2: Verify failing**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest tests/test_api_accounts.py -v
```

Expected: ImportError for `schwab_cli.api.accounts`.

- [ ] **Step 5.3: Create `src/schwab_cli/api/accounts.py`**

```python
from __future__ import annotations

from schwab_cli.api.client import SchwabClient


def list_accounts(client: SchwabClient) -> list[dict]:
    """All accounts with positions included in one call."""
    return client.get(
        f"{client.TRADER_BASE}/accounts",
        params={"fields": "positions"},
    )


def get_account(client: SchwabClient, account_number: str) -> dict:
    """Single account with positions."""
    ids = client.resolve_account(account_number)
    return client.get(
        f"{client.TRADER_BASE}/accounts/{ids.hash_value}",
        params={"fields": "positions"},
    )


def get_positions(client: SchwabClient, account_number: str | None) -> list[dict]:
    """Flat list of position rows across the selected account(s).

    Each returned row is the raw position dict with a synthetic `_account`
    key set to the owning account number. Accounts without any positions
    are omitted.
    """
    if account_number is None:
        payload = list_accounts(client)
        if not isinstance(payload, list):
            return []
    else:
        payload = [get_account(client, account_number)]

    rows: list[dict] = []
    for item in payload:
        sec = item.get("securitiesAccount", {})
        acct = sec.get("accountNumber", "")
        for pos in sec.get("positions", []) or []:
            pos = dict(pos)
            pos["_account"] = acct
            rows.append(pos)
    return rows
```

- [ ] **Step 5.4: Run tests**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest tests/test_api_accounts.py -v
```

Expected: 5 passed.

- [ ] **Step 5.5: Commit**

```bash
cd /Users/weig/Projects/finance/schwab_cli && git add src/schwab_cli/api/accounts.py tests/test_api_accounts.py && git commit -m "feat(api): add list_accounts / get_account / get_positions wrappers"
```

---

## Task 6: `api/quotes.py` — endpoint wrapper

**Files:**
- Create: `src/schwab_cli/api/quotes.py`
- Create: `tests/test_api_quotes.py`

- [ ] **Step 6.1: Create `tests/test_api_quotes.py`**

```python
import httpx
import respx

from schwab_cli.api.client import SchwabClient
from schwab_cli.api.quotes import get_quotes
from schwab_cli.config import Config
from schwab_cli.session import Session


def _client() -> SchwabClient:
    cfg = Config(client_id="cid", client_secret="csec", redirect_uri="https://127.0.0.1:8443")
    s = Session(
        access_token="atok", refresh_token="rtok",
        expires_at=1_000_000, refresh_token_expires_at=2_000_000,
    )
    return SchwabClient(cfg, s)


@respx.mock
def test_get_quotes_single_symbol():
    route = respx.get("https://api.schwabapi.com/marketdata/v1/quotes").mock(
        return_value=httpx.Response(200, json={
            "AAPL": {"symbol": "AAPL", "quote": {"lastPrice": 232.14}},
        }),
    )
    result = get_quotes(_client(), ["AAPL"])
    assert result["AAPL"]["quote"]["lastPrice"] == 232.14
    assert route.calls.last.request.url.params["symbols"] == "AAPL"


@respx.mock
def test_get_quotes_multi_symbol_comma_joined():
    route = respx.get("https://api.schwabapi.com/marketdata/v1/quotes").mock(
        return_value=httpx.Response(200, json={
            "AAPL": {"symbol": "AAPL"},
            "MSFT": {"symbol": "MSFT"},
        }),
    )
    get_quotes(_client(), ["AAPL", "MSFT", "NVDA"])
    assert route.calls.last.request.url.params["symbols"] == "AAPL,MSFT,NVDA"


@respx.mock
def test_get_quotes_unknown_symbol_passthrough():
    """Schwab returns per-symbol error metadata rather than a top-level error."""
    respx.get("https://api.schwabapi.com/marketdata/v1/quotes").mock(
        return_value=httpx.Response(200, json={
            "AAPL": {"symbol": "AAPL"},
            "errors": {"invalidSymbols": ["ZZZZZZ"]},
        }),
    )
    result = get_quotes(_client(), ["AAPL", "ZZZZZZ"])
    assert "AAPL" in result
    assert "errors" in result


@respx.mock
def test_get_quotes_empty_list_noop():
    """We don't want to call Schwab with no symbols."""
    result = get_quotes(_client(), [])
    assert result == {}
```

- [ ] **Step 6.2: Verify failing**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest tests/test_api_quotes.py -v
```

Expected: ImportError.

- [ ] **Step 6.3: Create `src/schwab_cli/api/quotes.py`**

```python
from __future__ import annotations

from schwab_cli.api.client import SchwabClient


def get_quotes(client: SchwabClient, symbols: list[str]) -> dict:
    """Fetch quotes for the given symbols. Returns the Schwab response dict.

    Callers get per-symbol entries plus an optional `errors` key with
    lists like `invalidSymbols` — not a 4xx, just per-symbol metadata.
    """
    if not symbols:
        return {}
    return client.get(
        f"{SchwabClient.MARKET_BASE}/quotes",
        params={"symbols": ",".join(symbols)},
    )
```

**Note:** `SchwabClient.MARKET_BASE` is not yet defined. Add it to `src/schwab_cli/api/client.py` next to `TRADER_BASE`:

Edit `src/schwab_cli/api/client.py` — find the line `TRADER_BASE = "https://api.schwabapi.com/trader/v1"` and immediately after it add:

```python
    MARKET_BASE = "https://api.schwabapi.com/marketdata/v1"
```

- [ ] **Step 6.4: Run tests**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest tests/test_api_quotes.py -v
```

Expected: 4 passed.

- [ ] **Step 6.5: Commit**

```bash
cd /Users/weig/Projects/finance/schwab_cli && git add src/schwab_cli/api/client.py src/schwab_cli/api/quotes.py tests/test_api_quotes.py && git commit -m "feat(api): add get_quotes wrapper + MARKET_BASE constant"
```

---

## Task 7: `output/accounts.py` — renderers

**Files:**
- Create: `src/schwab_cli/output/accounts.py`
- Create: `tests/test_output_accounts.py`

- [ ] **Step 7.1: Create `tests/test_output_accounts.py`**

```python
import json

from schwab_cli.output.accounts import render_account, render_accounts, render_positions
from schwab_cli.output.format import Format


_ACCOUNTS_PAYLOAD = [
    {"securitiesAccount": {
        "accountNumber": "12345678",
        "type": "MARGIN",
        "currentBalances": {"liquidationValue": 12345.67, "cashBalance": 1000.0},
        "positions": [{"instrument": {"symbol": "AAPL"}}],
    }},
    {"securitiesAccount": {
        "accountNumber": "87654321",
        "type": "CASH",
        "currentBalances": {"liquidationValue": 7890.12, "cashBalance": 100.0},
        "positions": [],
    }},
]


def test_render_accounts_json_is_parseable():
    out = render_accounts(_ACCOUNTS_PAYLOAD, Format.JSON)
    data = json.loads(out)
    assert isinstance(data, list)
    assert data[0]["accountNumber"] == "12345678"
    assert data[0]["type"] == "MARGIN"
    assert data[0]["liquidationValue"] == 12345.67


def test_render_accounts_md_has_header_and_rows():
    out = render_accounts(_ACCOUNTS_PAYLOAD, Format.MD)
    lines = out.strip().splitlines()
    # header + separator + 2 data rows
    assert len(lines) >= 4
    assert "|" in lines[0]
    assert "MARGIN" in out
    assert "12345678" in out


def test_render_accounts_human_includes_last_4_mask():
    out = render_accounts(_ACCOUNTS_PAYLOAD, Format.HUMAN)
    # Human renderer returns a string for now (we'll use Console.print in command)
    assert "5678" in out or "...5678" in out


_SINGLE_ACCOUNT = {"securitiesAccount": {
    "accountNumber": "12345678",
    "type": "MARGIN",
    "currentBalances": {
        "liquidationValue": 12345.67,
        "cashBalance": 1000.0,
        "buyingPower": 24691.34,
    },
    "initialBalances": {"cashBalance": 1000.0},
}}


def test_render_account_json_has_all_fields():
    out = render_account(_SINGLE_ACCOUNT, Format.JSON)
    data = json.loads(out)
    assert data["accountNumber"] == "12345678"
    assert data["type"] == "MARGIN"
    assert data["currentBalances"]["buyingPower"] == 24691.34


_POSITION_ROWS = [
    {
        "_account": "12345678",
        "instrument": {"symbol": "AAPL"},
        "longQuantity": 10.0,
        "averagePrice": 200.0,
        "marketValue": 2321.40,
        "currentDayProfitLoss": 4.20,
        "longOpenProfitLoss": 321.40,
    },
    {
        "_account": "87654321",
        "instrument": {"symbol": "MSFT"},
        "longQuantity": 5.0,
        "averagePrice": 400.0,
        "marketValue": 2050.0,
        "currentDayProfitLoss": -10.0,
        "longOpenProfitLoss": 50.0,
    },
]


def test_render_positions_json_shapes_rows():
    out = render_positions(_POSITION_ROWS, Format.JSON)
    data = json.loads(out)
    assert len(data) == 2
    assert data[0]["symbol"] == "AAPL"
    assert data[0]["account"] == "12345678"
    assert data[0]["qty"] == 10.0
    assert data[0]["avgPrice"] == 200.0


def test_render_positions_md_contains_symbols():
    out = render_positions(_POSITION_ROWS, Format.MD)
    assert "AAPL" in out
    assert "MSFT" in out
    assert "|" in out.splitlines()[0]
```

- [ ] **Step 7.2: Verify failing**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest tests/test_output_accounts.py -v
```

Expected: ImportError.

- [ ] **Step 7.3: Create `src/schwab_cli/output/accounts.py`**

```python
from __future__ import annotations

import json as _json
from io import StringIO

from rich.console import Console
from rich.table import Table

from schwab_cli.output.format import Format


def _shape_account(raw: dict) -> dict:
    sec = raw.get("securitiesAccount", {})
    bal = sec.get("currentBalances", {}) or {}
    return {
        "accountNumber": sec.get("accountNumber", ""),
        "type": sec.get("type", ""),
        "liquidationValue": bal.get("liquidationValue"),
        "cashBalance": bal.get("cashBalance"),
        "positionCount": len(sec.get("positions") or []),
    }


def _mask_account(n: str) -> str:
    return f"...{n[-4:]}" if len(n) >= 4 else n


def _fmt_money(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return "—"


def render_accounts(raw_list: list[dict], fmt: Format) -> str:
    rows = [_shape_account(a) for a in raw_list]
    if fmt is Format.JSON:
        return _json.dumps(rows, indent=2)
    if fmt is Format.MD:
        lines = [
            "| Account | Type | Liquidation Value | Cash Balance | Positions |",
            "|---------|------|-------------------|--------------|-----------|",
        ]
        for r in rows:
            lines.append(
                f"| {_mask_account(r['accountNumber'])} | {r['type']} | "
                f"{_fmt_money(r['liquidationValue'])} | "
                f"{_fmt_money(r['cashBalance'])} | {r['positionCount']} |"
            )
        return "\n".join(lines) + "\n"
    # HUMAN
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, no_color=True, width=100)
    t = Table(title="Accounts")
    t.add_column("Account", style="bold")
    t.add_column("Type")
    t.add_column("Liquidation Value", justify="right")
    t.add_column("Cash Balance", justify="right")
    t.add_column("Positions", justify="right")
    for r in rows:
        t.add_row(
            _mask_account(r["accountNumber"]),
            r["type"],
            _fmt_money(r["liquidationValue"]),
            _fmt_money(r["cashBalance"]),
            str(r["positionCount"]),
        )
    console.print(t)
    return buf.getvalue()


def render_account(raw: dict, fmt: Format) -> str:
    sec = raw.get("securitiesAccount", {})
    data = {
        "accountNumber": sec.get("accountNumber", ""),
        "type": sec.get("type", ""),
        "currentBalances": sec.get("currentBalances", {}),
        "initialBalances": sec.get("initialBalances", {}),
        "positionCount": len(sec.get("positions") or []),
    }
    if fmt is Format.JSON:
        return _json.dumps(data, indent=2)
    if fmt is Format.MD:
        bal = data["currentBalances"] or {}
        lines = [
            f"# Account {_mask_account(data['accountNumber'])}",
            "",
            f"- **Number:** {data['accountNumber']}",
            f"- **Type:** {data['type']}",
            f"- **Liquidation Value:** {_fmt_money(bal.get('liquidationValue'))}",
            f"- **Cash Balance:** {_fmt_money(bal.get('cashBalance'))}",
            f"- **Buying Power:** {_fmt_money(bal.get('buyingPower'))}",
            f"- **Positions:** {data['positionCount']}",
        ]
        return "\n".join(lines) + "\n"
    # HUMAN
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, no_color=True, width=100)
    t = Table(title=f"Account {_mask_account(data['accountNumber'])}")
    t.add_column("Field", style="bold")
    t.add_column("Value", justify="right")
    bal = data["currentBalances"] or {}
    t.add_row("Number", data["accountNumber"])
    t.add_row("Type", data["type"])
    t.add_row("Liquidation Value", _fmt_money(bal.get("liquidationValue")))
    t.add_row("Cash Balance", _fmt_money(bal.get("cashBalance")))
    t.add_row("Buying Power", _fmt_money(bal.get("buyingPower")))
    t.add_row("Positions", str(data["positionCount"]))
    console.print(t)
    return buf.getvalue()


def _shape_position(raw: dict) -> dict:
    inst = raw.get("instrument", {}) or {}
    return {
        "account": raw.get("_account", ""),
        "symbol": inst.get("symbol", ""),
        "qty": raw.get("longQuantity") or raw.get("shortQuantity") or 0.0,
        "avgPrice": raw.get("averagePrice"),
        "marketValue": raw.get("marketValue"),
        "dayPnL": raw.get("currentDayProfitLoss"),
        "totalPnL": raw.get("longOpenProfitLoss") or raw.get("shortOpenProfitLoss"),
    }


def render_positions(rows: list[dict], fmt: Format) -> str:
    shaped = [_shape_position(r) for r in rows]
    if fmt is Format.JSON:
        return _json.dumps(shaped, indent=2)
    if fmt is Format.MD:
        lines = [
            "| Account | Symbol | Qty | Avg Price | Market Value | Day P&L | Total P&L |",
            "|---------|--------|-----|-----------|--------------|---------|-----------|",
        ]
        for r in shaped:
            lines.append(
                f"| {_mask_account(r['account'])} | {r['symbol']} | {r['qty']} | "
                f"{_fmt_money(r['avgPrice'])} | {_fmt_money(r['marketValue'])} | "
                f"{_fmt_money(r['dayPnL'])} | {_fmt_money(r['totalPnL'])} |"
            )
        return "\n".join(lines) + "\n"
    # HUMAN
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, no_color=True, width=120)
    t = Table(title="Positions")
    t.add_column("Account")
    t.add_column("Symbol", style="bold")
    t.add_column("Qty", justify="right")
    t.add_column("Avg Price", justify="right")
    t.add_column("Market Value", justify="right")
    t.add_column("Day P&L", justify="right")
    t.add_column("Total P&L", justify="right")
    for r in shaped:
        t.add_row(
            _mask_account(r["account"]),
            r["symbol"],
            f"{r['qty']}",
            _fmt_money(r["avgPrice"]),
            _fmt_money(r["marketValue"]),
            _fmt_money(r["dayPnL"]),
            _fmt_money(r["totalPnL"]),
        )
    console.print(t)
    return buf.getvalue()
```

- [ ] **Step 7.4: Run tests**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest tests/test_output_accounts.py -v
```

Expected: 6 passed.

- [ ] **Step 7.5: Commit**

```bash
cd /Users/weig/Projects/finance/schwab_cli && git add src/schwab_cli/output/accounts.py tests/test_output_accounts.py && git commit -m "feat(output): add account/accounts/positions renderers (human/json/md)"
```

---

## Task 8: `output/quotes.py` — renderer

**Files:**
- Create: `src/schwab_cli/output/quotes.py`
- Create: `tests/test_output_quotes.py`

- [ ] **Step 8.1: Create `tests/test_output_quotes.py`**

```python
import json

from schwab_cli.output.format import Format
from schwab_cli.output.quotes import render_quotes


_QUOTES_PAYLOAD = {
    "AAPL": {
        "symbol": "AAPL",
        "quote": {
            "lastPrice": 232.14,
            "bidPrice": 232.13,
            "askPrice": 232.15,
            "netChange": 0.42,
            "netPercentChangeInDouble": 0.18,
            "totalVolume": 1234567,
        },
    },
    "errors": {"invalidSymbols": ["ZZZZZ"]},
}


def test_render_quotes_json_includes_all_symbols():
    out = render_quotes(["AAPL", "ZZZZZ"], _QUOTES_PAYLOAD, Format.JSON)
    data = json.loads(out)
    symbols = [row["symbol"] for row in data]
    assert "AAPL" in symbols
    assert "ZZZZZ" in symbols


def test_render_quotes_json_marks_invalid():
    out = render_quotes(["AAPL", "ZZZZZ"], _QUOTES_PAYLOAD, Format.JSON)
    data = json.loads(out)
    zz = next(r for r in data if r["symbol"] == "ZZZZZ")
    assert zz["last"] is None
    assert zz.get("error") == "invalid symbol"


def test_render_quotes_md_includes_invalid_row():
    out = render_quotes(["AAPL", "ZZZZZ"], _QUOTES_PAYLOAD, Format.MD)
    assert "AAPL" in out
    assert "ZZZZZ" in out
    assert "—" in out


def test_render_quotes_human_table():
    out = render_quotes(["AAPL"], _QUOTES_PAYLOAD, Format.HUMAN)
    assert "AAPL" in out
    assert "232.14" in out
```

- [ ] **Step 8.2: Verify failing**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest tests/test_output_quotes.py -v
```

Expected: ImportError.

- [ ] **Step 8.3: Create `src/schwab_cli/output/quotes.py`**

```python
from __future__ import annotations

import json as _json
from io import StringIO

from rich.console import Console
from rich.table import Table

from schwab_cli.output.format import Format


def _fmt_num(v, decimals: int = 2) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{decimals}f}"
    except (TypeError, ValueError):
        return "—"


def _shape_row(symbol: str, payload: dict, invalid: set[str]) -> dict:
    if symbol in invalid:
        return {
            "symbol": symbol,
            "last": None,
            "change": None,
            "changePct": None,
            "bid": None,
            "ask": None,
            "volume": None,
            "error": "invalid symbol",
        }
    entry = payload.get(symbol) or {}
    q = entry.get("quote") or {}
    return {
        "symbol": symbol,
        "last": q.get("lastPrice"),
        "change": q.get("netChange"),
        "changePct": q.get("netPercentChangeInDouble") or q.get("netPercentChange"),
        "bid": q.get("bidPrice"),
        "ask": q.get("askPrice"),
        "volume": q.get("totalVolume"),
    }


def render_quotes(symbols: list[str], payload: dict, fmt: Format) -> str:
    invalid = set((payload.get("errors") or {}).get("invalidSymbols") or [])
    rows = [_shape_row(s, payload, invalid) for s in symbols]

    if fmt is Format.JSON:
        return _json.dumps(rows, indent=2)
    if fmt is Format.MD:
        lines = [
            "| Symbol | Last | Change | Change% | Bid | Ask | Volume |",
            "|--------|------|--------|---------|-----|-----|--------|",
        ]
        for r in rows:
            lines.append(
                f"| {r['symbol']} | {_fmt_num(r['last'])} | {_fmt_num(r['change'])} | "
                f"{_fmt_num(r['changePct'])} | {_fmt_num(r['bid'])} | "
                f"{_fmt_num(r['ask'])} | {_fmt_num(r['volume'], 0)} |"
            )
        return "\n".join(lines) + "\n"
    # HUMAN
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, no_color=True, width=100)
    t = Table(title="Quotes")
    t.add_column("Symbol", style="bold")
    t.add_column("Last", justify="right")
    t.add_column("Change", justify="right")
    t.add_column("Change%", justify="right")
    t.add_column("Bid", justify="right")
    t.add_column("Ask", justify="right")
    t.add_column("Volume", justify="right")
    for r in rows:
        t.add_row(
            r["symbol"],
            _fmt_num(r["last"]),
            _fmt_num(r["change"]),
            _fmt_num(r["changePct"]),
            _fmt_num(r["bid"]),
            _fmt_num(r["ask"]),
            _fmt_num(r["volume"], 0),
        )
    console.print(t)
    return buf.getvalue()
```

- [ ] **Step 8.4: Run tests**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest tests/test_output_quotes.py -v
```

Expected: 4 passed.

- [ ] **Step 8.5: Commit**

```bash
cd /Users/weig/Projects/finance/schwab_cli && git add src/schwab_cli/output/quotes.py tests/test_output_quotes.py && git commit -m "feat(output): add quotes renderer with invalid-symbol handling"
```

---

## Task 9: `commands/accounts.py` — CLI wiring + `accounts` subcommand

**Files:**
- Create: `src/schwab_cli/commands/accounts.py`
- Modify: `src/schwab_cli/cli.py` (register subcommand)
- Create: `tests/test_commands_accounts.py`

- [ ] **Step 9.1: Create `tests/test_commands_accounts.py`**

```python
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from schwab_cli.api.client import ApiError, SessionExpired
from schwab_cli.cli import app
from schwab_cli.config import Config
from schwab_cli.config import save as save_config
from schwab_cli.session import Session
from schwab_cli.session import save as save_session

runner = CliRunner()


def _prep(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save_config(Config(
        client_id="cid",
        client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
    ))
    save_session(Session(
        access_token="atok", refresh_token="rtok",
        expires_at=1_000_000, refresh_token_expires_at=2_000_000,
    ))


_ACCOUNTS = [
    {"securitiesAccount": {
        "accountNumber": "12345678", "type": "MARGIN",
        "currentBalances": {"liquidationValue": 1000.0, "cashBalance": 500.0},
        "positions": [],
    }},
]


def test_accounts_no_session_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save_config(Config(
        client_id="cid", client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
    ))
    # No session saved
    result = runner.invoke(app, ["accounts"])
    assert result.exit_code == 1
    assert "No session found" in result.output


def test_accounts_no_config_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    # No config or session
    result = runner.invoke(app, ["accounts"])
    assert result.exit_code == 1
    assert "No config" in result.output or "No session" in result.output


def test_accounts_happy_path_human(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.commands.accounts.list_accounts", return_value=_ACCOUNTS):
        result = runner.invoke(app, ["accounts"])
    assert result.exit_code == 0, result.output
    assert "12345678" in result.output or "5678" in result.output
    assert "MARGIN" in result.output


def test_accounts_json_flag_outputs_json(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.commands.accounts.list_accounts", return_value=_ACCOUNTS):
        result = runner.invoke(app, ["accounts", "--json"])
    assert result.exit_code == 0
    import json
    data = json.loads(result.stdout)
    assert data[0]["accountNumber"] == "12345678"


def test_accounts_md_flag_outputs_md(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.commands.accounts.list_accounts", return_value=_ACCOUNTS):
        result = runner.invoke(app, ["accounts", "--md"])
    assert result.exit_code == 0
    assert "| Account" in result.stdout


def test_accounts_both_flags_errors(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["accounts", "--json", "--md"])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_accounts_session_expired_message(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.list_accounts",
        side_effect=SessionExpired("Session expired. Run `schwab_cli auth --force`."),
    ):
        result = runner.invoke(app, ["accounts"])
    assert result.exit_code == 1
    assert "Session expired" in result.output


def test_accounts_api_error_surfaces(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.accounts.list_accounts",
        side_effect=ApiError("500 internal server error"),
    ):
        result = runner.invoke(app, ["accounts"])
    assert result.exit_code == 1
    assert "500" in result.output
```

- [ ] **Step 9.2: Verify failing**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest tests/test_commands_accounts.py -v
```

Expected: import error / "No such command 'accounts'".

- [ ] **Step 9.3: Create `src/schwab_cli/commands/accounts.py`**

```python
from __future__ import annotations

import typer

from schwab_cli import config as config_module
from schwab_cli.api.accounts import get_account, get_positions, list_accounts
from schwab_cli.api.client import ApiError, SchwabClient, SessionExpired
from schwab_cli.output.accounts import (
    render_account,
    render_accounts,
    render_positions,
)
from schwab_cli.output.format import FormatError, pick_format
from schwab_cli.session import load as load_session


def _client() -> SchwabClient:
    cfg = config_module.load()
    if cfg is None:
        typer.secho(
            "No config found. Run `schwab_cli setup` first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    session = load_session()
    if session is None:
        typer.secho(
            "No session found. Run `schwab_cli auth` first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    return SchwabClient(cfg, session)


def _resolve_format(json: bool, md: bool):
    try:
        return pick_format(json, md)
    except FormatError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)


def _handle_api_error(e: Exception) -> None:
    msg = str(e) if str(e) else type(e).__name__
    typer.secho(msg, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def run_list(*, as_json: bool, as_md: bool) -> None:
    fmt = _resolve_format(as_json, as_md)
    client = _client()
    try:
        data = list_accounts(client)
    except (ApiError, SessionExpired) as e:
        _handle_api_error(e)
    typer.echo(render_accounts(data, fmt))


def run_show(account_number: str, *, as_json: bool, as_md: bool) -> None:
    fmt = _resolve_format(as_json, as_md)
    client = _client()
    try:
        data = get_account(client, account_number)
    except (ApiError, SessionExpired) as e:
        _handle_api_error(e)
    typer.echo(render_account(data, fmt))


def run_positions(account_number: str | None, *, as_json: bool, as_md: bool) -> None:
    fmt = _resolve_format(as_json, as_md)
    client = _client()
    try:
        rows = get_positions(client, account_number)
    except (ApiError, SessionExpired) as e:
        _handle_api_error(e)
    typer.echo(render_positions(rows, fmt))
```

- [ ] **Step 9.4: Register subcommands in `src/schwab_cli/cli.py`**

Open `src/schwab_cli/cli.py` and add these imports near the existing ones:

```python
from schwab_cli.commands import accounts as accounts_cmd
```

Then append these commands after the existing `auth` command definition:

```python
@app.command("accounts", help="List Schwab accounts.")
def accounts(
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
    as_md: bool = typer.Option(False, "--md", help="Output GitHub-flavored markdown."),
) -> None:
    accounts_cmd.run_list(as_json=as_json, as_md=as_md)


@app.command("account", help="Show one Schwab account by number (or suffix).")
def account(
    account_number: str = typer.Argument(..., help="Full number or last-N-digit suffix."),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
    as_md: bool = typer.Option(False, "--md", help="Output GitHub-flavored markdown."),
) -> None:
    accounts_cmd.run_show(account_number, as_json=as_json, as_md=as_md)


@app.command("positions", help="List positions across accounts (or one account).")
def positions(
    account_number: str = typer.Argument(None, help="Optional account number or suffix."),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
    as_md: bool = typer.Option(False, "--md", help="Output GitHub-flavored markdown."),
) -> None:
    accounts_cmd.run_positions(account_number, as_json=as_json, as_md=as_md)
```

- [ ] **Step 9.5: Run tests**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest tests/test_commands_accounts.py -v
```

Expected: 8 passed.

- [ ] **Step 9.6: Commit**

```bash
cd /Users/weig/Projects/finance/schwab_cli && git add src/schwab_cli/commands/accounts.py src/schwab_cli/cli.py tests/test_commands_accounts.py && git commit -m "feat(commands): add accounts/account/positions subcommands"
```

---

## Task 10: `commands/quote.py` — `quote` subcommand

**Files:**
- Create: `src/schwab_cli/commands/quote.py`
- Modify: `src/schwab_cli/cli.py` (register subcommand)
- Create: `tests/test_commands_quote.py`

- [ ] **Step 10.1: Create `tests/test_commands_quote.py`**

```python
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from schwab_cli.api.client import ApiError, SessionExpired
from schwab_cli.cli import app
from schwab_cli.config import Config
from schwab_cli.config import save as save_config
from schwab_cli.session import Session
from schwab_cli.session import save as save_session

runner = CliRunner()


def _prep(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save_config(Config(
        client_id="cid", client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
    ))
    save_session(Session(
        access_token="atok", refresh_token="rtok",
        expires_at=1_000_000, refresh_token_expires_at=2_000_000,
    ))


_QUOTES = {
    "AAPL": {"symbol": "AAPL", "quote": {"lastPrice": 232.14}},
}


def test_quote_happy_human(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.commands.quote.get_quotes", return_value=_QUOTES):
        result = runner.invoke(app, ["quote", "AAPL"])
    assert result.exit_code == 0, result.output
    assert "AAPL" in result.output
    assert "232.14" in result.output


def test_quote_multi_symbol(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    payload = {
        "AAPL": {"symbol": "AAPL", "quote": {"lastPrice": 232.14}},
        "MSFT": {"symbol": "MSFT", "quote": {"lastPrice": 451.22}},
    }
    with patch("schwab_cli.commands.quote.get_quotes", return_value=payload):
        result = runner.invoke(app, ["quote", "AAPL", "MSFT"])
    assert result.exit_code == 0
    assert "AAPL" in result.output
    assert "MSFT" in result.output


def test_quote_json_output(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.commands.quote.get_quotes", return_value=_QUOTES):
        result = runner.invoke(app, ["quote", "AAPL", "--json"])
    assert result.exit_code == 0
    import json
    data = json.loads(result.stdout)
    assert data[0]["symbol"] == "AAPL"


def test_quote_both_flags_errors(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["quote", "AAPL", "--json", "--md"])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_quote_no_symbols_errors(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["quote"])
    # typer reports missing required argument → exit 2
    assert result.exit_code != 0


def test_quote_session_expired_message(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.quote.get_quotes",
        side_effect=SessionExpired("Session expired. Run `schwab_cli auth --force`."),
    ):
        result = runner.invoke(app, ["quote", "AAPL"])
    assert result.exit_code == 1
    assert "Session expired" in result.output
```

- [ ] **Step 10.2: Verify failing**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest tests/test_commands_quote.py -v
```

Expected: "No such command 'quote'".

- [ ] **Step 10.3: Create `src/schwab_cli/commands/quote.py`**

```python
from __future__ import annotations

import typer

from schwab_cli import config as config_module
from schwab_cli.api.client import ApiError, SchwabClient, SessionExpired
from schwab_cli.api.quotes import get_quotes
from schwab_cli.output.format import FormatError, pick_format
from schwab_cli.output.quotes import render_quotes
from schwab_cli.session import load as load_session


def _client() -> SchwabClient:
    cfg = config_module.load()
    if cfg is None:
        typer.secho(
            "No config found. Run `schwab_cli setup` first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    session = load_session()
    if session is None:
        typer.secho(
            "No session found. Run `schwab_cli auth` first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    return SchwabClient(cfg, session)


def run(symbols: list[str], *, as_json: bool, as_md: bool) -> None:
    try:
        fmt = pick_format(as_json, as_md)
    except FormatError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    client = _client()
    try:
        payload = get_quotes(client, symbols)
    except (ApiError, SessionExpired) as e:
        msg = str(e) if str(e) else type(e).__name__
        typer.secho(msg, fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    typer.echo(render_quotes(symbols, payload, fmt))
```

- [ ] **Step 10.4: Register subcommand in `src/schwab_cli/cli.py`**

Add to the imports:

```python
from schwab_cli.commands import quote as quote_cmd
```

Append after the accounts-related commands:

```python
@app.command("quote", help="Get real-time quotes for one or more symbols.")
def quote(
    symbols: list[str] = typer.Argument(..., help="One or more ticker symbols."),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
    as_md: bool = typer.Option(False, "--md", help="Output GitHub-flavored markdown."),
) -> None:
    quote_cmd.run(symbols, as_json=as_json, as_md=as_md)
```

- [ ] **Step 10.5: Run tests**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest tests/test_commands_quote.py -v
```

Expected: 6 passed.

- [ ] **Step 10.6: Commit**

```bash
cd /Users/weig/Projects/finance/schwab_cli && git add src/schwab_cli/commands/quote.py src/schwab_cli/cli.py tests/test_commands_quote.py && git commit -m "feat(commands): add quote subcommand"
```

---

## Task 11: README update + final verification

**Files:**
- Modify: `README.md`

- [ ] **Step 11.1: Append new command documentation**

Open `README.md` and insert a new section after the existing "Authenticate" section (before "Run tests"):

```markdown
## Data commands

Once authenticated, read-only data commands are available:

```bash
schwab_cli accounts                  # all accounts, total value, position count
schwab_cli account 1234              # one account (suffix or full number)
schwab_cli positions                 # positions across all accounts
schwab_cli positions 5678            # positions for one account
schwab_cli quote AAPL                # one quote
schwab_cli quote AAPL MSFT NVDA      # multi-symbol quote
```

Output formats:

```bash
schwab_cli accounts --json           # JSON for scripting (| jq)
schwab_cli accounts --md             # GitHub-flavored markdown for LLM context
schwab_cli accounts                  # human-readable rich table (default)
```

`--json` and `--md` are mutually exclusive.

The first HTTP 401 from Schwab's API triggers an automatic token refresh and a
single retry — no user action needed as long as the 7-day refresh token is
still valid. After it expires, re-run `schwab_cli auth --force`.
```

- [ ] **Step 11.2: Run full suite**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest -q
```

Expected: all tests pass. New count ≈ 100 (prior) + 20 (skipped) + 5 (format) + 7 (client base) + 5 (client resolve) + 5 (accounts) + 4 (quotes api) + 6 (output accounts) + 4 (output quotes) + 8 (cmd accounts) + 6 (cmd quote) = ~150 pass / 20 skipped.

Verify coverage ≥80% on each new module via:

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run pytest --cov=src/schwab_cli/api --cov=src/schwab_cli/output --cov=src/schwab_cli/commands --cov-report=term-missing
```

- [ ] **Step 11.3: Verify CLI surface**

```bash
cd /Users/weig/Projects/finance/schwab_cli && uv run schwab_cli --help
cd /Users/weig/Projects/finance/schwab_cli && uv run schwab_cli accounts --help
cd /Users/weig/Projects/finance/schwab_cli && uv run schwab_cli account --help
cd /Users/weig/Projects/finance/schwab_cli && uv run schwab_cli positions --help
cd /Users/weig/Projects/finance/schwab_cli && uv run schwab_cli quote --help
```

Expected: each exits 0 and prints a help message listing the right options. Top-level `--help` lists `setup`, `auth`, `accounts`, `account`, `positions`, `quote`.

- [ ] **Step 11.4: Manual smoke test with real Schwab**

Run these live; they need a valid session:

```bash
uv run schwab_cli auth                     # ensure session valid (refresh path)
uv run schwab_cli accounts                 # human table
uv run schwab_cli accounts --json | jq .   # JSON → jq
uv run schwab_cli accounts --md            # markdown
uv run schwab_cli positions
uv run schwab_cli quote AAPL MSFT NVDA
uv run schwab_cli account <last4>          # replace with your real suffix
```

Each should succeed and display your real account / quote data.

- [ ] **Step 11.5: Commit**

```bash
cd /Users/weig/Projects/finance/schwab_cli && git add README.md && git commit -m "docs: document accounts / account / positions / quote commands"
```
