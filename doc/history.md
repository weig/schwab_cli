# `history`

OHLCV candles for a stock or option over a date range.

## Usage

```
schwab_cli history TICKER [--range=...] [--interval=...] [--json | --md]
```

## Argument

| Arg | Purpose |
| --- | --- |
| `TICKER` | Stock (`NVDA`) or option (`NVDA260501C240`). See [ticker](ticker.md) for all supported forms. |

## Flags

| Flag | Default | Purpose |
| --- | --- | --- |
| `--range` | `-1y..now` | Window spec. See [time](time.md). |
| `--interval` | `1day` | Candle size: `1min`, `5min`, `10min`, `15min`, `30min`, `1day`, `1wk`, `1mo`. |
| `--json` | | JSON envelope with candles as an array. |
| `--md` | | Markdown table. |

## Example: stock

```
$ schwab_cli history NVDA --range=-5d..now --interval=1day
NVDA — 1day · 2026-04-17 → 2026-04-22

DATE         OPEN    HIGH    LOW    CLOSE   VOLUME    CHG    CHG%
2026-04-17  198.12  202.45  197.80  201.68  98.3M    +2.12   +1.06%
2026-04-20  202.00  203.10  200.90  202.06  45.7M    +0.38   +0.19%
2026-04-21  201.50  201.90  198.70  199.88  67.2M    -2.18   -1.08%
2026-04-22  199.95  202.85  199.50  202.50  62.8M    +2.62   +1.31%
```

## Example: option (same command)

```
$ schwab_cli history 'NVDA  260501C00202500' --range=-5d..now --interval=1day
NVDA  260501C00202500 — 1day · 2026-04-17 → 2026-04-22

DATE         OPEN    HIGH    LOW    CLOSE   VOLUME    CHG    CHG%
2026-04-17   4.20    4.95    3.80    4.75    1,542    +0.55  +13.1%
2026-04-20   4.50    5.10    4.25    4.95    2,211    +0.20   +4.21%
2026-04-21   4.80    4.95    3.50    3.70    3,114    -1.25  -25.25%
2026-04-22   3.80    4.87    3.62    4.75      484    +1.05  +28.38%
```

## Example: `--json`

```json
{
  "symbol": "NVDA",
  "interval": "1day",
  "from": "2025-04-23T00:00:00-04:00",
  "to": "2026-04-22T23:59:59-04:00",
  "previousClose": 198.56,
  "candles": [
    {
      "datetime": "2026-04-22",
      "open": 199.95,
      "high": 202.85,
      "low": 199.50,
      "close": 202.50,
      "volume": 62800000,
      "change": 2.62,
      "changePct": 1.31
    }
  ]
}
```

## Example: `--md`

```markdown
# NVDA — 1day (2026-04-17 → 2026-04-22)

| Date | Open | High | Low | Close | Volume | Chg | Chg% |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2026-04-17 | 198.12 | 202.45 | 197.80 | 201.68 | 98,321,456 | +2.12 | +1.06% |
| 2026-04-18 | 201.50 | 203.20 | 200.80 | 202.06 | 45,712,983 | +0.38 | +0.19% |
```

## Range examples

```bash
# Year to date
schwab_cli history NVDA --range=ytd --interval=1day

# Last 30 days, one row per day
schwab_cli history NVDA --range=-30d..now --interval=1day

# Explicit date window
schwab_cli history NVDA --range=20240101..20240601 --interval=1wk

# One day of minute candles
schwab_cli history NVDA --range=20260422..20260422 --interval=5min
```

See [time](time.md) for the full range grammar.

## Option tickers

Any of the accepted [ticker forms](ticker.md) works — the command
auto-pads to the canonical Schwab OSI symbol before hitting
`/pricehistory`.

```bash
schwab_cli history NVDA260501C202.5 --range=-10d..now
schwab_cli history 'NVDA  260501C00202500' --range=-10d..now   # same result
schwab_cli history NVDA260501C00202500 --range=-10d..now       # same result
```

Use this with the underlying's history to compute implied vol / greeks
for past dates when the live `option` endpoint has moved on.

## Notes

- Schwab retains roughly 10 days of 1-minute candles. For longer-horizon
  minute data, use 5-minute or higher granularity.
- The most recent candle is live until the session closes; it may update
  intra-day.
- Timestamps are NY-market-time labels (see [time](time.md)), not UTC.
