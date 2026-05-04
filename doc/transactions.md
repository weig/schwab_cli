# `transactions`

Account activity — trades, dividends, journals, cash transfers — across
one account or all of them.

## Usage

```
schwab_cli transactions [--account 0756] [--range=...] [--type=...] [--refresh] [--json | --md]
```

## Flags

| Flag | Default | Purpose |
| --- | --- | --- |
| `--account` / `-a` | _all accounts_ | Account number or last-N-digit suffix. Omit to fan out across every account. When supplied, the **Account column is dropped** from human/MD output (redundant). JSON output keeps the `account` field for shape stability. |
| `--range` / `-r` | `-7d..now` | Date window. See [time](time.md). |
| `--type` | `TRADE` | Filter: `TRADE`, `DIVIDEND_OR_INTEREST`, `JOURNAL`, `RECEIVE_AND_DELIVER`, or `ALL`. |
| `--json` | | JSON array of transactions. |
| `--md` | | Markdown table. |
| `--refresh` | | Bypass the local cache for this run and re-fetch from Schwab. Result still upserts into the cache. |

## Example: HUMAN, single account (Account column hidden)

```
$ schwab_cli transactions --account 0756 --range=-7d..now
Transactions — 3 rows   Net cashflow: -5,034.59

Date        Type   Symbol  Effect       Qty   Price        Net
2026-04-30  TRADE  SPY     OPENING  +0.0160  712.02     -11.37
2026-04-30  TRADE  JPM     OPENING  +0.0087  312.65      -2.72
2026-05-01  TRADE  SGOV    OPENING      +50  100.41  -5,020.50
```

## Example: HUMAN, all accounts (Account column shown)

```
$ schwab_cli transactions --range=-7d..now
Transactions — 3 rows   Net cashflow: -5,034.59

Date        Account  Type   Symbol  Effect       Qty   Price        Net
2026-04-30  ...0756  TRADE  SPY     OPENING  +0.0160  712.02     -11.37
2026-04-30  ...0756  TRADE  JPM     OPENING  +0.0087  312.65      -2.72
2026-05-01  ...0756  TRADE  SGOV    OPENING      +50  100.41  -5,020.50
```

## Caching

The `transactions` command keeps a local SQLite cache so historical
data isn't re-fetched on every invocation.

**Where:** `~/.config/schwab_cli/storage/account.db` (sibling to
`vol_history.db`; override with `SCHWAB_CLI_STORAGE`).

**What's cached:** the full set of transactions per account regardless
of the requested `--type`. Type filtering is applied locally on the
returned set, so the cache is reusable across different `--type`
values without re-fetching.

**How freshness works** — calendar-based, two zones per query:

| Zone | Definition | Behavior |
| --- | --- | --- |
| **Old** | `time` ≤ `fresh_cutoff` (= earliest of "first day of previous month" or "today − 30 days") | Read from cache. Missing chunks fetched once and cached forever. |
| **Fresh** | `time` > `fresh_cutoff` | Always re-fetched from Schwab on every call (settlement, corrections, dividend posting can mutate this window). |

The fresh window is always at least 30 days. As days roll forward,
data that was once "fresh" naturally becomes "old" — already cached,
so no special re-fetch needed at the boundary.

**Force a full re-fetch:** `--refresh` bypasses the split and fetches
the entire requested range. Result still UPSERTs into the cache.

**Nuke the cache:** `rm ~/.config/schwab_cli/storage/account.db` —
rebuilds on next invocation.

**Schema:** the `transactions` table is keyed by `activity_id` alone
(Schwab's `activityId` is globally unique per user — verified
empirically). Each row stores `gross_amount` (the non-fee leg's cost),
`total_fees` (signed sum of all fee leg costs), and the raw JSON
payload for full fidelity (per-fee-type breakdown lives in the JSON).

## Example: `--json`

```json
[
  {
    "account": "########1234",
    "date": "2026-04-17",
    "time": "2026-04-17T14:32:18+0000",
    "type": "TRADE",
    "symbol": "NVDA  270115C00250000",
    "qty": 3,
    "price": 5.40,
    "effect": "OPENING",
    "netAmount": -1620.00
  }
]
```

## Type filter semantics

| Filter | Meaning |
| --- | --- |
| `TRADE` | Buys, sells, open/close of any instrument. |
| `DIVIDEND_OR_INTEREST` | Cash dividends and interest credits. |
| `JOURNAL` | Cash transfers between your Schwab accounts. |
| `RECEIVE_AND_DELIVER` | ACATs / stock-by-stock transfers. |
| `ALL` | No filter — all of the above. |

Type filtering happens locally after the cache fetch. Switching
`--type` between calls does not invalidate the cache.

## Range examples

```bash
# Default: last 7 days of trades
schwab_cli transactions

# All activity for April, single account, no Account column
schwab_cli transactions -a 0756 --range=20260401..20260430 --type=ALL

# Year-to-date dividends across every account
schwab_cli transactions --range=ytd --type=DIVIDEND_OR_INTEREST

# Force-refresh (cache out of date or you don't trust it)
schwab_cli transactions -r -7d..now --refresh
```

## Notes

- Multi-account mode fans out one API call per account on a cold
  cache; subsequent calls hit the cache for everything older than ~1
  month, so it's much faster after the first warm-up.
- Transaction timestamps are UTC in JSON; human output displays the
  date portion as-is.
- Short trades are negative quantities.
- Per-fee-type breakdowns (COMMISSION, SEC_FEE, OPT_REG_FEE, TAF_FEE,
  ...) live in the cached payload JSON; the table view shows only the
  net amount. Use `--json` if you need per-fee detail.
