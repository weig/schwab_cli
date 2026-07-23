"""Pure backup logic — hashing, export, naming, retention. No network."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
RETENTION_DAYS = 28

# market_data.db — append-only tables safely captured by a captured_at_ms
# watermark. Everything else (rows updated in place: rv_fwd_21d backfill,
# ledger settlement, tier state, subscriptions...) is exported complete.
APPEND_TABLES = ["vol_snapshots", "put_chain_snapshots", "ohlcv_daily",
                 "account_nav_daily", "option_chain_snapshots"]
MUTABLE_TABLES = ["contract_snapshots", "events", "index_membership",
                  "daily_ranking", "paper_ledger", "subscriptions",
                  "index_subscriptions", "ticker_state"]

# Config backup scope (relative to the schwab config dir). session.json is
# excluded (rotates constantly, useless+risky to archive); backup credentials
# and passphrase must never ride the backup they protect; state is runtime.
CONFIG_INCLUDE = ["config.json", "dataset.json", "notification.json"]
CONFIG_INCLUDE_DIRS = ["webauth", "jobs"]
CONFIG_EXCLUDE = {"session.json", "backup_r2.env", "backup_passphrase",
                  "backup_state.json"}


# ---------------------------------------------------------------- config hash

def config_file_list(config_dir: Path) -> list[Path]:
    out = []
    for name in CONFIG_INCLUDE:
        p = config_dir / name
        if p.is_file():
            out.append(p)
    for d in CONFIG_INCLUDE_DIRS:
        dd = config_dir / d
        if dd.is_dir():
            out.extend(sorted(p for p in dd.glob("*.json") if p.is_file()))
    return [p for p in out if p.name not in CONFIG_EXCLUDE]


def config_hash(config_dir: Path) -> tuple[str, str]:
    """(hash12, manifest_text): per-file sha256, `<hash>  <relpath>` lines
    sorted by filename, then sha256 over the whole list."""
    lines = []
    for p in config_file_list(config_dir):
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        lines.append(f"{h}  {p.relative_to(config_dir)}")
    lines.sort(key=lambda ln: ln.split("  ", 1)[1])
    manifest = "\n".join(lines) + "\n"
    return hashlib.sha256(manifest.encode()).hexdigest()[:12], manifest


def build_config_zip(config_dir: Path, out_path: Path) -> tuple[str, Path]:
    h, manifest = config_hash(config_dir)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("MANIFEST.sha256", manifest)
        for p in config_file_list(config_dir):
            z.write(p, arcname=str(p.relative_to(config_dir)))
    return h, out_path


# ---------------------------------------------------------------- data export

def _insertable_cols(conn: sqlite3.Connection, table: str) -> list[str]:
    """Real columns only — generated columns (hidden 2/3 in table_xinfo,
    e.g. vol_snapshots.archive_date) can't be INSERTed on restore."""
    return [r[1] for r in conn.execute(f"PRAGMA table_xinfo({table})")
            if r[6] == 0]  # noqa: S608


def _rows(conn: sqlite3.Connection, table: str, where: str = "",
          params: tuple = ()) -> list[dict]:
    cols = _insertable_cols(conn, table)
    cur = conn.execute(
        f"SELECT {', '.join(cols)} FROM {table} {where}", params)  # noqa: S608
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def vacuum_into(db_path: Path, out_path: Path) -> None:
    """Consistent, compacted, byte-faithful snapshot of a live SQLite db."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("VACUUM INTO ?", (str(out_path),))
    finally:
        conn.close()


def table_counts(db_path: Path, prefix: str) -> dict:
    conn = sqlite3.connect(str(db_path))
    try:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        return {f"{prefix}.{t}":
                {"rows": conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0],  # noqa: S608
                 "complete": True}
                for t in names}
    finally:
        conn.close()


def export_market_data(db_path: Path, out_dir: Path,
                       watermark_ms: int | None) -> dict:
    """Export market_data tables to jsonl. watermark_ms=None → full export.
    Returns per-table manifest {table: {rows, complete}} + new watermark.
    Runs inside one transaction for a consistent snapshot."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    manifest: dict = {"tables": {}}
    try:
        conn.execute("BEGIN")
        new_wm = 0
        for t in APPEND_TABLES:
            if watermark_ms is None:
                rows = _rows(conn, t)
            else:
                rows = _rows(conn, t, "WHERE captured_at_ms > ?",
                             (watermark_ms,))
            _write_jsonl(out_dir / f"market_data.{t}.jsonl", rows)
            manifest["tables"][f"market_data.{t}"] = {
                "rows": len(rows), "complete": watermark_ms is None}
            top = conn.execute(
                f"SELECT MAX(captured_at_ms) FROM {t}").fetchone()[0]  # noqa: S608
            new_wm = max(new_wm, top or 0)
        for t in MUTABLE_TABLES:
            rows = _rows(conn, t)
            _write_jsonl(out_dir / f"market_data.{t}.jsonl", rows)
            manifest["tables"][f"market_data.{t}"] = {
                "rows": len(rows), "complete": True}
        ver = conn.execute("SELECT version FROM schema_version").fetchone()
        manifest["schema_version"] = ver[0] if ver else None
        manifest["watermark_ms"] = new_wm
    finally:
        conn.rollback()
        conn.close()
    return manifest


def export_account(db_path: Path, out_dir: Path) -> dict:
    """account.db is tiny — always exported complete."""
    manifest: dict = {}
    if not db_path.exists():
        return manifest
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        for t in names:
            rows = _rows(conn, t)
            _write_jsonl(out_dir / f"account.{t}.jsonl", rows)
            manifest[f"account.{t}"] = {"rows": len(rows), "complete": True}
    finally:
        conn.rollback()
        conn.close()
    return manifest


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":"), default=str) + "\n")


def build_data_zip(stage: Path, out_path: Path, manifest: dict) -> Path:
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, indent=1))
        for p in sorted(stage.glob("*.jsonl")):
            z.write(p, arcname=p.name)
    return out_path


# ---------------------------------------------------------------- naming

_FULL_RE = re.compile(r"^data/(\d{8})\.jsonl\.zip\.enc$")
_DIFF_RE = re.compile(r"^data/(\d{8})-(\d{8})\.jsonl\.zip\.enc$")


def data_key(base: str, day: str | None = None) -> str:
    return (f"data/{base}.jsonl.zip.enc" if day is None or day == base
            else f"data/{base}-{day}.jsonl.zip.enc")


def parse_data_key(key: str) -> tuple[str, str] | None:
    """→ (base, day) for data/ objects, else None."""
    m = _FULL_RE.match(key)
    if m:
        return m.group(1), m.group(1)
    m = _DIFF_RE.match(key)
    if m:
        return m.group(1), m.group(2)
    return None


def monthly_key(base: str) -> str:
    return f"monthly/{base[:6]}.jsonl.zip.enc"


def config_key(h: str, day: str) -> str:
    return f"config/{h}.{day}.zip.enc"


# ---------------------------------------------------------------- decisions

def et_today(now: datetime | None = None) -> date:
    now = now or datetime.now(tz=NY)
    return now.astimezone(NY).date()


def decide_kind(state: dict | None, schema_version: int | None,
                today: date, force_full: bool) -> str:
    """full | diff. Full when forced, Saturday ET, no usable base, or the
    schema migrated since the base (migrations may rewrite old rows)."""
    if force_full or today.weekday() == 5:
        return "full"
    if not state or not state.get("base_date") or not state.get("watermark_ms"):
        return "full"
    if schema_version is not None and state.get("schema_version") != schema_version:
        return "full"
    return "diff"


def retention_deletions(keys: list[str], today: date) -> list[str]:
    """data/ objects whose BASE date fell out of the retention window.
    Base and its differentials share the leading date → deleted as a group,
    so no differential ever loses its base (孤儿-free by construction)."""
    cutoff = (today - timedelta(days=RETENTION_DAYS)).strftime("%Y%m%d")
    out = []
    for k in keys:
        parsed = parse_data_key(k)
        if parsed and parsed[0] < cutoff:
            out.append(k)
    return out
