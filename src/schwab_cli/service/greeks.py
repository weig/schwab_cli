from __future__ import annotations

from datetime import date

from schwab_cli import config as config_module
from schwab_cli.api.chains import get_chain
from schwab_cli.api.client import SchwabClient
from schwab_cli.output.chains import shape_envelope
from schwab_cli.service import ServiceError
from schwab_cli.service import auth as service_auth
from schwab_cli.service.auth import NotConfigured
from schwab_cli.service.types import GreeksResult


class ContractNotFound(ServiceError):
    """Raised when no contract matches the requested strike + side.

    Carries the resolved request fields (for callers that want them) and
    a complete, user-ready message — ``str(e)`` is the full sentence, so
    interfaces can surface it directly.
    """

    def __init__(
        self,
        *,
        underlying: str,
        expiry: date,
        strike: float,
        contract_type: str,
    ) -> None:
        self.underlying = underlying
        self.expiry = expiry
        self.strike = strike
        self.contract_type = contract_type
        super().__init__(
            f"No {contract_type} contract for {underlying} "
            f"{expiry.isoformat()} strike ${strike:.2f}. "
            "Verify the expiry + strike exist."
        )


def _pick_contract(contracts: list[dict], strike: float, side: str) -> dict | None:
    """Choose the contract whose strike + side match the requested option.

    Schwab's ``strike`` filter is fuzzy (it returns the closest strikes, not
    an exact match), so we enforce exactness locally. `strike` comparison
    uses a small epsilon because strikes on exchange feeds are sometimes
    reported as floats with 3-decimal drift.
    """
    for c in contracts:
        if c.get("side") != side:
            continue
        cs = c.get("strike")
        if cs is None:
            continue
        if abs(cs - strike) < 1e-4:
            return c
    return None


def get_greeks(
    underlying: str,
    *,
    strike: float,
    expiry: date,
    side: str,
) -> GreeksResult:
    """Owns auth + business logic for the ``greeks`` command.

    Loads config and session, fetches the option chain filtered to the
    single strike + expiry + side, shapes it, picks the matching contract,
    and builds the display envelope. ``side`` is ``"C"`` or ``"P"``.

    Raises :class:`schwab_cli.service.auth.NotConfigured` when no config is
    on disk, the auth exceptions from :mod:`schwab_cli.service.auth` when the
    session is missing/expired, and :class:`ContractNotFound` when no contract
    matches the requested strike + side.
    """
    contract_type = "CALL" if side == "C" else "PUT"

    cfg = config_module.load()
    if cfg is None:
        raise NotConfigured

    session = service_auth.get_session(cfg)

    with SchwabClient(cfg, session) as client:
        # Schwab's chain endpoint prefers `strikeCount` over `strike` when
        # both are passed, returning strikes around ATM rather than the
        # one we asked for. Omit `strike_count` entirely so `strike` wins
        # — we just want this single strike back.
        raw = get_chain(
            client,
            underlying,
            contract_type=contract_type,
            strike=strike,
            strike_count=None,
            from_date=expiry,
            to_date=expiry,
        )

    chain = shape_envelope(raw)
    match = _pick_contract(chain.get("contracts") or [], strike, side)
    if match is None:
        raise ContractNotFound(
            underlying=underlying,
            expiry=expiry,
            strike=strike,
            contract_type=contract_type,
        )

    envelope = {
        "underlyingSymbol": underlying,
        "expiry": expiry.isoformat(),
        "dte": chain.get("dte"),
        "underlying": chain.get("underlying") or {},
        "contract": match,
    }
    return GreeksResult(envelope=envelope)
