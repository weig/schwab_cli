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
