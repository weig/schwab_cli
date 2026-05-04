"""Cached-fetch orchestrator for transactions.

Sits between ``commands/transactions.py`` and ``api/transactions.py``,
adding a SQLite-backed cache. The caller's view is unchanged from
calling ``get_all_transactions`` directly — same return shape, same
per-row ``_account`` annotation.

Cache strategy: split the requested range at ``_fresh_cutoff(today)``
— the earliest of (first day of previous month) and (today − 30 days).

* The **old** portion (≤ cutoff) is treated as immutable. We compute
  coverage gaps and fetch only what's missing. Once cached, never
  re-fetched.
* The **fresh** portion (> cutoff) is treated as mutable (settlement
  adjustments, dividend posting, late-broadcast fees). We always
  re-fetch this range and UPSERT, regardless of coverage. Coverage
  is still recorded so when the data ages into "old" next month it's
  already cached.
* ``refresh=True`` bypasses the split and fetches the full requested
  range as one big gap.

API calls always pass ``types=None`` — the cache stores the full set
so any local filter can be applied without re-hitting the API.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from schwab_cli.api.client import SchwabClient
from schwab_cli.api.transactions import get_transactions
from schwab_cli.storage import transactions_history as th


_API_CHUNK = timedelta(days=60)
_FRESH_FLOOR_DAYS = 30


def _today() -> date:
    """Indirected so tests can pin time."""
    return datetime.now(tz=timezone.utc).date()


def _to_ms(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def _from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _fresh_cutoff(today: date) -> date:
    """Anything ≤ this date is 'old' (cached); > this date is 'fresh'.

    Returns earliest of:
      * first day of the previous month
      * today minus ``_FRESH_FLOOR_DAYS``

    The "earliest" rule guarantees a fresh range of ≥30 days even at
    the start of the month, where first-of-prev-month would otherwise
    give too short a window (e.g. Mar 1 → Feb 1 is only 28 days back
    in a non-leap year, so we push back to Jan 30).
    """
    first_of_this_month = today.replace(day=1)
    first_of_prev_month = (first_of_this_month - timedelta(days=1)).replace(day=1)
    floor = today - timedelta(days=_FRESH_FLOOR_DAYS)
    return min(first_of_prev_month, floor)


def fetch_cached(
    client: SchwabClient,
    account_number: str | None,
    *,
    start: datetime,
    end: datetime,
    refresh: bool = False,
) -> list[dict]:
    """Fetch transactions across one or all accounts, cache-aware.

    Returns the same shape as ``api.transactions.get_all_transactions``
    — each transaction tagged with a synthetic ``_account`` key.

    No ``types`` / ``symbol`` kwargs by design: the cache stores the
    full set; callers filter locally on the returned list.
    """
    if account_number is None:
        ids = client.account_ids()
    else:
        ids = [client.resolve_account(account_number)]

    out: list[dict] = []
    for acct in ids:
        per_account = _fetch_one_account(
            client, acct.hash_value,
            start=start, end=end, refresh=refresh,
        )
        for txn in per_account:
            tagged = dict(txn)
            tagged["_account"] = acct.account_number
            out.append(tagged)
    return out


def _fetch_one_account(
    client: SchwabClient,
    account_hash: str,
    *,
    start: datetime,
    end: datetime,
    refresh: bool,
) -> list[dict]:
    start_ms = _to_ms(start)
    end_ms = _to_ms(end)
    cutoff_date = _fresh_cutoff(_today())
    # Cutoff is end-of-day inclusive: anything with time_ms <= cutoff_ms
    # belongs to "old". Fresh starts at the next millisecond.
    cutoff_dt = datetime.combine(
        cutoff_date, datetime.max.time(), tzinfo=timezone.utc,
    )
    cutoff_ms = _to_ms(cutoff_dt)

    with th.connect() as conn:
        gaps: list[tuple[int, int]] = []
        if refresh:
            gaps = [(start_ms, end_ms)]
        else:
            # Old portion: cache-trusted, fetch only gaps.
            if start_ms <= cutoff_ms:
                old_end_ms = min(end_ms, cutoff_ms)
                gaps += th.coverage_gaps(
                    conn, account_hash=account_hash,
                    start_ms=start_ms, end_ms=old_end_ms,
                )
            # Fresh portion: always fetched, regardless of coverage.
            if end_ms > cutoff_ms:
                fresh_start_ms = max(start_ms, cutoff_ms + 1)
                gaps.append((fresh_start_ms, end_ms))

        for gap_start_ms, gap_end_ms in gaps:
            for chunk_start, chunk_end in _chunk_range(
                _from_ms(gap_start_ms), _from_ms(gap_end_ms),
            ):
                payloads = get_transactions(
                    client, account_hash,
                    start=chunk_start, end=chunk_end,
                    types=None, symbol=None,
                )
                th.upsert_many(conn, account_hash, payloads)
                th.merge_coverage(
                    conn, account_hash,
                    start_ms=_to_ms(chunk_start),
                    end_ms=_to_ms(chunk_end),
                )

        return th.read_range(
            conn, account_hash=account_hash,
            start_ms=start_ms, end_ms=end_ms,
        )


def _chunk_range(
    start: datetime, end: datetime,
) -> list[tuple[datetime, datetime]]:
    """Split [start, end] into chunks of at most ``_API_CHUNK``."""
    chunks: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + _API_CHUNK, end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(milliseconds=1)
    return chunks
