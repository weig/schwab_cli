# Schwab CLI — `history` Command (Phase 5)

**Date:** 2026-04-22
**Status:** Approved (pending user spec review)
**Scope:** Add a read-only `history` command that fetches OHLCV candle data from Schwab's price-history endpoint and renders it across HUMAN / JSON / MD with consistent date-handling and color rules.

## Goal

Give users a fast, scriptable way to pull historical price candles:

```bash
schwab_cli history NVDA                           # default: 1day candles, last 1y
schwab_cli history NVDA --range=ytd               # year-to-date
schwab_cli history NVDA --range=20240101..20241231 --interval=1wk
schwab_cli history NVDA --range=-7d..now --interval=15min
schwab_cli history NVDA --range=ytd --json | jq '.candles | length'
```

Output defaults to a colored Rich table; `--json` and `--md` produce piping-clean output.

## Non-Goals

- **Multi-symbol fetch in one command** — Schwab's `pricehistory` is single-symbol per call. Multi-symbol can be added later if needed.
- **Width adaptation** — only 8 fixed columns; fits a 100-col terminal. Defer the `_layout_*_kept` mechanism from option chains until users hit real overflow.
- **Custom timezones** — always render in market timezone (`America/New_York` for US equities). A `--tz` flag can be added later if Schwab adds non-US instruments.
- **Extended-hours data by default** — set `needExtendedHoursData=false` for cleaner regular-session candles. Toggle exposed only if users ask.
- **Indicator overlays** (SMA, EMA, RSI, etc.) — out of scope for this command. Belongs in a future analysis layer.

## Decisions Summary

| Decision | Choice |
|---|---|
| Command name | `history` |
| Symbols per call | one |
| Range syntax | `<start>..<end>` with fixed (YYYYMMDD), relative (`-Nu`), or `now` endpoints; plus shortcut keywords `ytd`/`mtd`/`wtd` |
| Date separator | `..` (avoids hyphen ambiguity inside ISO dates) |
| Interval syntax | strict whitelist using long suffixes (`1min`, `15min`, `1day`, `1wk`, `1mo`) — `1m` is ambiguous so we don't accept it |
| Defaults | `--interval=1day`, `--range=-1y..now` |
| Datetime output | market timezone (US/Eastern), date-only for daily+, `YYYY-MM-DD HH:MM:SS` for intraday |
| Color rule | Close green/red vs Open; Change & Change% green positive / red negative |
| Width adaptation | none in v1 |
| New dependencies | none (stdlib `zoneinfo` covers tz) |

## CLI Grammar

```
schwab_cli history <SYMBOL> [--range=<spec>] [--interval=<spec>] [--json | --md]
```

### `--interval` allowed values (strict whitelist)

| `--interval` value | Schwab `frequencyType` | Schwab `frequency` |
|---|---|---|
| `1min` | minute | 1 |
| `5min` | minute | 5 |
| `10min` | minute | 10 |
| `15min` | minute | 15 |
| `30min` | minute | 30 |
| `1day` | daily | 1 |
| `1wk` | weekly | 1 |
| `1mo` | monthly | 1 |

Anything else → error: `--interval must be one of: 1min, 5min, 10min, 15min, 30min, 1day, 1wk, 1mo`. Exit 2.

Default when omitted: `1day`.

### `--range` grammar

Two forms:

**Form 1 — explicit range with `..`:** `<endpoint>..<endpoint>` where each endpoint is one of:

| Endpoint | Meaning | Example |
|---|---|---|
| `YYYYMMDD` | fixed calendar date at market open (09:30 ET) for start, market close (16:00 ET) for end | `20240101` |
| `-Nu` | offset from "now"; `N` is integer ≥1 and `u ∈ {d, w, mo, y}` | `-7d`, `-2w`, `-3mo`, `-1y` |
| `now` | the current moment | `now` |

Examples: `20240101..20241231`, `-7d..now`, `-30d..-1d`, `20230101..now`.

**Form 2 — shortcut keywords (no `..`):**

| Keyword | Resolves to |
|---|---|
| `ytd` | Jan 1 of current year (00:00 ET) `..` now |
| `mtd` | 1st of current month (00:00 ET) `..` now |
| `wtd` | most recent Monday 00:00 ET `..` now (ISO week, matches cash-equity trading week) |

Default when omitted: `-1y..now`.

**Endpoint timing convention:**

- **Fixed `YYYYMMDD` as start** → that date at 00:00 ET.
- **Fixed `YYYYMMDD` as end** → that date at 23:59:59 ET (inclusive whole day).
- **Relative `-Nu` and `now`** → exact moment offset/equal to "now"; not snapped to start/end of day. So `-7d..now` is exactly the last 7 days × 86400 seconds, not the last 7 calendar days.

**Validation errors** (after parsing):

| Condition | Message | Exit |
|---|---|---|
| Range string matches neither form | `--range must be '<start>..<end>' or one of: ytd, mtd, wtd` | 2 |
| Endpoint has unknown unit/format | `invalid endpoint '<v>': expected YYYYMMDD, -Nu (u in d/w/mo/y), or 'now'` | 2 |
| `start >= end` | `range start must be before end` | 1 |
| `start` is in the future | `range start is in the future` | 1 |

## Parsing Module

### `src/schwab_cli/history_spec.py`

Pure module — no IO, no imports from other project modules beyond stdlib + `dataclasses`.

```python
from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class Interval:
    frequency_type: Literal["minute", "daily", "weekly", "monthly"]
    frequency: int
    label: str   # original token, e.g. "15min"


class IntervalSpecError(ValueError):
    """Raised when --interval is not one of the allowed values."""


class RangeSpecError(ValueError):
    """Raised when --range can't be parsed or is semantically invalid.

    `kind` discriminator lets the command layer set the right exit code:
      - "invalid"   → bad grammar              (exit 2)
      - "ordering"  → start >= end             (exit 1)
      - "future"    → start is in the future   (exit 1)
    """
    def __init__(self, message: str, *, kind: str = "invalid") -> None:
        super().__init__(message)
        self.kind = kind


def parse_interval(s: str) -> Interval: ...


def parse_range(s: str, *, now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return (start_utc, end_utc). Tz-aware datetimes in UTC.

    `now` is injectable for deterministic tests; defaults to datetime.now(tz=UTC)
    in production. Calendar interpretation is anchored to America/New_York
    (e.g. "ytd" means Jan 1 of the year in NY time, not UTC year boundary).
    """
```

The `kind` discriminator on `RangeSpecError` mirrors `OptionSpecError` from the option-command spec — keeps the command layer free of message-text matching.

## API Layer

### `src/schwab_cli/api/history.py`

**Endpoint:** `GET https://api.schwabapi.com/marketdata/v1/pricehistory`

```python
def get_history(
    client: SchwabClient,
    symbol: str,
    *,
    frequency_type: Literal["minute", "daily", "weekly", "monthly"],
    frequency: int,
    start: datetime,
    end: datetime,
    need_previous_close: bool = True,
    need_extended_hours: bool = False,
) -> dict:
    """Fetch raw OHLCV candle data for `symbol` over [start, end]."""
```

### Schwab parameter mapping

| Our arg | Schwab param | Notes |
|---|---|---|
| `symbol` | `symbol` | command layer uppercases before calling |
| `frequency_type` | `frequencyType` | one of `minute`/`daily`/`weekly`/`monthly` |
| `frequency` | `frequency` | per the interval table above |
| `start` | `startDate` | epoch milliseconds (UTC); we serialize via `int(dt.timestamp() * 1000)` |
| `end` | `endDate` | epoch milliseconds (UTC) |
| `need_previous_close` | `needPreviousClose` | default `true`; powers the row-0 `change` calculation |
| `need_extended_hours` | `needExtendedHoursData` | default `false` — regular-session candles only for cleaner data |

### Schwab response shape (passed through verbatim)

```jsonc
{
  "symbol": "NVDA",
  "empty": false,
  "previousClose": 142.30,
  "previousCloseDate": 1713312000000,
  "candles": [
    {
      "datetime": 1713398400000,    // UTC epoch ms
      "open": 142.50,
      "high": 144.10,
      "low":  141.90,
      "close": 143.20,
      "volume": 32450123
    }
  ]
}
```

**Auth:** reuses the existing `SchwabClient.get()` — 401→refresh→retry handled there.

**Errors:**

- 4xx (not 401): `ApiError` from `client.get()`.
- 401 after refresh: `SessionExpired` from `client.get()`.
- Network: `ApiError("network: <ExcType>")` — same as other endpoints.
- `empty: true` or `candles: []` → returned as-is; command layer formats a "no candles found" message.

### Tests (`tests/test_api_history.py`)

Respx-mocked, mirroring `test_api_chains.py`:

- Default-params case — verifies `frequencyType=daily`, `frequency=1`, `needPreviousClose=true`, `needExtendedHoursData=false`, and `startDate`/`endDate` correctly converted from `datetime` to epoch ms.
- Per-interval case — `frequency_type=minute`, `frequency=15` round-trip.
- Empty-response passthrough.
- 401→refresh→retry happy path (reuses client fixture pattern).

## Output Layer

### `src/schwab_cli/output/history.py`

```python
def shape_envelope(raw: dict, *, interval: str) -> dict: ...

def render_history(envelope: dict, *, fmt: Format) -> str: ...
```

### Envelope (canonical JSON shape, also feeds HUMAN and MD renderers)

```jsonc
{
  "symbol": "NVDA",
  "interval": "1day",
  "from": "2023-04-22T00:00:00-04:00",
  "to":   "2024-04-22T16:00:00-04:00",
  "previousClose": 270.10,
  "candles": [
    {
      "datetime": "2024-04-22",   // "YYYY-MM-DD" for daily/wk/mo,
                                   // "YYYY-MM-DD HH:MM:SS" for intraday
      "open":   142.50,
      "high":   144.10,
      "low":    141.90,
      "close":  143.20,
      "volume": 32450123,
      "change":     0.90,         // close - prior close
      "changePct":  0.63           // change / prior * 100
    }
  ]
}
```

**Datetime conversion:** Schwab returns UTC epoch ms; we `datetime.fromtimestamp(ms / 1000, tz=ZoneInfo("America/New_York"))` and format per interval. Same NY tz used for `from` and `to` ISO strings in the envelope header.

**Change baseline:**

- Row 0: prior = `previousClose` if present, else `null` → `change`/`changePct` are both `null`.
- Row N (N ≥ 1): prior = `candles[N-1]["close"]`.

**NaN / missing handling:** numeric fields use the same `_finite()` helper from `output/chains.py` (returns `None` for NaN/Infinity); `None` → `null` in JSON, `—` in HUMAN/MD.

### HUMAN format

```
NVDA — 1day  2023-04-22 → 2024-04-22  (252 candles)

Date         Open      High      Low       Close     Change    Change%   Volume
2023-04-24   270.55    273.20    269.10    272.85    +1.20     +0.44     31,452,000
2023-04-25   272.50    274.30    270.40    269.80    -3.05     -1.12     28,331,500
...
```

**Implementation:** Rich `Console(force_terminal=True, color_system="standard")` writing to a `StringIO` (same pattern as `output/accounts.py:render_positions` and `output/chains.py`).

**Coloring:**

- **Close** cell: green if `close > open`, red if `close < open`, plain otherwise.
- **Change** and **Change%** cells: `_fmt_signed`-style — green for positive, red for negative, plain for zero, `—` for `None` (row 0 when `previousClose` absent).
- Volume rendered with thousands separator and zero decimals (`f"{v:,.0f}"`).
- Header line dimmed via Rich `[dim]…[/]`.

### JSON format

`json.dumps(envelope, indent=2)`. No ANSI, no styling. Pipes cleanly to `jq`.

### MD format

GitHub-flavored markdown:

```markdown
# NVDA — 1day  2023-04-22 → 2024-04-22

**Previous close:** $270.10 · **Candles:** 252

| Date       | Open   | High   | Low    | Close  | Change | Change% | Volume     |
|------------|--------|--------|--------|--------|--------|---------|------------|
| 2024-04-22 | 142.50 | 144.10 | 141.90 | 143.20 | +0.90  | +0.63   | 32,450,123 |
```

No ANSI. ITM-style cell bolding from chains is **not** applied here; every row is regular data.

### Tests (`tests/test_output_history.py`)

- Envelope shaping — datetime format adapts to interval (date-only vs `YYYY-MM-DD HH:MM:SS`); America/New_York timezone applied; change/changePct math (row 0 from previousClose, row N from prior close); NaN → null.
- HUMAN renderer — Date/Open/High/Low/Close/Change/Change%/Volume columns present; ANSI green/red on Close + Change cells; `—` for missing first-row change.
- JSON renderer — envelope round-trip; no ANSI in output.
- MD renderer — header (`# <symbol> — <interval> …`), table separator row, no ANSI.
- Empty-candles envelope renders as a clearly empty body without raising.

## Command Entry & CLI Registration

### `src/schwab_cli/commands/history.py`

```python
def run(
    symbol: str,
    *,
    range_str: str,
    interval_str: str,
    as_json: bool,
    as_md: bool,
) -> None:
    # 1. Format mutex (exit 2 on conflict)
    fmt = pick_format(as_json, as_md)

    # 2. Parse interval (exit 2 on invalid)
    try:
        interval = parse_interval(interval_str)
    except IntervalSpecError as e:
        ...; raise typer.Exit(code=2)

    # 3. Parse range
    try:
        start, end = parse_range(range_str)
    except RangeSpecError as e:
        code = 2 if e.kind == "invalid" else 1
        ...; raise typer.Exit(code=code)

    # 4. Load config + session (exit 1 if either missing)
    client = _client()

    # 5. API call (exit 1 on ApiError/SessionExpired)
    try:
        raw = get_history(
            client, symbol.upper(),
            frequency_type=interval.frequency_type,
            frequency=interval.frequency,
            start=start, end=end,
        )
    except (ApiError, SessionExpired) as e:
        ...; raise typer.Exit(code=1)

    # 6. Shape + check empty
    envelope = shape_envelope(raw, interval=interval.label)
    if not envelope["candles"]:
        typer.secho(
            f"No candles found for {symbol.upper()} in "
            f"{range_str} at {interval.label}.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=1)

    # 7. Render
    typer.echo(render_history(envelope, fmt=fmt))
```

### `src/schwab_cli/cli.py` — register subcommand

```python
@app.command(
    "history",
    help="Fetch OHLCV price history for a symbol.",
)
def history(
    symbol: str = typer.Argument(..., help="Ticker (e.g. NVDA)."),
    range_str: str = typer.Option(
        "-1y..now", "--range",
        help="Date range: '<start>..<end>' or one of: ytd, mtd, wtd. "
             "Endpoints: YYYYMMDD, -Nu (u in d/w/mo/y), or 'now'.",
    ),
    interval_str: str = typer.Option(
        "1day", "--interval",
        help="Candle interval: 1min, 5min, 10min, 15min, 30min, 1day, 1wk, 1mo.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
    as_md: bool = typer.Option(False, "--md", help="Output GitHub-flavored markdown."),
) -> None:
    history_cmd.run(
        symbol, range_str=range_str, interval_str=interval_str,
        as_json=as_json, as_md=as_md,
    )
```

## Error Handling Summary

| Failure | Where | Message | Exit |
|---|---|---|---|
| `--json` + `--md` | format picker | `--json and --md are mutually exclusive.` | 2 |
| Invalid interval | parse_interval | `--interval must be one of: …` | 2 |
| Invalid range grammar | parse_range | `--range must be '<start>..<end>' or one of: ytd, mtd, wtd` (or per-endpoint message) | 2 |
| Range start ≥ end | parse_range | `range start must be before end` | 1 |
| Range start in future | parse_range | `range start is in the future` | 1 |
| No config | command entry | `No config found. Run schwab_cli setup first.` | 1 |
| No session | command entry | `No session found. Run schwab_cli auth first.` | 1 |
| Session expired (refresh failed) | client.get() | `Session expired. Run schwab_cli auth --force.` | 1 |
| `ApiError` (4xx/5xx/network) | command entry | passthrough of `ApiError` message | 1 |
| Empty candles | command entry | `No candles found for <SYMBOL> in <range> at <interval>.` | 1 |

## File Layout

```
src/schwab_cli/
├── api/
│   └── history.py                NEW
├── history_spec.py               NEW
├── output/
│   └── history.py                NEW
├── commands/
│   └── history.py                NEW
└── cli.py                        MODIFIED — register `history` subcommand

tests/
├── test_history_spec.py          NEW
├── test_api_history.py           NEW
├── test_output_history.py        NEW
└── test_commands_history.py      NEW
```

**No new dependencies.** `httpx`, `typer`, `rich`, `respx`, plus stdlib `zoneinfo`.

## Test Plan

Coverage target: ≥80% per project rules.

### `test_history_spec.py`

- **`parse_interval`** — one test per allowed token (8 happy paths). Negative tests for `1m`, `2min`, `45min`, `1hr`, `1y`, empty string, garbage.
- **`parse_range` shortcuts** — `ytd`/`mtd`/`wtd` resolve correctly with a fixed `now=datetime(2024, 4, 22, 14, 30, tzinfo=NY)`.
- **`parse_range` explicit forms** — fixed/fixed, fixed/relative, relative/relative, fixed/`now`, relative/`now`.
- **Relative units** — `-7d`, `-2w`, `-3mo`, `-1y`. Negative test for `-2x`, `-d`, `7d` (no minus).
- **Validation** — `start >= end` raises with `kind="ordering"`; future start raises with `kind="future"`; bad grammar raises with `kind="invalid"`.

### `test_api_history.py`

- Default-params request — `frequencyType=daily`, `frequency=1`, `needPreviousClose=true`, `needExtendedHoursData=false`, correct `startDate`/`endDate` epoch-ms conversion.
- Minute-interval request — `frequencyType=minute`, `frequency=15`.
- Empty-response passthrough — `empty: true` returned as-is.
- 401→refresh→retry happy path.

### `test_output_history.py`

- Envelope shaping — datetime format adapts (date for `1day`, `YYYY-MM-DD HH:MM:SS` for `15min`), NY tz applied, change/changePct math (row-0 from previousClose, row-N from previous close, both `null` when previousClose missing), NaN → `null`.
- HUMAN renderer — header line present, all 8 columns, ANSI green and red codes both present (when at least one up day and one down day exist), em-dash on missing change.
- JSON renderer — round-trip via `json.loads`; field set complete; no ANSI in output.
- MD renderer — heading line (`# NVDA — 1day …`), separator row, no ANSI.

### `test_commands_history.py`

`CliRunner` with mocked client. One test per row of the error matrix above, plus happy-path runs at default flags, `--json`, `--md`, custom `--range` and `--interval`.

### Manual smoke (after tests pass, requires live session)

1. `uv run schwab_cli history NVDA` — default 1y of daily candles.
2. `uv run schwab_cli history NVDA --range=ytd`
3. `uv run schwab_cli history NVDA --range=-7d..now --interval=15min`
4. `uv run schwab_cli history NVDA --range=20240101..20241231 --interval=1wk --md`
5. `uv run schwab_cli history NVDA --range=mtd --json | jq '.candles | length'`
6. Bad interval: `uv run schwab_cli history NVDA --interval=2min` — exit 2 with allowed list.
7. Inverted range: `uv run schwab_cli history NVDA --range=20240601..20240101` — exit 1, "start must be before end".

## Future Work (deferred)

When these land, each slots into the existing API + output structure:

- **Multi-symbol batch** — `schwab_cli history NVDA AAPL MSFT --range=…` issuing N parallel HTTP calls (Schwab is single-symbol per request) and rendering one table per symbol.
- **`--extended-hours` flag** — toggle Schwab's `needExtendedHoursData=true` for users who want pre/post-market data.
- **`--tz` flag** — render datetimes in a user-chosen timezone instead of America/New_York.
- **Width adaptation** — drop Volume / Change% columns at narrow widths, copying the `_layout_*_kept` pattern from option chains.
- **Indicator overlays** — SMA / EMA / RSI computed on the fly and rendered as additional columns. Probably its own subcommand (`schwab_cli indicators`) rather than load on `history`.
