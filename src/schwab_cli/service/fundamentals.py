from __future__ import annotations

from schwab_cli.api import quotes as api_quotes
from schwab_cli.service.base import BaseService
from schwab_cli.service.types import FundamentalsResult
from schwab_cli.ticker import to_schwab_form


class FundamentalsService(BaseService):
    """Layer-2 service for the ``fundamentals`` command."""

    def get_fundamentals(self, symbols: list[str]) -> FundamentalsResult:
        """Owns auth + business logic for the ``fundamentals`` command.

        Loads config and session, builds the HTTP client, normalizes the
        requested symbols to Schwab form (so the renderer's per-symbol lookup
        matches the canonical keys Schwab returns), fetches the full quotes
        payload, and returns it wrapped with the normalized symbol list the
        renderer keys off — preserving input order.
        """
        normalized = [to_schwab_form(s) for s in symbols]

        with self._authed_client() as client:
            payload = api_quotes.get_quotes(client, normalized, fields="all")

        return FundamentalsResult(symbols=tuple(normalized), payload=payload)
