"""Daily account NAV reconstruction.

Two modes:

- :func:`snapshot_today` — accurate. Uses the live positions API
  payload (each position's ``marketValue`` is reported by Schwab) +
  cash from ``currentBalances``. ``is_estimated = False``.
- :func:`backfill_day` — approximate. Replays transactions backwards
  to recover the positions held on a target date, then prices:
    * equity via ``ohlcv_daily`` close,
    * options via Black-Scholes (parsed strike + expiry from OSI
      symbol, underlying close from ``ohlcv_daily``, ATM IV from
      ``vol_snapshots``, simple constant risk-free rate).
  Whenever an option contributes to NAV via BS — or when an option's
  inputs are missing and we fall back to cost basis — the day is
  flagged ``is_estimated = True`` so the performance command can warn.

Cash handling: ``end_cash + Σ(reversed cash_deltas after target_day)``
recovers cash on the target day exactly (deterministic; no estimation).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable
from zoneinfo import ZoneInfo

from schwab_cli.analytics.bs import bs_price
from schwab_cli.analytics.twr import parse_transaction
from schwab_cli.ticker import Ticker, resolve as resolve_ticker


_NY = ZoneInfo("America/New_York")

# Pragmatic constants — exactness here is overshadowed by the ATM-IV
# approximation in BS-priced options. Sensitivity analysis: a 100 bps
# rate shift moves an at-the-money 30-DTE call by < 0.5%.
_RISK_FREE_RATE = 0.05
_OPTION_MULTIPLIER = 100.0
_TRADING_DAYS_PER_YEAR = 252.0


@dataclass(frozen=True)
class NavPoint:
    """Result of pricing one historical day. ``estimated`` is True when
    BS reconstruction (or cost-basis fallback) priced any position."""
    day: date
    market_value: float
    cash: float
    estimated: bool


def snapshot_today_from_payload(sec: dict) -> NavPoint:
    """Build today's NavPoint directly from a Schwab account payload.
    No estimation — uses the live ``marketValue`` per position."""
    today = datetime.now(tz=_NY).date()
    cash = float((sec.get("currentBalances") or {}).get("cashBalance") or 0.0)
    mv = 0.0
    for pos in (sec.get("positions") or []):
        try:
            mv += float(pos.get("marketValue") or 0.0)
        except (TypeError, ValueError):
            pass
    return NavPoint(day=today, market_value=mv, cash=cash, estimated=False)


# ---- backfill ---------------------------------------------------------


@dataclass
class _ReconstructedState:
    cash: float
    positions: dict[str, float]   # signed qty per symbol


def _walk_back_to(
    *,
    target: date,
    today: date,
    today_cash: float,
    today_positions: dict[str, float],
    transactions: Iterable[dict],
) -> _ReconstructedState:
    """Roll today's snapshot backwards to ``target`` by un-applying all
    transactions whose NY-date is strictly after ``target``.

    Same algorithm as :func:`schwab_cli.analytics.twr.reconstruct_history`
    but returns a single point instead of a full series.
    """
    cash = float(today_cash)
    positions: dict[str, float] = dict(today_positions)
    for raw in transactions:
        delta = parse_transaction(raw)
        if delta is None or delta.day <= target:
            continue
        cash -= delta.cash_delta
        for sym, qty in delta.position_deltas.items():
            new_qty = positions.get(sym, 0.0) - qty
            if abs(new_qty) < 1e-9:
                positions.pop(sym, None)
            else:
                positions[sym] = new_qty
    return _ReconstructedState(cash=cash, positions=positions)


def backfill_day(
    *,
    day: date,
    today: date,
    today_cash: float,
    today_positions: dict[str, float],
    transactions: list[dict],
    equity_close: dict[str, dict[date, float]],
    underlying_close: dict[str, dict[date, float]],
    atm_iv: dict[str, dict[date, float]],
    avg_price: dict[str, float] | None = None,
) -> NavPoint:
    """Reconstruct + price one historical day.

    Inputs are passed in pre-loaded so the caller can batch fetches
    across many days. ``equity_close`` is keyed by held equity symbol;
    ``underlying_close`` and ``atm_iv`` are keyed by the option's
    underlying symbol (e.g. for "NVDA  260116C00200000" → "NVDA").
    ``avg_price`` is the cost-basis fallback used when BS inputs are
    missing for an option.
    """
    state = _walk_back_to(
        target=day, today=today,
        today_cash=today_cash, today_positions=today_positions,
        transactions=transactions,
    )

    avg_price = avg_price or {}
    market_value = 0.0
    estimated = False
    for sym, qty in state.positions.items():
        price, used_estimate = _price_position_at(
            symbol=sym, day=day,
            equity_close=equity_close,
            underlying_close=underlying_close,
            atm_iv=atm_iv,
            avg_price=avg_price,
        )
        if price is None:
            continue
        estimated = estimated or used_estimate
        market_value += qty * price

    return NavPoint(
        day=day, market_value=market_value, cash=state.cash,
        estimated=estimated,
    )


def _price_position_at(
    *,
    symbol: str,
    day: date,
    equity_close: dict[str, dict[date, float]],
    underlying_close: dict[str, dict[date, float]],
    atm_iv: dict[str, dict[date, float]],
    avg_price: dict[str, float],
) -> tuple[float | None, bool]:
    """Return ``(per-unit price, estimated_flag)`` for one symbol.

    Order of attempts:

    1. Equity OHLCV close on ``day`` (exact, ``estimated=False``).
    2. Equity OHLCV nearest-earlier-day close (exact, ``estimated=False``).
    3. Option BS price using underlying close + ATM IV (estimated).
    4. Cost-basis × multiplier fallback (estimated).
    5. ``(None, False)`` — caller skips this symbol.
    """
    # Equity path.
    if not _is_option_symbol(symbol):
        sym_closes = equity_close.get(symbol) or {}
        if day in sym_closes:
            return sym_closes[day], False
        earlier = [d for d in sym_closes if d <= day]
        if earlier:
            return sym_closes[max(earlier)], False
        return None, False

    # Option path — try BS, then cost-basis fallback.
    parsed = _safe_parse_option(symbol)
    if parsed is not None:
        und = parsed.underlying
        und_closes = underlying_close.get(und) or {}
        u_price = und_closes.get(day)
        if u_price is None:
            earlier = [d for d in und_closes if d <= day]
            if earlier:
                u_price = und_closes[max(earlier)]
        iv_series = atm_iv.get(und) or {}
        iv = iv_series.get(day)
        if iv is None:
            earlier = [d for d in iv_series if d <= day]
            if earlier:
                iv = iv_series[max(earlier)]

        if u_price is not None and iv is not None and parsed.option is not None:
            expiry = _parse_iso_date(parsed.option.date)
            if expiry is not None:
                T = max(
                    (expiry - day).days / 365.25, 1.0 / _TRADING_DAYS_PER_YEAR,
                )
                price_per_share = bs_price(
                    S=u_price, K=parsed.option.strike, T=T,
                    r=_RISK_FREE_RATE, sigma=iv,
                    is_call=(parsed.option.type == "C"),
                )
                return price_per_share * _OPTION_MULTIPLIER, True

    # Cost-basis fallback for options whose BS inputs are missing.
    if symbol in avg_price:
        return avg_price[symbol] * _OPTION_MULTIPLIER, True
    return None, False


# ---- helpers ----------------------------------------------------------


def _is_option_symbol(symbol: str) -> bool:
    return " " in symbol or len(symbol) > 6


def _safe_parse_option(symbol: str) -> Ticker | None:
    try:
        t = resolve_ticker(symbol)
        return t if t.type == "option" else None
    except Exception:
        return None


def _parse_iso_date(s: str) -> date | None:
    try:
        return datetime.strptime(s, "%Y%m%d").date()
    except ValueError:
        return None
