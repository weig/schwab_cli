# `fundamentals`

Company fundamentals for one or more symbols: valuation, profitability,
balance-sheet, ownership, and headline dividend context — powered by a
single `/quotes?fields=quote,fundamental` call.

## Usage

```
schwab_cli fundamentals SYMBOL [SYMBOL ...] [--json | --md]
```

## Argument

| Arg | Purpose |
| --- | --- |
| `SYMBOL ...` | One or more stock tickers. Options/ETFs return whatever fundamental fields Schwab populates — sparse for derivatives. |

## Flags

| Flag | Purpose |
| --- | --- |
| `--json` | JSON array, one entry per symbol. Full `fundamental` block preserved. |
| `--md` | Markdown table with the headline columns. Good for PR/ticket comments. |

## Example: HUMAN, single symbol

```
$ schwab_cli fundamentals AAPL
AAPL  $232.14
────────────────────────────────────────────────────────────
  Price
  Last                              $232.14
  52W High                           260.10
  52W Low                            164.08
  Beta                                 1.25
  Valuation
  Market Cap                         $3.43T
  P/E                                 33.85
  PEG                                  3.21
  P/B                                 63.52
  EPS (TTM)                            6.54
  EPS Δ (TTM)                        10.85%
  Rev Δ (TTM)                         4.81%
  Profitability
  Gross Margin                       46.86%
  Op Margin                          31.03%
  Net Margin                         24.30%
  ROE                               160.58%
  Balance Sheet
  Current Ratio                        0.87
  Debt/Equity                       146.99%
  Dividends
  Yield                               0.44%
  Amount (annual)                      1.00
  Ownership
  Shares Out                14,855,911,000
```

## Example: HUMAN, multiple symbols

Each symbol gets its own stacked block — a wide table would be
unreadable in a terminal for thirty-plus metrics, so we stack:

```
$ schwab_cli fundamentals AAPL MSFT
AAPL  $232.14
────────────────────────────────────────────────────────────
…

MSFT  $450.00
────────────────────────────────────────────────────────────
…
```

For cross-symbol comparison use `--md` — it renders one row per
symbol with the headline columns.

## Example: `--md`

```
$ schwab_cli fundamentals AAPL MSFT --md
| Symbol | Last | Market Cap | P/E | PEG | EPS (TTM) | EPS Δ (TTM) | Rev Δ (TTM) | Div Yield | Beta | 52W High | 52W Low |
|--------|-----:|-----------:|----:|----:|----------:|------------:|------------:|----------:|-----:|---------:|--------:|
| AAPL | $232.14 | $3.43T | 33.85 | 3.21 | 6.54 | 10.85% | 4.81% | 0.44% | 1.25 | 260.10 | 164.08 |
| MSFT | $450.00 | $3.35T | 35.00 | —    | 12.90 | —      | —     | —     | —   | —      | —       |
```

## Example: `--json`

JSON preserves the full Schwab `fundamental` block plus `last` from the
quote block, so any field we don't render in HUMAN/MD is still accessible
downstream.

```json
[
  {
    "symbol": "AAPL",
    "last": 232.14,
    "fundamental": {
      "high52": 260.10,
      "low52": 164.08,
      "peRatio": 33.85,
      "pegRatio": 3.21,
      "pbRatio": 63.52,
      "epsTTM": 6.54,
      "epsChangePercentTTM": 10.85,
      "revChangeTTM": 4.81,
      "grossMarginTTM": 46.86,
      "netProfitMarginTTM": 24.30,
      "operatingMarginTTM": 31.03,
      "returnOnEquity": 160.58,
      "currentRatio": 0.87,
      "totalDebtToEquity": 146.99,
      "marketCap": 3430000000000,
      "sharesOutstanding": 14855911000,
      "beta": 1.25,
      "dividendYield": 0.44,
      "dividendAmount": 1.00
    }
  }
]
```

## Fields surfaced in HUMAN/MD

| Section | Field | Schwab key |
| --- | --- | --- |
| Price | 52W high/low | `high52`, `low52` |
| Price | Beta | `beta` |
| Valuation | Market cap | `marketCap` |
| Valuation | P/E, PEG, P/B | `peRatio`, `pegRatio`, `pbRatio` |
| Valuation | EPS (TTM), EPS Δ, Rev Δ | `epsTTM`, `epsChangePercentTTM`, `revChangeTTM` |
| Profitability | Gross / Op / Net margin | `grossMarginTTM`, `operatingMarginTTM`, `netProfitMarginTTM` |
| Profitability | ROE | `returnOnEquity` |
| Balance sheet | Current ratio, D/E | `currentRatio`, `totalDebtToEquity` |
| Dividends | Yield, Annual amount | `dividendYield`, `dividendAmount` |
| Ownership | Shares outstanding | `sharesOutstanding` |

More granular dividend output (schedule, ex-dates) lives in the
[`dividends`](dividends.md) command — it reads the same response, but
filters and renders the dividend fields.

## API calls per invocation

Exactly **one** — `/marketdata/v1/quotes?symbols=…&fields=quote,fundamental`.
Batches across symbols in a single call; no per-symbol fan-out.

## Notes

- Schwab reports margin / return / yield values as **percentage values**
  (e.g. `46.86` for 46.86%, `0.44` for 0.44%). The renderer suffixes `%`
  without multiplying, and JSON preserves the raw numeric form.
- Fields come and go per symbol depending on Schwab's data source. ETFs
  tend to have fewer populated fundamentals than common stocks; ADRs and
  foreign-listed tickers are the sparsest.
- The `fundamental` block is cached server-side and not real-time —
  expect delays on the order of minutes to hours for `marketCap`, and
  days-to-quarters for balance-sheet lines.
