# `skew`

Option skew / smile metrics for one symbol at one expiry (L1), across a
list of expiries (L2), or across a list of symbols at one expiry (L3).

Skew reflects the market's asymmetric pricing of tail risk. For US
equities and indexes, **OTM puts typically carry higher IV than
equidistant OTM calls** — a positive 25Δ risk reversal (RR) and a
negatively-sloped volatility curve. This command surfaces the standard
building blocks traders use to gauge that signal: RR, wing skew,
butterfly, ATM slope, and IV range.

## Usage

```
# L1 — single chain
schwab_cli skew SYMBOL YYMMDD [--strikes=N] [--json | --md]

# L2 — term structure across explicit expiries
schwab_cli skew SYMBOL --term YYMMDD [YYMMDD ...] [--strikes=N] [--json | --md]

# L2 — term structure at target DTEs (picks closest available expiry)
schwab_cli skew SYMBOL --dtes N [N ...] [--strikes=N] [--json | --md]

# L3 — cross-ticker at one expiry
schwab_cli skew --cross YYMMDD SYMBOL [SYMBOL ...] [--strikes=N] [--json | --md]

# L3 — cross-ticker at a target DTE (each symbol picks its own nearest expiry)
schwab_cli skew --cross --dtes N SYMBOL [SYMBOL ...] [--strikes=N] [--json | --md]
```

`--term` is mutually exclusive with both `--cross` and `--dtes`.
`--cross --dtes` is the one legal combination — use it to compare
symbols at the same *target* DTE even when their listed expiries
differ (weekly vs. monthly chains).

## Arguments

| Arg | Purpose |
| --- | --- |
| `SYMBOL` | Stock ticker (upper-cased internally). L3 accepts multiple. |
| `YYMMDD` | Expiry date, six digits (e.g. `260501` = 2026-05-01). Must be today or later. |

## Flags

| Flag | Default | Purpose |
| --- | --- | --- |
| `--term` | off | Interpret trailing positional args as a list of YYMMDD expiries. |
| `--dtes` | off | Interpret trailing positional args as target days-to-expiry — each picks the closest available expiry. |
| `--cross` | off | Treat the first positional as an expiry and the rest as symbols. |
| `--strikes=N` | 40 | Strike window (total, around ATM) per expiry. |
| `--json` |  | JSON envelope matching the metrics schema below. |
| `--md` |  | GitHub-flavoured markdown. |

## Metrics definitions

All IV values are quoted in **vol points** (the 0-100 scale — 61.62 = 61.62%).

| Metric | Formula | Read |
| --- | --- | --- |
| ATM IV | IV at the call whose \|Δ\| is closest to 0.50 | baseline term vol |
| 25Δ Risk Reversal | `IV(25Δ put) - IV(25Δ call)` | + = put premium (downside fear) |
| 10Δ Wing Skew | `IV(10Δ put) - IV(10Δ call)` | same direction, tail edition |
| 25Δ Butterfly | `(IV(25Δ put) + IV(25Δ call)) / 2 - ATM IV` | + = convex smile (wings rich) |
| 10Δ Butterfly | `(IV(10Δ put) + IV(10Δ call)) / 2 - ATM IV` | wing convexity |
| ATM Slope | Average `dIV/dStrike` across consecutive near-ATM call strikes (within ±$15) | vol pt per $1 of strike; < 0 = put skew |
| IV Range | Min / max of all call IVs on the chain | spread = chain width |

Delta matching is nearest-neighbour — the chain's discrete strikes
rarely land exactly on 0.25 or 0.10; no interpolation is performed.

## Example: L1 HUMAN

```
$ schwab_cli skew AMZN 260501
=== AMZN Skew — exp 2026-05-01 (DTE 8) ===
Spot: $255.36

ATM  strike $257.50   IV 61.62%

25Δ Skew:
  Put   K $240.00   Δ -0.25   IV 62.80%
  Call  K $272.50   Δ +0.26   IV 59.51%
  Risk Reversal:  +3.29 vol pt   (put premium)
  Butterfly:      -0.46 vol pt   (inverted smile)

10Δ Skew:
  Put   K $232.50   Δ -0.16   IV 63.80%
  Call  K $280.00   Δ +0.17   IV 60.02%
  Risk Reversal:  +3.78 vol pt   (put premium)
  Butterfly:      +0.29 vol pt   (convex smile)

ATM Slope:  -0.0371 vol pt / $1   (-0.37 per $10, put skew)
IV Range:   59.51% – 63.80%   (spread +4.29 pt)
```

## Example: L2 HUMAN (term structure)

```
$ schwab_cli skew AMZN --term 260501 260515 270115
=== AMZN Term Structure ===
Expiry         DTE    ATM IV    25Δ RR    25Δ BF     Slope/$
-----------------------------------------------------------
2026-05-01       8     61.6%     +3.29     -0.46     -0.0371
2026-05-15      22     45.4%     +1.16     -0.44     -0.0181
2027-01-15     267     38.4%     -3.11     -2.73     +0.2340
```

## Example: L3 HUMAN (cross-ticker)

```
$ schwab_cli skew --cross 260501 NVDA AMZN MSFT AAPL
=== Cross-Ticker Skew (DTE ~8) ===
Ticker    DTE    ATM IV    25Δ RR   10Δ Wing    25Δ BF     Slope/$
------------------------------------------------------------------
NVDA        8     36.6%     +4.40    +10.84     +1.20     -0.3097
AMZN        8     61.6%     +3.29     +3.77     -0.46     -0.0371
MSFT        8     54.1%     +0.88     +0.88     -0.69     +0.0030
AAPL        8     35.4%     +0.77     +4.29     +0.55     -0.0794
```

Rows are sorted by 25Δ RR descending — the ticker with the heaviest put
premium leads the table.

## Example: L3 `--cross --dtes` HUMAN

```
$ schwab_cli skew --cross --dtes 30 NVDA AMZN AAPL
=== Cross-Ticker Skew (DTE ~30) ===
Ticker    DTE    ATM IV    25Δ RR   10Δ Wing    25Δ BF     Slope/$
------------------------------------------------------------------
NVDA       29     42.1%     +3.12     +7.40     +0.22     -0.1820
AMZN       31     40.0%     +1.62     +2.55     +1.54     +0.0136
AAPL       29     28.9%     +0.44     +1.10     +0.30     -0.0412
```

Each row's `DTE` is the actual expiry each symbol landed on — weeklies
and monthlies don't always line up on the same calendar day, so the
target (`30`) is the *intent* and each `DTE` column is the *truth*.

## Example: `--json` (L1)

```json
{
  "symbol": "AMZN",
  "expiry": "2026-05-01",
  "dte": 8,
  "spot": 255.36,
  "atm": {
    "strike": 257.5,
    "iv_pct": 61.62,
    "put_strike": 257.5,
    "put_iv_pct": 61.58
  },
  "d25": {
    "put":  {"strike": 240.0, "delta": -0.251, "iv_pct": 62.80},
    "call": {"strike": 272.5, "delta":  0.256, "iv_pct": 59.51},
    "rr":   3.29,
    "bf":  -0.46
  },
  "d10": {
    "put":  {"strike": 232.5, "delta": -0.159, "iv_pct": 63.80},
    "call": {"strike": 280.0, "delta":  0.174, "iv_pct": 60.02},
    "rr":   3.78,
    "bf":   0.29
  },
  "atm_slope_per_dollar": -0.0371,
  "iv_range": {
    "min_pct": 59.51,
    "max_pct": 63.80,
    "spread_pct": 4.29
  }
}
```

L2 `--json` returns a list of L1 objects sorted by DTE ascending;
L3 `--json` returns a list of L1 objects sorted by 25Δ RR descending.

## Sign convention

| Value | Sign → | Meaning |
| --- | --- | --- |
| Risk reversal | `> 0` | put premium (typical stock skew) |
| Risk reversal | `< 0` | call premium (LEAPS, growth names) |
| Butterfly | `> 0` | convex smile — wings above ATM |
| Butterfly | `< 0` | inverted smile — wings below ATM (rare) |
| ATM slope | `< 0` | put skew — IV falls as strike rises |
| ATM slope | `> 0` | call skew — unusual outside deep-ITM |

## Data robustness

* A contract missing `delta` or `iv` is skipped silently — the metric
  that depended on it renders as `—`.
* An empty chain exits non-zero with a stderr message rather than
  rendering an all-dash table.
* ATM slope requires at least 3 call strikes within ±$15 of spot;
  otherwise it reports `—`.
* In L2/L3 modes, a per-chain fetch failure downgrades to a
  `[warn]` line on stderr and the remaining chains still render —
  the whole report bails only if every chain fails.

## API cost

| Mode | Chain fetches | Notes |
| --- | --- | --- |
| L1 | 1 | One expiry. |
| L2 `--term` | N | One per listed expiry. |
| L2 `--dtes` | N + 1 | One cheap discovery fetch plus one per picked expiry. |
| L3 `--cross` | N | One per symbol at the shared expiry. |
| L3 `--cross --dtes` | 2N | One discovery + one fetch per symbol. |

No local storage — this command is stateless. For vol persistence, see
[`vol`](vol.md).
