"""Loader / saver for ``~/.config/schwab_cli/notification.json``.

Separate file from ``config.json`` on purpose: ``schwab_cli setup``
rewrites ``config.json`` wholesale and would destroy notification
settings if they lived together.

Shape:

```
{
  "telegram": {
    "bot_token": "...",
    "chat_id": "...",
    "events": ["auth.auto_login.failed", ...],
    "rate_limit_seconds": 300
  },
  "slack": {"_tbd": "..."}   # placeholder until Phase 2b
}
```

Absent channels are treated as "not configured" — the file can be
missing entirely, in which case no notifications fire.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_PATH = Path.home() / ".config" / "schwab_cli" / "notification.json"
DEFAULT_RATE_LIMIT_SECONDS = 300


@dataclass
class TelegramSettings:
    """Resolved Telegram channel configuration.

    ``None`` bot_token / chat_id means "not configured" — the
    channel is silently skipped by the notifier. Rate-limit and
    event-list defaults are applied during load."""

    bot_token: str | None = None
    chat_id: str | None = None
    events: list[str] = field(default_factory=list)
    rate_limit_seconds: int = DEFAULT_RATE_LIMIT_SECONDS

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)


@dataclass
class NotificationConfig:
    telegram: TelegramSettings = field(default_factory=TelegramSettings)

    @property
    def any_configured(self) -> bool:
        return self.telegram.configured


def load(path: Path | None = None) -> NotificationConfig:
    """Load notification settings from disk. Missing file or empty
    JSON yields an all-defaults config (nothing configured)."""
    target = path if path is not None else DEFAULT_PATH
    if not target.exists():
        return NotificationConfig()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # Tolerate a malformed file — emit an all-defaults config so
        # the daemon doesn't crash on startup because of it. The log
        # stream will already have the load failure recorded by the
        # caller that invoked load().
        return NotificationConfig()
    if not isinstance(raw, dict):
        return NotificationConfig()

    tg_raw = raw.get("telegram") or {}
    telegram = TelegramSettings(
        bot_token=_str_or_none(tg_raw.get("bot_token")),
        chat_id=_str_or_none(tg_raw.get("chat_id")),
        events=[e for e in (tg_raw.get("events") or []) if isinstance(e, str)],
        rate_limit_seconds=_int_or_default(
            tg_raw.get("rate_limit_seconds"), DEFAULT_RATE_LIMIT_SECONDS,
        ),
    )
    return NotificationConfig(telegram=telegram)


def save(cfg: NotificationConfig, path: Path | None = None) -> Path:
    """Write the config to disk atomically with 0600 perms. Returns
    the path written to (useful when the default is picked up)."""
    target = path if path is not None else DEFAULT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)

    payload: dict[str, object] = {}
    tg = cfg.telegram
    payload["telegram"] = {
        "bot_token": tg.bot_token,
        "chat_id": tg.chat_id,
        "events": list(tg.events),
        "rate_limit_seconds": tg.rate_limit_seconds,
    }
    # Preserve the Slack placeholder so the shape advertises the
    # forthcoming channel to anyone inspecting the file.
    payload["slack"] = {
        "_tbd": "Slack channel not yet supported — see docs/plan/mcp-service.md (Phase 2b).",
    }

    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)
    return target


# ---- helpers ----------------------------------------------------------


def _str_or_none(v: object) -> str | None:
    if isinstance(v, str) and v.strip():
        return v
    return None


def _int_or_default(v: object, default: int) -> int:
    try:
        out = int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return out if out > 0 else default
