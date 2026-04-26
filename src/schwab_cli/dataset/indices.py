"""Index member fetchers.

stockanalysis.com is the primary provider — exposes CSV-download
endpoints at ``https://stockanalysis.com/list/{slug}-stocks/?p=csv``
for the three indices we support there. SSGA is a fallback for SPX
and DJI only (their public xlsx holdings of SPY / DIA).

Both providers return ``set[str]`` of normalized symbols (Schwab's
dot form, e.g. ``'BRK.B'``).
"""
from __future__ import annotations

import csv
import io

import httpx


INDEX_TO_STOCKANALYSIS_SLUG = {
    "SPX": "sp-500",
    "DJI": "dow-jones",
    "NQ":  "nasdaq-100",
}

INDEX_TO_SSGA_ETF = {
    "SPX": "spy",
    "DJI": "dia",
}

_STOCKANALYSIS_BASE = "https://stockanalysis.com/list"
_SSGA_URL = (
    "https://www.ssga.com/us/en/intermediary/library-content/products/"
    "fund-data/etfs/us/holdings-daily-us-en-{etf}.xlsx"
)
_USER_AGENT = "schwab_cli/dataset (+https://github.com/weig/schwab_cli)"


def fetch_stockanalysis_members(
    index_name: str,
    *,
    client: httpx.Client,
) -> set[str]:
    """Pull members from stockanalysis.com CSV download."""
    slug = INDEX_TO_STOCKANALYSIS_SLUG.get(index_name)
    if slug is None:
        raise ValueError(
            f"{index_name!r} not supported by stockanalysis.com "
            f"(supported: {sorted(INDEX_TO_STOCKANALYSIS_SLUG)})"
        )
    url = f"{_STOCKANALYSIS_BASE}/{slug}-stocks/"
    resp = client.get(url, params={"p": "csv"},
                      headers={"User-Agent": _USER_AGENT})
    if resp.status_code != 200:
        raise RuntimeError(
            f"stockanalysis.com HTTP {resp.status_code} for {url}"
        )
    return _parse_stockanalysis_csv(resp.text)


def _parse_stockanalysis_csv(text: str) -> set[str]:
    """Parse a stockanalysis.com export. Symbol column is 'Symbol'."""
    out: set[str] = set()
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "Symbol" not in reader.fieldnames:
        raise RuntimeError(
            f"stockanalysis CSV missing 'Symbol' column "
            f"(got {reader.fieldnames!r})"
        )
    for row in reader:
        sym = (row.get("Symbol") or "").strip()
        if not sym or " " in sym:
            continue
        out.add(_normalize_symbol(sym))
    if not out:
        raise RuntimeError("stockanalysis CSV had no parseable rows")
    return out


def _normalize_symbol(s: str) -> str:
    """Schwab uses dots (``BRK.B``); SSGA uses dashes (``BRK-B``).

    stockanalysis.com uses dots already, so this is a no-op for that
    path. Used by both adapters so callers always get the same form.
    """
    return s.replace("-", ".").upper()


def fetch_ssga_members(
    index_name: str,
    *,
    client: httpx.Client,
) -> set[str]:
    """Pull members from SSGA's daily-holdings xlsx (SPY / DIA only)."""
    etf = INDEX_TO_SSGA_ETF.get(index_name)
    if etf is None:
        raise ValueError(
            f"{index_name!r} not supported by SSGA "
            f"(supported: {sorted(INDEX_TO_SSGA_ETF)})"
        )
    url = _SSGA_URL.format(etf=etf)
    resp = client.get(url, headers={"User-Agent": _USER_AGENT})
    if resp.status_code != 200:
        raise RuntimeError(f"SSGA HTTP {resp.status_code} for {url}")
    return _parse_ssga_xlsx(resp.content)


def _parse_ssga_xlsx(blob: bytes) -> set[str]:
    """Walk the SSGA xlsx and collect tickers from the 'Ticker' column."""
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    header_idx = None
    ticker_col = None
    for i, row in enumerate(rows):
        if row is None:
            continue
        normalized = [str(c or "").strip().lower() for c in row]
        if "ticker" in normalized:
            header_idx = i
            ticker_col = normalized.index("ticker")
            break
    if header_idx is None or ticker_col is None:
        raise RuntimeError("SSGA xlsx: could not locate Ticker column")
    out: set[str] = set()
    for row in rows[header_idx + 1:]:
        if row is None or all(c is None or str(c).strip() == "" for c in row):
            break  # blank row terminates the table
        sym = row[ticker_col]
        if sym is None:
            continue
        sym = str(sym).strip()
        if not sym or sym.upper() in {"USD", "CASH", "-"}:
            continue
        out.add(_normalize_symbol(sym))
    if not out:
        raise RuntimeError("SSGA xlsx had no parseable tickers")
    return out


def fetch_index_members(
    index_name: str,
    *,
    client: httpx.Client,
) -> set[str]:
    """Resolve members via primary (stockanalysis), then fallback (SSGA)."""
    if index_name == "RUT":
        raise NotImplementedError(
            "RUT (Russell 2000): no clean upstream provider yet — "
            "see docs/plan/dataset.md (TODO)."
        )

    errors: list[str] = []

    if index_name in INDEX_TO_STOCKANALYSIS_SLUG:
        try:
            return fetch_stockanalysis_members(index_name, client=client)
        except (RuntimeError, ValueError) as e:
            errors.append(f"stockanalysis: {e}")

    if index_name in INDEX_TO_SSGA_ETF:
        try:
            return fetch_ssga_members(index_name, client=client)
        except (RuntimeError, ValueError) as e:
            errors.append(f"ssga: {e}")

    raise RuntimeError(
        f"{index_name}: all providers failed — {'; '.join(errors)}"
    )
