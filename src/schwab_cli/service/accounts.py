from __future__ import annotations

from schwab_cli.api import accounts as api_accounts
from schwab_cli.service.base import BaseService
from schwab_cli.service.types import AccountResult, AccountsResult, PositionsResult


class AccountsService(BaseService):
    """Layer-2 service for the ``accounts`` / ``account`` / ``positions``
    commands."""

    def list_accounts(self) -> AccountsResult:
        """Owns auth + business logic for the ``accounts`` (list) command.

        Loads config and session, builds the HTTP client, and fetches every
        account (with positions). The raw payload list is wrapped verbatim into
        an :class:`AccountsResult` so the renderer's HUMAN/JSON/MD output stays
        byte-identical to the pre-migration command.
        """
        with self._authed_client() as client:
            payload = api_accounts.list_accounts(client)

        return AccountsResult(accounts=tuple(payload))

    def get_account(self, account_number: str) -> AccountResult:
        """Owns auth + business logic for the ``account <number>`` command.

        Loads config and session, builds the HTTP client, and fetches the single
        account (with positions). The raw payload dict is wrapped verbatim into
        an :class:`AccountResult`.
        """
        with self._authed_client() as client:
            payload = api_accounts.get_account(client, account_number)

        return AccountResult(account=payload)

    def get_positions(self, account_number: str | None) -> PositionsResult:
        """Owns auth + business logic for the ``positions`` command.

        Loads config and session, builds the HTTP client, and fetches the flat
        list of position rows across the selected account(s). ``account_number``
        is forwarded to the Layer-1 api unchanged (``None`` -> all accounts).
        The raw rows are wrapped verbatim into a :class:`PositionsResult`.
        """
        with self._authed_client() as client:
            rows = api_accounts.get_positions(client, account_number)

        return PositionsResult(positions=tuple(rows))
