"""Encrypted incremental backups of the schwab_cli databases + config to R2.

Design (agreed 2026-07-23):
- Daily differential `data/BASE-DATE.jsonl.zip.enc`: append-only big tables
  exported by captured_at_ms watermark since the BASE full; mutable tables
  exported complete (they're small and rows get updated in place — a date
  watermark would silently miss e.g. rv_fwd_21d backfills).
- Weekly full `data/DATE.jsonl.zip.enc` (Saturday ET), also forced whenever
  the schema_version changed (migrations may rewrite history).
- Retention: groups share their BASE leading date, so `base < today-28d`
  deletes a full and its differentials atomically — no orphans. The first
  full of each month is copied to `monthly/YYYYMM.jsonl.zip.enc` and kept
  forever. `config/` is content-addressed and never swept.
- Config `config/<hash12>.DATE.zip.enc`: per-file sha256 list sorted by
  filename, hashed again → skip upload when the hash already exists.
  session.json and the backup credentials/passphrase are excluded.
- Everything is client-side encrypted (openssl AES-256-CBC, PBKDF2) with a
  passphrase kept only on this machine (0600) + user's 1Password.
"""
