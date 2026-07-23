"""Backup/restore orchestration over a Remote backend."""
from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from schwab_cli.backup import core, crypto


def _state_path(config_dir: Path) -> Path:
    return config_dir / "backup_state.json"


def load_state(config_dir: Path) -> dict | None:
    p = _state_path(config_dir)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def save_state(config_dir: Path, state: dict) -> None:
    _state_path(config_dir).write_text(json.dumps(state, indent=1) + "\n")


def _max_watermark(db: Path) -> int:
    conn = sqlite3.connect(str(db))
    try:
        wm = 0
        for t in core.APPEND_TABLES:
            row = conn.execute(
                f"SELECT MAX(captured_at_ms) FROM {t}").fetchone()  # noqa: S608
            wm = max(wm, row[0] or 0)
        return wm
    finally:
        conn.close()


def _zip_stage(stage: Path, out_path: Path, manifest: dict) -> Path:
    import zipfile

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, indent=1))
        for p in sorted(stage.iterdir()):
            z.write(p, arcname=p.name)
    return out_path


def _schema_version(db: Path) -> int | None:
    if not db.exists():
        return None
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def run_backup(remote, *, config_dir: Path, storage_dir: Path,
               passfile: Path, force_full: bool = False,
               now: datetime | None = None) -> dict:
    today = core.et_today(now)
    day = today.strftime("%Y%m%d")
    new_pass = crypto.ensure_passphrase(passfile)
    summary: dict = {"date": day, "passphrase_created": new_pass}

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # ---- config (content-addressed; skip when hash already uploaded) --
        h, _ = core.config_hash(config_dir)
        existing = remote.list(f"config/{h}.")
        if existing:
            summary["config"] = {"hash": h, "action": "skip (unchanged)"}
        else:
            zpath = tmp / "config.zip"
            core.build_config_zip(config_dir, zpath)
            enc = crypto.encrypt(zpath, tmp / "config.zip.enc", passfile)
            key = core.config_key(h, day)
            remote.put(enc, key)
            summary["config"] = {"hash": h, "action": "uploaded", "key": key}

        # ---- data ---------------------------------------------------------
        md = storage_dir / "market_data.db"
        acct = storage_dir / "account.db"
        ver = _schema_version(md)
        state = load_state(config_dir)
        kind = core.decide_kind(state, ver, today, force_full)
        base = day if kind == "full" else state["base_date"]
        wm = None if kind == "full" else state["watermark_ms"]

        stage = tmp / "stage"
        stage.mkdir()
        if kind == "full":
            # Full = byte-faithful compacted SQLite snapshots (VACUUM INTO):
            # restore is a file copy — no reconstruction risk. Diffs replay
            # jsonl on top of this genuine base.
            core.vacuum_into(md, stage / "market_data.db")
            manifest = {"tables": core.table_counts(stage / "market_data.db",
                                                    "market_data")}
            if acct.exists():
                core.vacuum_into(acct, stage / "account.db")
                manifest["tables"].update(
                    core.table_counts(stage / "account.db", "account"))
            manifest["schema_version"] = ver
            manifest["watermark_ms"] = _max_watermark(stage / "market_data.db")
            manifest.update(kind=kind, base_date=base, date=day, format="sqlite")
            zpath = _zip_stage(stage, tmp / "data.zip", manifest)
        else:
            manifest = core.export_market_data(md, stage, wm)
            manifest["tables"].update(core.export_account(acct, stage))
            manifest.update(kind=kind, base_date=base, date=day, format="jsonl")
            zpath = core.build_data_zip(stage, tmp / "data.zip", manifest)
        enc = crypto.encrypt(zpath, tmp / "data.zip.enc", passfile)
        key = core.data_key(base, day)
        remote.put(enc, key)
        summary["data"] = {
            "kind": kind, "key": key,
            "rows": sum(t["rows"] for t in manifest["tables"].values()),
            "bytes_enc": enc.stat().st_size,
        }

        if kind == "full":
            mk = core.monthly_key(day)
            if not remote.list(mk):
                remote.put(enc, mk)
                summary["data"]["monthly"] = mk
            save_state(config_dir, {
                "base_date": day, "watermark_ms": manifest["watermark_ms"],
                "schema_version": manifest.get("schema_version"),
            })
        # diff keeps the base watermark in state untouched.

        # ---- retention ----------------------------------------------------
        doomed = core.retention_deletions(remote.list("data/"), today)
        for k in doomed:
            remote.delete(k)
        summary["retention_deleted"] = doomed
    return summary


# ------------------------------------------------------------------- restore

def _apply_archive(zpath: Path, dest: Path) -> dict:
    """Load one decrypted data zip into the dbs under ``dest``.

    Tables marked complete are replaced wholesale; append exports are
    INSERT OR REPLACE so a later archive wins on key collisions.
    """
    import os
    import zipfile

    os.environ["SCHWAB_CLI_STORAGE"] = str(dest)
    from schwab_cli.storage import transactions_history, vol_history

    with zipfile.ZipFile(zpath) as z:
        manifest = json.loads(z.read("manifest.json"))
        if manifest.get("format") == "sqlite":
            for name in z.namelist():
                if name.endswith(".db"):
                    z.extract(name, dest)
            return manifest
        with vol_history.connect() as mconn, transactions_history.connect() as aconn:
            for name in sorted(z.namelist()):
                if not name.endswith(".jsonl"):
                    continue
                dbname, table = name[:-6].split(".", 1)
                conn = mconn if dbname == "market_data" else aconn
                meta = manifest["tables"].get(f"{dbname}.{table}", {})
                rows = [json.loads(ln) for ln in
                        z.read(name).decode().splitlines() if ln]
                if meta.get("complete"):
                    conn.execute(f"DELETE FROM {table}")  # noqa: S608
                if rows:
                    cols = list(rows[0].keys())
                    q = (f"INSERT OR REPLACE INTO {table} "  # noqa: S608
                         f"({', '.join(cols)}) VALUES "
                         f"({', '.join('?' for _ in cols)})")
                    conn.executemany(q, [[r.get(c) for c in cols] for r in rows])
            mconn.commit()
            aconn.commit()
    return manifest


def run_restore(remote, *, day: str, dest: Path, passfile: Path) -> dict:
    """Rebuild the databases as of ``day`` (YYYYMMDD) into ``dest``."""
    keys = remote.list("data/")
    parsed = {k: core.parse_data_key(k) for k in keys}
    target = next((k for k, p in parsed.items() if p and p[1] == day), None)
    if target is None:
        raise SystemExit(f"no backup found for {day}; have: "
                         f"{sorted(p[1] for p in parsed.values() if p)[-5:]}")
    base = parsed[target][0]
    chain = [core.data_key(base)] if base != day else []
    chain.append(target)

    dest.mkdir(parents=True, exist_ok=True)
    applied = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for key in chain:
            enc = tmp / Path(key).name
            remote.get(key, enc)
            z = crypto.decrypt(enc, tmp / (Path(key).name[:-4]), passfile)
            manifest = _apply_archive(z, dest)
            applied.append({"key": key, "kind": manifest.get("kind"),
                            "rows": sum(t["rows"] for t in
                                        manifest["tables"].values())})
    return {"restored_to": str(dest), "chain": applied}
