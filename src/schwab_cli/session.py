from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from schwab_cli.paths import config_dir

if TYPE_CHECKING:
    from schwab_cli.oauth import TokenResponse


SUPPORTED_VERSION = 2
_READABLE_VERSIONS = (1, 2)
_REQUIRED_FIELDS = (
    "access_token",
    "refresh_token",
    "expires_at",
    "refresh_token_expires_at",
)
REFRESH_TOKEN_LIFETIME_SECONDS = 7 * 24 * 3600
# Schwab access tokens live 30 minutes. v1 session files predate the
# persisted lifetime field; loads of those fall back to this value.
DEFAULT_ACCESS_TOKEN_LIFETIME_S = 1800


class SessionError(Exception):
    """Raised when an existing session file cannot be used as-is."""


def session_path() -> Path:
    """Return the absolute path to session.json.

    Resolution follows ``schwab_cli.paths.config_dir()``: respects
    ``SCHWAB_CLI_CONFIG_DIR`` (test-isolation override) and
    ``XDG_CONFIG_HOME``, falling back to ``~/.config/schwab_cli``.
    """
    return config_dir() / "session.json"


@dataclass(frozen=True)
class Session:
    access_token: str
    refresh_token: str
    expires_at: int
    refresh_token_expires_at: int
    access_token_lifetime_s: int = DEFAULT_ACCESS_TOKEN_LIFETIME_S
    version: int = SUPPORTED_VERSION

    @classmethod
    def from_token_response(cls, tr: "TokenResponse", now: int) -> "Session":
        """Build a session from a FULL auth (authorization_code grant).

        The refresh token is brand-new, so its expiry resets to a full
        lifetime from now. For a refresh-grant exchange use
        :meth:`refreshed_from` instead — that grant does NOT extend the
        refresh token's life.
        """
        return cls(
            access_token=tr.access_token,
            refresh_token=tr.refresh_token,
            expires_at=now + tr.expires_in,
            refresh_token_expires_at=now + REFRESH_TOKEN_LIFETIME_SECONDS,
            access_token_lifetime_s=tr.expires_in,
        )

    @classmethod
    def refreshed_from(
        cls, old: "Session", tr: "TokenResponse", now: int,
    ) -> "Session":
        """Build a session from a refresh-grant exchange.

        Schwab returns the SAME refresh token with its ORIGINAL expiry;
        only the access token is new. Carrying ``old``'s
        ``refresh_token_expires_at`` forward keeps the persisted expiry
        truthful so renewal checkpoints fire when they should.
        """
        return cls(
            access_token=tr.access_token,
            refresh_token=tr.refresh_token,
            expires_at=now + tr.expires_in,
            refresh_token_expires_at=old.refresh_token_expires_at,
            access_token_lifetime_s=tr.expires_in,
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
    if version not in _READABLE_VERSIONS:
        raise SessionError(
            f"unsupported session version {version} in {path} "
            f"(this build supports versions {_READABLE_VERSIONS})"
        )
    for field in _REQUIRED_FIELDS:
        if field not in raw:
            raise SessionError(f"missing required field '{field}' in {path}")
    # The in-memory Session is always the current schema: v1 files (which
    # predate the persisted access-token lifetime) get the default filled in.
    return Session(
        access_token=raw["access_token"],
        refresh_token=raw["refresh_token"],
        expires_at=int(raw["expires_at"]),
        refresh_token_expires_at=int(raw["refresh_token_expires_at"]),
        access_token_lifetime_s=int(
            raw.get("access_token_lifetime_s", DEFAULT_ACCESS_TOKEN_LIFETIME_S)
        ),
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
        "version": SUPPORTED_VERSION,
        "access_token": s.access_token,
        "refresh_token": s.refresh_token,
        "expires_at": s.expires_at,
        "refresh_token_expires_at": s.refresh_token_expires_at,
        "access_token_lifetime_s": s.access_token_lifetime_s,
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
