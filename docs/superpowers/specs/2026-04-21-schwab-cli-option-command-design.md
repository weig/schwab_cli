# Schwab CLI — `option` Command (Phase 4)

**Date:** 2026-04-21
**Status:** Approved (pending user spec review)
**Scope:** Add a read-only `option` command that fetches Schwab's option chain for a symbol/expiry and renders it in three display modes with three detail levels.

## Goal

Give users a compact way to look up option chains from the CLI:

```bash
schwab_cli option NVDA '270115*250'     # strike 250, both put & call
schwab_cli option NVDA '270115P*' --strikes=4   # puts, 4 strikes around ATM
schwab_cli option NVDA 270115           # default: both sides, 10 strikes around ATM
```

Output defaults to a classic side-by-side option chain in the terminal. `--detail=1` and `--detail=2` expand to richer column sets. `--json` and `--md` produce piping-clean output for scripts and LLM context.

## Non-Goals

- **Multi-expiration chains** — one expiry per invocation. A future flag could add this.
- **Multi-leg strategies** (verticals, straddles, etc.) — Schwab supports it via `strategy`; not needed here.
- **Streaming / realtime** — single REST call per invocation.
- **Order placement** — read-only. Trading ships separately per Phase 3 spec.

## CLI Grammar

```
schwab_cli option <SYMBOL> <SPEC> [--strikes N] [--detail [LEVEL]] [--json | --md]
```

### Spec regex

`^(?P<date>\d{6})(?P<type>[PC])?\*?(?P<strike>\d+(?:\.\d+)?)?$`

| Input          | Expiry      | Type   | Strike  | Uses `--strikes` |
|----------------|-------------|--------|---------|------------------|
| `270115`       | 2027-01-15  | ALL    | —       | yes              |
| `270115*`      | 2027-01-15  | ALL    | —       | yes              |
| `270115P`      | 2027-01-15  | PUT    | —       | yes              |
| `270115P*`     | 2027-01-15  | PUT    | —       | yes              |
| `270115C*`     | 2027-01-15  | CALL   | —       | yes              |
| `270115*250`   | 2027-01-15  | ALL    | 250     | no (ignored)     |
| `270115P*250`  | 2027-01-15  | PUT    | 250     | no (ignored)     |
| `270115C*250`  | 2027-01-15  | CALL   | 250     | no (ignored)     |

**Date format:** YYMMDD, `20YY` century assumed. `270115` → 2027-01-15.

**Strike format:** integer or decimal (e.g., `250`, `250.5`).

**Shell quoting:** `*` is a glob in bash/zsh. Users must quote the spec:
`schwab_cli option NVDA '270115*250'`. Documented in `--help` and README.

### Flags

| Flag | Default | Meaning |
|------|---------|---------|
| `--strikes N` | `10` | Total strikes around ATM when no explicit strike is given. Ignored when strike is in spec. |
| `--detail [LEVEL]` | `0` | `0` = Layout A; `1` = Layout B; `2` = Layout B + inline sub-table. Bare `--detail` means `--detail=1`. |
| `--json` | off | Emit JSON envelope on stdout. Mutex with `--md`. |
| `--md` | off | Emit GitHub-flavored markdown. Mutex with `--json`. |

`--strikes=N` semantics:
- Even N: `N/2` ITM + `N/2` OTM (no explicit ATM row).
- Odd N: `(N-1)/2` ITM + 1 ATM + `(N-1)/2` OTM.
- Default N = 10 → 5 ITM + 5 OTM.

## API Layer

### `src/schwab_cli/api/chains.py`

```python
def get_chain(
    client: SchwabClient,
    symbol: str,
    *,
    contract_type: Literal["CALL", "PUT", "ALL"] = "ALL",
    strike: float | None = None,
    strike_count: int = 10,
    from_date: date | None = None,
    to_date: date | None = None,
) -> dict: ...
```

Hits `GET https://api.schwabapi.com/marketdata/v1/chains` via `client.get()`.

**Query param mapping:**

| Arg | Schwab param |
|-----|---------------|
| `symbol` | `symbol` |
| `contract_type` | `contractType` |
| `strike` | `strike` (omitted when `None`) |
| `strike_count` | `strikeCount = ceil(our_N / 2)` (Schwab treats it as N-per-side) |
| `from_date` / `to_date` | `fromDate` / `toDate` (YYYY-MM-DD) |
| — | `includeUnderlyingQuote=true` (always) |
| — | `strategy=SINGLE` (always) |

**Returns** the raw Schwab JSON dict (`{underlying, callExpDateMap, putExpDateMap, status, ...}`). Output layer flattens.

**Auth:** reuses `client.get()` — 401→refresh→retry handled there.

**Errors:**
- Non-2xx (not 401): `ApiError` from `client.get()`.
- 401 after refresh: `SessionExpired` from `client.get()`.
- Empty response (`status == "FAILED"` or empty maps): returned as-is; command layer formats "No options found".

## Spec Parser

### `src/schwab_cli/option_spec.py`

```python
from dataclasses import dataclass
from datetime import date
from typing import Literal

@dataclass(frozen=True)
class OptionSpec:
    expiry: date
    contract_type: Literal["CALL", "PUT", "ALL"]
    strike: float | None  # None → use strike_count window


class OptionSpecError(ValueError):
    """Raised when the spec string doesn't match the grammar."""


def parse_option_spec(spec: str) -> OptionSpec: ...
```

**Error messages:**
- Regex miss: `"Invalid option spec '<spec>'. Expected YYMMDD[P|C]*[strike] — e.g. '270115*250' or '270115P*'."`
- Date out of range or in past: `"Expiry <YYYY-MM-DD> is in the past."`
- Non-numeric strike: (caught by regex)

Located at package root (not `commands/`) because parsing is pure, has no IO, and may be reused by future commands.

## Output Layer

### `src/schwab_cli/output/chains.py`

```python
def render_chain(
    envelope: dict,          # API response
    *,
    fmt: Format,
    detail: int,             # 0, 1, or 2
    requested_type: Literal["CALL", "PUT", "ALL"],
    width: int | None = None,   # None → auto-detect via shutil
) -> str: ...
```

`requested_type` lets Layout A fall back to Layout B when one side is filtered (see §Layout A).

### Shared envelope (for JSON/MD, and what HUMAN reads)

```jsonc
{
  "symbol": "NVDA",
  "expiry": "2027-01-15",
  "dte": 632,
  "underlying": { "last": 142.35, "netChange": 2.10, "pctChange": 1.50 },
  "contracts": [ /* ascending strike; call before put at each strike */ ]
}
```

### Contract field matrix by `--detail`

| Field | `0` | `1` | `2` |
|---|---|---|---|
| `optionSymbol` (OSI) | • | • | • |
| `side` (`"C"` / `"P"`) | • | • | • |
| `strike` | • | • | • |
| `bid`, `ask`, `last` | • | • | • |
| `delta` | • | • | • |
| `iv`, `gamma`, `theta`, `vega` |   | • | • |
| `volume`, `openInterest` |   | • | • |
| `mark` |   |   | • |
| `bidSize`, `askSize`, `lastSize` |   |   | • |
| `open`, `high`, `low`, `close` |   |   | • |
| `rho` |   |   | • |
| `timeValue`, `intrinsic` |   |   | • |
| `inTheMoney` (bool) |   |   | • (JSON only) |
| `multiplier` |   |   | • (JSON only) |
| `settlementType` |   |   | • |

Numbers remain numbers in JSON (never strings). Schwab's `NaN` / `Infinity` map to `null`.

### HUMAN — Layout A (`--detail=0`, default)

Classic side-by-side chain. Strike centered, calls on left (columns mirrored so bid/ask nearest strike sit innermost), puts on right.

```
NVDA — 2027-01-15 (632 DTE)    Spot: $142.35  (+2.10 / +1.50%)

              CALLS                  |          |                PUTS
Δ       Last   Ask    Bid    Vol  OI | STRIKE   | OI  Vol  Bid    Ask    Last   Δ
 0.71    8.45   8.50   8.40  123 456 | 135.00   | 89   12  0.42   0.45   0.43  -0.12
 0.58    5.20   5.25   5.15  200 789 | 140.00   | 150  45  1.15   1.20   1.18  -0.23
 0.50    3.10   3.15   3.05  340 900 | 142.50 ← | 410  67  2.35   2.40   2.38  -0.50
 0.41    1.75   1.80   1.70  280 650 | 145.00   | 540  85  4.10   4.15   4.12  -0.58
 0.28    0.85   0.90   0.80  190 420 | 150.00   | 700  98  7.90   7.95   7.93  -0.71
```

**Styling:**
- `Δ` and `Last` cells: green if positive, red if negative (same rule as `positions`).
- ATM row marked with `←` next to STRIKE. **ATM definition:** the row whose strike is closest in absolute value to `underlying.last`; ties broken by choosing the lower strike.
- ITM rows bold. We trust Schwab's per-contract `inTheMoney` field rather than deriving from spot. In Layout A a row is bold when *either* the call or the put at that strike reports ITM (the strike row is a union).

**Auto-fallback:** when `requested_type != "ALL"`, Layout A falls back to Layout B automatically. Printed to stderr as a dim note: `[note] one-sided chain — rendering as --detail=1.`

### HUMAN — Layout B (`--detail=1`)

One row per contract, sorted by strike ascending. At each strike, call row first, put row second.

```
NVDA — 2027-01-15 (632 DTE)    Spot: $142.35  (+2.10 / +1.50%)

Symbol                Side  Strike  Bid    Ask    Last   IV     Δ      Γ       Θ      𝒱     Vol   OI
NVDA 270115C00135000  C     135.00  8.40   8.50   8.45   0.35   0.71   0.018  -0.04   0.18  123   456
NVDA 270115P00135000  P     135.00  0.42   0.45   0.43   0.38  -0.12   0.015  -0.02   0.14   12    89
NVDA 270115C00140000  C     140.00  5.15   5.25   5.20   0.33   0.58   0.022  -0.05   0.20  200   789
...
```

ITM rows bold. Sign coloring on `Δ`.

### HUMAN — Layout B + inline (`--detail=2`)

Layout B plus two indented continuation lines per contract for additional fields. Symbol cell gets a `(PM)` / `(AM)` suffix showing settlement type.

```
NVDA — 2027-01-15 (632 DTE)    Spot: $142.35  (+2.10 / +1.50%)

Symbol                     Side  Strike  Bid    Ask    Last   IV     Δ      Γ       Θ      𝒱     Vol   OI
NVDA 270115C00135000 (PM)   C    135.00  8.40   8.50   8.45   0.35   0.71   0.018  -0.04   0.18  123   456
  ├─ Mark: 8.45   L.Sz: 1    B.Sz: 10   A.Sz: 15   Open: 8.10   High: 8.60  Low: 8.05  Close: 8.35
  └─ DTE: 632     ρ: 0.052   Time Val: 8.45   Intrinsic: 7.35
NVDA 270115P00135000 (PM)   P    135.00  0.42   0.45   0.43   0.38  -0.12   0.015  -0.02   0.14   12    89
  ├─ Mark: 0.435  L.Sz: 1    B.Sz: 22   A.Sz: 30   Open: 0.48   High: 0.52  Low: 0.41  Close: 0.45
  └─ DTE: 632     ρ: -0.008  Time Val: 0.43   Intrinsic: 0.00
```

Size labels shortened: `B.Sz`, `A.Sz`, `L.Sz`. Settle type in the contract heading. `Multiplier` and `ITM` columns are dropped from visual output (ITM rendered as bold row styling).

**Settlement mapping:** Schwab returns `settlementType` as a single letter (`"P"` for PM-settled, `"A"` for AM-settled). We render `(PM)` / `(AM)` in the Symbol cell. Unknown values pass through verbatim — e.g. `"X"` would render as `(X)` rather than crash.

### Width adaptation (HUMAN only)

`shutil.get_terminal_size((120, 24)).columns` (or `120` when stdout isn't a TTY). Drop columns from the right until the row fits. Width adaptation never applies to JSON or MD output.

**Layout A drop order (symmetric; call side drops first in each pair, then put):**

| Priority | Column pair dropped |
|----------|---------------------|
| 1 (last) | Bid / Ask / Last / STRIKE (never dropped) |
| 2 | Volume (call) + Volume (put) |
| 3 | OI (call) + OI (put) |
| 4 | Δ (call) + Δ (put) |

**Layout B drop order:**

| Priority | Column dropped |
|----------|----------------|
| 1 | OI |
| 2 | Vol |
| 3 | 𝒱 (vega) |
| 4 | Θ (theta) |
| 5 | Γ (gamma) |
| 6 | IV |
| 7 | Δ |

`Symbol | Side | Strike | Bid | Ask | Last` are required.

When columns are dropped, a dim stderr note is printed: `[note] terminal too narrow — dropped columns: X, Y. Use --detail=1 or widen terminal for full view.` Stdout stays clean for piping.

**Layout B+inline:** main row follows Layout B drop rules. At widths <60 cols the sub-table prints as one `key: value` per line.

### JSON format

`json.dumps(envelope, indent=2, default=_num)` where `_num(v)` maps `NaN` / `Infinity` to `None`. No ANSI, no styling, field matrix per detail level. `inTheMoney` and `multiplier` are included at `--detail=2`.

### MD format

```markdown
# NVDA — 2027-01-15 (632 DTE)

**Spot:** $142.35 (+2.10 / +1.50%)

| Symbol | Side | Strike | Bid | Ask | Last | Δ |
|--------|------|--------|-----|-----|------|---|
| **NVDA 270115C00135000** | C | **135.00** | 8.40 | 8.50 | 8.45 | 0.71 |   <!-- ITM: bold Symbol + Strike -->
| NVDA 270115P00135000 | P | 135.00 | 0.42 | 0.45 | 0.43 | -0.12 |
```

At `--detail=1`, columns expand to the full Layout-B set. At `--detail=2`, each main-table row is followed by a blockquoted sub-table:

```markdown
> **Details — NVDA 270115C00135000** (Settle: PM)
>
> | Mark | L.Sz | B.Sz | A.Sz | Open | High | Low  | Close | ρ     | TimeVal | Intrinsic |
> |------|------|------|------|------|------|------|-------|-------|---------|-----------|
> | 8.45 | 1    | 10   | 15   | 8.10 | 8.60 | 8.05 | 8.35  | 0.052 | 8.45    | 7.35      |
```

ITM bolding: `Symbol` and `Strike` cells wrapped in `**...**` (whole-row bold isn't clean in GitHub-flavored MD). `Multiplier` and `ITM` columns are dropped (bold conveys ITM). `settlementType` moved into the sub-table heading.

## Error Handling Summary

| Failure | Where | User-facing message | Exit |
|---|---|---|---|
| Invalid spec | spec parser | `"Invalid option spec '<spec>'. Expected YYMMDD[P|C]*[strike] — e.g. '270115*250'."` | 2 |
| Expiry in the past | spec parser | `"Expiry <YYYY-MM-DD> is in the past."` | 1 |
| No session | command entry | `"No session found. Run `schwab_cli auth` first."` | 1 |
| Session expired (refresh failed) | client.get() | `"Session expired. Run `schwab_cli auth --force`."` | 1 |
| Empty chain (unknown symbol or no contracts) | command | `"No options found for <SYMBOL> on <date>."` | 1 |
| Exact strike with no contract | command | `"No contract at strike <N> for <SYMBOL> <date>."` | 1 |
| `--json` + `--md` | format picker | `"--json and --md are mutually exclusive."` | 2 |
| Network / 5xx | client.get() | Existing `ApiError` messages | 1 |

## File Layout

```
src/schwab_cli/
├── api/
│   └── chains.py                 NEW
├── option_spec.py                NEW
├── output/
│   └── chains.py                 NEW
├── commands/
│   └── option.py                 NEW
└── cli.py                        MODIFIED — register `option` subcommand

tests/
├── test_option_spec.py           NEW
├── test_api_chains.py            NEW
├── test_output_chains.py         NEW
└── test_commands_option.py       NEW
```

**No new dependencies.** Reuses `rich`, `httpx`, `respx`, `typer`.

## Test Plan

Coverage target: ≥80% per project rules.

### `test_option_spec.py`

- Valid grammar cases — one test each for: `270115`, `270115*`, `270115P`, `270115P*`, `270115C*`, `270115*250`, `270115P*250`, `270115C*250`.
- Decimal strike (`270115*250.5`).
- Invalid grammar — each raises `OptionSpecError`: `27015` (short date), `abcdef`, `270115X*250` (bad type), `270115*250x`, empty string.
- Past expiry → `OptionSpecError` with date in the message.
- Date parsing — `270115` → `date(2027, 1, 15)`.

### `test_api_chains.py`

Uses `respx` for HTTP mocking, same pattern as existing `test_api_quotes.py`.

- Request URL & query params — verifies `contractType`, `strikeCount=ceil(N/2)`, `fromDate=toDate`, `strike` (when set), `strategy=SINGLE`, `includeUnderlyingQuote=true`.
- Happy path: response dict passthrough.
- Empty response: returned as-is.
- 401→refresh→retry — reuses existing client fixture.

### `test_output_chains.py`

- Layout A (`--detail=0`, both sides) — header, strike centered, ATM marker `←`, sign colors on Δ, bold on ITM row.
- Layout A auto-fallback → Layout B when `requested_type != "ALL"` (stderr note captured).
- Layout B (`--detail=1`) — columns present, one row per contract, ITM bold, color on Δ.
- Layout B+inline (`--detail=2`) — main row + two indented continuation lines, `(PM)` suffix on Symbol.
- Width adaptation — 80-col width drops Δ/IV/Γ as specified, stderr note present.
- JSON detail levels — field matrix per level, `NaN` → `null`, no ANSI.
- MD detail levels — header, ITM bold on Symbol+Strike, detail=2 sub-table blockquote, no ANSI.

### `test_commands_option.py`

`CliRunner` with mocked `SchwabClient`.

- Invalid spec → exit 2.
- No session → exit 1.
- Session expired → exit 1.
- Empty chain → exit 1.
- Happy path at `--detail=0`, `--detail=1`, `--detail=2`.
- `--json` routing.
- `--md` routing.
- `--json --md` → exit 2.

### Manual smoke (after tests pass)

1. `schwab_cli option NVDA 270115` → Layout A.
2. `schwab_cli option NVDA '270115*250'` → Layout A, one-strike.
3. `schwab_cli option NVDA '270115P*' --strikes=4` → Layout B (auto-fallback).
4. `schwab_cli option NVDA 270115 --detail=1` → Layout B full columns.
5. `schwab_cli option NVDA 270115 --detail=2` → Layout B+inline.
6. `schwab_cli option NVDA 270115 --json | jq '.contracts[0]'` → structured.
7. `schwab_cli option NVDA 270115 --md` → markdown.
8. Run in 80-col terminal → Layout A drops Vol/OI with stderr note.

## Decisions Summary

| Decision | Choice |
|---|---|
| Spec syntax | `<YYMMDD>[P|C]*[strike]` with `*` separator |
| Date format | YYMMDD, assume 20YY |
| `--strikes` semantics | N total around ATM (even: N/2 each side; odd: ATM + (N-1)/2 each side) |
| API endpoint | `GET /marketdata/v1/chains`, hand-rolled via existing `SchwabClient.get()` |
| Default layout | A (side-by-side, classic chain) |
| `--detail=1` | Layout B, one row per contract with Greeks + Vol/OI |
| `--detail=2` | Layout B + indented sub-table per contract |
| One-side filter + `--detail=0` | auto-fallback to Layout B |
| Width adaptation | drop columns from the right; stderr note; HUMAN only |
| ITM indication | bold row styling (column dropped in HUMAN and MD) |
| Multiplier | dropped from HUMAN and MD visual; kept in JSON |
| Settlement type | shown in contract heading at `--detail=2`; separate field in JSON |
| Sign colors | green positive / red negative on `Δ` and `Last` (HUMAN only) |
| Shell quoting | `*` must be quoted; documented in `--help` and README |
| New dependencies | none |

## Future Work (deferred)

- Multi-expiration view (`--expiry 2027-01-15,2027-02-19` or `--weekly`).
- Multi-leg strategy views (verticals, straddles).
- Historical option data (Schwab's pricing history endpoint).
- `brief` / `context` command composing chains into LLM-friendly JSON blobs.
