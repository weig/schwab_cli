from __future__ import annotations

from schwab_cli.api.client import SchwabClient


def list_accounts(client: SchwabClient) -> list[dict]:
    """All accounts with positions included in one call."""
    return client.get(
        f"{client.TRADER_BASE}/accounts",
        params={"fields": "positions"},
    )


def get_account(client: SchwabClient, account_number: str) -> dict:
    """Single account with positions."""
    ids = client.resolve_account(account_number)
    return client.get(
        f"{client.TRADER_BASE}/accounts/{ids.hash_value}",
        params={"fields": "positions"},
    )


def get_positions(client: SchwabClient, account_number: str | None) -> list[dict]:
    """Flat list of position rows across the selected account(s).

    Each returned row is the raw position dict with a synthetic `_account`
    key set to the owning account number. Accounts without any positions
    are omitted.
    """
    if account_number is None:
        payload = list_accounts(client)
        if not isinstance(payload, list):
            return []
    else:
        payload = [get_account(client, account_number)]

    rows: list[dict] = []
    for item in payload:
        sec = item.get("securitiesAccount", {})
        acct = sec.get("accountNumber", "")
        for pos in sec.get("positions", []) or []:
            pos = dict(pos)
            pos["_account"] = acct
            rows.append(pos)
    return rows
