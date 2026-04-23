# `quote`

Real-time quote(s) for one or more stocks.

## Usage

```
schwab_cli quote SYMBOL [SYMBOL ...] [--json | --md]
```

## Argument

| Arg | Purpose |
| --- | --- |
| `SYMBOL ...` | One or more stock tickers. Options are not supported here — use [`greeks`](greeks.md) or [`option`](option.md) for contract pricing. |

## Flags

| Flag | Purpose |
| --- | --- |
| `--json` | JSON array, one entry per symbol. |
| `--md` | Markdown table. |

## Example: HUMAN, single symbol

```
$ schwab_cli quote NVDA
SYMBOL    LAST     BID      ASK     CHANGE   CHANGE%   VOLUME
NVDA    $202.50  $202.42  $202.50    +2.62    +1.31%  107,501,042
```

## Example: HUMAN, multiple

```
$ schwab_cli quote NVDA AAPL SPY
SYMBOL    LAST     BID      ASK    CHANGE   CHANGE%    VOLUME
NVDA    $202.50  $202.42  $202.50   +2.62    +1.31%  107,501,042
AAPL    $195.00  $194.98  $195.00   -1.20    -0.61%   45,678,901
SPY     $525.50  $525.48  $525.52   +3.45    +0.66%   78,123,456
```

## Example: `--json`

```json
[
  {
    "symbol": "NVDA",
    "last": 202.50,
    "bid": 202.42,
    "ask": 202.50,
    "change": 2.62,
    "changePct": 1.31,
    "volume": 107501042
  }
]
```

## Notes

- Quotes reflect the Schwab feed at request time; they are the same
  values ThinkOrSwim and the Schwab web app would show for a single
  retail trader.
- After market hours the `last` is the closing print; `bid`/`ask` may
  be the closing book or zero depending on the symbol.
