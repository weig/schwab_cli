"""Back-compat shim — the browser-driven auth flow now lives in
:mod:`schwab_cli.browser._seleniumbase_flow`.

The previous Playwright backend was removed; SeleniumBase UC is the only
browser driver because it's the only one that reliably bypasses Schwab's
Akamai bot detection. This module re-exports :class:`AuthError` so existing
imports (``from schwab_cli.browser.flow import AuthError``) keep working.
"""

from schwab_cli.browser._seleniumbase_flow import AuthError  # noqa: F401

__all__ = ["AuthError"]
