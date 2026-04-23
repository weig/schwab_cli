# `greeks`

Detailed view for a single option contract — quote, greeks, and value
decomposition. For a chain of strikes around a target, use
[option](option.md) instead.

## Usage

```
schwab_cli greeks TICKER [--json | --md]
```

## Argument

| Arg | Purpose |
| --- | --- |
| `TICKER` | Option ticker in any supported form — see [ticker](ticker.md). |

Accepted forms (all resolve to the same contract):

- `NVDA260501C240` — compact.
- `NVDA260501C240.0` — decimal strike.
- `NVDA260501C202.5` — fractional strike.
- `NVDA  260501C00240000` — canonical OSI (quote it for the space).
- `NVDA260501C00240000` — OSI without padding.

## Flags

| Flag | Purpose |
| --- | --- |
| `--json` | JSON envelope, one contract. |
| `--md` | Markdown with Quote / Greeks / Value sections. |

## Example: HUMAN

```
$ schwab_cli greeks NVDA260501C202.5
NVDA 2026-05-01 CALL $202.50  (NVDA  260501C00202500)
Expiry 2026-05-01 (9 DTE)
Underlying  $202.50  +2.62 / +1.31%

Quote                 Greeks
 Bid        $4.70      Δ  delta           0.5100
 Ask        $4.80      Γ  gamma           0.0350
 Mid        $4.75      Θ  theta     -0.2670 /day
 Last       $4.75      𝒱  vega     0.1250 /1% IV
 Mark       $4.75      ρ  rho    0.0230 /1% rate
 Volume     8,809      IV               36.58%
 Open Int.  5,174

Value
 Intrinsic                             $0.00
 Extrinsic (time)                      $4.75
 Break-even        $207.25  (+2.35% vs spot)
 In the money                             no
 Multiplier                              100
 Settlement                                P
```

## Example: `--json`

```json
{
  "underlyingSymbol": "NVDA",
  "expiry": "2026-05-01",
  "dte": 9,
  "underlying": {
    "last": 202.50,
    "netChange": 2.62,
    "pctChange": 1.31
  },
  "contract": {
    "optionSymbol": "NVDA  260501C00202500",
    "side": "C",
    "strike": 202.5,
    "bid": 4.70,
    "ask": 4.80,
    "last": 4.75,
    "mark": 4.75,
    "delta": 0.510,
    "gamma": 0.035,
    "theta": -0.267,
    "vega": 0.125,
    "rho": 0.023,
    "iv": 0.36582,
    "volume": 8809,
    "openInterest": 5174,
    "timeValue": 4.75,
    "intrinsic": 0.0,
    "inTheMoney": false,
    "multiplier": 100,
    "settlementType": "P"
  }
}
```

## Example: `--md`

```markdown
# NVDA 2026-05-01 CALL $202.50

**Contract:** `NVDA  260501C00202500`
**Expiry:** 2026-05-01 (9 DTE)
**Underlying:** $202.50 (+2.62 / +1.31%)

## Quote

| Field | Value |
| --- | ---: |
| Bid | $4.70 |
| Ask | $4.80 |
| Mid | $4.75 |
| Last | $4.75 |
| Mark | $4.75 |
| Volume | 8,809 |
| Open Interest | 5,174 |

## Greeks

| Greek | Value |
| --- | ---: |
| Δ delta | 0.5100 |
| Γ gamma | 0.0350 |
| Θ theta (per day) | -0.2670 |
| 𝒱 vega (per 1% IV) | 0.1250 |
| ρ rho (per 1% rate) | 0.0230 |
| IV | 36.58% |

## Value

| Field | Value |
| --- | ---: |
| Intrinsic | $0.00 |
| Extrinsic (time) | $4.75 |
| Break-even | $207.25 (+2.35% vs spot) |
| In the money | no |
| Multiplier | 100 |
| Settlement | P |
```

## Notes

- Greeks and IV come directly from Schwab's chain endpoint — this
  command does no Black-Scholes work of its own. The values match
  what `schwab_cli option --detail=1` shows for the same strike.
- Break-even is computed as `strike + mark` for calls and
  `strike − mark` for puts (mark falls back to bid/ask mid, then last).
- ThinkOrSwim's greeks often differ from Schwab API's by a few percent
  — ToS uses a smile-adjusted model and an American-exercise binomial,
  while Schwab API ships straight Black-Scholes values. Neither is
  "more correct"; they're different conventions. This CLI matches
  Schwab's.
- Rejects stock tickers with exit code 2 and a clear message.
- Rejects unparseable tickers (wrong format, wrong separator) with
  exit code 2.
