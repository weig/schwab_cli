from __future__ import annotations

from datetime import datetime
from typing import Literal

from schwab_cli.api.client import SchwabClient
from schwab_cli.ticker import to_schwab_form

# Schwab's /pricehistory validates `frequencyType` against `periodType`:
#   day   → minute only
#   month → daily, weekly
#   year  → daily, weekly, monthly
#   ytd   → daily, weekly
# We drive by frequencyType and pick the widest compatible periodType so any
# start/end date works without tripping the period/frequencyType cross-check.
_PERIOD_TYPE: dict[str, str] = {
    "minute": "day",
    "daily": "year",
    "weekly": "year",
    "monthly": "year",
}


def get_history(
    client: SchwabClient,
    symbol: str,
    *,
    frequency_type: Literal["minute", "daily", "weekly", "monthly"],
    frequency: int,
    start: datetime,
    end: datetime,
    need_previous_close: bool = True,
    need_extended_hours: bool = False,
) -> dict:
    """Fetch raw OHLCV candle data for `symbol` over [start, end].

    `start` and `end` must be tz-aware datetimes; Schwab wants epoch ms (UTC).
    """
    params: dict[str, str | int] = {
        "symbol": to_schwab_form(symbol),
        "periodType": _PERIOD_TYPE[frequency_type],
        "frequencyType": frequency_type,
        "frequency": frequency,
        "startDate": int(start.timestamp() * 1000),
        "endDate": int(end.timestamp() * 1000),
        "needPreviousClose": "true" if need_previous_close else "false",
        "needExtendedHoursData": "true" if need_extended_hours else "false",
    }
    return client.get(f"{SchwabClient.MARKET_BASE}/pricehistory", params=params)
