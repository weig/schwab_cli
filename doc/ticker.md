# Ticker format

`schwab_cli` accepts tickers in the forms you already use when looking at
option quotes. The resolver (`schwab_cli.ticker`) normalises them all to
the canonical OSI-padded form Schwab's API expects internally.

## Stock

A stock ticker is 1–5 uppercase letters, optionally with a `.X`
share-class suffix.

| Input | Resolves to |
| --- | --- |
| `NVDA` | stock NVDA |
| `nvda` | stock NVDA (case is normalised) |
| `BRK.B` | stock BRK.B |
| `SPY` | stock SPY |

## Option

An option ticker combines an underlying, an expiry (`YYMMDD`), a `C`/`P`
for call/put, and a strike. All of these resolve to the **same contract**
(NVDA 2026-05-01 call, strike $240):

| Input | Notes |
| --- | --- |
| `NVDA260501C240` | Compact form — integer strike. |
| `NVDA260501C240.0` | Decimal strike. |
| `NVDA260501C202.5` | Fractional strike (.5, .25, etc.). |
| `NVDA  260501C240` | Schwab-style padding (2 spaces). |
| `NVDA260501C00240000` | Full OSI 8-digit strike (strike × 1000, zero-padded). |
| `NVDA  260501C00240000` | Canonical form — what the Schwab API sees. |

Resolution rules:

1. Leading/trailing whitespace is stripped; input is uppercased.
2. Internal whitespace between the underlying and the date is collapsed.
3. A strike with a decimal point (`240.5`) is parsed as a float.
4. A strike that is **exactly 8 digits** and no decimal point is treated
   as OSI and divided by 1000 (`00240000` → $240.000). Any shorter
   all-digit strike is a plain integer dollar amount.

## Where tickers are accepted

| Command | Accepts |
| --- | --- |
| `quote` | Stocks only (one or more). |
| `option` | Underlying (stock) plus a separate spec string for expiry + strike. |
| `greeks` | Option tickers only — any of the forms above. |
| `history` | Either stocks or options. |

Example — `history` accepts all of these identically:

```bash
schwab_cli history NVDA                     # stock
schwab_cli history NVDA260501C202.5         # option, compact
schwab_cli history 'NVDA  260501C00202500'  # option, canonical (quoted for the space)
```

## Programmatic use

The resolver is a plain Python module — import it if you're scripting:

```python
from schwab_cli.ticker import resolve

t = resolve("NVDA260501C240")
# Ticker(type='option', underlying='NVDA',
#        option=OptionPart(date='20260501', type='C', strike=240.0))

t.to_schwab_symbol()   # 'NVDA  260501C00240000'
t.to_dict()            # {'type': 'option', 'underlying': 'NVDA',
                       #  'option': {'date': '20260501', 'type': 'C', 'strike': 240.0}}
```

Invalid inputs raise `schwab_cli.ticker.TickerError`:

```
NVDA260501       # missing C/P + strike
NVDA260501X240   # wrong put/call letter
NVDA-260501C240  # wrong separator
```
