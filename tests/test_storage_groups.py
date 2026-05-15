"""Group discriminator constants for the ``group_name`` column."""
from __future__ import annotations

from schwab_cli.storage.groups import (
    ALL_GROUPS,
    GROUP_OHLCV,
    GROUP_VOLATILITY,
)


def test_volatility_constant_matches_existing_string_literal() -> None:
    """The Task 2 sweep replaces ``"volatility"`` literals with this
    constant; the value MUST equal the legacy string or every row in
    the live DB becomes invisible."""
    assert GROUP_VOLATILITY == "volatility"


def test_ohlcv_constant() -> None:
    assert GROUP_OHLCV == "ohlcv"
    assert GROUP_OHLCV != GROUP_VOLATILITY


def test_all_groups_contains_both() -> None:
    assert set(ALL_GROUPS) == {GROUP_VOLATILITY, GROUP_OHLCV}
