from __future__ import annotations

from schwab_cli import config as config_module
from schwab_cli.api import quotes as api_quotes
from schwab_cli.api.client import SchwabClient
from schwab_cli.service import auth as service_auth
from schwab_cli.service.auth import NotConfigured
from schwab_cli.service.types import DividendsResult
from schwab_cli.ticker import to_schwab_form


def get_dividends(symbols: list[str]) -> DividendsResult:
    """Owns auth + business logic for the ``dividends`` command.

    Loads config and session, builds the HTTP client, normalizes the
    requested symbols to Schwab form (so the renderer's per-symbol lookup
    matches the canonical keys Schwab returns), fetches the full quotes
    payload, and returns it wrapped with the normalized symbol list the
    renderer keys off — preserving input order. The upcoming-window
    filtering stays in the renderer, driven by the command flag.
    """
    cfg = config_module.load()
    if cfg is None:
        raise NotConfigured

    session = service_auth.get_session(cfg)
    normalized = [to_schwab_form(s) for s in symbols]

    with SchwabClient(cfg, session) as client:
        payload = api_quotes.get_quotes(client, normalized, fields="all")

    return DividendsResult(symbols=tuple(normalized), payload=payload)
