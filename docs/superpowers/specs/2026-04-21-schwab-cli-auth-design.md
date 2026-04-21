# Schwab CLI — `auth` Command Design

**Date:** 2026-04-21
**Status:** Approved (pending user spec review)
**Scope:** Second milestone of the Schwab CLI — the `auth` subcommand. Reuses `Config` from the `setup` milestone; introduces session persistence, OAuth token exchange, browser-automated login, and 1Password secret resolution.

## Goal

Provide a `schwab_cli auth [--force]` command that obtains a Schwab API access token by either:
1. Exchanging an existing refresh token for a fresh access token (fast path), or
2. Driving Schwab's OAuth2 authorization-code flow in an automated Playwright browser (full auth).

Tokens are persisted to `~/.config/schwab_cli/session.json` with strict permissions, ready for use by downstream commands.

## Non-Goals

- Using Schwab's REST API beyond the OAuth endpoints.
- Trading, quoting, or any business features — those build on `auth` later.
- Handling MFA (Schwab's OAuth portal does not present MFA in the automated flow for typical personal accounts; if it appears, the flow will hit the "UI may have changed" timeout path).
- Supporting multiple Schwab identities per host.
- A GUI config editor.

## Decisions Summary

| Decision | Choice |
|---|---|
| Token exchange | Hand-rolled `httpx` POST to Schwab's token endpoint |
| Session storage | `~/.config/schwab_cli/session.json`, mode `0o600` |
| Session fields | `access_token`, `refresh_token`, `expires_at`, `refresh_token_expires_at`, `version` |
| Refresh token lifetime | Assumed 7 days (Schwab standard); computed at save time |
| Fast path | Always attempt refresh on startup (if session exists, no `--force`). Success → "Already logged in"; failure → full auth |
| Full auth driver | Playwright Chromium |
| Chromium install | Documented in README; runtime-missing → one-liner suggestion, no auto-install |
| Headless toggle | `headless = not _is_debug_truthy(os.environ.get("DEBUG"))`; truthy = `true`/`yes`/`1` case-insensitive |
| Password resolution | `op://` → `subprocess.run(["op", "read", <path>])`; literal → passthrough |
| Error handling | Every failure closes the browser, prints a single-line reason, exits 1 |
| UI-change handling | Per-step timeouts (15s). On timeout: "auth incomplete — Schwab may have changed" with selector-file pointer |
| Debug artifact | When DEBUG is truthy, screenshot on failure to `~/.config/schwab_cli/auth-error-<ts>.png` |

## Project Layout Additions

```
src/schwab_cli/
├── cli.py                    # (modified) register `auth` subcommand
├── config.py                 # (unchanged)
├── session.py                # NEW — Session dataclass + I/O
├── oauth.py                  # NEW — hand-rolled OAuth: build_auth_url, exchange_code, refresh
├── secrets.py                # NEW — resolve_secret (literal or op://)
├── browser/
│   ├── __init__.py           # NEW (empty)
│   ├── flow.py               # NEW — Playwright orchestration
│   └── selectors.py          # NEW — centralized selectors + error markers
└── commands/
    └── auth.py               # NEW — auth command (load config, try refresh, else full auth)

tests/
├── test_session.py           # NEW
├── test_oauth.py             # NEW
├── test_secrets.py           # NEW
├── test_auth_command.py      # NEW (oauth + browser.flow mocked)
└── test_browser_flow.py      # NEW (Playwright Page stubbed)
```

**New dependencies** (`pyproject.toml`):
- Runtime: `playwright>=1.45`, `httpx>=0.27`
- Dev: `respx>=0.21` (httpx mocks), `pytest-asyncio>=0.23` (for async Playwright tests if needed)

## Data Models

### `Session`

File: `~/.config/schwab_cli/session.json`

```json
{
  "version": 1,
  "access_token": "...",
  "refresh_token": "...",
  "expires_at": 1745251200,
  "refresh_token_expires_at": 1745856000
}
```

```python
@dataclass(frozen=True)
class Session:
    access_token: str
    refresh_token: str
    expires_at: int
    refresh_token_expires_at: int
    version: int = 1
```

Helpers in `session.py`:
- `session_path() -> Path` — XDG-aware, returns `~/.config/schwab_cli/session.json` by default.
- `Session.load() -> Session | None` — None when missing; raises `SessionError` on malformed JSON, unsupported version, or missing required fields (same rules as `config.load()`).
- `Session.save(s)` — atomic write with `0o600` file and `0o700` parent dir (mirrors `config.save()`).
- `Session.from_token_response(tr, now)` — builds a Session with `expires_at = now + tr.expires_in` and `refresh_token_expires_at = now + 7*24*3600`.

### `TokenResponse`

```python
@dataclass(frozen=True)
class TokenResponse:
    access_token: str
    refresh_token: str
    expires_in: int   # seconds

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
```

## Command Flow

### `schwab_cli auth [--force]`

```
1. cfg = config.load()
   if cfg is None:
       print "No config found. Run `schwab_cli setup` first."
       exit 1

2. if not --force:
       session = Session.load()           # None when missing
       if session is not None:
           try:
               tr = oauth.refresh(cfg, session.refresh_token)
               new_session = Session.from_token_response(tr, now=time.time())
               Session.save(new_session)
               print f"Already logged in. Access token valid until <ISO expires_at>."
               exit 0
           except (httpx.HTTPStatusError, httpx.RequestError, OAuthError) as e:
               # _summarize_error returns a one-line human-readable reason:
               #   HTTPStatusError → "<status> <body_first_line>"
               #   RequestError    → "network: <type(e).__name__>"
               #   OAuthError      → str(e)
               print f"Refresh token rejected ({_summarize_error(e)}); doing full auth."
               # fall through

3. Full auth (see below).
```

### Full auth

Executed by `browser.flow.run_full_auth(cfg: Config) -> TokenResponse`:

```
3.1 username = secrets.resolve_secret(cfg.username)
    password = secrets.resolve_secret(cfg.password)
    # Either literal or op://; op missing / op error raises SecretError

3.2 auth_url = oauth.build_auth_url(cfg)

3.3 headless = not _is_debug_truthy(os.environ.get("DEBUG"))
    # _is_debug_truthy(v): True iff v is a string equal to "true", "yes", or "1"
    # (case-insensitive). None and empty string → False.

3.4 Launch Playwright Chromium.
    Except BrowserType.launch error → print one-liner:
      "Chromium not found. Run: `uv run playwright install chromium`"
    → exit 1

3.5 page.goto(auth_url)
    wait_any(
        expected=LOGIN_USERNAME_SELECTOR,
        known_errors={INVALID_CLIENT_MARKERS: "Schwab rejected client_id/secret — verify setup."},
        timeout=15s,
    )

3.6 page.fill(LOGIN_USERNAME_SELECTOR, username)
    page.fill(LOGIN_PASSWORD_SELECTOR, password)
    page.click(LOGIN_SUBMIT_SELECTOR)
    wait_any(
        expected=CONSENT_PAGE_SELECTOR,
        known_errors={
            "Invalid login ID or password.": "Login failed — incorrect username/password.",
            "We are unable to complete your request.": "Redirect URI mismatch — re-check setup.",
        },
        timeout=15s,
    )

3.7 Consent page: scroll to bottom, click ACCEPT_SELECTOR.
    wait_any(
        expected=ACCOUNT_SELECTION_SELECTOR,
        known_errors={},   # no documented errors here
        timeout=15s,
    )

3.8 Account selection:
    checkboxes = page.query_selector_all(ACCOUNT_CHECKBOX_SELECTOR)
    if not checkboxes:
        raise AuthError("No accounts available on this login.")
    for cb in checkboxes:
        if not cb.is_checked():
            cb.check()
    page.click(CONTINUE_SELECTOR)
    wait_any(
        expected=CONFIRM_PAGE_SELECTOR,
        known_errors={},
        timeout=15s,
    )

3.9 Confirm page: page.click(DONE_SELECTOR)
    page.wait_for_url(
        lambda u: u.startswith(cfg.redirect_uri),
        timeout=15_000,
    )
    code = parse_qs(urlparse(page.url).query).get("code", [None])[0]
    if not code:
        raise AuthError("Redirect reached but no `code` param present.")

3.10 close browser

3.11 tr = oauth.exchange_code(cfg, code)
     Session.save(Session.from_token_response(tr, now=time.time()))
     print f"Authenticated. Access token expires at <ISO>."
```

### `wait_any` helper

Centralizes the "race an expected element vs known error texts vs timeout" pattern:

```python
def wait_any(
    page: Page,
    expected: str,
    known_errors: dict[str, str],  # {marker_substring: user_message}
    timeout: int = 15_000,
) -> None:
    """Wait until either the expected selector appears or a known error marker is
    detected in page content. On known error → raise AuthError(user_message). On
    timeout with neither matched → raise AuthError(
        "Auth step timed out — Schwab may have changed. Selectors live at "
        "src/schwab_cli/browser/selectors.py. Auth incomplete."
    )."""
```

Implementation: loop with small sleeps (200ms) until deadline; check `page.content()` for error markers (case-sensitive substring), attempt `page.wait_for_selector(expected, timeout=200)` for positive match. On timeout, if DEBUG is truthy, save screenshot to `~/.config/schwab_cli/auth-error-<timestamp>.png` before raising.

## OAuth Specifics

```python
AUTH_URL = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"

def build_auth_url(cfg: Config) -> str:
    return f"{AUTH_URL}?" + urlencode({
        "response_type": "code",
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
    })

def exchange_code(cfg: Config, code: str) -> TokenResponse:
    resp = httpx.post(
        TOKEN_URL,
        auth=(cfg.client_id, cfg.client_secret),   # Basic auth
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

Exceptions bubble to `commands/auth.py` where they become user-facing messages.

## Secret Resolution

`src/schwab_cli/secrets.py`:

```python
class SecretError(Exception):
    """Raised when a secret reference cannot be resolved."""


def resolve_secret(value: str) -> str:
    """Resolve a secret reference. op:// → shell out to `op read`; anything
    else is returned verbatim.

    Never logs the resolved value or the input if it's an op reference.
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
        # Surface stderr but never echo the op:// path contents.
        raise SecretError(f"op read failed: {e.stderr.strip() or 'unknown error'}") from e
    return result.stdout.rstrip("\n")
```

Rules:
- Stdin is not passed to `op`.
- `capture_output=True` keeps stdout out of the parent process's output.
- The resolved value is only held in memory; never stored in logs or persisted to disk.

## Selectors Module

`src/schwab_cli/browser/selectors.py` holds all page selectors and error markers so Schwab UI changes mean editing one file:

```python
# Login page
LOGIN_USERNAME_SELECTOR = 'input#loginIdInput'   # placeholder; verified during first manual run
LOGIN_PASSWORD_SELECTOR = 'input#passwordInput'
LOGIN_SUBMIT_SELECTOR = 'button#btnLogin'

# Consent / agree page
CONSENT_PAGE_SELECTOR = 'text=Terms of Use'
ACCEPT_SELECTOR = 'button:has-text("Accept")'

# Account selection
ACCOUNT_SELECTION_SELECTOR = 'text=Select accounts'
ACCOUNT_CHECKBOX_SELECTOR = 'input[type="checkbox"][name^="account"]'
CONTINUE_SELECTOR = 'button:has-text("Continue")'

# Confirmation page
CONFIRM_PAGE_SELECTOR = 'text=You will now be redirected'
DONE_SELECTOR = 'button:has-text("Done")'

# Error text markers
INVALID_CLIENT_MARKERS = ('"error": "invalid_client"',)
# Only the JSON-shaped marker is reliable; "Unauthorized" alone is too generic
# and would false-match on other pages.
INVALID_CREDENTIALS_TEXT = "Invalid login ID or password."
REDIRECT_URI_MISMATCH_TEXT = "We are unable to complete your request."
```

Initial selectors are **best guesses**; the first end-to-end manual run (with DEBUG=1) will likely require tightening. This is acceptable and expected — the centralization means fixes are one-file.

## Error Handling Summary

| Failure | Detected at | Message | Exit |
|---|---|---|---|
| No config.json | Top of `auth` | "No config found. Run `schwab_cli setup` first." | 1 |
| Session refresh rejected | Step 2 | "Refresh token rejected (<reason>); doing full auth." → continues to full auth | — |
| 1Password not on PATH | Step 3.1 | "1Password CLI (`op`) not found on PATH." | 1 |
| `op read` failed | Step 3.1 | "op read failed: <stderr>" | 1 |
| Chromium missing | Step 3.4 | "Chromium not found. Run: `uv run playwright install chromium`" | 1 |
| `invalid_client` on auth URL | Step 3.5 | "Schwab rejected client_id/secret — verify setup." | 1 |
| Bad login credentials | Step 3.6 | "Login failed — incorrect username/password." | 1 |
| Redirect URI mismatch | Step 3.6 | "Redirect URI mismatch — re-check setup." | 1 |
| No accounts listed | Step 3.8 | "No accounts available on this login." | 1 |
| Timeout at any step | any `wait_any` | "Auth step timed out — Schwab may have changed. Selectors live at src/schwab_cli/browser/selectors.py. Auth incomplete." | 1 |
| Redirect reached but `code` missing | Step 3.9 | "Redirect reached but no `code` param present." | 1 |
| Token exchange HTTP error | Step 3.11 | "Token exchange failed: <status> <body-summary>" | 1 |

On any exit-1 path, the browser is closed first; no partial session is written.

## Testing Strategy

**Unit tests (fast, no browser, no network):**

- `tests/test_session.py` — mirrors `test_config.py`. Round-trip, `0o600`, atomic write, missing-field validation, version check.
- `tests/test_oauth.py` — uses `respx` to mock httpx. Verify `build_auth_url` produces the expected URL; `exchange_code` and `refresh` send correct body + Basic auth; `TokenResponse.parse` rejects missing fields; HTTP 4xx and network errors bubble.
- `tests/test_secrets.py` — `resolve_secret` with mocked `subprocess.run`:
  - Literal → passthrough
  - `op://...` → calls `op read <path>` and returns stripped stdout
  - `FileNotFoundError` → `SecretError("...not found on PATH.")`
  - `CalledProcessError` → `SecretError` preserving stderr summary
- `tests/test_auth_command.py` — the `auth` CLI with both `oauth` and `browser.flow` mocked:
  - No config → exit 1, "Run setup first"
  - Session present, refresh succeeds → new session saved, prints "Already logged in"
  - Session present, refresh fails → falls through; full-auth mock returns tokens; new session saved
  - `--force` with session present → skips refresh; full-auth invoked directly
  - Full-auth raises `AuthError` → exit 1, message propagated, no session written
- `tests/test_browser_flow.py` — Playwright `Page` stubbed with a `FakePage` class supporting `.goto`, `.fill`, `.click`, `.content`, `.wait_for_selector`, `.wait_for_url`, `.query_selector_all`. Test the decision/sequencing logic in `run_full_auth` and the `wait_any` matcher.

**Optional E2E smoke test** (`@pytest.mark.e2e`, skipped unless `E2E=1`):
- Serves a static HTML page that mimics Schwab's OAuth sequence locally; runs actual Playwright against it. Validates that the selectors file works with a real browser. Not run in CI.

**Coverage target:** ≥80% on each new module. `browser/flow.py` will exercise the mocked `Page` thoroughly; the thin Playwright wrapper calls themselves are exempt from strict coverage (they have no branches worth asserting).

**Not tested automatically:** real Schwab. A manual smoke run with DEBUG=1 is the qualification gate before each milestone.

## Open Questions / Future Work

Deferred to later milestones, captured here for context:

- **Refresh-token-expired warning:** Later we can have commands print a warning when `refresh_token_expires_at` is within 24 hours, nudging the user to re-auth proactively.
- **2FA / MFA support:** Currently unsupported. If Schwab prompts for MFA, the flow times out and surfaces "auth incomplete". Adding interactive MFA would require a mode where the Playwright browser is visible and the user completes the step manually.
- **Multiple profiles:** One host → one Schwab identity. Future: `--profile` flag pointing at `~/.config/schwab_cli/profiles/<name>/{config,session}.json`.
- **Token introspection command:** `schwab_cli status` to show session validity + expiry without running a full `auth`.
