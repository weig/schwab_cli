"""Layer-2 service for option-chain envelopes.

Owns auth + the fetch/shape orchestration the MCP ``get_chain`` tool used
to perform inline against the daemon's persistent client. The tool (Layer
3) becomes a thin parse -> service -> serialize shim; this service loads
config + session, builds a short-lived :class:`SchwabClient`, fetches the
chain, and shapes it into the display envelope.

Layer-1 is reached via the MODULE ATTRIBUTE ``api_chains.get_chain`` — the
stable test seam the characterization suite patches.
"""

from __future__ import annotations

from datetime import date

from schwab_cli.api import chains as api_chains
from schwab_cli.output.chains import shape_envelope
from schwab_cli.service.base import BaseService

__all__ = ["ChainsService"]


class ChainsService(BaseService):
    """Layer-2 service for the MCP ``get_chain`` tool."""

    def get_chain_envelope(
        self,
        symbol: str,
        *,
        expiry: date,
        strike_count: int = 20,
        contract_type: str = "ALL",
    ) -> dict:
        """Owns auth + fetch/shape for the MCP ``get_chain`` tool.

        Loads config and session, builds the HTTP client, fetches the option
        chain for ``symbol`` at the single ``expiry``, and returns the shaped
        display envelope (:func:`schwab_cli.output.chains.shape_envelope`).
        This mirrors the pre-refactor MCP tool exactly so its serialized
        output stays byte-identical.

        Raises :class:`schwab_cli.service.auth.NotConfigured` when no config is
        on disk, and the auth exceptions from :mod:`schwab_cli.service.auth`
        when the session is missing/expired.
        """
        with self._authed_client() as client:
            raw = api_chains.get_chain(
                client,
                symbol,
                contract_type=contract_type,
                strike_count=strike_count,
                from_date=expiry,
                to_date=expiry,
            )
        return shape_envelope(raw)
