# `vol`

Volatility context for a stock — implied vol (IV), historical vol (HV),
HV percentile (HVP), and put/call ratio (P/C). Phase 1 ships without
any local storage. IVP is a placeholder until phase 2 wires up local
accumulation (see
`docs/superpowers/plans/2026-04-23-schwab-cli-vol-command.md`).

## Usage

```
schwab_cli vol SYMBOL [--hv-window=N] [--hv-lookback=N] [--json | --md]
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
| `--json` | | JSON envelope. |
| `--md` | | GitHub-flavoured markdown. |

## Example: HUMAN

```
$ schwab_cli vol NVDA
NVDA  $202.50
────────────────────────────────────────────────────────────
 IV            36.48%  ATM 2026-04-24, 1 DTE, strike $202.50
 HV            31.61%  30-day realized
 HVP              39%  252-day percentile (179/252 available)
 P/C vol         0.43  puts/calls, volume, all expiries
 P/C OI          0.58  puts/calls, open interest, all expiries
 IVP                —  not yet active (phase 2)
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
| **IVP** | Placeholder in phase 1. Phase 2 will accumulate daily ATM IV in a local SQLite DB and rank today's value against the 252-day series. | Local storage (phase 2). |

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

## IVP state machine (for future phase-2 rendering)

| State | Meaning |
| --- | --- |
| `not_yet_active` | Phase 2 not deployed (current build). |
| `insufficient` | < 30 days of snapshots accumulated. |
| `partial` | 30–251 days accumulated. Value is shown with `(N/252 days)` annotation. |
| `ok` | ≥ 252 days of snapshots; full-year percentile. |

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
