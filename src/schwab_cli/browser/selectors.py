from __future__ import annotations

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
