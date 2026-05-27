from __future__ import annotations

from schwab_cli import config as config_module
from schwab_cli.api import accounts as api_accounts
from schwab_cli.api.client import SchwabClient
from schwab_cli.service import auth as service_auth
from schwab_cli.service.auth import NotConfigured
from schwab_cli.service.types import AccountResult, AccountsResult, PositionsResult


def list_accounts() -> AccountsResult:
    """Owns auth + business logic for the ``accounts`` (list) command.

    Loads config and session, builds the HTTP client, and fetches every
    account (with positions). The raw payload list is wrapped verbatim into
    an :class:`AccountsResult` so the renderer's HUMAN/JSON/MD output stays
    byte-identical to the pre-migration command.
    """
    cfg = config_module.load()
    if cfg is None:
        raise NotConfigured

    session = service_auth.get_session(cfg)

    with SchwabClient(cfg, session) as client:
        payload = api_accounts.list_accounts(client)

    return AccountsResult(accounts=tuple(payload))


def get_account(account_number: str) -> AccountResult:
    """Owns auth + business logic for the ``account <number>`` command.

    Loads config and session, builds the HTTP client, and fetches the single
    account (with positions). The raw payload dict is wrapped verbatim into
    an :class:`AccountResult`.
    """
    cfg = config_module.load()
    if cfg is None:
        raise NotConfigured

    session = service_auth.get_session(cfg)

    with SchwabClient(cfg, session) as client:
        payload = api_accounts.get_account(client, account_number)

    return AccountResult(account=payload)


def get_positions(account_number: str | None) -> PositionsResult:
    """Owns auth + business logic for the ``positions`` command.

    Loads config and session, builds the HTTP client, and fetches the flat
    list of position rows across the selected account(s). ``account_number``
    is forwarded to the Layer-1 api unchanged (``None`` -> all accounts).
    The raw rows are wrapped verbatim into a :class:`PositionsResult`.
    """
    cfg = config_module.load()
    if cfg is None:
        raise NotConfigured

    session = service_auth.get_session(cfg)

    with SchwabClient(cfg, session) as client:
        rows = api_accounts.get_positions(client, account_number)

    return PositionsResult(positions=tuple(rows))
