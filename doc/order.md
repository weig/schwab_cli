# `order`

Place, preview, list, get, cancel, and replace Schwab orders — equity
single-leg and option single- or multi-leg.

> ⚠️ **Real money.** Unless you pass `--dry-run` (or use `order preview`),
> every `order place` invocation submits a live order to Schwab.
> Mutating commands (`place`, `cancel`, `replace`) render a confirmation
> panel and require you to type `yes` (or pass `--yes` to skip). Read
> [Safety](#safety) before your first place.

## Subcommands

| Command | Purpose |
| --- | --- |
| `order place`    | Build and submit an order. Always renders a confirmation panel. |
| `order preview`  | Render the confirmation panel and exit. Same as `place --dry-run`. |
| `order get`      | Fetch one order by id. |
| `order list`     | List orders with status / time-range filters. |
| `order cancel`   | Cancel one order by id. |
| `order replace`  | Replace an existing LIMIT order (price override only). |

## Common flags

| Flag | Applies to | Purpose |
| --- | --- | --- |
| `--account`, `-a` | all | Account number or trailing-digit suffix. Required for `place`; warned-but-allowed for read commands. |
| `--json`          | all | Emit JSON on stdout (human panel still goes to stderr). |
| `--yes`           | place / cancel / replace | Skip the typed `yes` confirmation. Panel still renders. |
| `--profile`       | place / preview | Policy profile name (default `default`, honours `$SCHWAB_CLI_PROFILE`). |

## Building an order — three ways

### 1. Equity single-leg (positional + flags)

```bash
schwab order place NVDA -a 1234 \
    --side BUY --quantity 10 --type LIMIT --price 220.00
```

| Flag | Purpose |
| --- | --- |
| `--side`     | `BUY`, `SELL`, `SELL_SHORT`, `BUY_TO_COVER` (default `BUY`). |
| `--quantity`, `-q` | Share count (default `1`). |
| `--type`     | Phase 1 grammar: `MARKET`, `LIMIT`, `NET_DEBIT`, `NET_CREDIT`. Stop variants (`STOP`, `STOP_LIMIT`, `TRAILING_STOP`, `TRAILING_STOP_LIMIT`) accepted via the [stop/trailing-stop flags](#stop--trailing-stop-flags) below. |
| `--price`    | Limit price. Required for `LIMIT` / `NET_DEBIT` / `NET_CREDIT` / `STOP_LIMIT` / `TRAILING_STOP_LIMIT`. |
| `--duration` | `DAY`, `GTC`, `FOK`, `IOC` (default `DAY`). |
| `--session`  | `NORMAL`, `AM`, `PM`, `SEAMLESS`. Extended sessions require `LIMIT` + `DAY`. |

### 2. Option (single or multi-leg via `--leg`)

```bash
# Single-leg: BUY 1 NVDA 270115 call @5 strike
schwab order place -a 1234 --type LIMIT --price 1.50 \
    --leg "+1@270115C250o"

# Vertical spread (BUY 250C / SELL 260C, May 2026)
schwab order place -a 1234 --type NET_DEBIT --price 1.50 \
    --leg "+1@260515C250o" --leg "-1@260515C260o" \
    --complex VERTICAL
```

**Leg grammar**: `±N@YYMMDD{C|P}STRIKE[o|c]`

- `±N` — signed quantity. `+` opens / buys, `-` closes / sells.
- `YYMMDD` — expiration. `260515` → 2026-05-15.
- `{C|P}` — call or put.
- `STRIKE` — numeric strike.
- `o` / `c` — optional `OPEN` / `CLOSE` position effect override.

### 3. Parse a Schwab/TOS ticket string

```bash
schwab order place -a 1234 \
    --parse "BUY +1 VERTICAL AMZN 1 MAY 26 250/260 CALL @1.50 LMT"
```

`--parse` is mutually exclusive with `--leg` / positional symbol.

## Stop / trailing-stop flags

| Flag | Required when | Purpose |
| --- | --- | --- |
| `--stop-price`       | `--type STOP` or `STOP_LIMIT` | Trigger price. |
| `--trailing-offset`  | `--type TRAILING_STOP*` | Distance the stop trails the basis. |
| `--trailing-basis`   | `--type TRAILING_STOP*` | `BID`, `ASK`, `LAST`, `MARK`. |
| `--trailing-type`    | `--type TRAILING_STOP*` | `VALUE` (absolute $) or `PERCENT` (e.g. `5` = 5%). |

## Reading and listing

```bash
# All ACTIVE orders across every account (default).
schwab order list

# Filled orders in May 2026 for one account, as JSON.
schwab order list -a 1234 --status FILLED --range 20260501..20260531 --json

# One order by id.
schwab order get 1003456789 -a 1234
```

`--status` accepts the canonical buckets (`ACTIVE`, `FILLED`, `CANCELED`,
`REPLACED`, `REJECTED`, `EXPIRED`, `ALL`) or any raw Schwab status (e.g.
`WORKING`, `AWAITING_PARENT_ORDER`).

`--range` understands `ytd` / `mtd` / `wtd` and explicit `<start>..<end>`
with tokens like `-7d`, `-1mo`, `now`, `YYYYMMDD`.

**Default range depends on `--status`:** `--status=ACTIVE` (the default)
implies `--range=ALL` so working orders from any era surface — Schwab
doesn't time-bound active state. Any other status falls back to
`-7d..now` so list operations stay snappy.

## Cancel / replace

```bash
schwab order cancel 1003456789 -a 1234
schwab order replace 1003456789 -a 1234 --price 219.50
```

`order replace` currently supports price-only edits (V1). For other
changes, cancel and re-place.

## Safety

`order` ships with a multi-layer safety system:

1. **Confirmation panel** renders for every `place` / `cancel` / `replace`,
   spelling out every leg, the dollar cost / credit, and the resolved
   account number. Requires typing `yes` unless `--yes` is passed.
2. **Policy gate** — a configurable profile (see `$SCHWAB_CLI_PROFILE`)
   can block orders matching risk patterns (e.g. naked short calls,
   notional > $X, off-hours). To bypass:
   ```bash
   schwab order place ... --override "REASON_10_TO_500_CHARS" --override-confirm
   ```
   Reasons are logged verbatim. Tier-driven ceremony — high-tier
   overrides require Telegram confirmation (see profile docs).
3. **Audit log** — every stage of every order (build, panel, place,
   rollback, etc.) is captured as a JSONL line.

## Notes

- **Read-only commands work without auth-elevation.** `place` / `cancel` /
  `replace` will refresh the token first; if Schwab's auth tier changed
  (e.g. you re-enabled trading), re-run `schwab auth --force`.
- **No paper-trading mode.** Schwab's API treats every submission as
  live. Use `order preview` / `--dry-run` for risk-free experimentation;
  panel output is identical to the real path.
- **Multi-account scans** — omitting `--account` on read commands queries
  every account. The CLI warns when this happens to make accidental
  cross-account reads visible.
