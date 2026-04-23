# Time and range syntax

The `history` and `transactions` commands accept a `--range` flag that
describes a date window, and `history` also takes `--interval` for candle
granularity. The same parser powers both.

## `--range`

### Shortcuts

| Input | Meaning |
| --- | --- |
| `ytd` | Jan 1 of the current year → now. |
| `mtd` | The 1st of the current month → now. |
| `wtd` | Most recent Monday → now. |

### Explicit `<start>..<end>` form

Either side can be a **fixed date**, a **relative offset**, or `now`:

| Token | Meaning |
| --- | --- |
| `YYYYMMDD` | Absolute date — `20240101` = Jan 1 2024, NY time. |
| `-Nu` | Relative offset from the other side of the range. `u` is `d`, `w`, `mo`, or `y`. |
| `now` | Current moment (NY time). |

Examples:

| `--range` | Window |
| --- | --- |
| `20240101..20240601` | Jan 1 → Jun 1 2024. |
| `-1y..now` | One year ago → now. |
| `-30d..now` | Last 30 days. |
| `20240101..-3mo` | Never useful but legal — fixed start, relative end. |
| `-7d..now` | One week, used as the default on `transactions`. |
| `-1mo..-1d` | A month ago to yesterday. |

Rules:

- **Start must be before end.** Out-of-order ranges exit with code 1.
- **Start cannot be in the future.** End can be (it's clamped server-side).
- Fixed dates snap: `start → 00:00:00 NY`, `end → 23:59:59 NY`.
- Relative offsets anchor to the other side of the range, not to "now" —
  so `-7d..20240801` means "the 7 days ending 2024-08-01".

### Default ranges per command

- `history`: `-1y..now`
- `transactions`: `-7d..now`

## `--interval` (history only)

| Input | Candle size |
| --- | --- |
| `1min`, `5min`, `10min`, `15min`, `30min` | Intraday minute candles. |
| `1day` | Daily OHLCV (the default). |
| `1wk` | Weekly. |
| `1mo` | Monthly. |

Minute candles have shorter history retention than daily candles. Schwab
serves ~10 days of 1-min candles; daily and above stretch back years.

## Timezone handling

- **All input dates are interpreted in America/New_York**, the market's
  trading day. A range of `20240101..20240101` is Jan 1 2024 Eastern,
  not UTC.
- **Candle timestamps in output** are rendered as:
  - Minute candles → `YYYY-MM-DD HH:MM:SS` in NY time.
  - Daily / weekly / monthly → `YYYY-MM-DD` (the trading day label in NY).
- **JSON output** uses the same NY-formatted strings as HUMAN output so
  day boundaries stay aligned to trading-day semantics, not UTC calendar
  days.

## Errors

The parser emits specific exit codes:

| Kind | Example | Exit |
| --- | --- | --- |
| `invalid` | `--range=banana` or an unknown interval | 2 |
| `ordering` | end before start | 1 |
| `future` | start is in the future | 1 |
| (no candles) | range is valid but Schwab returned zero rows | 1 |

The `invalid` vs `ordering`/`future` distinction mirrors shell convention:
2 means *you typed something wrong*, 1 means *your request was well-formed
but produced no usable result*.
