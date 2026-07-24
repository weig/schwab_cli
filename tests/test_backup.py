"""Tests for the encrypted incremental backup system."""
from __future__ import annotations

import json
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from schwab_cli.backup import core, crypto
from schwab_cli.backup.remote import LocalDirRemote
from schwab_cli.backup.runner import load_state, run_backup, run_restore

NY = ZoneInfo("America/New_York")


# ---------------------------------------------------------------- fixtures

@pytest.fixture
def env(monkeypatch, tmp_path):
    """Isolated config dir + storage dir with a seeded market_data.db."""
    cdir = tmp_path / "config"; cdir.mkdir()
    sdir = tmp_path / "config" / "storage"
    monkeypatch.setenv("SCHWAB_CLI_STORAGE", str(sdir))
    (cdir / "config.json").write_text('{"client_id": "x"}')
    (cdir / "dataset.json").write_text('{"v": 1}')
    (cdir / "session.json").write_text('{"secret": "MUST-NOT-BACKUP"}')
    (cdir / "backup_r2.env").write_text("R2_BUCKET=x")
    jobs = cdir / "jobs"; jobs.mkdir()
    (jobs / "a.json").write_text('{"cron": "1"}')

    from schwab_cli.storage.vol_history import connect, record_snapshot
    from schwab_cli.storage import screener as sc
    with connect() as c:
        record_snapshot(c, symbol="AAA", spot=10.0, atm_iv=0.3,
                        atm_strike=10.0, atm_expiry="2026-08-21", atm_dte=30,
                        captured_at_ms=1000)
        sc.record_contract_snapshot(c, sc.ContractSnapshot(
            snapshot_date="2026-07-01", symbol="AAA", captured_at_ms=1000,
            put_strike=10.0, dte=30))
        c.commit()
    passfile = cdir / "backup_passphrase"
    return dict(cdir=cdir, sdir=sdir, passfile=passfile,
                remote=LocalDirRemote(tmp_path / "r2"))


def _wed(day: str) -> datetime:  # a mid-week ET timestamp for the given date
    d = datetime.strptime(day, "%Y%m%d")
    return datetime(d.year, d.month, d.day, 18, 30, tzinfo=NY)


def _add_snapshot(sym: str, ms: int) -> None:
    from schwab_cli.storage.vol_history import connect, record_snapshot
    with connect() as c:
        record_snapshot(c, symbol=sym, spot=11.0, atm_iv=0.4,
                        atm_strike=11.0, atm_expiry="2026-08-21", atm_dte=29,
                        captured_at_ms=ms)
        c.commit()


# ---------------------------------------------------------------- config hash

def test_config_hash_stable_and_scoped(env):
    h1, manifest = core.config_hash(env["cdir"])
    assert len(h1) == 12
    assert "session.json" not in manifest       # excluded (secrets, rotating)
    assert "backup_r2.env" not in manifest      # never back up the credentials
    assert "jobs/a.json" in manifest
    h2, _ = core.config_hash(env["cdir"])
    assert h1 == h2                              # deterministic
    (env["cdir"] / "config.json").write_text('{"client_id": "y"}')
    h3, _ = core.config_hash(env["cdir"])
    assert h3 != h1                              # content-addressed


def test_config_zip_never_contains_secrets(env):
    _, zpath = core.build_config_zip(env["cdir"], env["cdir"] / "c.zip")
    names = zipfile.ZipFile(zpath).namelist()
    assert "session.json" not in names
    assert "backup_r2.env" not in names
    assert "backup_passphrase" not in names
    assert "config.json" in names and "MANIFEST.sha256" in names


# ---------------------------------------------------------------- crypto

def test_encrypt_roundtrip(tmp_path):
    pf = tmp_path / "pass"; crypto.ensure_passphrase(pf)
    src = tmp_path / "a.txt"; src.write_text("secret payload")
    enc = crypto.encrypt(src, tmp_path / "a.enc", pf)
    assert enc.read_bytes()[:8] == b"Salted__"     # actually encrypted
    dec = crypto.decrypt(enc, tmp_path / "a.dec", pf)
    assert dec.read_text() == "secret payload"


# ---------------------------------------------------------------- run/restore

def test_full_then_diff_then_restore(env):
    r = env["remote"]
    s1 = run_backup(r, config_dir=env["cdir"], storage_dir=env["sdir"],
                    passfile=env["passfile"], force_full=True,
                    now=_wed("20260701"))
    assert s1["data"]["kind"] == "full"
    assert s1["config"]["action"] == "uploaded"
    assert "data/20260701.jsonl.zip.enc" in r.list("data/")
    assert r.list("monthly/") == ["monthly/202607.jsonl.zip.enc"]

    _add_snapshot("BBB", 2000)  # new day of data
    s2 = run_backup(r, config_dir=env["cdir"], storage_dir=env["sdir"],
                    passfile=env["passfile"], now=_wed("20260702"))
    assert s2["data"]["kind"] == "diff"
    assert s2["data"]["key"] == "data/20260701-20260702.jsonl.zip.enc"
    assert s2["config"]["action"].startswith("skip")   # hash unchanged

    out = run_restore(r, day="20260702", dest=env["cdir"] / "restored",
                      passfile=env["passfile"])
    assert [c["kind"] for c in out["chain"]] == ["full", "diff"]
    import sqlite3
    db = sqlite3.connect(str(env["cdir"] / "restored" / "market_data.db"))
    syms = {row[0] for row in db.execute("SELECT symbol FROM vol_snapshots")}
    assert syms == {"AAA", "BBB"}   # base row + incremental row both restored
    n = db.execute("SELECT count(*) FROM contract_snapshots").fetchone()[0]
    assert n == 1                    # mutable table came through complete


def test_diff_captures_mutable_update_without_new_date(env):
    """rv_fwd_21d backfill touches an OLD row — a date watermark would miss
    it; the mutable-table complete export must carry it."""
    r = env["remote"]
    run_backup(r, config_dir=env["cdir"], storage_dir=env["sdir"],
               passfile=env["passfile"], force_full=True, now=_wed("20260701"))
    from schwab_cli.storage.vol_history import connect
    from schwab_cli.storage import screener as sc
    with connect() as c:
        sc.set_forward_rv(c, snapshot_date="2026-07-01", symbol="AAA", rv=0.55)
        c.commit()
    run_backup(r, config_dir=env["cdir"], storage_dir=env["sdir"],
               passfile=env["passfile"], now=_wed("20260702"))
    run_restore(r, day="20260702", dest=env["cdir"] / "rest2",
                passfile=env["passfile"])
    import sqlite3
    db = sqlite3.connect(str(env["cdir"] / "rest2" / "market_data.db"))
    rv = db.execute("SELECT rv_fwd_21d FROM contract_snapshots "
                    "WHERE symbol='AAA'").fetchone()[0]
    assert rv == 0.55


def test_saturday_and_migration_promote_to_full(env):
    r = env["remote"]
    run_backup(r, config_dir=env["cdir"], storage_dir=env["sdir"],
               passfile=env["passfile"], force_full=True, now=_wed("20260701"))
    # Saturday ET → full even without --full.
    sat = datetime(2026, 7, 4, 20, 0, tzinfo=NY)
    s = run_backup(r, config_dir=env["cdir"], storage_dir=env["sdir"],
                   passfile=env["passfile"], now=sat)
    assert s["data"]["kind"] == "full"
    # Simulate a migration: bump recorded schema_version in state.
    st = load_state(env["cdir"]); st["schema_version"] = 1
    from schwab_cli.backup.runner import save_state
    save_state(env["cdir"], st)
    s2 = run_backup(r, config_dir=env["cdir"], storage_dir=env["sdir"],
                    passfile=env["passfile"], now=_wed("20260707"))
    assert s2["data"]["kind"] == "full"


def test_retention_deletes_base_groups_atomically():
    keys = [
        "data/20260501.jsonl.zip.enc",
        "data/20260501-20260502.jsonl.zip.enc",
        "data/20260501-20260506.jsonl.zip.enc",
        "data/20260710.jsonl.zip.enc",
        "data/20260710-20260711.jsonl.zip.enc",
        "monthly/202605.jsonl.zip.enc",
        "config/abc123def456.20260501.zip.enc",
    ]
    doomed = core.retention_deletions(keys, datetime(2026, 7, 23).date())
    # whole 20260501 group gone together; recent group, monthly, config kept.
    assert doomed == ["data/20260501.jsonl.zip.enc",
                      "data/20260501-20260502.jsonl.zip.enc",
                      "data/20260501-20260506.jsonl.zip.enc"]


def test_restore_refuses_unknown_day(env):
    with pytest.raises(SystemExit):
        run_restore(env["remote"], day="20260101",
                    dest=env["cdir"] / "x", passfile=env["passfile"])


# ---- aws resolution (launchd minimal-PATH regression) ---------------------

def test_resolve_aws_prefers_explicit_override():
    from schwab_cli.backup.remote import _resolve_aws
    assert _resolve_aws("/custom/aws") == "/custom/aws"


def test_resolve_aws_falls_back_to_known_location(monkeypatch):
    """Under launchd the PATH excludes Homebrew, so shutil.which returns None;
    we must still find aws at a well-known absolute path rather than shelling
    out a bare 'aws' (which raised FileNotFoundError in the failed job)."""
    import schwab_cli.backup.remote as rem

    monkeypatch.setattr(rem.shutil, "which", lambda _: None)
    monkeypatch.setattr(rem.Path, "exists",
                        lambda self: str(self) == "/opt/homebrew/bin/aws")
    assert rem._resolve_aws() == "/opt/homebrew/bin/aws"


def test_resolve_aws_uses_path_when_available(monkeypatch):
    import schwab_cli.backup.remote as rem
    monkeypatch.setattr(rem.shutil, "which", lambda _: "/usr/local/bin/aws")
    assert rem._resolve_aws() == "/usr/local/bin/aws"


def test_r2remote_resolves_absolute_aws(tmp_path, monkeypatch):
    """R2Remote must hold an absolute aws path, never the bare name."""
    import schwab_cli.backup.remote as rem

    env = tmp_path / "r2.env"
    env.write_text("R2_BUCKET=b\nR2_ENDPOINT=https://x\n")
    monkeypatch.setattr(rem.shutil, "which", lambda _: "/usr/local/bin/aws")
    r = rem.R2Remote(env)
    assert r.aws == "/usr/local/bin/aws"
