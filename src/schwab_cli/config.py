from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


def config_path() -> Path:
    """Return the absolute path to config.json.

    Honors XDG_CONFIG_HOME; falls back to ~/.config.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "schwab_cli" / "config.json"


@dataclass(frozen=True)
class Config:
    client_id: str
    client_secret: str
    redirect_uri: str
    username: str | None = None
    password: str | None = None
    version: int = 1

    @property
    def auto_login_enabled(self) -> bool:
        return self.username is not None and self.password is not None


SUPPORTED_VERSION = 1
_REQUIRED_FIELDS = ("client_id", "client_secret", "redirect_uri")


class ConfigError(Exception):
    """Raised when an existing config file cannot be used as-is."""


def load() -> Config | None:
    """Load config from disk.

    Returns None if the file does not exist. Raises ConfigError on malformed
    JSON, unsupported schema versions, or missing required fields.
    """
    path = config_path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ConfigError(f"malformed JSON in {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"expected object at top level of {path}")
    version = raw.get("version", 1)
    if version != SUPPORTED_VERSION:
        raise ConfigError(
            f"unsupported config version {version} in {path} "
            f"(this build supports version {SUPPORTED_VERSION})"
        )
    for field in _REQUIRED_FIELDS:
        if field not in raw:
            raise ConfigError(f"missing required field '{field}' in {path}")
    return Config(
        client_id=raw["client_id"],
        client_secret=raw["client_secret"],
        redirect_uri=raw["redirect_uri"],
        username=raw.get("username"),
        password=raw.get("password"),
        version=version,
    )


def save(cfg: Config) -> None:
    """Persist a Config to disk atomically with strict permissions.

    Writes to a temp file in the same directory, chmods it 0600, then
    atomically renames it over the target. If the rename fails, cleans up
    the temp file and leaves any prior config untouched.
    """
    path = config_path()
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    try:
        parent.chmod(0o700)
    except OSError:
        pass
    payload: dict = {
        "version": cfg.version,
        "client_id": cfg.client_id,
        "client_secret": cfg.client_secret,
        "redirect_uri": cfg.redirect_uri,
    }
    if cfg.username is not None:
        payload["username"] = cfg.username
    if cfg.password is not None:
        payload["password"] = cfg.password

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    try:
        tmp.chmod(0o600)
        os.replace(tmp, path)
    except OSError:
        # Clean up temp file so we don't leave stragglers.
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def mask_secret(value: str) -> str:
    """Mask all but the last 4 characters.

    Strings of length <= 4 are fully masked so we never leak a partial short secret.
    """
    if len(value) <= 4:
        return "*" * len(value)
    return "*" * (len(value) - 4) + value[-4:]
