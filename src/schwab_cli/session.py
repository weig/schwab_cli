from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from schwab_cli.oauth import TokenResponse


SUPPORTED_VERSION = 1
_REQUIRED_FIELDS = (
    "access_token",
    "refresh_token",
    "expires_at",
    "refresh_token_expires_at",
)
REFRESH_TOKEN_LIFETIME_SECONDS = 7 * 24 * 3600


class SessionError(Exception):
    """Raised when an existing session file cannot be used as-is."""


def session_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "schwab_cli" / "session.json"


@dataclass(frozen=True)
class Session:
    access_token: str
    refresh_token: str
    expires_at: int
    refresh_token_expires_at: int
    version: int = 1

    @classmethod
    def from_token_response(cls, tr: "TokenResponse", now: int) -> "Session":
        return cls(
            access_token=tr.access_token,
            refresh_token=tr.refresh_token,
            expires_at=now + tr.expires_in,
            refresh_token_expires_at=now + REFRESH_TOKEN_LIFETIME_SECONDS,
        )


def load() -> Session | None:
    path = session_path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise SessionError(f"malformed JSON in {path}: {e}") from e
    if not isinstance(raw, dict):
        raise SessionError(f"expected object at top level of {path}")
    version = raw.get("version", 1)
    if version != SUPPORTED_VERSION:
        raise SessionError(
            f"unsupported session version {version} in {path} "
            f"(this build supports version {SUPPORTED_VERSION})"
        )
    for field in _REQUIRED_FIELDS:
        if field not in raw:
            raise SessionError(f"missing required field '{field}' in {path}")
    return Session(
        access_token=raw["access_token"],
        refresh_token=raw["refresh_token"],
        expires_at=int(raw["expires_at"]),
        refresh_token_expires_at=int(raw["refresh_token_expires_at"]),
        version=version,
    )


def save(s: Session) -> None:
    path = session_path()
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    try:
        parent.chmod(0o700)
    except OSError:
        pass
    payload = {
        "version": s.version,
        "access_token": s.access_token,
        "refresh_token": s.refresh_token,
        "expires_at": s.expires_at,
        "refresh_token_expires_at": s.refresh_token_expires_at,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    try:
        tmp.chmod(0o600)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
