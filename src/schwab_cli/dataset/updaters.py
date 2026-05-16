"""Pluggable data updaters dispatched by the unified scheduler.

Each updater describes one independent daily sync task — its name
(used in logs, Telegram alerts, and ``last_run.json``) and how to
spawn it as a subprocess. The scheduler iterates :data:`UPDATERS`
and spawns each in parallel; one failure doesn't cascade.

Adding a new data type means: define one ``DataUpdater`` subclass
and append it to :data:`UPDATERS`. No scheduler edits required.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class DataUpdater(ABC):
    """Abstract base for one dispatchable data-sync task.

    Subclasses set ``name`` (class attribute) and implement
    :meth:`spawn_argv`. Anything else (anchor hour, freshness
    threshold, retry policy) lives as a field on the subclass — the
    scheduler doesn't need to know about job-specific configuration.

    Inheriting from :class:`abc.ABC` means a subclass that forgets to
    override ``spawn_argv`` raises ``TypeError`` at construction
    rather than only at first call.
    """
    # No default — subclasses MUST override. Empty would let a
    # nameless updater silently slip through type-checking.
    name: str

    @abstractmethod
    def spawn_argv(
        self, *, binary: str, skip_wait: bool,
    ) -> list[str]:
        """Return the argv for running this updater as an isolated
        subprocess.

        ``skip_wait`` is the operator's manual-rerun override — when
        True, the child should bypass any ``sleep_until_ny`` anchor.
        Subclasses must honour the flag if they support waiting.
        """
        ...


@dataclass(frozen=True)
class MarketDataUpdater(DataUpdater):
    """Volatility-group sync: chain snapshot + OHLCV write.

    Anchors internally to NY 17:00 ET via ``sleep_until_ny`` inside
    the ``dataset update --group volatility`` command.
    """
    name: str = "market-data"

    def spawn_argv(
        self, *, binary: str, skip_wait: bool,
    ) -> list[str]:
        argv = [binary, "dataset", "update", "--group", "volatility"]
        if skip_wait:
            argv.append("--skip-wait")
        return argv


@dataclass(frozen=True)
class AccountsUpdater(DataUpdater):
    """Daily account NAV snapshot for every subscribed account.

    Anchors internally to NY 17:00 ET so the snapshot reflects the
    day's close.
    """
    name: str = "accounts"

    def spawn_argv(
        self, *, binary: str, skip_wait: bool,
    ) -> list[str]:
        argv = [binary, "dataset", "accounts", "snapshot"]
        if skip_wait:
            argv.append("--skip-wait")
        return argv


@dataclass(frozen=True)
class IndicesUpdater(DataUpdater):
    """Index-constituent membership sync.

    Anchored to NY 18:00 ET (one hour after market-data) so the
    *outbound* HTTP request to the constituent provider is spaced
    from the market-data job's chain pulls. ``max_age_days`` makes
    a daily dispatch behave as weekly — the child short-circuits
    when the local ``subscriptions`` table was last touched inside
    that window.
    """
    name: str = "indices"
    max_age_days: int = 6
    anchor_hour: int = 18

    def spawn_argv(
        self, *, binary: str, skip_wait: bool,
    ) -> list[str]:
        argv = [
            binary, "dataset", "update", "--indices",
            "--max-age-days", str(self.max_age_days),
            "--anchor-hour", str(self.anchor_hour),
        ]
        if skip_wait:
            argv.append("--skip-wait")
        return argv


# Tuple (not list) so importers can't ``.append`` plugins at runtime
# from random call sites — pluggability is by source edit, not by
# mutable global. Adding a new updater = one line here + one new
# subclass above. Order is meaningless: children run in parallel and
# their results are reported by name.
UPDATERS: tuple[DataUpdater, ...] = (
    MarketDataUpdater(),
    AccountsUpdater(),
    IndicesUpdater(),
)

# Fail-fast invariant: name uniqueness is load-bearing because
# ``last_run.json`` keys jobs by ``updater.name``. A duplicate would
# silently clobber peer results.
assert len({u.name for u in UPDATERS}) == len(UPDATERS), (
    "UPDATERS registry has duplicate names"
)
