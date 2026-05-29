"""Certificate management for the local HTTPS callback server.

Provides generation of a name-constrained local CA + leaf certificate,
on-disk storage under the user's config dir, a macOS keychain trust store
abstraction, and a manager orchestrating install / uninstall / status.
"""
from __future__ import annotations
