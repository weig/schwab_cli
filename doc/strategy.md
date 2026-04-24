# `strategy`

Multi-leg option-strategy probability and risk analysis. For a
user-specified set of legs, `strategy` fetches the relevant chains,
prices each leg against live mid/mark data, classifies the position
shape, and emits: probability of profit (POP), expected P/L (EV),
breakevens, max profit / loss, combined greeks, and a
copy-paste-ready Schwab order ticket.

The analytics are closed-form under a log-normal terminal-price
density with a flat σ derived from the leg IVs (arithmetic mean of
each leg's IV at the chain). This is close to ToS's "Prob OTM"
(which anchors on ATM IV) but not identical — under a convex smile
the leg-mean σ tends to be slightly higher, producing modestly more
conservative POP estimates.

## Usage

```
schwab_cli strategy SYMBOL --leg LEG [--leg LEG ...] [--risk-free FLOAT] [--json | --md]
```

Each `--leg` is one OCC-style token:

```
±N@YYYYMMDD{C|P}STRIKE
```

| Token | Meaning | Example |
| --- | --- | --- |
| `±N` | Signed integer quantity. `+` (or omitted) = buy/long; `-` = sell/short. Ratios allowed (`+2`, `-3`). Zero is rejected. | `-1`, `+2`, `3` |
| `@` | Required separator. | — |
| `YYYYMMDD` | Full 8-digit calendar date. Must be a real date (Feb 30 rejected). | `20260501` |
| `C` / `P` | Side. Lowercase accepted and normalised. | `P` |
| `STRIKE` | Positive number, decimals allowed. Snapped to the nearest available strike on the chain; deviations > $0.50 emit a `strike_snap:…` warning. | `192.5` |

No positional expiry, no preset shortcuts. One grammar, one path.

## Examples

### Bull call spread

```bash
schwab_cli strategy AMZN \
    --leg +1@20260501C255 \
    --leg -1@20260501C260
```

### Long straddle

```bash
schwab_cli strategy AMZN \
    --leg +1@20260501C255 \
    --leg +1@20260501P255
```

### Iron condor (short / credit)

```bash
schwab_cli strategy NVDA \
    --leg +1@20260501P192.5 \
    --leg -1@20260501P197.5 \
    --leg -1@20260501C207.5 \
    --leg +1@20260501C210
```

### Long call butterfly

```bash
schwab_cli strategy AMZN \
    --leg +1@20260501C250 \
    --leg -2@20260501C255 \
    --leg +1@20260501C260
```

### Calendar (multi-expiry — Phase 2)

The parser accepts multi-expiry legs; the Schwab order ticket renders;
analytics are deferred (`supported=false`, `warnings` list names the
reason):

```bash
schwab_cli strategy AMZN \
    --leg -1@20260501C300 \
    --leg +1@20260701C300
```

## Flags

| Flag | Default | Purpose |
| --- | --- | --- |
| `--leg` | required | OCC leg token. Repeatable. |
| `--risk-free` | `0.0` | Annualised risk-free drift for the log-normal density. ToS's "Prob OTM" uses zero; override when you want a drifted model. |
| `--json` | — | JSON envelope. |
| `--md` | — | GitHub-flavoured markdown. |
| `--doc` | — | Open this page. |

## Supported strategies (Phase 1)

Single-expiry only. Multi-expiry shapes still render the Schwab ticket
but skip analytics in Phase 1.

| Legs | Named shapes |
| --- | --- |
| 1 | Long/short call, long/short put |
| 2 | Bull/bear call spread, bull/bear put spread, long/short straddle, long/short strangle |
| 3 | Long/short call butterfly, long/short put butterfly, broken-wing fly (CUSTOM ticket) |
| 4 | Iron condor, iron butterfly, reverse IC (debit) |

Irregular shapes — ratio spreads, 3-leg risk reversals, asymmetric
wings — parse and analyze just fine; they simply render under the
`CUSTOM` ticket keyword with per-leg strikes/sides/dates.

## Metrics definitions

All metric values are dollars per spread (×100 multiplier already
applied; equity options assumed) unless noted otherwise.

| Metric | Formula | Read |
| --- | --- | --- |
| POP | ∫ over profitable intervals of log-normal density | Prob of profit at expiry |
| EV | Σ over intervals of `m · E[S·1_a<S<b] + c · P(a<S<b)` | Expected P/L in dollars |
| Max Profit | Vertex scan of payoff kinks, asymptote slope check | `"unlimited"` when unbounded above |
| Max Loss | Vertex scan including S=0 | `"unlimited"` when unbounded below (naked short call) |
| Breakevens | Piecewise-linear zero-crossings | Sorted ascending |
| Prob(touch) | Reflection principle, zero-drift approximation | Per breakeven; multi-breakeven flagged `prob_touch_approx` |
| Combined Δ | Σ `qty × delta` | Shares-equivalent exposure per spread |
| Combined Γ | Σ `qty × gamma` | Shares-per-dollar |
| Combined Θ | Σ `qty × theta × 100` | Dollars per day |
| Combined ν | Σ `qty × vega × 100` | Dollars per vol-point |

## Sign conventions in output

- `net_premium`: signed float. **Positive = credit received, negative
  = debit paid.**
- `net_credit`, `net_debit`: convenience non-negative fields, one is
  always `0`.
- HUMAN renderer uses text labels (`Net Credit $1.30`, `Net Debit $2.00`).
- Schwab ticket uses absolute `@PRICE LMT` with BUY/SELL conveying
  debit vs. credit.

## Schwab order-ticket format

Each run prints a copy-paste-ready ticket line matching Schwab's
order-entry UI.

### Named shapes

```
SELL -1 VERTICAL AMZN 100 (Weeklys) 1 MAY 26 260/255 CALL @0.85 LMT
BUY +1 STRADDLE AMZN 100 (Weeklys) 1 MAY 26 255 CALL/PUT @5.00 LMT
BUY +1 BUTTERFLY AMZN 100 (Weeklys) 1 MAY 26 260/255/250 CALL @1.50 LMT
SELL -1 IRON CONDOR NVDA 100 (Weeklys) 1 MAY 26 210/207.5/197.5/192.5 CALL/PUT @1.30 LMT
```

### CUSTOM fallback

Irregular ratios and non-standard shapes render via Schwab's `CUSTOM`
keyword with per-leg ratios, dates, strikes, and sides:

```
BUY +1 1/1/1 CUSTOM AMZN 100 (Weeklys) 1 MAY 26/1 MAY 26/1 MAY 26 225/217.5/210 PUT/PUT/PUT @2.38 LMT
SELL -1 2/1 CUSTOM AMZN 100 (Weeklys) 1 MAY 26/1 MAY 26 260/255 CALL/CALL @0.60 LMT
```

### Conventions

- Strikes listed **descending**; for mixed-side shapes, calls before
  puts.
- Dates formatted `D MON YY` (no leading zero on day).
- `(Weeklys)` tag appended only when the expiry is not the third
  Friday of its month.
- `@PRICE` is always an absolute number; `BUY`/`SELL` carries the
  sign.

## Model assumptions

- **Log-normal at expiry** with flat drift `r` (default `0`).
  Matches ToS "Prob OTM" when `r=0`.
- **Flat IV**: the density uses a single σ = arithmetic mean of the
  per-leg IVs returned by the chain. Wide-wing ICs may see POP
  slightly under-estimated vs. ATM-only anchoring (ToS "Prob OTM")
  and more so vs. a true skew-aware (non-lognormal) density;
  skew-aware POP is a Phase-2 upgrade.
- **DTE floor**: `T = max(dte/365, 1e-6)`. 0-DTE short-circuits to
  `pop = 1 if currently profitable else 0`.
- **American-style exercise**: Schwab equity options are American.
  For defined-risk positions opened fresh, the early-exercise premium
  is negligible and no correction is applied — documented here,
  not silently assumed.
- **Reflection-principle `prob_touch`**: uses the zero-drift barrier
  formula. For strategies with two breakevens, the formula returned
  is `1 − P(finish within the band)` — an approximation that
  under-states true any-touch path probability. Flagged via
  `prob_touch_approx` in warnings.

## JSON envelope

```json
{
  "symbol": "NVDA",
  "strategy": "Iron Condor",
  "ticket_name": "IRON CONDOR",
  "supported": true,
  "reason": null,
  "naked": false,
  "model": "lognormal_flat_iv",
  "spot": 200.12,
  "dte": 8,
  "legs": [
    {"qty": 1, "side": "P", "strike": 192.5, "expiry": "2026-05-01",
     "premium": 0.80, "iv_pct": 41.2, "delta": -0.08,
     "gamma": 0.02, "theta": -0.05, "vega": 0.15}
  ],
  "ticket": "SELL -1 IRON CONDOR NVDA 100 (Weeklys) 1 MAY 26 210/207.5/197.5/192.5 CALL/PUT @1.30 LMT",
  "net_premium": 1.30,
  "net_credit": 1.30,
  "net_debit": 0.0,
  "pop": 0.683,
  "ev": 42.0,
  "max_profit": 130.0,
  "max_loss": -370.0,
  "breakevens": [196.20, 208.80],
  "prob_touch": [0.245, 0.231],
  "greeks": {"delta": -0.02, "gamma": -0.012, "theta": 6.20, "vega": -8.40},
  "warnings": ["prob_touch_approx"]
}
```

`max_loss` serialises as the string `"unlimited"` when the position
has unbounded downside (naked short call). Agents that want a numeric
value should check the `warnings` array for `"unlimited_loss"` rather
than coercing the string.

## Warnings

| Warning | Cause |
| --- | --- |
| `strike_snap:P192→P192.5` | Requested strike differed from the nearest available by more than $0.50. |
| `naked_short_call` | Uncovered short call — unlimited loss. |
| `naked_short_put` | Uncovered short put — bounded loss at `strike × 100 − credit`. |
| `unlimited_loss` | `max_loss` is unbounded (set together with `naked_short_call`). |
| `short_dte:2d` | DTE < 3; log-normal fit degrades near expiry. |
| `iv_anomaly:leg_C280_iv_0.82` | Leg IV > 2× the reference ATM IV — possible data glitch. |
| `prob_touch_approx` | Strategy has multiple breakevens; touch probability is an endpoint-based approximation. |
| `analytics_not_supported_yet:multi-expiry` | Phase 1 doesn't run full analytics on calendars/diagonals. |

## Data robustness

- A leg whose requested strike isn't in the chain snaps to the nearest
  available strike; differences > $0.50 warn.
- Chain fetch failures exit non-zero — `strategy` never renders
  partial analytics.
- Unsupported shapes (multi-expiry in Phase 1) still render legs and
  Schwab ticket so you can paste the order even when automated
  metrics are deferred.

## API cost

| Legs | Chain fetches |
| --- | --- |
| Single-expiry (any leg count) | 1 |
| Multi-expiry | N (one per unique expiry) |

Stateless — no local storage or caching.
