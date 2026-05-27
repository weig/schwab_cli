from __future__ import annotations

from schwab_cli.api import quotes as api_quotes
from schwab_cli.service.base import BaseService
from schwab_cli.service.types import DividendsResult
from schwab_cli.ticker import to_schwab_form


class DividendsService(BaseService):
    """Layer-2 service for the ``dividends`` command."""

    def get_dividends(self, symbols: list[str]) -> DividendsResult:
        """Owns auth + business logic for the ``dividends`` command.

        Loads config and session, builds the HTTP client, normalizes the
        requested symbols to Schwab form (so the renderer's per-symbol lookup
        matches the canonical keys Schwab returns), fetches the full quotes
        payload, and returns it wrapped with the normalized symbol list the
        renderer keys off — preserving input order. The upcoming-window
        filtering stays in the renderer, driven by the command flag.
        """
        normalized = [to_schwab_form(s) for s in symbols]

        with self._authed_client() as client:
            payload = api_quotes.get_quotes(client, normalized, fields="all")

        return DividendsResult(symbols=tuple(normalized), payload=payload)
