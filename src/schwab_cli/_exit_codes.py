"""Process-exit codes shared between child commands and the scheduler.

A small, stable contract so the scheduler can distinguish *why* a child
failed without parsing stdout. Anything beyond ``EXIT_AUTH_FAILED``
should be treated as a generic error by the scheduler (no auto-retry).
"""
from __future__ import annotations

# 0 / 1 are reserved for "success" / "generic error" (standard POSIX).
EXIT_AUTH_FAILED = 2
"""Child detected that auth is unrecoverable from its side.

Raised when an API call returns 401 and the refresh-token-based renewal
inside ``api/client._refresh_or_expire`` also fails. The scheduler
treats this as a signal to invoke the full auto-login flow once and
respawn just the auth-failed children with ``--skip-wait``.

Other non-zero exits are NOT retried — they're real errors and rerunning
won't help.
"""
