"""Layer-2 service package.

Owns authentication and business logic for the CLI commands, returning
structured frozen dataclasses. Commands (Layer 3) become thin
parse -> service -> render shims; the HTTP wrapper (Layer 1,
``schwab_cli.api``) stays unaware of auth lifecycle decisions.
"""
from __future__ import annotations


class ServiceError(Exception):
    """Base for every service-layer error.

    Lets a caller catch any service-originated problem with a single
    ``except ServiceError`` instead of enumerating each subclass.
    """
