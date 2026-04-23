from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


def config_path() -> Path:
    """Return the absolute path to config.json.

    Resolution order (first match wins):
      1. ``SCHWAB_CLI_CONFIG`` — absolute path override. Use this for
         ad-hoc shell runs or scripted setups; it bypasses every other
         lookup so there's no risk of a stray ``HOME`` tweak pointing at
         the real file.
      2. ``XDG_CONFIG_HOME/schwab_cli/config.json``.
      3. ``~/.config/schwab_cli/config.json``.
    """
    override = os.environ.get("SCHWAB_CLI_CONFIG")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "schwab_cli" / "config.json"


AUTH_FLOWS = ("client", "code_relay")


@dataclass(frozen=True)
class Config:
    client_id: str
    client_secret: str
    redirect_uri: str
    auth_flow: str = "client"
    code_relay_url: str | None = None
    username: str | None = None
    password: str | None = None
    version: int = 1

    @property
    def auto_login_enabled(self) -> bool:
        return self.username is not None and self.password is not None

    def to_payload(self) -> dict:
        """Return the on-disk JSON representation of this config.

        Omits optional fields when they are ``None`` so the written file
        doesn't carry explicit ``null`` entries. Used by :func:`save` and
        by ``setup --dry-run`` to show what would be written.
        """
        payload: dict = {
            "version": self.version,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
            "auth_flow": self.auth_flow,
        }
        if self.code_relay_url is not None:
            payload["code_relay_url"] = self.code_relay_url
        if self.username is not None:
            payload["username"] = self.username
        if self.password is not None:
            payload["password"] = self.password
        return payload


SUPPORTED_VERSION = 1
_REQUIRED_FIELDS = ("client_id", "client_secret", "redirect_uri", "auth_flow")


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
    auth_flow = raw["auth_flow"]
    if auth_flow not in AUTH_FLOWS:
        raise ConfigError(
            f"invalid auth_flow {auth_flow!r} in {path}; "
            f"expected one of: {', '.join(AUTH_FLOWS)}"
        )
    code_relay_url = raw.get("code_relay_url")
    if auth_flow == "code_relay" and not code_relay_url:
        raise ConfigError(
            f"auth_flow='code_relay' requires 'code_relay_url' in {path}"
        )
    return Config(
        client_id=raw["client_id"],
        client_secret=raw["client_secret"],
        redirect_uri=raw["redirect_uri"],
        auth_flow=auth_flow,
        code_relay_url=code_relay_url,
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
    payload = cfg.to_payload()

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
