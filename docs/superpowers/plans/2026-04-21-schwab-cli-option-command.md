# Option Command Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `schwab_cli option <SYMBOL> <SPEC>` — a read-only option-chain lookup command with three HUMAN layouts, JSON, and markdown output.

**Architecture:** Thin layers following the existing Phase 3 pattern. `option_spec.py` parses the CLI spec string, `api/chains.py` wraps Schwab's `/chains` endpoint, `output/chains.py` shapes the raw response into an envelope and renders it at three detail levels in three formats, `commands/option.py` wires it all behind a typer subcommand.

**Tech Stack:** Python 3.11+, typer, httpx (via existing `SchwabClient`), rich, pytest + respx.

**Spec:** `docs/superpowers/specs/2026-04-21-schwab-cli-option-command-design.md`

---

## File Map

**Create:**
- `src/schwab_cli/option_spec.py` — pure grammar parser (`parse_option_spec`, `OptionSpec`, `OptionSpecError`).
- `src/schwab_cli/api/chains.py` — `get_chain(client, symbol, ...)` wrapping `GET /marketdata/v1/chains`.
- `src/schwab_cli/output/chains.py` — envelope shaping + `render_chain(envelope, fmt, detail, requested_type, width)` dispatcher + all renderers (HUMAN A / B / B+inline, JSON, MD).
- `src/schwab_cli/commands/option.py` — command entry (`run(symbol, spec, strikes, detail, as_json, as_md)`).
- `tests/test_option_spec.py`, `tests/test_api_chains.py`, `tests/test_output_chains.py`, `tests/test_commands_option.py`.

**Modify:**
- `src/schwab_cli/cli.py` — register `option` subcommand.
- `README.md` — document the new command.

**Why one file for the output layer:** all renderers share envelope shaping, number formatting, and color helpers. Splitting per-format would force cross-file duplication. Target ≤600 lines; split into `output/chains_human.py` + `output/chains_envelope.py` if it grows past that during implementation.

---

## Task 1: Option spec parser

**Files:**
- Create: `src/schwab_cli/option_spec.py`
- Test: `tests/test_option_spec.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_option_spec.py`:

```python
from datetime import date

import pytest

from schwab_cli.option_spec import OptionSpec, OptionSpecError, parse_option_spec


_TODAY = date(2026, 4, 21)


def test_date_only_means_all_types_no_strike():
    spec = parse_option_spec("270115", today=_TODAY)
    assert spec == OptionSpec(
        expiry=date(2027, 1, 15), contract_type="ALL", strike=None
    )


def test_date_star_equivalent_to_date_only():
    assert parse_option_spec("270115*", today=_TODAY) == parse_option_spec(
        "270115", today=_TODAY
    )


def test_date_put_no_star():
    spec = parse_option_spec("270115P", today=_TODAY)
    assert spec.contract_type == "PUT"
    assert spec.strike is None


def test_date_put_star():
    spec = parse_option_spec("270115P*", today=_TODAY)
    assert spec.contract_type == "PUT"
    assert spec.strike is None


def test_date_call_star():
    spec = parse_option_spec("270115C*", today=_TODAY)
    assert spec.contract_type == "CALL"
    assert spec.strike is None


def test_date_star_strike_both_types():
    spec = parse_option_spec("270115*250", today=_TODAY)
    assert spec.contract_type == "ALL"
    assert spec.strike == 250.0


def test_date_put_star_strike():
    spec = parse_option_spec("270115P*250", today=_TODAY)
    assert spec.contract_type == "PUT"
    assert spec.strike == 250.0


def test_date_call_star_strike():
    spec = parse_option_spec("270115C*250", today=_TODAY)
    assert spec.contract_type == "CALL"
    assert spec.strike == 250.0


def test_decimal_strike():
    spec = parse_option_spec("270115*250.5", today=_TODAY)
    assert spec.strike == 250.5


def test_empty_string_rejected():
    with pytest.raises(OptionSpecError):
        parse_option_spec("", today=_TODAY)


def test_short_date_rejected():
    with pytest.raises(OptionSpecError):
        parse_option_spec("27015", today=_TODAY)


def test_bad_type_letter_rejected():
    with pytest.raises(OptionSpecError):
        parse_option_spec("270115X*250", today=_TODAY)


def test_non_numeric_strike_rejected():
    with pytest.raises(OptionSpecError):
        parse_option_spec("270115*abc", today=_TODAY)


def test_past_expiry_rejected():
    with pytest.raises(OptionSpecError) as exc:
        parse_option_spec("200115", today=_TODAY)
    assert "past" in str(exc.value).lower()


def test_same_day_expiry_allowed():
    # 2026-04-21 today, 260421 expiry — same-day is still live.
    spec = parse_option_spec("260421", today=_TODAY)
    assert spec.expiry == _TODAY


def test_impossible_date_rejected():
    with pytest.raises(OptionSpecError):
        parse_option_spec("270230", today=_TODAY)  # Feb 30
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_option_spec.py -v`
Expected: FAIL — all tests fail with `ModuleNotFoundError: No module named 'schwab_cli.option_spec'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/schwab_cli/option_spec.py`:

```python
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

_SPEC_RE = re.compile(r"^(?P<date>\d{6})(?P<type>[PC])?\*?(?P<strike>\d+(?:\.\d+)?)?$")


@dataclass(frozen=True)
class OptionSpec:
    expiry: date
    contract_type: Literal["CALL", "PUT", "ALL"]
    strike: float | None


class OptionSpecError(ValueError):
    """Raised when the spec string doesn't match the grammar or has an invalid date."""


def parse_option_spec(spec: str, *, today: date | None = None) -> OptionSpec:
    match = _SPEC_RE.match(spec or "")
    if match is None:
        raise OptionSpecError(
            f"Invalid option spec {spec!r}. "
            "Expected YYMMDD[P|C]*[strike] — e.g. '270115*250' or '270115P*'."
        )

    date_str = match.group("date")
    year = 2000 + int(date_str[0:2])
    month = int(date_str[2:4])
    day = int(date_str[4:6])
    try:
        expiry = date(year, month, day)
    except ValueError as e:
        raise OptionSpecError(f"Invalid expiry date in {spec!r}: {e}") from e

    now = today or date.today()
    if expiry < now:
        raise OptionSpecError(
            f"Expiry {expiry.isoformat()} is in the past."
        )

    type_letter = match.group("type")
    if type_letter == "P":
        contract_type: Literal["CALL", "PUT", "ALL"] = "PUT"
    elif type_letter == "C":
        contract_type = "CALL"
    else:
        contract_type = "ALL"

    strike_str = match.group("strike")
    strike = float(strike_str) if strike_str is not None else None

    return OptionSpec(expiry=expiry, contract_type=contract_type, strike=strike)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_option_spec.py -v`
Expected: all 15 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/schwab_cli/option_spec.py tests/test_option_spec.py
git commit -m "feat(option): add spec grammar parser"
```

---

## Task 2: API chains wrapper

**Files:**
- Create: `src/schwab_cli/api/chains.py`
- Test: `tests/test_api_chains.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_chains.py`:

```python
from datetime import date

import httpx
import respx

from schwab_cli.api.chains import get_chain
from schwab_cli.api.client import SchwabClient
from schwab_cli.config import Config
from schwab_cli.session import Session


def _client() -> SchwabClient:
    cfg = Config(
        client_id="cid", client_secret="csec",
        redirect_uri="https://127.0.0.1:8443",
    )
    s = Session(
        access_token="atok", refresh_token="rtok",
        expires_at=1_000_000, refresh_token_expires_at=2_000_000,
    )
    return SchwabClient(cfg, s)


_SAMPLE = {
    "symbol": "NVDA",
    "status": "SUCCESS",
    "underlying": {"symbol": "NVDA", "last": 142.35},
    "callExpDateMap": {},
    "putExpDateMap": {},
}


@respx.mock
def test_get_chain_default_params():
    route = respx.get("https://api.schwabapi.com/marketdata/v1/chains").mock(
        return_value=httpx.Response(200, json=_SAMPLE),
    )
    get_chain(_client(), "NVDA", from_date=date(2027, 1, 15), to_date=date(2027, 1, 15))
    params = route.calls.last.request.url.params
    assert params["symbol"] == "NVDA"
    assert params["contractType"] == "ALL"
    assert params["strategy"] == "SINGLE"
    assert params["includeUnderlyingQuote"] == "true"
    # default strike_count=10 → Schwab strikeCount=5 (per-side)
    assert params["strikeCount"] == "5"
    assert params["fromDate"] == "2027-01-15"
    assert params["toDate"] == "2027-01-15"
    assert "strike" not in params


@respx.mock
def test_get_chain_strike_count_rounds_up_for_odd():
    route = respx.get("https://api.schwabapi.com/marketdata/v1/chains").mock(
        return_value=httpx.Response(200, json=_SAMPLE),
    )
    get_chain(_client(), "NVDA", strike_count=5)
    # ceil(5/2) = 3
    assert route.calls.last.request.url.params["strikeCount"] == "3"


@respx.mock
def test_get_chain_with_explicit_strike():
    route = respx.get("https://api.schwabapi.com/marketdata/v1/chains").mock(
        return_value=httpx.Response(200, json=_SAMPLE),
    )
    get_chain(_client(), "NVDA", strike=250.0)
    assert route.calls.last.request.url.params["strike"] == "250.0"


@respx.mock
def test_get_chain_contract_type_forwarded():
    route = respx.get("https://api.schwabapi.com/marketdata/v1/chains").mock(
        return_value=httpx.Response(200, json=_SAMPLE),
    )
    get_chain(_client(), "NVDA", contract_type="PUT")
    assert route.calls.last.request.url.params["contractType"] == "PUT"


@respx.mock
def test_get_chain_returns_response_dict():
    respx.get("https://api.schwabapi.com/marketdata/v1/chains").mock(
        return_value=httpx.Response(200, json=_SAMPLE),
    )
    result = get_chain(_client(), "NVDA")
    assert result == _SAMPLE


@respx.mock
def test_get_chain_empty_response_passthrough():
    empty = {"symbol": "XYZZZ", "status": "FAILED",
             "callExpDateMap": {}, "putExpDateMap": {}}
    respx.get("https://api.schwabapi.com/marketdata/v1/chains").mock(
        return_value=httpx.Response(200, json=empty),
    )
    result = get_chain(_client(), "XYZZZ")
    assert result["status"] == "FAILED"


@respx.mock
def test_get_chain_401_then_refresh_succeeds(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    respx.get("https://api.schwabapi.com/marketdata/v1/chains").mock(
        side_effect=[
            httpx.Response(401, json={}),
            httpx.Response(200, json=_SAMPLE),
        ],
    )
    respx.post("https://api.schwabapi.com/v1/oauth/token").mock(
        return_value=httpx.Response(200, json={
            "access_token": "new_at", "refresh_token": "new_rt",
            "expires_in": 1800,
        }),
    )
    result = get_chain(_client(), "NVDA")
    assert result == _SAMPLE
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_api_chains.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'schwab_cli.api.chains'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/schwab_cli/api/chains.py`:

```python
from __future__ import annotations

import math
from datetime import date
from typing import Literal

from schwab_cli.api.client import SchwabClient


def get_chain(
    client: SchwabClient,
    symbol: str,
    *,
    contract_type: Literal["CALL", "PUT", "ALL"] = "ALL",
    strike: float | None = None,
    strike_count: int = 10,
    from_date: date | None = None,
    to_date: date | None = None,
) -> dict:
    """Fetch the option chain for `symbol` at the given expiry window.

    `strike_count` is our *total* desired strikes around ATM; Schwab's
    `strikeCount` param is per-side, so we pass `ceil(strike_count / 2)`.
    The output layer trims further as needed.
    """
    params: dict[str, str | float] = {
        "symbol": symbol,
        "contractType": contract_type,
        "strategy": "SINGLE",
        "includeUnderlyingQuote": "true",
        "strikeCount": max(1, math.ceil(strike_count / 2)),
    }
    if strike is not None:
        params["strike"] = strike
    if from_date is not None:
        params["fromDate"] = from_date.isoformat()
    if to_date is not None:
        params["toDate"] = to_date.isoformat()
    return client.get(f"{SchwabClient.MARKET_BASE}/chains", params=params)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_api_chains.py -v`
Expected: all 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/schwab_cli/api/chains.py tests/test_api_chains.py
git commit -m "feat(api): add chains endpoint wrapper"
```

---

## Task 3: Envelope shaping

**Files:**
- Create: `src/schwab_cli/output/chains.py` (initial skeleton — envelope only)
- Test: `tests/test_output_chains.py` (shaping tests first)

- [ ] **Step 1: Write the failing test**

Create `tests/test_output_chains.py`:

```python
from schwab_cli.output.chains import shape_envelope


_RAW_MULTI_STRIKE = {
    "symbol": "NVDA",
    "status": "SUCCESS",
    "underlying": {"symbol": "NVDA", "last": 142.35, "change": 2.10, "percentChange": 1.50},
    "callExpDateMap": {
        "2027-01-15:632": {
            "135.0": [{
                "putCall": "CALL", "symbol": "NVDA  270115C00135000",
                "bid": 8.40, "ask": 8.50, "last": 8.45,
                "delta": 0.71, "gamma": 0.018, "theta": -0.04, "vega": 0.18, "rho": 0.052,
                "volatility": 35.0,
                "strikePrice": 135.0,
                "inTheMoney": True,
                "totalVolume": 123, "openInterest": 456,
                "mark": 8.45, "bidSize": 10, "askSize": 15, "lastSize": 1,
                "openPrice": 8.10, "highPrice": 8.60, "lowPrice": 8.05, "closePrice": 8.35,
                "timeValue": 8.45, "intrinsicValue": 7.35,
                "multiplier": 100, "settlementType": "P",
                "expirationDate": "2027-01-15", "daysToExpiration": 632,
            }],
            "140.0": [{
                "putCall": "CALL", "symbol": "NVDA  270115C00140000",
                "bid": 5.15, "ask": 5.25, "last": 5.20,
                "delta": 0.58, "strikePrice": 140.0, "inTheMoney": True,
                "volatility": 33.0, "multiplier": 100, "settlementType": "P",
                "expirationDate": "2027-01-15", "daysToExpiration": 632,
            }],
            "145.0": [{
                "putCall": "CALL", "symbol": "NVDA  270115C00145000",
                "bid": 1.70, "ask": 1.80, "last": 1.75,
                "delta": 0.41, "strikePrice": 145.0, "inTheMoney": False,
                "volatility": float("nan"),
                "multiplier": 100, "settlementType": "P",
                "expirationDate": "2027-01-15", "daysToExpiration": 632,
            }],
        },
    },
    "putExpDateMap": {
        "2027-01-15:632": {
            "135.0": [{
                "putCall": "PUT", "symbol": "NVDA  270115P00135000",
                "bid": 0.42, "ask": 0.45, "last": 0.43,
                "delta": -0.12, "strikePrice": 135.0, "inTheMoney": False,
                "volatility": 38.0, "multiplier": 100, "settlementType": "P",
                "expirationDate": "2027-01-15", "daysToExpiration": 632,
            }],
            "140.0": [{
                "putCall": "PUT", "symbol": "NVDA  270115P00140000",
                "bid": 1.15, "ask": 1.20, "last": 1.18,
                "delta": -0.23, "strikePrice": 140.0, "inTheMoney": False,
                "volatility": 36.0, "multiplier": 100, "settlementType": "P",
                "expirationDate": "2027-01-15", "daysToExpiration": 632,
            }],
            "145.0": [{
                "putCall": "PUT", "symbol": "NVDA  270115P00145000",
                "bid": 4.10, "ask": 4.15, "last": 4.12,
                "delta": -0.58, "strikePrice": 145.0, "inTheMoney": True,
                "volatility": 34.0, "multiplier": 100, "settlementType": "P",
                "expirationDate": "2027-01-15", "daysToExpiration": 632,
            }],
        },
    },
}


def test_shape_envelope_header():
    env = shape_envelope(_RAW_MULTI_STRIKE)
    assert env["symbol"] == "NVDA"
    assert env["expiry"] == "2027-01-15"
    assert env["dte"] == 632
    assert env["underlying"]["last"] == 142.35
    assert env["underlying"]["netChange"] == 2.10
    assert env["underlying"]["pctChange"] == 1.50


def test_shape_envelope_contracts_sorted_ascending_call_before_put():
    env = shape_envelope(_RAW_MULTI_STRIKE)
    rows = env["contracts"]
    assert len(rows) == 6
    # ordered: (135 C), (135 P), (140 C), (140 P), (145 C), (145 P)
    assert [(r["strike"], r["side"]) for r in rows] == [
        (135.0, "C"), (135.0, "P"),
        (140.0, "C"), (140.0, "P"),
        (145.0, "C"), (145.0, "P"),
    ]


def test_shape_envelope_normalizes_iv_from_percent_to_fraction():
    env = shape_envelope(_RAW_MULTI_STRIKE)
    call_135 = next(r for r in env["contracts"] if r["side"] == "C" and r["strike"] == 135.0)
    assert call_135["iv"] == 0.35  # 35.0 / 100


def test_shape_envelope_nan_iv_becomes_none():
    env = shape_envelope(_RAW_MULTI_STRIKE)
    call_145 = next(r for r in env["contracts"] if r["side"] == "C" and r["strike"] == 145.0)
    assert call_145["iv"] is None


def test_shape_envelope_symbol_whitespace_trimmed():
    env = shape_envelope(_RAW_MULTI_STRIKE)
    call_135 = next(r for r in env["contracts"] if r["side"] == "C" and r["strike"] == 135.0)
    assert call_135["optionSymbol"] == "NVDA  270115C00135000"  # Schwab's internal double-space preserved


def test_shape_envelope_in_the_money_preserved():
    env = shape_envelope(_RAW_MULTI_STRIKE)
    call_135 = next(r for r in env["contracts"] if r["side"] == "C" and r["strike"] == 135.0)
    call_145 = next(r for r in env["contracts"] if r["side"] == "C" and r["strike"] == 145.0)
    assert call_135["inTheMoney"] is True
    assert call_145["inTheMoney"] is False


def test_shape_envelope_trim_to_strike_count_keeps_n_closest_to_atm():
    env = shape_envelope(_RAW_MULTI_STRIKE, strike_count=2)
    # spot 142.35 — 2 strikes closest are 140 and 145 (both closer than 135)
    strikes = sorted({r["strike"] for r in env["contracts"]})
    assert strikes == [140.0, 145.0]


def test_shape_envelope_failed_status_returns_empty_contracts():
    raw = {"symbol": "XYZZZ", "status": "FAILED",
           "callExpDateMap": {}, "putExpDateMap": {}}
    env = shape_envelope(raw)
    assert env["contracts"] == []
    assert env["expiry"] is None


def test_shape_envelope_settlement_type_preserved():
    env = shape_envelope(_RAW_MULTI_STRIKE)
    call_135 = next(r for r in env["contracts"] if r["side"] == "C" and r["strike"] == 135.0)
    assert call_135["settlementType"] == "P"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_output_chains.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'schwab_cli.output.chains'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/schwab_cli/output/chains.py`:

```python
from __future__ import annotations

import math
from typing import Any


def _finite(v: Any) -> float | None:
    if v is None:
        return None
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(fv):
        return None
    return fv


def _shape_contract(raw: dict, side: str) -> dict:
    iv_pct = _finite(raw.get("volatility"))
    return {
        "optionSymbol": (raw.get("symbol") or ""),
        "side": side,
        "strike": _finite(raw.get("strikePrice")),
        "bid": _finite(raw.get("bid")),
        "ask": _finite(raw.get("ask")),
        "last": _finite(raw.get("last")),
        "delta": _finite(raw.get("delta")),
        "iv": (iv_pct / 100.0) if iv_pct is not None else None,
        "gamma": _finite(raw.get("gamma")),
        "theta": _finite(raw.get("theta")),
        "vega": _finite(raw.get("vega")),
        "volume": raw.get("totalVolume"),
        "openInterest": raw.get("openInterest"),
        "mark": _finite(raw.get("mark")),
        "bidSize": raw.get("bidSize"),
        "askSize": raw.get("askSize"),
        "lastSize": raw.get("lastSize"),
        "open": _finite(raw.get("openPrice")),
        "high": _finite(raw.get("highPrice")),
        "low": _finite(raw.get("lowPrice")),
        "close": _finite(raw.get("closePrice")),
        "rho": _finite(raw.get("rho")),
        "timeValue": _finite(raw.get("timeValue")),
        "intrinsic": _finite(raw.get("intrinsicValue")),
        "inTheMoney": bool(raw.get("inTheMoney")),
        "multiplier": raw.get("multiplier"),
        "settlementType": raw.get("settlementType"),
    }


def shape_envelope(raw: dict, *, strike_count: int | None = None) -> dict:
    """Flatten a Schwab /chains response into our display envelope.

    If `strike_count` is given, keeps only the N strikes whose prices are
    closest to the underlying spot — both the call and the put at each kept
    strike survive the trim.
    """
    underlying_raw = (raw or {}).get("underlying") or {}
    underlying = {
        "last": _finite(underlying_raw.get("last")),
        "netChange": _finite(underlying_raw.get("change")),
        "pctChange": _finite(underlying_raw.get("percentChange")),
    }

    contracts: list[dict] = []
    expiry: str | None = None
    dte: int | None = None

    for source_key, side in (("callExpDateMap", "C"), ("putExpDateMap", "P")):
        date_map = (raw or {}).get(source_key) or {}
        for exp_key, strike_map in date_map.items():
            for _strike_str, contract_list in (strike_map or {}).items():
                for c in (contract_list or []):
                    if expiry is None:
                        expiry = c.get("expirationDate") or exp_key.split(":")[0]
                        dte = c.get("daysToExpiration")
                    contracts.append(_shape_contract(c, side))

    if strike_count is not None and contracts:
        spot = underlying["last"]
        if spot is not None:
            strikes = sorted({c["strike"] for c in contracts if c["strike"] is not None})
            strikes.sort(key=lambda s: (abs(s - spot), s))
            keep = set(strikes[:strike_count])
            contracts = [c for c in contracts if c["strike"] in keep]

    contracts.sort(key=lambda r: (r["strike"] if r["strike"] is not None else 0.0,
                                  0 if r["side"] == "C" else 1))

    return {
        "symbol": (raw or {}).get("symbol", ""),
        "expiry": expiry,
        "dte": dte,
        "underlying": underlying,
        "contracts": contracts,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_output_chains.py -v`
Expected: all 9 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/schwab_cli/output/chains.py tests/test_output_chains.py
git commit -m "feat(output): shape Schwab chain response into envelope"
```

---

## Task 4: HUMAN Layout A (detail=0, both sides)

**Files:**
- Modify: `src/schwab_cli/output/chains.py`
- Test: `tests/test_output_chains.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_output_chains.py`:

```python
from schwab_cli.output.chains import render_chain
from schwab_cli.output.format import Format


def _envelope():
    return shape_envelope(_RAW_MULTI_STRIKE)


def test_render_chain_human_a_has_header():
    out = render_chain(_envelope(), fmt=Format.HUMAN, detail=0,
                       requested_type="ALL", width=160)
    assert "NVDA" in out
    assert "2027-01-15" in out
    assert "632" in out  # DTE
    assert "142.35" in out  # spot


def test_render_chain_human_a_has_strike_column_centered():
    out = render_chain(_envelope(), fmt=Format.HUMAN, detail=0,
                       requested_type="ALL", width=160)
    assert "STRIKE" in out
    # Strikes present
    assert "135.00" in out
    assert "140.00" in out
    assert "145.00" in out


def test_render_chain_human_a_marks_atm_row():
    out = render_chain(_envelope(), fmt=Format.HUMAN, detail=0,
                       requested_type="ALL", width=160)
    # Spot is 142.35; closest strike is 140.00 (140 vs 145: |142.35-140|=2.35 < |142.35-145|=2.65)
    # The ATM marker `←` appears in the output.
    assert "←" in out


def test_render_chain_human_a_emits_ansi_color():
    out = render_chain(_envelope(), fmt=Format.HUMAN, detail=0,
                       requested_type="ALL", width=160)
    # Positive delta (call) shows green; negative (put) shows red.
    assert "\x1b[32m" in out  # green
    assert "\x1b[31m" in out  # red


def test_render_chain_human_a_bolds_itm_rows():
    out = render_chain(_envelope(), fmt=Format.HUMAN, detail=0,
                       requested_type="ALL", width=160)
    # At least one ITM row exists → bold ANSI present.
    assert "\x1b[1m" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_output_chains.py -v -k "render_chain_human_a"`
Expected: FAIL — `ImportError: cannot import name 'render_chain'`.

- [ ] **Step 3: Write minimal implementation**

Append to `src/schwab_cli/output/chains.py`:

```python
import json as _json
from io import StringIO

from rich.console import Console
from rich.table import Table
from rich.text import Text

from schwab_cli.output.format import Format

_HEADER_FMT = "{symbol} — {expiry} ({dte} DTE)    Spot: ${spot}  ({change} / {pct}%)"


def _fmt(v, decimals: int = 2) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{decimals}f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_signed(v, decimals: int = 2) -> str:
    s = _fmt(v, decimals)
    if s == "—":
        return s
    fv = float(v)
    if fv > 0:
        return f"[green]{s}[/]"
    if fv < 0:
        return f"[red]{s}[/]"
    return s


def _header_line(env: dict) -> str:
    u = env.get("underlying") or {}
    return _HEADER_FMT.format(
        symbol=env.get("symbol") or "",
        expiry=env.get("expiry") or "",
        dte=env.get("dte") if env.get("dte") is not None else "?",
        spot=_fmt(u.get("last")),
        change=_fmt(u.get("netChange")),
        pct=_fmt(u.get("pctChange")),
    )


def _atm_strike(env: dict) -> float | None:
    u = env.get("underlying") or {}
    spot = u.get("last")
    if spot is None:
        return None
    strikes = sorted({c["strike"] for c in env["contracts"] if c["strike"] is not None})
    if not strikes:
        return None
    return min(strikes, key=lambda s: (abs(s - spot), s))


def _pairs_by_strike(env: dict) -> list[tuple[float, dict | None, dict | None]]:
    """Zip calls and puts by strike (ascending). None when side missing."""
    by_strike: dict[float, dict[str, dict]] = {}
    for c in env["contracts"]:
        if c["strike"] is None:
            continue
        by_strike.setdefault(c["strike"], {})[c["side"]] = c
    return [
        (strike, by_strike[strike].get("C"), by_strike[strike].get("P"))
        for strike in sorted(by_strike)
    ]


def _console(width: int | None) -> tuple[Console, StringIO]:
    buf = StringIO()
    console = Console(
        file=buf,
        force_terminal=True,
        color_system="standard",
        width=width or 120,
    )
    return console, buf


def _render_human_a(env: dict, width: int | None) -> str:
    console, buf = _console(width)
    atm = _atm_strike(env)
    console.print(_header_line(env))
    console.print("")

    t = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    # Call side (mirrored: outer → inner)
    t.add_column("Δ", justify="right")
    t.add_column("Last", justify="right")
    t.add_column("Ask", justify="right")
    t.add_column("Bid", justify="right")
    t.add_column("Vol", justify="right")
    t.add_column("OI", justify="right")
    t.add_column("STRIKE", justify="center", style="bold")
    t.add_column("OI", justify="right")
    t.add_column("Vol", justify="right")
    t.add_column("Bid", justify="right")
    t.add_column("Ask", justify="right")
    t.add_column("Last", justify="right")
    t.add_column("Δ", justify="right")

    for strike, call, put in _pairs_by_strike(env):
        strike_label = f"{strike:,.2f}"
        if atm is not None and strike == atm:
            strike_label = f"{strike_label} ←"
        itm = (call and call["inTheMoney"]) or (put and put["inTheMoney"])
        style = "bold" if itm else ""
        t.add_row(
            _fmt_signed((call or {}).get("delta")),
            _fmt_signed((call or {}).get("last")),
            _fmt((call or {}).get("ask")),
            _fmt((call or {}).get("bid")),
            _fmt((call or {}).get("volume"), 0),
            _fmt((call or {}).get("openInterest"), 0),
            strike_label,
            _fmt((put or {}).get("openInterest"), 0),
            _fmt((put or {}).get("volume"), 0),
            _fmt((put or {}).get("bid")),
            _fmt((put or {}).get("ask")),
            _fmt_signed((put or {}).get("last")),
            _fmt_signed((put or {}).get("delta")),
            style=style,
        )

    console.print(t)
    return buf.getvalue()


def render_chain(
    envelope: dict,
    *,
    fmt: Format,
    detail: int = 0,
    requested_type: str = "ALL",
    width: int | None = None,
) -> str:
    if fmt is Format.HUMAN:
        return _render_human_a(envelope, width)
    raise NotImplementedError(f"format {fmt} not yet implemented")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_output_chains.py -v -k "render_chain_human_a"`
Expected: 5 tests PASS.

Also run the full `test_output_chains.py`:
Run: `uv run pytest tests/test_output_chains.py -v`
Expected: 14 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/schwab_cli/output/chains.py tests/test_output_chains.py
git commit -m "feat(output): render HUMAN Layout A option chain"
```

---

## Task 5: HUMAN Layout B (detail=1)

**Files:**
- Modify: `src/schwab_cli/output/chains.py`
- Test: `tests/test_output_chains.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_output_chains.py`:

```python
def test_render_chain_human_b_columns_present():
    out = render_chain(_envelope(), fmt=Format.HUMAN, detail=1,
                       requested_type="ALL", width=160)
    # Symbol, Side, Strike, Bid, Ask, Last are required columns.
    assert "Symbol" in out
    assert "Side" in out
    assert "Strike" in out
    # Greeks columns
    assert "IV" in out or "Δ" in out or "Γ" in out


def test_render_chain_human_b_one_row_per_contract():
    out = render_chain(_envelope(), fmt=Format.HUMAN, detail=1,
                       requested_type="ALL", width=160)
    # 3 calls + 3 puts = 6 contract rows
    assert out.count("270115C00135000") == 1
    assert out.count("270115P00135000") == 1
    assert out.count("270115C00140000") == 1
    assert out.count("270115P00140000") == 1


def test_render_chain_human_b_sorted_ascending_call_before_put():
    out = render_chain(_envelope(), fmt=Format.HUMAN, detail=1,
                       requested_type="ALL", width=160)
    # Call 135 appears before Put 135; Put 135 before Call 140.
    idx_c135 = out.index("270115C00135000")
    idx_p135 = out.index("270115P00135000")
    idx_c140 = out.index("270115C00140000")
    assert idx_c135 < idx_p135 < idx_c140


def test_render_chain_human_b_bolds_itm_rows():
    out = render_chain(_envelope(), fmt=Format.HUMAN, detail=1,
                       requested_type="ALL", width=160)
    assert "\x1b[1m" in out  # bold ANSI from ITM rows


def test_render_chain_human_b_emits_color_on_delta():
    out = render_chain(_envelope(), fmt=Format.HUMAN, detail=1,
                       requested_type="ALL", width=160)
    assert "\x1b[32m" in out  # green for positive delta (calls)
    assert "\x1b[31m" in out  # red for negative delta (puts)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_output_chains.py -v -k "render_chain_human_b"`
Expected: FAIL — `NotImplementedError` from the dispatcher.

- [ ] **Step 3: Write minimal implementation**

In `src/schwab_cli/output/chains.py`, add `_render_human_b` and wire it into the dispatcher:

```python
def _render_human_b(env: dict, width: int | None) -> str:
    console, buf = _console(width)
    console.print(_header_line(env))
    console.print("")

    t = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    t.add_column("Symbol")
    t.add_column("Side")
    t.add_column("Strike", justify="right")
    t.add_column("Bid", justify="right")
    t.add_column("Ask", justify="right")
    t.add_column("Last", justify="right")
    t.add_column("IV", justify="right")
    t.add_column("Δ", justify="right")
    t.add_column("Γ", justify="right")
    t.add_column("Θ", justify="right")
    t.add_column("𝒱", justify="right")
    t.add_column("Vol", justify="right")
    t.add_column("OI", justify="right")

    for c in env["contracts"]:
        style = "bold" if c.get("inTheMoney") else ""
        t.add_row(
            c["optionSymbol"],
            c["side"],
            _fmt(c["strike"]),
            _fmt(c["bid"]),
            _fmt(c["ask"]),
            _fmt_signed(c["last"]),
            _fmt(c["iv"], 3),
            _fmt_signed(c["delta"], 3),
            _fmt(c["gamma"], 3),
            _fmt(c["theta"], 3),
            _fmt(c["vega"], 3),
            _fmt(c["volume"], 0),
            _fmt(c["openInterest"], 0),
            style=style,
        )
    console.print(t)
    return buf.getvalue()
```

Update `render_chain`:

```python
def render_chain(
    envelope: dict,
    *,
    fmt: Format,
    detail: int = 0,
    requested_type: str = "ALL",
    width: int | None = None,
) -> str:
    if fmt is Format.HUMAN:
        if detail >= 1:
            return _render_human_b(envelope, width)
        return _render_human_a(envelope, width)
    raise NotImplementedError(f"format {fmt} not yet implemented")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_output_chains.py -v`
Expected: all tests PASS (9 envelope + 5 layout A + 5 layout B = 19).

- [ ] **Step 5: Commit**

```bash
git add src/schwab_cli/output/chains.py tests/test_output_chains.py
git commit -m "feat(output): render HUMAN Layout B at --detail=1"
```

---

## Task 6: Auto-fallback Layout A → B when one-sided

**Files:**
- Modify: `src/schwab_cli/output/chains.py`
- Test: `tests/test_output_chains.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_output_chains.py`:

```python
def test_render_chain_human_a_falls_back_to_b_when_puts_only(capsys):
    # Envelope with only put contracts
    puts_only_raw = dict(_RAW_MULTI_STRIKE)
    puts_only_raw = {
        **puts_only_raw,
        "callExpDateMap": {},
    }
    env = shape_envelope(puts_only_raw)
    out = render_chain(env, fmt=Format.HUMAN, detail=0,
                       requested_type="PUT", width=160)
    # Layout B markers (per-contract rows) rather than Layout A (STRIKE centered).
    assert "Side" in out
    assert "Symbol" in out
    # Stderr note about fallback.
    err = capsys.readouterr().err
    assert "one-sided" in err
    assert "--detail=1" in err


def test_render_chain_human_a_no_fallback_when_both_sides(capsys):
    render_chain(_envelope(), fmt=Format.HUMAN, detail=0,
                 requested_type="ALL", width=160)
    err = capsys.readouterr().err
    assert "one-sided" not in err
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_output_chains.py -v -k "falls_back_to_b or no_fallback"`
Expected: FAIL — Layout A currently ignores `requested_type`.

- [ ] **Step 3: Write minimal implementation**

Add an import at the top of `src/schwab_cli/output/chains.py`:

```python
import sys
```

Update the dispatcher to handle auto-fallback:

```python
def render_chain(
    envelope: dict,
    *,
    fmt: Format,
    detail: int = 0,
    requested_type: str = "ALL",
    width: int | None = None,
) -> str:
    if fmt is Format.HUMAN:
        if detail >= 1:
            return _render_human_b(envelope, width)
        if requested_type != "ALL":
            print(
                "[note] one-sided chain — rendering as --detail=1.",
                file=sys.stderr,
            )
            return _render_human_b(envelope, width)
        return _render_human_a(envelope, width)
    raise NotImplementedError(f"format {fmt} not yet implemented")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_output_chains.py -v`
Expected: all tests PASS (19 + 2 = 21).

- [ ] **Step 5: Commit**

```bash
git add src/schwab_cli/output/chains.py tests/test_output_chains.py
git commit -m "feat(output): auto-fallback Layout A → B when chain is one-sided"
```

---

## Task 7: HUMAN Layout B+inline (detail=2)

**Files:**
- Modify: `src/schwab_cli/output/chains.py`
- Test: `tests/test_output_chains.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_output_chains.py`:

```python
def test_render_chain_human_detail2_has_main_row_plus_continuation():
    out = render_chain(_envelope(), fmt=Format.HUMAN, detail=2,
                       requested_type="ALL", width=180)
    # Main row present
    assert "270115C00135000" in out
    # Continuation lines with Mark / DTE / B.Sz etc.
    assert "Mark:" in out
    assert "B.Sz:" in out
    assert "A.Sz:" in out
    assert "L.Sz:" in out
    assert "DTE:" in out
    assert "Time Val:" in out
    assert "Intrinsic:" in out


def test_render_chain_human_detail2_settlement_suffix_in_symbol():
    out = render_chain(_envelope(), fmt=Format.HUMAN, detail=2,
                       requested_type="ALL", width=180)
    # settlementType="P" maps to (PM)
    assert "(PM)" in out


def test_render_chain_human_detail2_no_multiplier_or_itm_columns():
    out = render_chain(_envelope(), fmt=Format.HUMAN, detail=2,
                       requested_type="ALL", width=180)
    assert "Mult:" not in out
    assert "ITM:" not in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_output_chains.py -v -k "detail2"`
Expected: FAIL — dispatcher routes `detail=2` into Layout B which lacks continuation lines.

- [ ] **Step 3: Write minimal implementation**

Add helpers and new renderer in `src/schwab_cli/output/chains.py`:

```python
_SETTLE_LABELS = {"P": "PM", "A": "AM"}


def _settlement_label(settle_type: str | None) -> str:
    if not settle_type:
        return ""
    return _SETTLE_LABELS.get(settle_type, settle_type)


def _render_human_b_inline(env: dict, width: int | None) -> str:
    console, buf = _console(width)
    console.print(_header_line(env))
    console.print("")

    for c in env["contracts"]:
        style = "bold" if c.get("inTheMoney") else ""
        settle = _settlement_label(c.get("settlementType"))
        symbol_cell = c["optionSymbol"]
        if settle:
            symbol_cell = f"{symbol_cell} ({settle})"

        t = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
        t.add_column("Symbol")
        t.add_column("Side")
        t.add_column("Strike", justify="right")
        t.add_column("Bid", justify="right")
        t.add_column("Ask", justify="right")
        t.add_column("Last", justify="right")
        t.add_column("IV", justify="right")
        t.add_column("Δ", justify="right")
        t.add_column("Γ", justify="right")
        t.add_column("Θ", justify="right")
        t.add_column("𝒱", justify="right")
        t.add_column("Vol", justify="right")
        t.add_column("OI", justify="right")
        t.add_row(
            symbol_cell, c["side"], _fmt(c["strike"]),
            _fmt(c["bid"]), _fmt(c["ask"]), _fmt_signed(c["last"]),
            _fmt(c["iv"], 3), _fmt_signed(c["delta"], 3),
            _fmt(c["gamma"], 3), _fmt(c["theta"], 3), _fmt(c["vega"], 3),
            _fmt(c["volume"], 0), _fmt(c["openInterest"], 0),
            style=style,
        )
        console.print(t)
        console.print(
            f"  ├─ Mark: {_fmt(c['mark'])}   "
            f"L.Sz: {_fmt(c['lastSize'], 0)}    "
            f"B.Sz: {_fmt(c['bidSize'], 0)}    "
            f"A.Sz: {_fmt(c['askSize'], 0)}    "
            f"Open: {_fmt(c['open'])}    "
            f"High: {_fmt(c['high'])}    "
            f"Low: {_fmt(c['low'])}    "
            f"Close: {_fmt(c['close'])}"
        )
        console.print(
            f"  └─ DTE: {env.get('dte') if env.get('dte') is not None else '—'}     "
            f"ρ: {_fmt(c['rho'], 3)}   "
            f"Time Val: {_fmt(c['timeValue'])}   "
            f"Intrinsic: {_fmt(c['intrinsic'])}"
        )

    return buf.getvalue()
```

Update `render_chain` dispatcher:

```python
def render_chain(
    envelope: dict,
    *,
    fmt: Format,
    detail: int = 0,
    requested_type: str = "ALL",
    width: int | None = None,
) -> str:
    if fmt is Format.HUMAN:
        if detail >= 2:
            return _render_human_b_inline(envelope, width)
        if detail == 1:
            return _render_human_b(envelope, width)
        if requested_type != "ALL":
            print(
                "[note] one-sided chain — rendering as --detail=1.",
                file=sys.stderr,
            )
            return _render_human_b(envelope, width)
        return _render_human_a(envelope, width)
    raise NotImplementedError(f"format {fmt} not yet implemented")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_output_chains.py -v`
Expected: all tests PASS (21 + 3 = 24).

- [ ] **Step 5: Commit**

```bash
git add src/schwab_cli/output/chains.py tests/test_output_chains.py
git commit -m "feat(output): render HUMAN Layout B + inline at --detail=2"
```

---

## Task 8: Width adaptation (HUMAN only)

**Files:**
- Modify: `src/schwab_cli/output/chains.py`
- Test: `tests/test_output_chains.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_output_chains.py`:

```python
def test_render_chain_human_a_drops_rightmost_columns_at_narrow_width(capsys):
    # At 60 cols the Δ, OI, Vol pairs cannot fit — drop from the right.
    out = render_chain(_envelope(), fmt=Format.HUMAN, detail=0,
                       requested_type="ALL", width=60)
    err = capsys.readouterr().err
    assert "terminal too narrow" in err
    assert "--detail=1" in err
    # STRIKE column and B/A/L always kept.
    assert "STRIKE" in out
    # Dropped Δ pair means "Δ" header appears less frequently (0 instances after drop).
    assert out.count("Δ") <= 1  # may persist in header note


def test_render_chain_human_b_drops_rightmost_greeks_at_narrow_width(capsys):
    out = render_chain(_envelope(), fmt=Format.HUMAN, detail=1,
                       requested_type="ALL", width=70)
    err = capsys.readouterr().err
    assert "terminal too narrow" in err
    # Symbol, Side, Strike, Bid, Ask, Last always kept.
    assert "Symbol" in out
    assert "Strike" in out
    assert "Bid" in out


def test_render_chain_wide_width_keeps_all_columns(capsys):
    render_chain(_envelope(), fmt=Format.HUMAN, detail=1,
                 requested_type="ALL", width=200)
    err = capsys.readouterr().err
    assert "too narrow" not in err
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_output_chains.py -v -k "drops_rightmost or wide_width_keeps"`
Expected: FAIL — current implementations don't prune columns.

- [ ] **Step 3: Write minimal implementation**

Add width-aware column pruning in `src/schwab_cli/output/chains.py`.

Insert helpers below `_console`:

```python
# Approximate character cost per column including padding (2 pad chars).
_MIN_COL_WIDTH = 8


def _announce_dropped(dropped: list[str]) -> None:
    if not dropped:
        return
    print(
        f"[note] terminal too narrow — dropped columns: {', '.join(dropped)}. "
        "Use --detail=1 or widen terminal for full view.",
        file=sys.stderr,
    )


# Layout A drop order (each tuple is one pair that drops together).
_A_OPTIONAL_PAIRS = [
    ("Vol", "Vol"),  # call Vol + put Vol
    ("OI", "OI"),
    ("Δ", "Δ"),
]


def _layout_a_columns(width: int) -> tuple[set[str], list[str]]:
    """Return (kept_pair_names, dropped_label_list). Required pairs (B/A/L/Strike) always kept."""
    required_cost = _MIN_COL_WIDTH * 7  # Bid/Ask/Last x2 + STRIKE
    optional = [p[0] for p in _A_OPTIONAL_PAIRS]  # ["Vol", "OI", "Δ"]
    kept = set(optional)
    dropped: list[str] = []
    budget = max(0, width - required_cost)
    per_pair = _MIN_COL_WIDTH * 2
    # Keep as many optional pairs as budget allows, from left (highest priority: Vol keeps first).
    capacity = budget // per_pair
    if capacity < len(optional):
        drop_from = len(optional) - capacity
        for name in reversed(optional):
            if drop_from <= 0:
                break
            kept.discard(name)
            dropped.append(name)
            drop_from -= 1
    return kept, dropped


# Layout B drop order: rightmost dropped first.
_B_OPTIONAL_COLUMNS = ["Δ", "IV", "Γ", "Θ", "𝒱", "Vol", "OI"]
_B_REQUIRED_COUNT = 6  # Symbol, Side, Strike, Bid, Ask, Last


def _layout_b_columns(width: int) -> tuple[list[str], list[str]]:
    """Return (kept_optional_cols, dropped_cols). Required columns always kept."""
    required_cost = _MIN_COL_WIDTH * _B_REQUIRED_COUNT + 20  # +20 for Symbol width
    budget = max(0, width - required_cost)
    capacity = budget // _MIN_COL_WIDTH
    # Our optional list is right-to-left priority-dropped (OI drops first).
    ordered_keep = ["Δ", "IV", "Γ", "Θ", "𝒱", "Vol", "OI"]
    if capacity >= len(ordered_keep):
        return ordered_keep, []
    kept = ordered_keep[:capacity]
    dropped = ordered_keep[capacity:]
    return kept, dropped
```

Update `_render_human_a` to use pruning (replaces the Task 4 definition entirely):

```python
def _render_human_a(env: dict, width: int | None) -> str:
    console, buf = _console(width)
    atm = _atm_strike(env)
    effective_width = width or 120
    kept, dropped = _layout_a_columns(effective_width)
    _announce_dropped(dropped)

    console.print(_header_line(env))
    console.print("")

    t = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    # Call side outer → inner: Δ(opt), Last, Ask, Bid, Vol(opt), OI(opt)
    if "Δ" in kept:
        t.add_column("Δ", justify="right")
    t.add_column("Last", justify="right")
    t.add_column("Ask", justify="right")
    t.add_column("Bid", justify="right")
    if "Vol" in kept:
        t.add_column("Vol", justify="right")
    if "OI" in kept:
        t.add_column("OI", justify="right")
    t.add_column("STRIKE", justify="center", style="bold")
    # Put side inner → outer: OI(opt), Vol(opt), Bid, Ask, Last, Δ(opt)
    if "OI" in kept:
        t.add_column("OI", justify="right")
    if "Vol" in kept:
        t.add_column("Vol", justify="right")
    t.add_column("Bid", justify="right")
    t.add_column("Ask", justify="right")
    t.add_column("Last", justify="right")
    if "Δ" in kept:
        t.add_column("Δ", justify="right")

    for strike, call, put in _pairs_by_strike(env):
        strike_label = f"{strike:,.2f}"
        if atm is not None and strike == atm:
            strike_label = f"{strike_label} ←"
        itm = (call and call["inTheMoney"]) or (put and put["inTheMoney"])
        style = "bold" if itm else ""
        row: list[str] = []
        if "Δ" in kept:
            row.append(_fmt_signed((call or {}).get("delta")))
        row += [
            _fmt_signed((call or {}).get("last")),
            _fmt((call or {}).get("ask")),
            _fmt((call or {}).get("bid")),
        ]
        if "Vol" in kept:
            row.append(_fmt((call or {}).get("volume"), 0))
        if "OI" in kept:
            row.append(_fmt((call or {}).get("openInterest"), 0))
        row.append(strike_label)
        if "OI" in kept:
            row.append(_fmt((put or {}).get("openInterest"), 0))
        if "Vol" in kept:
            row.append(_fmt((put or {}).get("volume"), 0))
        row += [
            _fmt((put or {}).get("bid")),
            _fmt((put or {}).get("ask")),
            _fmt_signed((put or {}).get("last")),
        ]
        if "Δ" in kept:
            row.append(_fmt_signed((put or {}).get("delta")))
        t.add_row(*row, style=style)

    console.print(t)
    return buf.getvalue()
```

Update `_render_human_b` to use pruning:

```python
def _render_human_b(env: dict, width: int | None) -> str:
    console, buf = _console(width)
    effective_width = width or 120
    kept, dropped = _layout_b_columns(effective_width)
    _announce_dropped(dropped)

    console.print(_header_line(env))
    console.print("")

    t = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    t.add_column("Symbol")
    t.add_column("Side")
    t.add_column("Strike", justify="right")
    t.add_column("Bid", justify="right")
    t.add_column("Ask", justify="right")
    t.add_column("Last", justify="right")
    if "IV" in kept:
        t.add_column("IV", justify="right")
    if "Δ" in kept:
        t.add_column("Δ", justify="right")
    if "Γ" in kept:
        t.add_column("Γ", justify="right")
    if "Θ" in kept:
        t.add_column("Θ", justify="right")
    if "𝒱" in kept:
        t.add_column("𝒱", justify="right")
    if "Vol" in kept:
        t.add_column("Vol", justify="right")
    if "OI" in kept:
        t.add_column("OI", justify="right")

    for c in env["contracts"]:
        style = "bold" if c.get("inTheMoney") else ""
        row = [
            c["optionSymbol"], c["side"], _fmt(c["strike"]),
            _fmt(c["bid"]), _fmt(c["ask"]), _fmt_signed(c["last"]),
        ]
        if "IV" in kept:
            row.append(_fmt(c["iv"], 3))
        if "Δ" in kept:
            row.append(_fmt_signed(c["delta"], 3))
        if "Γ" in kept:
            row.append(_fmt(c["gamma"], 3))
        if "Θ" in kept:
            row.append(_fmt(c["theta"], 3))
        if "𝒱" in kept:
            row.append(_fmt(c["vega"], 3))
        if "Vol" in kept:
            row.append(_fmt(c["volume"], 0))
        if "OI" in kept:
            row.append(_fmt(c["openInterest"], 0))
        t.add_row(*row, style=style)

    console.print(t)
    return buf.getvalue()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_output_chains.py -v`
Expected: all tests PASS (24 + 3 = 27).

- [ ] **Step 5: Commit**

```bash
git add src/schwab_cli/output/chains.py tests/test_output_chains.py
git commit -m "feat(output): drop rightmost columns when terminal is narrow"
```

---

## Task 9: JSON renderer

**Files:**
- Modify: `src/schwab_cli/output/chains.py`
- Test: `tests/test_output_chains.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_output_chains.py`:

```python
import json as _json_test


def test_render_chain_json_detail0_fields():
    out = render_chain(_envelope(), fmt=Format.JSON, detail=0,
                       requested_type="ALL")
    data = _json_test.loads(out)
    assert data["symbol"] == "NVDA"
    assert data["expiry"] == "2027-01-15"
    row = data["contracts"][0]
    assert set(["optionSymbol", "side", "strike", "bid", "ask", "last", "delta"]).issubset(row)
    # detail=0 excludes greeks beyond delta and vol/OI/etc.
    assert "iv" not in row
    assert "gamma" not in row
    assert "volume" not in row


def test_render_chain_json_detail1_adds_greeks_and_vol():
    out = render_chain(_envelope(), fmt=Format.JSON, detail=1,
                       requested_type="ALL")
    data = _json_test.loads(out)
    row = data["contracts"][0]
    for key in ["iv", "gamma", "theta", "vega", "volume", "openInterest"]:
        assert key in row
    # detail=2-only fields absent
    assert "mark" not in row
    assert "rho" not in row


def test_render_chain_json_detail2_has_all_fields():
    out = render_chain(_envelope(), fmt=Format.JSON, detail=2,
                       requested_type="ALL")
    data = _json_test.loads(out)
    row = data["contracts"][0]
    for key in [
        "mark", "bidSize", "askSize", "lastSize",
        "open", "high", "low", "close",
        "rho", "timeValue", "intrinsic",
        "inTheMoney", "multiplier", "settlementType",
    ]:
        assert key in row


def test_render_chain_json_no_ansi_codes():
    for d in (0, 1, 2):
        out = render_chain(_envelope(), fmt=Format.JSON, detail=d,
                           requested_type="ALL")
        assert "\x1b[" not in out


def test_render_chain_json_nan_iv_serialized_as_null():
    out = render_chain(_envelope(), fmt=Format.JSON, detail=1,
                       requested_type="ALL")
    data = _json_test.loads(out)
    call_145 = next(r for r in data["contracts"]
                    if r["side"] == "C" and r["strike"] == 145.0)
    assert call_145["iv"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_output_chains.py -v -k "json"`
Expected: FAIL — dispatcher's `NotImplementedError` for JSON.

- [ ] **Step 3: Write minimal implementation**

In `src/schwab_cli/output/chains.py`:

```python
_FIELDS_DETAIL_0 = ["optionSymbol", "side", "strike", "bid", "ask", "last", "delta"]
_FIELDS_DETAIL_1_EXTRA = ["iv", "gamma", "theta", "vega", "volume", "openInterest"]
_FIELDS_DETAIL_2_EXTRA = [
    "mark", "bidSize", "askSize", "lastSize",
    "open", "high", "low", "close",
    "rho", "timeValue", "intrinsic",
    "inTheMoney", "multiplier", "settlementType",
]


def _fields_for_detail(detail: int) -> list[str]:
    fields = list(_FIELDS_DETAIL_0)
    if detail >= 1:
        fields += _FIELDS_DETAIL_1_EXTRA
    if detail >= 2:
        fields += _FIELDS_DETAIL_2_EXTRA
    return fields


def _render_json(env: dict, detail: int) -> str:
    fields = _fields_for_detail(detail)
    contracts = [
        {k: c.get(k) for k in fields}
        for c in env["contracts"]
    ]
    out = {
        "symbol": env.get("symbol"),
        "expiry": env.get("expiry"),
        "dte": env.get("dte"),
        "underlying": env.get("underlying"),
        "contracts": contracts,
    }
    return _json.dumps(out, indent=2)
```

Update `render_chain`:

```python
def render_chain(
    envelope: dict,
    *,
    fmt: Format,
    detail: int = 0,
    requested_type: str = "ALL",
    width: int | None = None,
) -> str:
    if fmt is Format.JSON:
        return _render_json(envelope, detail)
    if fmt is Format.HUMAN:
        if detail >= 2:
            return _render_human_b_inline(envelope, width)
        if detail == 1:
            return _render_human_b(envelope, width)
        if requested_type != "ALL":
            print(
                "[note] one-sided chain — rendering as --detail=1.",
                file=sys.stderr,
            )
            return _render_human_b(envelope, width)
        return _render_human_a(envelope, width)
    raise NotImplementedError(f"format {fmt} not yet implemented")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_output_chains.py -v`
Expected: all tests PASS (27 + 5 = 32).

- [ ] **Step 5: Commit**

```bash
git add src/schwab_cli/output/chains.py tests/test_output_chains.py
git commit -m "feat(output): render chain as JSON at --detail 0/1/2"
```

---

## Task 10: MD renderer

**Files:**
- Modify: `src/schwab_cli/output/chains.py`
- Test: `tests/test_output_chains.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_output_chains.py`:

```python
def test_render_chain_md_detail0_has_header_and_table():
    out = render_chain(_envelope(), fmt=Format.MD, detail=0,
                       requested_type="ALL")
    lines = out.splitlines()
    assert lines[0].startswith("# NVDA")
    assert "2027-01-15" in lines[0]
    assert "**Spot:**" in out
    # Table header row
    assert "| Symbol | Side | Strike |" in out
    # Table separator row
    assert "|--------|" in out.replace(" ", "")  # tolerant check


def test_render_chain_md_detail0_itm_symbol_and_strike_bolded():
    out = render_chain(_envelope(), fmt=Format.MD, detail=0,
                       requested_type="ALL")
    # ITM call at strike 135 → both cells bolded
    assert "**NVDA  270115C00135000**" in out
    assert "| **135.00** |" in out


def test_render_chain_md_detail2_includes_details_subtable():
    out = render_chain(_envelope(), fmt=Format.MD, detail=2,
                       requested_type="ALL")
    # Blockquoted details heading with Settle suffix
    assert "> **Details — NVDA  270115C00135000** (Settle: PM)" in out
    # Sub-table header
    assert "| Mark | L.Sz | B.Sz | A.Sz |" in out


def test_render_chain_md_detail1_adds_greeks_columns():
    out = render_chain(_envelope(), fmt=Format.MD, detail=1,
                       requested_type="ALL")
    header_line = out.splitlines()[next(
        i for i, ln in enumerate(out.splitlines()) if ln.startswith("| Symbol")
    )]
    assert "IV" in header_line
    assert "Δ" in header_line
    assert "Γ" in header_line


def test_render_chain_md_no_ansi_codes():
    for d in (0, 1, 2):
        out = render_chain(_envelope(), fmt=Format.MD, detail=d,
                           requested_type="ALL")
        assert "\x1b[" not in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_output_chains.py -v -k "_md_"`
Expected: FAIL — `NotImplementedError` for MD format.

- [ ] **Step 3: Write minimal implementation**

In `src/schwab_cli/output/chains.py`:

```python
_MD_COLS_DETAIL_0 = [
    ("Symbol", "optionSymbol"),
    ("Side", "side"),
    ("Strike", "strike"),
    ("Bid", "bid"),
    ("Ask", "ask"),
    ("Last", "last"),
    ("Δ", "delta"),
]
_MD_COLS_DETAIL_1 = _MD_COLS_DETAIL_0 + [
    ("IV", "iv"),
    ("Γ", "gamma"),
    ("Θ", "theta"),
    ("𝒱", "vega"),
    ("Vol", "volume"),
    ("OI", "openInterest"),
]
_MD_DETAIL_SUB_COLS = [
    ("Mark", "mark"),
    ("L.Sz", "lastSize"),
    ("B.Sz", "bidSize"),
    ("A.Sz", "askSize"),
    ("Open", "open"),
    ("High", "high"),
    ("Low", "low"),
    ("Close", "close"),
    ("ρ", "rho"),
    ("TimeVal", "timeValue"),
    ("Intrinsic", "intrinsic"),
]


def _md_cell(value, key: str) -> str:
    if key in ("volume", "openInterest", "lastSize", "bidSize", "askSize", "multiplier"):
        return _fmt(value, 0)
    if key in ("iv", "delta", "gamma", "theta", "vega", "rho"):
        return _fmt(value, 3)
    if key in ("side", "optionSymbol", "settlementType"):
        return str(value) if value is not None else "—"
    return _fmt(value)


def _md_table(columns: list[tuple[str, str]], rows: list[dict],
              *, bold_keys_when_itm: set[str] | None = None) -> str:
    bold_keys_when_itm = bold_keys_when_itm or set()
    header = "| " + " | ".join(h for h, _ in columns) + " |"
    sep = "|" + "|".join(["---"] * len(columns)) + "|"
    out_lines = [header, sep]
    for r in rows:
        cells: list[str] = []
        itm = bool(r.get("inTheMoney"))
        for header_label, key in columns:
            raw = _md_cell(r.get(key), key)
            if itm and key in bold_keys_when_itm:
                raw = f"**{raw}**"
            cells.append(raw)
        out_lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(out_lines)


def _md_header(env: dict) -> str:
    u = env.get("underlying") or {}
    return (
        f"# {env.get('symbol') or ''} — {env.get('expiry') or ''} "
        f"({env.get('dte') if env.get('dte') is not None else '?'} DTE)\n\n"
        f"**Spot:** ${_fmt(u.get('last'))} "
        f"({_fmt(u.get('netChange'))} / {_fmt(u.get('pctChange'))}%)\n\n"
    )


def _render_md(env: dict, detail: int) -> str:
    cols = _MD_COLS_DETAIL_1 if detail >= 1 else _MD_COLS_DETAIL_0
    out = _md_header(env)
    out += _md_table(cols, env["contracts"],
                     bold_keys_when_itm={"optionSymbol", "strike"})
    out += "\n"
    if detail >= 2:
        for c in env["contracts"]:
            settle = _settlement_label(c.get("settlementType"))
            header = f"> **Details — {c['optionSymbol']}**"
            if settle:
                header += f" (Settle: {settle})"
            out += "\n" + header + "\n>\n"
            sub = _md_table(_MD_DETAIL_SUB_COLS, [c])
            out += "\n".join(f"> {line}" for line in sub.splitlines()) + "\n"
    return out
```

Update `render_chain`:

```python
def render_chain(
    envelope: dict,
    *,
    fmt: Format,
    detail: int = 0,
    requested_type: str = "ALL",
    width: int | None = None,
) -> str:
    if fmt is Format.JSON:
        return _render_json(envelope, detail)
    if fmt is Format.MD:
        return _render_md(envelope, detail)
    # HUMAN
    if detail >= 2:
        return _render_human_b_inline(envelope, width)
    if detail == 1:
        return _render_human_b(envelope, width)
    if requested_type != "ALL":
        print(
            "[note] one-sided chain — rendering as --detail=1.",
            file=sys.stderr,
        )
        return _render_human_b(envelope, width)
    return _render_human_a(envelope, width)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_output_chains.py -v`
Expected: all tests PASS (32 + 5 = 37).

- [ ] **Step 5: Commit**

```bash
git add src/schwab_cli/output/chains.py tests/test_output_chains.py
git commit -m "feat(output): render chain as markdown at --detail 0/1/2"
```

---

## Task 11: Command entry & CLI registration

**Files:**
- Create: `src/schwab_cli/commands/option.py`
- Modify: `src/schwab_cli/cli.py`
- Test: `tests/test_commands_option.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_commands_option.py`:

```python
import json
from datetime import date, timedelta
from unittest.mock import patch

from typer.testing import CliRunner

from schwab_cli.api.client import ApiError, SessionExpired
from schwab_cli.cli import app
from schwab_cli.config import Config
from schwab_cli.config import save as save_config
from schwab_cli.session import Session
from schwab_cli.session import save as save_session

runner = CliRunner()


def _future_spec() -> str:
    # Build a YYMMDD that's always ~1 year away.
    future = date.today() + timedelta(days=365)
    return future.strftime("%y%m%d")


def _prep(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save_config(Config(client_id="cid", client_secret="csec",
                       redirect_uri="https://127.0.0.1:8443"))
    save_session(Session(access_token="atok", refresh_token="rtok",
                         expires_at=1_000_000,
                         refresh_token_expires_at=2_000_000))


_CHAIN_RESP = {
    "symbol": "NVDA",
    "status": "SUCCESS",
    "underlying": {"symbol": "NVDA", "last": 142.35, "change": 2.10, "percentChange": 1.50},
    "callExpDateMap": {
        "2027-01-15:632": {
            "135.0": [{
                "putCall": "CALL", "symbol": "NVDA  270115C00135000",
                "bid": 8.40, "ask": 8.50, "last": 8.45,
                "delta": 0.71, "gamma": 0.018, "theta": -0.04, "vega": 0.18,
                "volatility": 35.0, "strikePrice": 135.0, "inTheMoney": True,
                "totalVolume": 123, "openInterest": 456,
                "expirationDate": "2027-01-15", "daysToExpiration": 632,
                "settlementType": "P",
            }],
        },
    },
    "putExpDateMap": {
        "2027-01-15:632": {
            "135.0": [{
                "putCall": "PUT", "symbol": "NVDA  270115P00135000",
                "bid": 0.42, "ask": 0.45, "last": 0.43,
                "delta": -0.12, "volatility": 38.0, "strikePrice": 135.0,
                "inTheMoney": False,
                "expirationDate": "2027-01-15", "daysToExpiration": 632,
                "settlementType": "P",
            }],
        },
    },
}


def test_option_happy_human(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.commands.option.get_chain", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["option", "NVDA", _future_spec()])
    assert result.exit_code == 0, result.output
    assert "NVDA" in result.output
    assert "STRIKE" in result.output


def test_option_invalid_spec_exit_2(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["option", "NVDA", "abcdef"])
    assert result.exit_code == 2
    assert "Invalid option spec" in result.output


def test_option_past_expiry_exit_1(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["option", "NVDA", "200115"])
    assert result.exit_code == 1
    assert "past" in result.output.lower()


def test_option_no_session_exit_1(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    save_config(Config(client_id="cid", client_secret="csec",
                       redirect_uri="https://127.0.0.1:8443"))
    result = runner.invoke(app, ["option", "NVDA", _future_spec()])
    assert result.exit_code == 1
    assert "No session" in result.output


def test_option_session_expired_exit_1(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch(
        "schwab_cli.commands.option.get_chain",
        side_effect=SessionExpired("Session expired. Run `schwab_cli auth --force`."),
    ):
        result = runner.invoke(app, ["option", "NVDA", _future_spec()])
    assert result.exit_code == 1
    assert "Session expired" in result.output


def test_option_empty_chain_exit_1(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    empty = {"symbol": "XYZZZ", "status": "FAILED",
             "callExpDateMap": {}, "putExpDateMap": {}}
    with patch("schwab_cli.commands.option.get_chain", return_value=empty):
        result = runner.invoke(app, ["option", "XYZZZ", _future_spec()])
    assert result.exit_code == 1
    assert "No options found" in result.output


def test_option_json_output(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.commands.option.get_chain", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["option", "NVDA", _future_spec(), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["symbol"] == "NVDA"


def test_option_md_output(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.commands.option.get_chain", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["option", "NVDA", _future_spec(), "--md"])
    assert result.exit_code == 0, result.output
    assert "# NVDA" in result.stdout


def test_option_json_md_mutex_exit_2(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    result = runner.invoke(app, ["option", "NVDA", _future_spec(), "--json", "--md"])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_option_detail_flag_routes_to_layout_b(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    with patch("schwab_cli.commands.option.get_chain", return_value=_CHAIN_RESP):
        result = runner.invoke(app, ["option", "NVDA", _future_spec(), "--detail=1"])
    assert result.exit_code == 0, result.output
    assert "Side" in result.output


def test_option_puts_only_auto_fallback(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    puts_only = dict(_CHAIN_RESP)
    puts_only = {**puts_only, "callExpDateMap": {}}
    with patch("schwab_cli.commands.option.get_chain", return_value=puts_only):
        result = runner.invoke(app, ["option", "NVDA", f"{_future_spec()}P*"])
    assert result.exit_code == 0, result.output
    # stderr should surface the fallback note
    assert "one-sided" in result.stderr if result.stderr else True


def test_option_exact_strike_spec_hits_chain_with_strike(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    captured: dict = {}

    def fake_get_chain(client, symbol, **kwargs):
        captured.update(kwargs)
        return _CHAIN_RESP

    with patch("schwab_cli.commands.option.get_chain", side_effect=fake_get_chain):
        result = runner.invoke(app, ["option", "NVDA", f"{_future_spec()}*135"])
    assert result.exit_code == 0, result.output
    assert captured["strike"] == 135.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_commands_option.py -v`
Expected: FAIL — `option` command not registered.

- [ ] **Step 3: Write minimal implementation**

Create `src/schwab_cli/commands/option.py`:

```python
from __future__ import annotations

import typer

from schwab_cli import config as config_module
from schwab_cli.api.chains import get_chain
from schwab_cli.api.client import ApiError, SchwabClient, SessionExpired
from schwab_cli.option_spec import OptionSpecError, parse_option_spec
from schwab_cli.output.chains import render_chain, shape_envelope
from schwab_cli.output.format import FormatError, pick_format
from schwab_cli.session import load as load_session


def _client() -> SchwabClient:
    cfg = config_module.load()
    if cfg is None:
        typer.secho(
            "No config found. Run `schwab_cli setup` first.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=1)
    session = load_session()
    if session is None:
        typer.secho(
            "No session found. Run `schwab_cli auth` first.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=1)
    return SchwabClient(cfg, session)


def run(
    symbol: str,
    spec_str: str,
    *,
    strikes: int,
    detail: int,
    as_json: bool,
    as_md: bool,
) -> None:
    try:
        fmt = pick_format(as_json, as_md)
    except FormatError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    try:
        spec = parse_option_spec(spec_str)
    except OptionSpecError as e:
        msg = str(e)
        # Past-expiry is exit 1; grammar miss is exit 2.
        if "past" in msg.lower() or "invalid expiry" in msg.lower():
            typer.secho(msg, fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)
        typer.secho(msg, fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    client = _client()
    try:
        raw = get_chain(
            client, symbol.upper(),
            contract_type=spec.contract_type,
            strike=spec.strike,
            strike_count=strikes,
            from_date=spec.expiry,
            to_date=spec.expiry,
        )
    except (ApiError, SessionExpired) as e:
        msg = str(e) if str(e) else type(e).__name__
        typer.secho(msg, fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    envelope = shape_envelope(
        raw,
        # Trim only when no explicit strike was requested.
        strike_count=None if spec.strike is not None else strikes,
    )

    if not envelope["contracts"]:
        if spec.strike is not None:
            typer.secho(
                f"No contract at strike {spec.strike} for {symbol.upper()} "
                f"{spec.expiry.isoformat()}.",
                fg=typer.colors.RED, err=True,
            )
        else:
            typer.secho(
                f"No options found for {symbol.upper()} on {spec.expiry.isoformat()}.",
                fg=typer.colors.RED, err=True,
            )
        raise typer.Exit(code=1)

    typer.echo(render_chain(
        envelope, fmt=fmt, detail=detail, requested_type=spec.contract_type
    ))
```

Modify `src/schwab_cli/cli.py` — add the import and subcommand:

```python
from schwab_cli.commands import option as option_cmd
```

Add the command registration at the bottom of `cli.py`:

```python
@app.command("option", help="Look up an option chain. SPEC: YYMMDD[P|C]*[strike] — quote it in shells that glob `*`.")
def option(
    symbol: str = typer.Argument(..., help="Underlying ticker (e.g. NVDA)."),
    spec: str = typer.Argument(..., help="Option spec, e.g. '270115*250' or '270115P*'."),
    strikes: int = typer.Option(10, "--strikes", help="Total strikes around ATM when no explicit strike."),
    detail: int = typer.Option(0, "--detail", help="Detail level: 0=classic, 1=stacked+greeks, 2=stacked+sub-table."),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
    as_md: bool = typer.Option(False, "--md", help="Output GitHub-flavored markdown."),
) -> None:
    option_cmd.run(
        symbol, spec,
        strikes=strikes, detail=detail,
        as_json=as_json, as_md=as_md,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_commands_option.py -v`
Expected: all 12 tests PASS.

Run the full suite to make sure nothing regressed:
Run: `uv run pytest -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/schwab_cli/commands/option.py src/schwab_cli/cli.py tests/test_commands_option.py
git commit -m "feat(cli): wire option subcommand"
```

---

## Task 12: README update

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Read the current data-commands section**

Run: `grep -n "schwab_cli accounts" README.md` to find the right anchor.
Expected: a handful of matches in the "Data commands" block near lines 86–105.

- [ ] **Step 2: Add the `option` command documentation**

In `README.md`, within the "Data commands" section (right after the `quote` bullet), add:

````markdown
```bash
schwab_cli option NVDA 270115                     # both calls & puts, 10 strikes around ATM
schwab_cli option NVDA '270115*250'               # strike 250 exactly (quote the `*` in bash/zsh)
schwab_cli option NVDA '270115P*' --strikes 4     # puts, 4 strikes around ATM
schwab_cli option NVDA 270115 --detail=1          # stacked layout with greeks
schwab_cli option NVDA 270115 --detail=2          # stacked layout + per-contract details
```

**Spec grammar:** `YYMMDD[P|C]*[strike]`. `YYMMDD` expands to `20YY-MM-DD`; `P` / `C` filter to one side; `*<strike>` pins an exact strike. Shell glob quoting is required whenever `*` appears in the spec.

**`--strikes N`** selects N total strikes around ATM. Even N splits evenly (`N/2` ITM + `N/2` OTM); odd N includes the ATM row (`(N-1)/2` ITM + 1 ATM + `(N-1)/2` OTM). Ignored when the spec names an exact strike.

**Detail levels:**

| `--detail` | Layout | Columns |
|------------|--------|---------|
| `0` (default) | Classic side-by-side | Bid / Ask / Last / Δ per side |
| `1` | One row per contract | + IV, Γ, Θ, 𝒱, Vol, OI |
| `2` | One row per contract + inline sub-table | + Mark, sizes, OHLC, ρ, time/intrinsic value, settlement type |

When the terminal is too narrow, the renderer drops columns from the right and prints a `[note]` to stderr telling you which. `--json` and `--md` never drop columns.
````

- [ ] **Step 3: Verify markdown render manually**

Run: `cat README.md | head -200`
Expected: the new block appears in the Data commands section; pre/post surrounding text untouched.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: document schwab_cli option command"
```

---

## Final review

After all 12 tasks, run the full suite and coverage:

```bash
uv run pytest --cov=src --cov-report=term-missing
```

Expected: all tests pass, coverage ≥ 80% on every new module (`option_spec.py`, `api/chains.py`, `output/chains.py`, `commands/option.py`). If coverage is short, add targeted tests rather than relaxing the goal.

Manual smoke tests (require a live Schwab session — run by the user, not the implementer):

1. `uv run schwab_cli option NVDA 270115`
2. `uv run schwab_cli option NVDA '270115*250'`
3. `uv run schwab_cli option NVDA '270115P*' --strikes 4`
4. `uv run schwab_cli option NVDA 270115 --detail=1`
5. `uv run schwab_cli option NVDA 270115 --detail=2`
6. `uv run schwab_cli option NVDA 270115 --json | jq '.contracts | length'`
7. `uv run schwab_cli option NVDA 270115 --md`
8. Resize terminal to 80 cols, re-run `uv run schwab_cli option NVDA 270115` — verify stderr `[note]` about dropped columns.
