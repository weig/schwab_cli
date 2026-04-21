from __future__ import annotations

# ---------------------------------------------------------------------------
# Selectors. Best-guess starting set; tighten after first manual run.
# ---------------------------------------------------------------------------

# Login page
LOGIN_USERNAME_SELECTOR = "input#loginIdInput"
LOGIN_PASSWORD_SELECTOR = "input#passwordInput"
LOGIN_SUBMIT_SELECTOR = "button#btnLogin"

# MFA / device-verification page (may be skipped on already-trusted devices)
MFA_PAGE_SELECTOR = "text=Verify your identity"
SCHWAB_APP_OPTION_SELECTOR = "text=Schwab App"

# Trust-device page (may be skipped if device is already trusted)
TRUST_DEVICE_PAGE_SELECTOR = "text=Trust this device"
TRUST_YES_SELECTOR = 'label:has-text("Yes, trust this device")'
TRUST_NEXT_SELECTOR = 'button:has-text("Next")'

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
