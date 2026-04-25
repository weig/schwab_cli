"""Audit log for the ``order`` subcommand.

Every call to ``schwab_cli order ...`` writes one or more JSON-line
records to ``~/.config/schwab_cli/audit/YYYY-MM-DD.order.log``,
covering the full lifecycle of the call:

* ``invoked``                  — at command entry, with the parsed flags / body
* ``preview_ok`` / ``preview_unavailable`` — after the previewOrder round-trip
* ``confirmed`` / ``aborted``  — what the user did at the prompt
* ``placed`` / ``rejected``    — outcome of placeOrder
* ``cancelled`` / ``cancel_failed`` — outcome of cancelOrder
* ``error``                    — any unexpected exception escaping the handler

The log captures **every** call regardless of outcome — aborted prompts
and Schwab rejections are just as interesting after-the-fact as
successful placements. Logging is best-effort: if the disk is full
or the directory isn't writable, we print a warning to stderr and
continue (we'd rather lose the audit row than break the user's
workflow).

File format: one JSON object per line. Sort keys for deterministic
diffs. Keys include at least ``ts`` (UTC ISO-8601), ``subcommand``,
``stage``. Values are sanitized — no access tokens, no refresh tokens.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_AUDIT_DIR = Path.home() / ".config" / "schwab_cli" / "audit"
DEFAULT_HMAC_KEY_PATH = (
    Path.home() / ".config" / "schwab_cli" / "audit_hmac.key"
)


def today_path(*, base_dir: Path | None = None, today: date | None = None) -> Path:
    """Return the audit log path for the current (or supplied) day.

    ``base_dir`` and ``today`` are injectable for tests.
    """
    base = base_dir or DEFAULT_AUDIT_DIR
    d = today or date.today()
    return base / f"{d.isoformat()}.order.log"


def write_event(
    event: dict[str, Any],
    *,
    base_dir: Path | None = None,
    now: datetime | None = None,
    today: date | None = None,
    hmac_key_path: Path | None = None,
) -> None:
    """Append one event to today's audit log.

    Always adds a ``ts`` field (UTC ISO-8601) if the caller didn't,
    plus an ``audit_id`` HMAC-SHA256 over the row's stable
    fingerprint (``ts | subcommand | stage | sha256(body)``) keyed off
    the secret in ``audit_hmac.key`` (auto-generated on first
    write).

    Errors writing the log are caught and surfaced as a one-line
    stderr warning so logging failures never break the command.
    """
    payload = dict(event)
    payload.setdefault(
        "ts", (now or datetime.now(tz=timezone.utc)).isoformat(),
    )

    # Compute audit_id last so it covers every other field.
    payload.setdefault("audit_id", _compute_audit_id(
        payload, hmac_key_path=hmac_key_path,
    ))

    line = json.dumps(payload, default=str, sort_keys=True) + "\n"
    path = today_path(base_dir=base_dir, today=today)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Restrict perms on the directory in case it pre-existed with looser ones.
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as e:
        # Best-effort — never break the command on a logging failure.
        print(
            f"warning: order audit log write failed ({type(e).__name__}: {e})",
            file=sys.stderr,
        )


def _compute_audit_id(
    payload: dict[str, Any], *, hmac_key_path: Path | None,
) -> str:
    """Return a stable HMAC-SHA256 hex digest over the row's
    fingerprint. The fingerprint is JSON-serialised with sorted keys
    over a stable subset (everything except ``audit_id`` itself).
    Failures fall back to plain SHA-256 of the same bytes — still
    tamper-evident under append-only assumptions, just not against a
    well-resourced attacker who can rewrite past rows.
    """
    fingerprint = {k: v for k, v in payload.items() if k != "audit_id"}
    msg = json.dumps(fingerprint, default=str, sort_keys=True).encode("utf-8")
    try:
        key = _ensure_hmac_key(hmac_key_path or DEFAULT_HMAC_KEY_PATH)
        return hmac.new(key, msg, hashlib.sha256).hexdigest()
    except OSError:
        return hashlib.sha256(msg).hexdigest()


def _ensure_hmac_key(path: Path) -> bytes:
    """Read the HMAC key, creating it on first call.

    The key is 32 random bytes hex-encoded (64 ASCII chars).
    Permissions are set to ``0600`` immediately after write.
    """
    if path.exists():
        return path.read_text(encoding="ascii").strip().encode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    key_hex = secrets.token_hex(32)
    # Atomic create + 0600 — write to a temp file then rename.
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(
        str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600,
    )
    try:
        os.write(fd, key_hex.encode("ascii"))
    finally:
        os.close(fd)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key_hex.encode("ascii")


def sanitise_body(body: dict[str, Any]) -> dict[str, Any]:
    """Strip / redact any obviously-sensitive fields before logging.

    The order body itself is the user's own data so we keep it
    largely intact, but we drop the admin-only ``accountNumber`` echo
    we tag onto the local body for clarity (the log already carries
    ``account`` separately) and any synthetic underscore-prefixed
    keys.
    """
    out: dict[str, Any] = {}
    for k, v in body.items():
        if k.startswith("_"):
            continue
        if k == "accountNumber":
            continue
        out[k] = v
    return out
