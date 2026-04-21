from __future__ import annotations

from dataclasses import dataclass


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
