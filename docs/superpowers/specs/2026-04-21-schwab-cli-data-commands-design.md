# Schwab CLI — Account + Market Data Commands (Phase 3)

**Date:** 2026-04-21
**Status:** Approved (pending user spec review)
**Scope:** Add read-only commands for account data and market quotes on top of the existing auth infrastructure (Phase 1 `setup`, Phase 2 `auth`).

## Goal

Turn the authenticated session into useful commands. After `schwab_cli auth` produces a valid `session.json`, users can:

- List their Schwab accounts and balances.
- Inspect a single account in detail.
- List positions across all accounts or for a specific one.
- Get real-time quotes for one or more symbols.

All commands render human-readable output by default, with `--json` and `--md` flags for scriptable / LLM-context output.

## Non-Goals

Listed as TODO for future milestones; not implemented in this phase:

- **C — Transaction/order history** (`history`, `orders list`, `order show <id>`).
- **D — Trading** (`trade buy/sell/cancel/modify`). Requires thinkorswim enablement and significantly more testing discipline.
- **E — Agent context dumps** (`brief`, `context`). Structured JSON snapshots of portfolio state for LLM consumption.
- **F — ML advisor / recommendation engine.** Its own project if/when it happens.

The API layer introduced here is shaped so that C, D, and E can be added without refactoring — each just needs a new endpoint wrapper in `api/` and a new command in `commands/`.

## Decisions Summary

| Decision | Choice |
|---|---|
| API client | Hand-rolled `httpx` (same as Phase 1/2 OAuth) — **not** `schwab-py` |
| Endpoint surface | Only what A+B need: 4 Schwab REST endpoints |
| Auth integration | Reuse `session.json` + `oauth.refresh()` from Phase 2 |
| Token expiry handling | Auto-refresh on 401, retry once; second 401 → user-facing "run `schwab_cli auth`" error |
| Account identifier UX | User types account number or suffix (last 4+ digits); CLI maps to Schwab's `hashValue` transparently |
| Default output | Rich-formatted human-readable tables |
| `--json` flag | Raw JSON (Schwab response lightly normalized) |
| `--md` flag | GitHub-flavored markdown table |
| `--json` + `--md` together | Error "mutually exclusive", exit 2 |
| Errors | Always to stderr; stdout remains clean for JSON/MD piping |
| New dependencies | `rich` for the default human-readable renderer |
| Schwab-py | Not added; revisit only if we later need 20+ endpoints or streaming |

## Commands

### `schwab_cli accounts`

List all of the user's accounts.

**Columns:** account number (masked — show last 4), type (CASH/MARGIN), total equity value, cash balance, position count.

**Implementation notes:**
- Uses `GET /trader/v1/accounts?fields=positions` (one request with everything, better than N+1).
- Rich table by default; `--json` / `--md` alternatives.

### `schwab_cli account <account_number>`

Show detailed info for one account.

**Argument:** `<account_number>` — real account number or a suffix that unambiguously matches one (e.g. the last 4 digits). If the suffix matches multiple, error with the list of candidates.

**Output includes:** account number (full), type, current balances (liquidation value, cash available to trade, buying power), initial balances.

**Implementation notes:** uses the cached `accountNumber → hashValue` mapping from `client.py`.

### `schwab_cli positions [<account_number>]`

List positions. If no argument, aggregates across all accounts. If an argument is given, filters to that account.

**Columns:** account (last 4), symbol, qty, avg price, current price, day P&L $, total P&L $, total P&L %.

**Implementation notes:** same data source as `accounts` (`GET /trader/v1/accounts?fields=positions`). Filter on the client side.

### `schwab_cli quote <symbol> [<symbol>...]`

Real-time quotes for one or more symbols.

**Columns:** symbol, last price, day change $, day change %, bid, ask, day volume.

**Implementation notes:** `GET /marketdata/v1/quotes?symbols=AAPL,MSFT` — one call for N symbols. Unknown symbols are rendered as rows with `—` values plus a stderr note ("Symbol XYZ not recognized by Schwab"); Schwab returns 200 with per-symbol error metadata rather than 4xx.

## Project Layout

```
src/schwab_cli/
├── api/                          NEW — pure API layer
│   ├── __init__.py
│   ├── client.py                 SchwabClient, ApiError
│   ├── accounts.py               list_accounts, get_account, get_positions
│   └── quotes.py                 get_quotes
├── output/                       NEW — formatters
│   ├── __init__.py
│   ├── format.py                 Format enum, pick_format(json, md), mutex check
│   ├── accounts.py               render_accounts / render_account / render_positions
│   └── quotes.py                 render_quotes
├── commands/
│   ├── accounts.py               NEW — accounts / account / positions subcommands
│   └── quote.py                  NEW — quote subcommand
└── cli.py                        MODIFIED — register the 4 subcommands

tests/
├── test_api_client.py            NEW — respx-mocked
├── test_api_accounts.py          NEW
├── test_api_quotes.py            NEW
├── test_output_format.py         NEW — pick_format + mutex
├── test_output_accounts.py       NEW
├── test_output_quotes.py         NEW
├── test_commands_accounts.py     NEW — CliRunner
└── test_commands_quote.py        NEW
```

**New dependencies** (`pyproject.toml`):
- Runtime: `rich>=13.7`
- No new dev deps (existing `respx`, `pytest`, `pytest-cov` cover all new tests).

## API Layer

### `SchwabClient` (`src/schwab_cli/api/client.py`)

```python
class ApiError(Exception):
    """Raised on Schwab API errors after auth handling."""


class SessionExpired(ApiError):
    """Refresh failed. User must re-auth interactively."""


@dataclass(frozen=True)
class _AccountIds:
    """Mapping between user-facing account_number and Schwab's hashValue."""
    account_number: str
    hash_value: str


class SchwabClient:
    TRADER_BASE = "https://api.schwabapi.com/trader/v1"
    MARKET_BASE = "https://api.schwabapi.com/marketdata/v1"

    def __init__(self, cfg: Config, session: Session) -> None:
        self._cfg = cfg
        self._session = session
        self._account_ids: list[_AccountIds] | None = None  # lazy cache

    def get(self, url: str, *, params: dict | None = None) -> dict | list:
        """Authed GET with single auto-refresh on 401."""
        ...

    def resolve_account(self, user_input: str) -> _AccountIds:
        """Translate user account-number or suffix to account_number + hash_value.

        Raises ApiError on no-match or multi-match (ambiguous suffix).
        """
        ...

    # All endpoint wrappers call self.get() underneath.
```

**Auto-refresh flow** in `get()`:

1. `httpx.get(url, headers={"Authorization": f"Bearer {self._session.access_token}"})`
2. If 401:
   a. Call `oauth.refresh(self._cfg, self._session.refresh_token)` — may itself 401.
   b. On refresh failure → raise `SessionExpired`.
   c. On success → build new `Session`, persist via `session.save()`, update `self._session`.
   d. Retry the original request once.
3. If still 401 → raise `SessionExpired`.
4. Other 4xx/5xx → raise `ApiError` with status + body summary.
5. Network errors → raise `ApiError("network: <ExcType>")`.

### Endpoint wrappers

`api/accounts.py`:
- `list_accounts(client) -> list[dict]` — hits `/accounts?fields=positions`, returns the array.
- `get_account(client, account_number) -> dict` — resolves, hits `/accounts/{hash}?fields=positions`.
- `get_positions(client, account_number=None) -> list[dict]` — uses same call; filters/aggregates client-side.

`api/quotes.py`:
- `get_quotes(client, symbols: list[str]) -> dict[str, dict]` — hits `/marketdata/v1/quotes?symbols=...`. Schwab returns a dict keyed by symbol; pass through.

## Output Layer

### `output/format.py`

```python
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

### Renderers

Each renderer takes the API data plus a `Format` and returns a string (for JSON / MD) or prints directly via `rich.Console` (for HUMAN). Rationale: `rich` handles terminal detection / colors, so the human path uses it directly; JSON / MD are plain strings so they pipe cleanly.

**Contract:**
- Human output → `rich.Console().print(table)` on `sys.stdout`.
- JSON output → `json.dumps(data, indent=2)` → print on stdout.
- MD output → GitHub-flavored markdown table → print on stdout.
- Error messages (e.g., unknown-symbol note, account-not-found) → always via `typer.secho(..., err=True)`.

## Error Handling Summary

| Failure | Where | User-facing message | Exit |
|---|---|---|---|
| `session.json` missing | command entry | "No session found. Run `schwab_cli auth`." | 1 |
| Access token rejected, refresh succeeds | client.get() | (silent — retry) | — |
| Refresh token rejected | client.get() | "Session expired. Run `schwab_cli auth --force`." | 1 |
| Network error | client.get() | "Network error: <ExcType>" | 1 |
| HTTP 4xx (not 401) | client.get() | "Schwab API rejected request: <status> <body_first_line>" | 1 |
| HTTP 5xx | client.get() | "Schwab server error: <status>. Try again later." | 1 |
| Account number / suffix matches 0 | resolve_account | "Account '<input>' not found. Available: <list of last-4>." | 1 |
| Account suffix matches ≥2 | resolve_account | "Multiple accounts match '<suffix>': <list>. Specify more digits." | 1 |
| Unknown symbol | quote render | Row with `—` values + stderr note; rest of output succeeds | 0 |
| `--json` and `--md` together | format picker | "--json and --md are mutually exclusive." | 2 |

## Testing Strategy

Framework: `pytest`. Coverage target: ≥80% per project rules.

### Unit tests (pure, fast)

- `test_api_client.py` — respx-mocked. 401→refresh→retry happy path, 401→refresh-fails→SessionExpired, account-number→hashValue translation + caching, 500 maps to ApiError, network errors bubble correctly.
- `test_api_accounts.py` — respx-mocked. `list_accounts` shape; `get_account` by full number and by suffix; `get_positions` with and without account filter.
- `test_api_quotes.py` — respx-mocked. Single / multi symbol; unknown-symbol passthrough.
- `test_output_format.py` — `pick_format` returns correct enum; mutex raises with clear text.
- `test_output_accounts.py` — feed fake API data, assert shape of human / json / md outputs.
- `test_output_quotes.py` — same pattern; unknown-symbol row.
- `test_commands_accounts.py` — `CliRunner` end-to-end with mocked `SchwabClient`. Exit codes for no-session, session-expired, unknown-account, normal runs. `--json` / `--md` routing.
- `test_commands_quote.py` — same pattern.

### Manual smoke (after tests pass)

1. `uv run schwab_cli accounts` → real data.
2. `uv run schwab_cli positions` → real positions across accounts.
3. `uv run schwab_cli quote AAPL MSFT NVDA` → 3 rows.
4. `uv run schwab_cli account <last4>` → one account's detail.
5. `uv run schwab_cli accounts --json` piped into `jq '.[].accountNumber'` works.
6. `uv run schwab_cli quote AAPL --md` produces copy-pasteable markdown.

## Future Work (deferred)

When these land, each slots in without API-layer refactoring:

- **C — History / orders.** `api/history.py` + `commands/history.py`. Endpoints `/accounts/{hash}/transactions`, `/accounts/{hash}/orders`.
- **D — Trading.** `api/trade.py` + `commands/trade.py`. Endpoints `POST/PUT/DELETE /accounts/{hash}/orders`. Needs thinkorswim enablement check.
- **E — Brief / context dumps.** `commands/brief.py`, `commands/context.py`. No new API calls — composes existing data into an LLM-friendly JSON blob.
- **F — Advisor / ML recommendations.** Its own package/plugin; pulls from the same API layer.

If a future phase needs 20+ endpoints or streaming (Level II quotes, account subscribers), revisit the decision to hand-roll and consider adding `schwab-py` or similar as the underlying client.
