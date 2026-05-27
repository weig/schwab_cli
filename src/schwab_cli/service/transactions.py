from __future__ import annotations

from datetime import datetime

from schwab_cli import config as config_module
from schwab_cli.api import transactions_cache as api_tx_cache
from schwab_cli.api.client import SchwabClient
from schwab_cli.output.transactions import shape_transactions
from schwab_cli.service import auth as service_auth
from schwab_cli.service.auth import NotConfigured
from schwab_cli.service.types import TransactionsResult

__all__ = ["get_transactions"]


def _filter_by_type(rows: list[dict], type_filter: str) -> list[dict]:
    if not type_filter or type_filter == "ALL":
        return rows
    wanted = {t.strip() for t in type_filter.split(",") if t.strip()}
    return [r for r in rows if (r.get("type") or "") in wanted]


def get_transactions(
    account: str | None,
    *,
    start: datetime,
    end: datetime,
    type_filter: str,
    refresh: bool = False,
) -> TransactionsResult:
    """Owns auth + cache + business logic for the ``transactions`` command.

    Loads config and session, builds the HTTP client, and fetches the
    transaction list via the cache-backed Layer-1 orchestrator
    (:func:`schwab_cli.api.transactions_cache.fetch_cached`, reached through
    the module attribute so test seams can patch it). The cache always
    stores the full type set, so the user's ``--type`` filter is applied
    locally on the way to the shaper.

    Preserves the exact pre-migration order: fetch -> filter -> shape. The
    shaped rows are wrapped verbatim into a :class:`TransactionsResult`,
    along with the Account-column-visibility signal (``show_account`` is
    ``True`` only when no specific account was requested) and the cache
    statistics for the HUMAN-view header.

    Raises :class:`schwab_cli.service.auth.NotConfigured` when no config is
    on disk and the auth exceptions from :mod:`schwab_cli.service.auth` when
    the session is missing/expired. ``(ApiError, SessionExpired)`` from the
    cache fetch propagate unchanged.
    """
    cfg = config_module.load()
    if cfg is None:
        raise NotConfigured

    session = service_auth.get_session(cfg)

    cache_stats: dict = {}
    with SchwabClient(cfg, session) as client:
        # Cache always fetches the full type set; apply the user's
        # filter locally on the way to the renderer.
        raw = api_tx_cache.fetch_cached(
            client, account,
            start=start, end=end,
            refresh=refresh,
            stats=cache_stats,
        )

    raw = _filter_by_type(raw, type_filter)
    rows = shape_transactions(raw)

    return TransactionsResult(
        rows=tuple(rows),
        # When the user filtered to a specific account, drop the
        # redundant Account column from human/MD output. JSON is
        # unaffected (stable shape for machine consumers).
        show_account=(account is None),
        cache_stats=cache_stats,
    )
