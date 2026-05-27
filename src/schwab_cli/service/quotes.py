from __future__ import annotations

from schwab_cli.api import quotes as api_quotes
from schwab_cli.service.base import BaseService
from schwab_cli.service.types import QuoteResult, QuoteRow
from schwab_cli.ticker import to_schwab_form


def _row_for(symbol: str, payload: dict, invalid: set[str]) -> QuoteRow:
    if symbol in invalid:
        return QuoteRow(
            symbol=symbol,
            last=None,
            change=None,
            change_pct=None,
            bid=None,
            ask=None,
            volume=None,
            error="invalid symbol",
        )
    entry = payload.get(symbol) or {}
    q = entry.get("quote") or {}
    return QuoteRow(
        symbol=symbol,
        last=q.get("lastPrice"),
        change=q.get("netChange"),
        change_pct=q.get("netPercentChangeInDouble") or q.get("netPercentChange"),
        bid=q.get("bidPrice"),
        ask=q.get("askPrice"),
        volume=q.get("totalVolume"),
    )


class QuoteService(BaseService):
    """Layer-2 service for the ``quote`` command + MCP ``get_quote`` tool."""

    def get_quote_payload(
        self, symbols: list[str], *, fields: str | None = None
    ) -> dict:
        """Owns auth + fetch for the MCP ``get_quote`` tool.

        Loads config and session, builds the HTTP client, fetches quotes for
        the given symbols, and returns the RAW Schwab payload dict unchanged.
        Unlike :meth:`get_quote`, this performs no mapping into a
        :class:`QuoteResult` — the MCP tool serializes the raw payload as-is
        so its output stays byte-identical to the pre-refactor daemon path.

        The caller owns symbol normalization (the MCP tool upcases). We do NOT
        apply ``to_schwab_form`` here — the pre-refactor daemon path didn't
        either, and adding it would change the MCP output for class-share
        tickers. (Contrast :meth:`get_quote`, which normalizes for the CLI.)
        """
        with self._authed_client() as client:
            return api_quotes.get_quotes(client, symbols, fields=fields)

    def get_quote(
        self, symbols: list[str], *, fields: str | None = None
    ) -> QuoteResult:
        """Owns auth + business logic for the ``quote`` command.

        Loads config and session, builds the HTTP client, normalizes the
        requested symbols to Schwab form, fetches quotes, and maps the raw
        payload to a :class:`QuoteResult` preserving input order. Invalid
        symbols get ``error="invalid symbol"`` and ``None`` fields.
        """
        normalized = [to_schwab_form(s) for s in symbols]

        with self._authed_client() as client:
            payload = api_quotes.get_quotes(client, normalized, fields=fields)

        invalid = set((payload.get("errors") or {}).get("invalidSymbols") or [])
        rows = tuple(_row_for(s, payload, invalid) for s in normalized)
        return QuoteResult(rows=rows)
