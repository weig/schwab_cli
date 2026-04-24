# `dividends`

Most-recent and next-upcoming dividend events for one or more symbols —
also aliased as `div`. Uses the same single-call source as
[`fundamentals`](fundamentals.md): `/quotes?fields=quote,fundamental`.

## Usage

```
schwab_cli dividends SYMBOL [SYMBOL ...]
                      [--upcoming [--within-days=N]]
                      [--json | --md]
schwab_cli div ...   # alias
```

## Arguments

| Arg | Purpose |
| --- | --- |
| `SYMBOL ...` | One or more stock tickers. Non-payers render a one-line `No dividend` marker. |

## Flags

| Flag | Default | Purpose |
| --- | --- | --- |
| `--upcoming` | off | Drop symbols whose next ex-date is missing, past, or past the window. |
| `--within-days=N` | 30 | Window (in days from today) used by `--upcoming`. |
| `--json` |  | JSON array, one entry per kept symbol. |
| `--md` |  | Markdown table. |

## Example: HUMAN, single dividend payer

```
$ schwab_cli dividends AAPL
AAPL  $232.14
────────────────────────────────────────────────────────────
  Yield                    0.44%
  Annual amount            $1.00
  Frequency              quarterly
  Pay amount               $0.25
  3yr growth               4.50%
  Last ex-date        2025-05-12
  Last pay date       2025-05-15
  Next ex-date        2025-08-12
  Next pay date       2025-08-15
  Declared            2025-05-01
```

## Example: HUMAN, non-payer

```
$ schwab_cli dividends TSLA
TSLA  $250.00
────────────────────────────────────────────────────────────
No dividend (non-payer or API reports none).
```

## Example: `--upcoming` filter

Great for checking which of a holdings list has an ex-date coming up:

```
$ schwab_cli dividends AAPL KO TSLA JNJ --upcoming --within-days=21
AAPL  $232.14
────────────────────────────────────────────────────────────
  Yield                    0.44%
  …  (next ex-date 2025-08-12, inside 21-day window)
JNJ   $165.30
────────────────────────────────────────────────────────────
  Yield                    3.20%
  …  (next ex-date 2025-08-24, inside 21-day window)
```

`KO` is a payer but its next ex-date is outside the 21-day window, and
`TSLA` is a non-payer — both drop out of the render.

## Example: `--md`

```
$ schwab_cli dividends AAPL KO --md
| Symbol | Yield | Annual | Pay | Freq | Next ex-date | Next pay date | Last ex-date | 3yr growth |
|--------|------:|-------:|----:|------|-------------:|--------------:|-------------:|-----------:|
| AAPL | 0.44% | $1.00 | $0.25 | quarterly | 2025-08-12 | 2025-08-15 | 2025-05-12 | 4.50% |
| KO   | 2.91% | $2.04 | $0.51 | quarterly | 2025-09-15 | 2025-10-01 | 2025-06-15 | 5.00% |
```

## Example: `--json`

```json
[
  {
    "symbol": "AAPL",
    "last": 232.14,
    "amount_annual": 1.00,
    "yield_pct": 0.44,
    "frequency_per_year": 4,
    "pay_amount": 0.25,
    "last_ex_date": "2025-05-12",
    "last_pay_date": "2025-05-15",
    "declaration_date": "2025-05-01",
    "next_ex_date": "2025-08-12",
    "next_pay_date": "2025-08-15",
    "growth_rate_3y_pct": 4.50,
    "is_payer": true
  }
]
```

## Fields surfaced

| Rendered label | Schwab key | Notes |
| --- | --- | --- |
| Yield | `dividendYield` | Percentage value (0.44 means 0.44%), rendered with `%` suffix. |
| Annual amount | `dividendAmount` | Trailing-twelve-months total. |
| Frequency | `dividendFreq` | Integer pay events / year → labelled (1=annual, 4=quarterly, 12=monthly). |
| Pay amount | `dividendPayAmount` | Per-share amount for the most recent distribution. |
| 3yr growth | `divGrowthRate3Year` | Percentage value; a few symbols report 0 even when growing. |
| Last ex-date / pay date | `dividendDate`, `dividendPayDate` | Most recent distribution. |
| Next ex-date / pay date | `nextDividendDate`, `nextDividendPayDate` | Upcoming distribution; both may be blank between announcements. |
| Declared | `declarationDate` | Board declaration for the most recent distribution. |

## API calls per invocation

Exactly **one** — same endpoint shape as [`fundamentals`](fundamentals.md).
Batches across every symbol you pass.

## Limitations — what the API does NOT give us

- **No historical series.** Schwab's fundamental block only reports the
  most-recent pay and the next upcoming event. There is no endpoint to
  fetch "last eight quarters of AAPL dividends" retroactively. If you
  need a long history you'd either:
    - accumulate your own row per daily `dividends` run into a local
      SQLite store (same pattern the [`vol`](vol.md) command uses for
      IVP), or
    - pull history from a non-Schwab source (Yahoo, FMP, etc.).

- **No dividend sub-types exposed.** Special dividends, stock splits,
  and DRIP internals aren't distinguishable in the returned fields — you
  just see the scheduled regular payout.

- **Announcement lag.** Between a distribution being paid and the next
  being declared, `nextDividendDate`/`nextDividendPayDate` can both be
  blank. `--upcoming` will drop such symbols.

## Notes

- The `--upcoming` filter keeps the row **only if** the next ex-date is
  `today <= ex_date <= today + within_days`. Past ex-dates are always
  dropped under `--upcoming`, even if the distribution hasn't been paid
  yet (use no-flag mode to see a paid-but-not-yet-posted row).
- Dates returned by Schwab look like `'2025-05-12 04:00:00.0'`; we split
  on the first space and render the calendar day. Time-of-day is an
  internal artefact, not a record cut-off.
