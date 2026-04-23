# `vol`

Volatility context for a stock — implied vol (IV), historical vol (HV),
HV percentile (HVP), put/call ratio (P/C), and IV percentile (IVP).

Each invocation makes **two** Schwab API calls (one chain, one
year-long price history) and appends one row to a local SQLite store
so IVP can ripen over time.

## Usage

```
schwab_cli vol SYMBOL [--hv-window=N] [--hv-lookback=N]
                      [--ivp-lookback=N] [--no-record] [--snapshot-only]
                      [--json | --md]
```

## Argument

| Arg | Purpose |
| --- | --- |
| `SYMBOL` | Stock ticker, e.g. `NVDA`. Options are rejected — use [`greeks`](greeks.md) for a single contract. |

## Flags

| Flag | Default | Purpose |
| --- | --- | --- |
| `--hv-window=N` | 30 | Rolling HV window in trading days. |
| `--hv-lookback=N` | 252 | HVP percentile lookback in trading days (~1 year). |
| `--ivp-lookback=N` | 252 | IVP percentile lookback in trading days. |
| `--no-record` | off | Skip appending today's ATM IV to the store. |
| `--snapshot-only` | off | Write today's snapshot and exit silently. Cron-friendly. |
| `--json` | | JSON envelope. |
| `--md` | | GitHub-flavoured markdown. |

## Example: HUMAN

First run against a fresh store:

```
$ schwab_cli vol NVDA
NVDA  $202.50
────────────────────────────────────────────────────────────
 IV            36.48%  ATM 2026-04-24, 1 DTE, strike $202.50
 HV            31.61%  30-day realized
 HVP              39%  252-day percentile (179/252 available)
 P/C vol         0.43  puts/calls, volume, all expiries
 P/C OI          0.58  puts/calls, open interest, all expiries
 IVP                —  insufficient history: 1/252 days
```

After a full year of daily runs (or via the cron pattern below):

```
IVP              54%  252-day percentile
```

## Example: `--json`

```json
{
  "symbol": "NVDA",
  "spot": 202.50,
  "iv": {
    "value": 0.3648,
    "expiry": "2026-04-24",
    "dte": 1,
    "strike": 202.50
  },
  "hv": { "window": 30, "value": 0.3161 },
  "hvp": { "lookback": 252, "value": 39.0, "sample_size": 179 },
  "pc": {
    "volume_ratio": 0.43,
    "oi_ratio": 0.58,
    "call_volume": 123456,
    "put_volume": 53056,
    "call_oi": 987654,
    "put_oi": 572839
  },
  "ivp": {
    "state": "not_yet_active",
    "value": null,
    "sample_size": 0,
    "lookback": 252
  }
}
```

## How each metric is computed

| Metric | Formula | Source |
| --- | --- | --- |
| **IV** | Midpoint of call IV and put IV at the strike closest to spot, in the nearest expiry with ≥ 100 volume across both legs. | `/chains` (call 1) |
| **HV** | `stdev(ln(Cₜ/Cₜ₋₁)) × √252` over the last 30 daily closes. | `/pricehistory` (call 2) |
| **HVP** | Percentile rank of today's HV within the rolling-30 HV series for the past 252 trading days. Uses midrank for ties. | Same history call. |
| **P/C vol** | `Σ put_volume / Σ call_volume` across every strike and every expiry in the chain response. | Chain, no extra call. |
| **P/C OI** | `Σ put_OI / Σ call_OI`, same aggregation. | Chain, no extra call. |
| **IVP** | Percentile rank of today's ATM IV against the accumulated daily ATM IV series in the local store. | Local SQLite (populated by this command). |

## API calls per invocation

Exactly **two**:

1. `/chains NVDA` with `strike_count=60`, `contract_type=ALL`, spanning
   today through +365 days — covers most of the open interest in one
   request. Powers IV + P/C.
2. `/pricehistory NVDA --range=~-1y..now --interval=1day` — powers HV + HVP.

This is a deliberate design choice. `option` and `greeks` do **not**
fetch extra data on your behalf; `vol` is the only command that runs
the broad volatility survey. Keep usage occasional (a handful of
symbols per day) to stay well within the individual-developer rate
limits.

## When HVP reports fewer than 252 days

The HV percentile uses whatever daily history Schwab returned. The
`(179/252 available)` note means only 179 rolling-30 HV values were
computable — typically because the symbol's listing history is
shorter than a year, or because the 1-year price-history call was
truncated server-side. The percentile is computed against the
available sample, so it's still useful — just note the denominator.

## IVP state machine

| State | Trigger | Rendering |
| --- | --- | --- |
| `insufficient` | fewer than `min(30, --ivp-lookback)` distinct NY trading days in the store | value `—`, note `insufficient history: N/lookback days` |
| `partial` | between the minimum and the lookback window | value `XX%`, note `partial: N/lookback days` |
| `ok` | at least `--ivp-lookback` days accumulated | value `XX%`, note `lookback-day percentile` |

The per-day collapse keeps the sample useful: the CLI can run many
times in a trading day without inflating the sample — only the latest
write per NY trading day counts.

## Local storage

The store lives at `~/.config/schwab_cli/storage/vol_history.db` by
default. Override the directory with the `SCHWAB_CLI_STORAGE` env var
(mirrors the `SCHWAB_CLI_CONFIG` pattern) — useful for scripting or
keeping separate stores per broker account.

Schema is idempotent — every invocation runs `CREATE TABLE IF NOT
EXISTS` before writing, and `INSERT OR IGNORE` on the
`(captured_at_ms, symbol)` primary key keeps rerun-within-the-same-
millisecond safe. The DB is < 1 KB per symbol per year of daily
snapshots.

No other command writes to this store. `option` and `greeks` stay
side-effect-free.

## Populating IVP without watching it

`--snapshot-only` captures today's ATM IV silently and exits 0 — ideal
for cron. A minimal setup that ripens IVP for a symbol over a year:

```cron
# Record NVDA's daily ATM IV at 3:55 pm ET, Mon-Fri.
55 15 * * 1-5 schwab_cli vol NVDA --snapshot-only
```

After ~30 trading days IVP starts rendering a `partial` percentile;
after ~252 it graduates to `ok`.

## Notes

- Schwab's chain endpoint returns IV as a percentage (e.g. 36.582).
  This command exposes it as a fraction (0.36582) in JSON and a percent
  string in HUMAN/MD output.
- P/C "all expiries" is literal: every expiration Schwab returned
  inside the 365-day window we requested. If you want near-dated-only
  figures, narrow `strike_count` in a future flag (not currently
  exposed).
- The ATM picker deliberately skips very-low-volume weeklies so the
  reported IV isn't a stale quote from an illiquid strike.
