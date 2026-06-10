from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from schwab_cli.paths import config_dir


def config_path() -> Path:
    """Return the absolute path to config.json.

    Resolution order (first match wins):
      1. ``SCHWAB_CLI_CONFIG`` — absolute path override (file-level).
         Use this for ad-hoc shell runs or scripted setups when you only
         want to swap out the config file without moving the session.
      2. ``SCHWAB_CLI_CONFIG_DIR/config.json`` — directory-level override
         (also moves ``session.json``); see ``schwab_cli.paths``.
      3. ``XDG_CONFIG_HOME/schwab_cli/config.json``.
      4. ``~/.config/schwab_cli/config.json``.
    """
    override = os.environ.get("SCHWAB_CLI_CONFIG")
    if override:
        return Path(override)
    return config_dir() / "config.json"


AUTH_FLOWS = ("local_server",)
"""
Allowed values for ``Config.auth_flow``.

- ``local_server``: schwab_cli binds a loopback HTTPS callback server on
  ``127.0.0.1`` and the IdP redirects the browser straight back to it.
  This is the only supported flow.
"""

_LEGACY_FLOWS = ("code_relay", "client")
"""
Retired ``auth_flow`` values that :func:`load` still tolerates so existing
on-disk configs keep loading (non-auth commands stay usable). Auth itself
defers a hard failure for these in :func:`schwab_cli.auth_flows.get_auth_response`.
"""


@dataclass(frozen=True)
class Config:
    client_id: str
    client_secret: str
    redirect_uri: str
    auth_flow: str = "local_server"
    auto_login_command: tuple[str, ...] | None = None
    auto_login_timeout_seconds: int = 300
    # webauth peer allowlist (nginx-style `allow`, implicit deny): which
    # DIRECT peer addresses may reach /api/* — i.e. which reverse proxy
    # may front the resource server. Loopback is always implied.
    web_allow: tuple[str, ...] = ("127.0.0.1", "::1")
    version: int = 1

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
        if self.auto_login_command is not None:
            payload["auto_login_command"] = list(self.auto_login_command)
            payload["auto_login_timeout_seconds"] = self.auto_login_timeout_seconds
        if self.web_allow != ("127.0.0.1", "::1"):
            payload["web"] = {"allow": list(self.web_allow)}
        return payload


SUPPORTED_VERSION = 1
_REQUIRED_FIELDS = ("client_id", "client_secret", "redirect_uri", "auth_flow")


class ConfigError(Exception):
    """Raised when an existing config file cannot be used as-is."""


def load() -> Config | None:
    """Load config from disk.

    Returns None if the file does not exist. Raises ConfigError on malformed
    JSON, unsupported schema versions, or missing/invalid required fields.

    Unknown fields (e.g. legacy ``username`` / ``password`` from before the
    auth refactor, or a legacy ``code_relay_url`` key) are silently ignored.

    Legacy ``auth_flow`` values (``code_relay`` / ``client``) are tolerated
    here so existing configs keep loading and non-auth commands stay usable;
    auth itself rejects them with an actionable message in
    ``auth_flows.get_auth_response``.
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
    for required in _REQUIRED_FIELDS:
        if required not in raw:
            raise ConfigError(f"missing required field '{required}' in {path}")
    auth_flow = raw["auth_flow"]
    if auth_flow not in AUTH_FLOWS + _LEGACY_FLOWS:
        raise ConfigError(
            f"invalid auth_flow {auth_flow!r} in {path}; "
            f"expected {', '.join(AUTH_FLOWS)} "
            f"(legacy {', '.join(_LEGACY_FLOWS)} are tolerated but no longer "
            f"usable — re-run `schwab setup`)"
        )
    auto_login_command = _parse_auto_login_command(raw.get("auto_login_command"), path)
    auto_login_timeout_seconds = _parse_timeout(
        raw.get("auto_login_timeout_seconds"), path,
    )
    return Config(
        client_id=raw["client_id"],
        client_secret=raw["client_secret"],
        redirect_uri=raw["redirect_uri"],
        auth_flow=auth_flow,
        auto_login_command=auto_login_command,
        auto_login_timeout_seconds=auto_login_timeout_seconds,
        web_allow=_parse_web_allow(raw.get("web"), path),
        version=version,
    )


def _parse_web_allow(raw: object, path: Path) -> tuple[str, ...]:
    """Validate the optional ``web.allow`` peer allowlist.

    Accepted shapes:
      * absent / null → loopback-only default
      * {"allow": ["ip-or-cidr", ...]} with non-empty strings

    Entries must parse as IP addresses or CIDR networks (nginx ``allow``
    semantics) — a typo'd hostname would otherwise never match any peer
    and silently lock the proxy out. Anything else is a ``ConfigError``.
    """
    default = ("127.0.0.1", "::1")
    if raw is None:
        return default
    if not isinstance(raw, dict):
        raise ConfigError(f"web in {path} must be an object")
    allow = raw.get("allow", list(default))
    if (
        not isinstance(allow, list)
        or not all(isinstance(a, str) and a for a in allow)
    ):
        raise ConfigError(
            f"web.allow in {path} must be a list of non-empty strings"
        )
    import ipaddress

    for entry in allow:
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError as e:
            raise ConfigError(
                f"web.allow entry {entry!r} in {path} is not an IP "
                f"address or CIDR network"
            ) from e
    return tuple(allow)


def _parse_auto_login_command(
    raw: object, path: Path,
) -> tuple[str, ...] | None:
    """Validate the optional ``auto_login_command`` field.

    Accepted shapes:
      * absent / null → None
      * list of strings (non-empty) → tuple of strings (frozen)

    Anything else is a ``ConfigError``.
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ConfigError(
            f"auto_login_command in {path} must be a list of strings; "
            f"got {type(raw).__name__}"
        )
    if not raw:
        raise ConfigError(
            f"auto_login_command in {path} cannot be empty (use null/omit "
            f"to disable auto-login)"
        )
    for i, token in enumerate(raw):
        if not isinstance(token, str):
            raise ConfigError(
                f"auto_login_command[{i}] in {path} must be a string; "
                f"got {type(token).__name__}"
            )
    return tuple(raw)


def _parse_timeout(raw: object, path: Path) -> int:
    """Validate the optional ``auto_login_timeout_seconds`` field.

    Default 300 when absent. Must be a positive int when present.
    """
    if raw is None:
        return 300
    if isinstance(raw, bool) or not isinstance(raw, int):
        # Reject bool too — bool is a subclass of int but `True == 1` is
        # almost certainly a user error in a config file.
        raise ConfigError(
            f"auto_login_timeout_seconds in {path} must be an integer; "
            f"got {type(raw).__name__}"
        )
    if raw <= 0:
        raise ConfigError(
            f"auto_login_timeout_seconds in {path} must be positive; got {raw}"
        )
    return raw


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
