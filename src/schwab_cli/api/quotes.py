from __future__ import annotations

from schwab_cli.api.client import SchwabClient


def get_quotes(client: SchwabClient, symbols: list[str]) -> dict:
    """Fetch quotes for the given symbols. Returns the Schwab response dict.

    Callers get per-symbol entries plus an optional `errors` key with
    lists like `invalidSymbols` — not a 4xx, just per-symbol metadata.
    """
    if not symbols:
        return {}
    return client.get(
        f"{SchwabClient.MARKET_BASE}/quotes",
        params={"symbols": ",".join(symbols)},
    )
