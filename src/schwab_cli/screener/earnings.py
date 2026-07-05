"""Earnings calendar (plan §3, Stage B).

``refresh_earnings`` drives an injectable fetcher over the symbol universe
and upserts results into the ``events`` table. Estimated dates are stored as
valid (confirmed=0) — we would rather over-exclude than sell into an event.
The screener's hard filter is fail-closed on a *missing* date, so a symbol
the feed can't resolve is dropped, not assumed event-free.

``nasdaq_earnings_fetcher`` is a best-effort free-source implementation; any
network/parse failure yields ``(None, False)`` so a rotten feed degrades to
fail-closed rather than raising.
"""
from __future__ import annotations

from typing import Callable

import httpx

from schwab_cli.storage import screener as store

# A fetcher maps a symbol to (next_earnings_date_iso | None, confirmed).
EarningsFetcher = Callable[[str], "tuple[str | None, bool]"]

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def refresh_earnings(
    conn, symbols: list[str], fetcher: EarningsFetcher, *, now_ms: int
) -> dict:
    """Fetch + upsert the next earnings date per symbol.

    Returns coverage counts so the caller can alert on a degraded feed
    (a low ``updated`` / high ``missing`` ratio ⇒ many names fail-closed).
    """
    updated = 0
    missing = 0
    for symbol in symbols:
        try:
            event_date, confirmed = fetcher(symbol)
        except Exception:  # noqa: BLE001 — a bad symbol must not abort the sweep
            event_date, confirmed = None, False
        if not event_date:
            missing += 1
            continue
        store.upsert_event(
            conn, symbol=symbol, event_type="earnings", event_date=event_date,
            confirmed=confirmed, updated_at_ms=now_ms,
        )
        updated += 1
    conn.commit()
    return {"updated": updated, "missing": missing, "total": len(symbols)}


def nasdaq_earnings_fetcher(client: httpx.Client) -> EarningsFetcher:
    """Best-effort fetcher backed by Nasdaq's public quote/EPS endpoint.

    Returns a closure so callers share one HTTP client. Any failure (network,
    shape change, missing field) resolves to ``(None, False)`` — the screener
    then fail-closes that symbol.
    """

    def _fetch(symbol: str) -> tuple[str | None, bool]:
        try:
            resp = client.get(
                f"https://api.nasdaq.com/api/quote/{symbol}/eps",
                headers={"User-Agent": _BROWSER_UA, "Accept": "application/json"},
                timeout=10.0,
            )
            resp.raise_for_status()
            body = resp.json()
            date_iso = _parse_next_report_date(body)
            return date_iso, False  # Nasdaq forecast dates are estimates
        except Exception:  # noqa: BLE001
            return None, False

    return _fetch


def _parse_next_report_date(body: dict) -> str | None:
    """Extract an ISO date from Nasdaq's EPS payload, defensively.

    The forecast block carries the upcoming fiscal report date; layouts
    change, so we walk generically and normalize the first ``MM/DD/YYYY`` or
    ``Mon DD, YYYY`` we find in a date-ish field to ISO. None on any miss.
    """
    from datetime import datetime

    def _norm(s: str) -> str | None:
        for fmt in ("%m/%d/%Y", "%b %d, %Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(s.strip(), fmt).date().isoformat()
            except (ValueError, AttributeError):
                continue
        return None

    data = (body or {}).get("data") or {}
    for key in ("nextReportDate", "reportDate", "earningsDate"):
        val = data.get(key)
        if isinstance(val, str):
            iso = _norm(val)
            if iso:
                return iso
    return None
