from __future__ import annotations

from schwab_cli import config as config_module
from schwab_cli.api import quotes as api_quotes
from schwab_cli.api.client import SchwabClient
from schwab_cli.service import auth as service_auth
from schwab_cli.service.auth import NotConfigured
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


def get_quote(symbols: list[str], *, fields: str | None = None) -> QuoteResult:
    """Owns auth + business logic for the ``quote`` command.

    Loads config and session, builds the HTTP client, normalizes the
    requested symbols to Schwab form, fetches quotes, and maps the raw
    payload to a :class:`QuoteResult` preserving input order. Invalid
    symbols get ``error="invalid symbol"`` and ``None`` fields.
    """
    cfg = config_module.load()
    if cfg is None:
        raise NotConfigured

    session = service_auth.get_session(cfg)
    normalized = [to_schwab_form(s) for s in symbols]

    with SchwabClient(cfg, session) as client:
        payload = api_quotes.get_quotes(client, normalized, fields=fields)

    invalid = set((payload.get("errors") or {}).get("invalidSymbols") or [])
    rows = tuple(_row_for(s, payload, invalid) for s in normalized)
    return QuoteResult(rows=rows)
