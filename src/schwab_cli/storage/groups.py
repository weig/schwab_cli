"""Discriminator constants for the ``group_name`` column shared by
``subscriptions``, ``index_subscriptions``, and ``ticker_state``.

A subscription row says "this symbol opts into this data product".
``"volatility"`` is the original group — daily ATM-IV snapshot + HV
+ tier state. ``"ohlcv"`` is Phase 2's new group — daily OHLCV
candles cached locally so the cron doesn't refetch 110d of history
per run.

Future groups (``"fundamentals"``, ``"intraday_1m"``, …) plug in here
without schema changes — the column is already wide-open TEXT.
"""

GROUP_VOLATILITY: str = "volatility"
GROUP_OHLCV: str = "ohlcv"

ALL_GROUPS: tuple[str, ...] = (GROUP_VOLATILITY, GROUP_OHLCV)
