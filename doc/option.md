# `option`

Show an option chain for an underlying at a specific expiry, filtered
around a strike window. For detailed greeks on **one specific contract**,
see [greeks](greeks.md).

## Usage

```
schwab_cli option SYMBOL SPEC [--strikes=N] [--detail=N] [--json | --md]
```

## Arguments

| Arg | Purpose |
| --- | --- |
| `SYMBOL` | Underlying stock ticker, e.g. `NVDA`. |
| `SPEC` | Expiry + optional strike window. See *Spec grammar* below. Quote it if the `*` character would otherwise be expanded by your shell. |

## Flags

| Flag | Default | Purpose |
| --- | --- | --- |
| `--strikes=N` | 10 | How many strikes around the target to include (total across both sides). |
| `--detail=N` | 0 | Presentation level — 0 = compact side-by-side, 1 = stacked with greeks, 2 = stacked + sub-table. |
| `--json` | | Emit JSON. |
| `--md` | | Emit markdown. |

## Spec grammar

| Spec | Meaning |
| --- | --- |
| `260501` | All strikes around ATM, expiring 2026-05-01. |
| `260501*250` | All strikes around $250, expiring 2026-05-01. |
| `260501C250` | Calls only, strike 250. |
| `260501P*` | All put strikes around ATM. |
| `270115*230` | LEAPS example — strikes around $230, expiring 2027-01-15. |

The `*` is a wildcard at whichever position is unfilled (strike or side).
Quote the spec in shells that glob `*`.

## Example: `--detail=0` (default)

```
$ schwab_cli option NVDA 260501*200 --strikes=3
NVDA — 2026-05-01 (9 DTE)    Spot: $202.50  (+2.62 / +1.31%)

         CALLS                         PUTS
BID   ASK   LAST    Δ    STRIKE    BID   ASK   LAST    Δ
6.15  6.25  6.20  0.595   200.00   3.50  3.60  3.50  -0.405
4.70  4.80  4.75  0.510   202.50   4.55  4.65  4.60  -0.489
3.45  3.55  3.50  0.425   205.00   6.25  6.40  6.30  -0.575
```

## Example: `--detail=1` (stacked, with greeks)

```
$ schwab_cli option NVDA 260501*202.5 --strikes=1 --detail=1
Symbol                 Side  Strike   Bid   Ask  Last     IV      Δ      Γ       Θ      𝒱    Vol     OI
NVDA  260501C00202500  C     202.50  4.70  4.80  4.75  0.366  0.510  0.035  -0.267  0.125  8,809  5,174
NVDA  260501P00202500  P     202.50  4.55  4.65  4.60  0.366 -0.489  0.035  -0.270  0.125  2,199  2,940
```

## Example: `--detail=2` (stacked with sub-table)

Same data as `--detail=1` but with quote / greeks / activity broken out
into labelled sub-tables per contract. Best for one or two strikes at a
time — the output gets tall with many rows.

## Example: `--json`

```json
{
  "symbol": "NVDA",
  "expiry": "2026-05-01T20:00:00.000+00:00",
  "dte": 9,
  "underlying": { "last": 202.50, "netChange": 2.62, "pctChange": 1.31 },
  "contracts": [
    {
      "optionSymbol": "NVDA  260501C00202500",
      "side": "C",
      "strike": 202.50,
      "bid": 4.70,
      "ask": 4.80,
      "last": 4.75,
      "delta": 0.510,
      "gamma": 0.035,
      "theta": -0.267,
      "vega": 0.125,
      "iv": 0.36582,
      "volume": 8809,
      "openInterest": 5174
    }
  ]
}
```

## Notes

- Schwab's `strikeCount` is per-side; `--strikes=N` is total across
  calls + puts, so we pass `ceil(N/2)` to the API and trim further
  client-side if needed.
- Greek values come straight from Schwab's chain response. They use
  a simple Black-Scholes model and may differ from ThinkOrSwim's
  smile-adjusted numbers by a few percent. See [greeks](greeks.md)
  for the full discussion.
- Expiries in the past are rejected with exit code 1.
