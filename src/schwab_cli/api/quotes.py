from __future__ import annotations

from schwab_cli.api.client import SchwabClient


def get_quotes(
    client: SchwabClient,
    symbols: list[str],
    *,
    fields: str | None = None,
) -> dict:
    """Fetch quotes for the given symbols. Returns the Schwab response dict.

    ``fields`` is forwarded as the ``fields`` query parameter when set.

    Use ``"all"`` to include the ``fundamental`` block consumed by the
    ``fundamentals`` and ``dividends`` commands — comma-separated lists
    like ``"quote,fundamental"`` silently fall back to a quote-only
    response, because httpx percent-encodes the comma (``%2C``) and
    Schwab's server does not round-trip the decoded value back to a list.
    Omit ``fields`` entirely for a plain quote call.

    Callers get per-symbol entries plus an optional ``errors`` key with
    lists like ``invalidSymbols`` — not a 4xx, just per-symbol metadata.
    """
    if not symbols:
        return {}
    params: dict[str, str] = {"symbols": ",".join(symbols)}
    if fields:
        params["fields"] = fields
    return client.get(f"{SchwabClient.MARKET_BASE}/quotes", params=params)
