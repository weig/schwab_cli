"""Direct contract tests for the pluggable updater registry. The
scheduler-side tests verify dispatch *through* this layer; these
tests pin the registry shape itself so a future plugin breaking the
contract fails here instead of at midnight on a real sync."""
from __future__ import annotations

import pytest

from schwab_cli.dataset.updaters import (
    AccountsUpdater,
    DataUpdater,
    IndicesUpdater,
    MarketDataUpdater,
    UPDATERS,
)


def test_registry_has_three_uniquely_named_entries():
    assert len(UPDATERS) == 3
    names = [u.name for u in UPDATERS]
    assert sorted(names) == ["accounts", "indices", "market-data"]


def test_every_updater_inherits_dataupdater():
    """``UPDATERS: tuple[DataUpdater, ...]`` is only meaningful when
    each entry is actually a DataUpdater. Static type checkers and
    runtime isinstance both need this."""
    for u in UPDATERS:
        assert isinstance(u, DataUpdater), (
            f"{type(u).__name__} doesn't inherit DataUpdater"
        )


def test_registry_is_immutable_tuple():
    """Pluggability is by source edit, not by ``UPDATERS.append`` at
    runtime — the latter would let any importer corrupt the daily
    sync."""
    assert isinstance(UPDATERS, tuple)


@pytest.mark.parametrize("cls,expected_argv", [
    (
        MarketDataUpdater,
        ["/x/schwab", "dataset", "update", "--group", "volatility"],
    ),
    (
        AccountsUpdater,
        ["/x/schwab", "dataset", "accounts", "snapshot"],
    ),
    (
        IndicesUpdater,
        [
            "/x/schwab", "dataset", "update", "--indices",
            "--max-age-days", "6",
            "--anchor-hour", "18",
        ],
    ),
])
def test_spawn_argv_default_shape(cls, expected_argv):
    """Pins the exact argv each updater emits — flag ordering,
    value pairs, no extras."""
    assert cls().spawn_argv(binary="/x/schwab", skip_wait=False) == \
        expected_argv


@pytest.mark.parametrize("cls", [
    MarketDataUpdater, AccountsUpdater, IndicesUpdater,
])
def test_skip_wait_appended_when_set(cls):
    """The operator's manual-rerun escape hatch. Every updater must
    honour the flag."""
    argv = cls().spawn_argv(binary="/x/schwab", skip_wait=True)
    assert argv[-1] == "--skip-wait"


def test_cannot_instantiate_dataupdater_directly():
    """Abstract base — instantiating without overriding spawn_argv
    must fail at construction, not at first call."""
    with pytest.raises(TypeError):
        DataUpdater()  # type: ignore[abstract]
