from __future__ import annotations

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
    username: str | None = None
    password: str | None = None
    version: int = 1

    @property
    def auto_login_enabled(self) -> bool:
        return self.username is not None and self.password is not None


def mask_secret(value: str) -> str:
    """Mask all but the last 4 characters.

    Strings of length <= 4 are fully masked so we never leak a partial short secret.
    """
    if len(value) <= 4:
        return "*" * len(value)
    return "*" * (len(value) - 4) + value[-4:]
