"""Doctor Dataset section: market-data stat block + freshness.

These exercise the rendering of ``_check_dataset`` (Subscriptions /
Tiers / Market Data Stat) and the standalone Data Freshness block of
the Data Sync Service section. The scheduling story (jobs run by
``schwab server``) is covered in ``test_doctor_jobs.py``.
"""
from __future__ import annotations

from schwab_cli.commands import doctor as doc


def _stub_db(monkeypatch):
    """Stub vol_history.connect() with a context manager returning a
    fake conn whose execute().fetchall()/fetchone() return safe defaults."""
    import contextlib

    class _FakeConn:
        def execute(self, sql, *_a, **_k):
            return _FakeCursor(sql)

    class _FakeCursor:
        def __init__(self, sql=""):
            self._sql = sql

        def fetchall(self):
            return []

        def fetchone(self):
            # GROUP BY / ORDER BY queries against an empty DB return no
            # rows. Aggregate queries (MAX/COUNT) return a single
            # NULL/0 row; mimic that so callers can do `[0]`.
            if "GROUP BY symbol" in self._sql or "ROW_NUMBER" in self._sql:
                return None
            # OHLCV freshness is coverage-based (MAX(day)); hand back a real
            # trading day so the Data Freshness block renders "latest day".
            if "MAX(day)" in self._sql:
                return ["2026-05-28"]
            return [0]

    @contextlib.contextmanager
    def _connect():
        yield _FakeConn()

    monkeypatch.setattr("schwab_cli.storage.vol_history.connect", _connect)


def test_data_freshness_block_renders_per_table(capsys, monkeypatch):
    """The Data Sync Service section carries a Data Freshness block: OHLCV by
    latest trading day (coverage-based), Volatility/Account by last write."""
    _stub_db(monkeypatch)
    # Don't depend on real launchd / jobs state; keep it quiet.
    monkeypatch.setattr(doc, "_launchctl_loaded", lambda _label: False)
    monkeypatch.setattr(doc, "_print_jobs_block", lambda *_a, **_k: None)

    doc._check_data_sync_service()
    out = capsys.readouterr().out

    assert "Data Sync Service" in out
    assert "Data Freshness" in out
    for task in ("OHLCV", "Volatility", "Account"):
        assert task in out
    # OHLCV is coverage-based (latest trading day); vol/account are write-time.
    assert "latest day 2026-05-28" in out
    assert out.count("last write") >= 2
    # Legacy scheduler / sync-scope artefacts are gone.
    assert "Sync Scope" not in out
    assert "next fire" not in out


def test_dataset_section_drops_last_run_subsection(capsys, monkeypatch):
    """The Dataset section carries no 'Last run' line — last-write lives
    in Data Freshness now. Subscriptions / Tiers / Market Data Stat
    remain."""
    _stub_db(monkeypatch)

    doc._check_dataset()
    out = capsys.readouterr().out

    assert "Last run" not in out
    assert "Subscriptions" in out
    assert "Tiers" in out
    assert "Market Data Stat" in out


def test_market_data_stat_renders_longest_per_group(capsys, monkeypatch):
    """The Market Data Stat block shows the longest series per
    (group, source) so operators can see cache depth at a glance."""
    import contextlib
    from datetime import datetime as _dt, timezone as _tz

    first_ms = int(_dt(2025, 9, 22, tzinfo=_tz.utc).timestamp() * 1000)

    class _Row(dict):
        def __getitem__(self, k):
            if isinstance(k, int):
                return list(self.values())[k]
            return super().__getitem__(k)

    # Two captures on the same NY trading day for AMZN — dedup should
    # collapse the 3 rows below to 2 unique days.
    day1_ms = int(_dt(2026, 4, 27, 18, 0, tzinfo=_tz.utc).timestamp() * 1000)
    day1_ms_b = int(_dt(2026, 4, 27, 21, 0, tzinfo=_tz.utc).timestamp() * 1000)
    day2_ms = int(_dt(2026, 4, 28, 18, 0, tzinfo=_tz.utc).timestamp() * 1000)

    class _Cur:
        def __init__(self, sql, params):
            self.sql = sql
            self.params = params

        def fetchall(self):
            if "ROW_NUMBER" in self.sql:
                return [
                    _Row(source="observed", symbol="AMZN",
                         n=43, first_ms=first_ms),
                    _Row(source="synthetic", symbol="INTC",
                         n=148, first_ms=first_ms),
                ]
            if "captured_at_ms" in self.sql and "WHERE source" in self.sql:
                (source, _symbol), = self.params
                if source == "observed":
                    return [
                        _Row(captured_at_ms=day1_ms),
                        _Row(captured_at_ms=day1_ms_b),
                        _Row(captured_at_ms=day2_ms),
                    ]
                return [_Row(captured_at_ms=first_ms)] * 148
            return []

        def fetchone(self):
            if "FROM ohlcv_daily" in self.sql and "GROUP BY symbol" in self.sql:
                return _Row(symbol="A", n=77, d="2026-01-26")
            return [0]

    class _Conn:
        def execute(self, sql, *params, **_k):
            return _Cur(sql, params)

    @contextlib.contextmanager
    def _connect():
        yield _Conn()

    monkeypatch.setattr("schwab_cli.storage.vol_history.connect", _connect)

    doc._check_dataset()
    out = capsys.readouterr().out
    assert "Market Data Stat" in out
    assert "OHLCV (1 day)" in out
    assert "77 since 2026-01-26" in out
    assert "(A)" not in out
    assert "volatility" in out
    assert "43 rows / 2 days since 2025-09-22" in out
    assert "(observed, AMZN)" in out
    assert "148 rows / 1 days since 2025-09-22" in out
    assert "(synthetic, INTC)" in out
