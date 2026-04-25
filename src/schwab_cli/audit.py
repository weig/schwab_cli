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

import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_AUDIT_DIR = Path.home() / ".config" / "schwab_cli" / "audit"


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
) -> None:
    """Append one event to today's audit log.

    Always adds a ``ts`` field (UTC ISO-8601) if the caller didn't.
    Errors writing the log are caught and surfaced as a one-line
    stderr warning so logging failures never break the command.
    """
    payload = dict(event)
    payload.setdefault(
        "ts", (now or datetime.now(tz=timezone.utc)).isoformat(),
    )
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
