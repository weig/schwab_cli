from __future__ import annotations

# ---------------------------------------------------------------------------
# Selectors discovered from a live Schwab OAuth walk-through on 2026-04-21.
# If Schwab changes their UI, regenerate by running the auth flow with
# DEBUG=1 and inspecting `~/.config/schwab_cli/auth-debug/<ts>/*.html`.
# ---------------------------------------------------------------------------

# --- Login page (URL fragment: /login-one-step) ----------------------------
LOGIN_USERNAME_SELECTOR = "input#loginIdInput"
LOGIN_PASSWORD_SELECTOR = "input#passwordInput"
LOGIN_SUBMIT_SELECTOR = "button#btnLogin"

# --- MFA "Confirm Your Identity" page (URL: /authenticators) ---------------
# The Schwab App option is a <div role="button">, NOT a real <button>.
# Use the stable id; the presence of this element is also our "we're on the
# MFA method-picker page" marker (it exists only on that page).
SCHWAB_APP_OPTION_SELECTOR = "#mobile_approve"
MFA_PICKER_PAGE_SELECTOR = SCHWAB_APP_OPTION_SELECTOR

# --- MFA waiting page (URL: /mobile_approve) -------------------------------
# Reached either by clicking Schwab App on the picker, OR directly when
# Schwab remembers the user's preferred method. Detect via a fragment of
# the page's prompt text — there's no stable id on this page.
MFA_WAITING_PAGE_SELECTOR = "text=We sent notification to your mobile device"

# --- Trust-device "Security Preference - Device" (URL: /devicetag/remember) -
# Default is "No, do not remember" — we must click Yes, then Continue.
TRUST_YES_SELECTOR = "input#remember-device-yes"
TRUST_CONTINUE_SELECTOR = "button#btnContinue"
TRUST_DEVICE_PAGE_SELECTOR = TRUST_YES_SELECTOR

# --- Consent "Consent and Grant Form" (URL: /third-party-auth/cag) ---------
# Two-step: check acceptTerms -> click Continue -> modal appears with Accept.
CONSENT_PAGE_SELECTOR = "input#acceptTerms"
CONSENT_CHECKBOX_SELECTOR = "input#acceptTerms"
CONSENT_CONTINUE_SELECTOR = "button#submit-btn"
# Trailing hyphen in the id is intentional — that's what Schwab emits.
CONSENT_MODAL_ACCEPT_SELECTOR = "button#agree-modal-btn-"

# --- Account selection (URL: /third-party-auth/account) --------------------
# The checkbox(es) have no stable id; only the type="checkbox" attribute.
# On the account page there's one checkbox per account and no other
# checkboxes, so this selector is safe on that page.
ACCOUNT_CHECKBOX_SELECTOR = 'input[type="checkbox"]'
ACCOUNT_CONTINUE_SELECTOR = "button#submit-btn"

# --- Confirmation (URL: /third-party-auth/confirmation) --------------------
# Yes, the id is literally "cancel-btn" even though the visible text is "Done".
DONE_SELECTOR = "button#cancel-btn"
CONFIRM_PAGE_SELECTOR = DONE_SELECTOR

# ---------------------------------------------------------------------------
# URL fragments for page transitions (Schwab's URLs change per step; elements
# may appear briefly during in-flight navigation, so URL-based waits are
# used where element-based waits would race).
# ---------------------------------------------------------------------------
URL_FRAGMENT_ACCOUNTS = "/third-party-auth/account"
URL_FRAGMENT_CONFIRMATION = "/third-party-auth/confirmation"
URL_FRAGMENT_CONSENT = "/third-party-auth/cag"

# ---------------------------------------------------------------------------
# Error text markers (page-content substrings).
# INVALID_CREDENTIALS_TEXT / REDIRECT_URI_MISMATCH_TEXT are best-effort — we
# didn't capture the exact error markup in the walk-through since login
# succeeded. If these produce false positives, set them to "" to disable.
# ---------------------------------------------------------------------------
INVALID_CLIENT_MARKERS = ('"error": "invalid_client"',)
INVALID_CREDENTIALS_TEXT = "Please try again"
REDIRECT_URI_MISMATCH_TEXT = "We are unable to complete your request."
