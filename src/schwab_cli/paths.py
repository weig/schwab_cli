"""Central path resolver for schwab_cli's per-user config directory.

Precedence (first match wins):
  1. ``SCHWAB_CLI_CONFIG_DIR`` — direct path to the schwab_cli config dir
     (no ``schwab_cli`` suffix appended). Use this for testing in
     isolation: ``SCHWAB_CLI_CONFIG_DIR=./test-config schwab_cli auth``.
  2. ``XDG_CONFIG_HOME/schwab_cli`` — XDG convention.
  3. ``~/.config/schwab_cli`` — fallback default.

TODO (follow-up migration): other consumers of ``~/.config/schwab_cli/``
should funnel through this resolver too, so a single env var moves them
all together. Today this PR migrates only ``config.py`` and
``session.py``. Remaining hardcoded consumers:

  - ``src/schwab_cli/audit.py``                    (audit log + HMAC key)
  - ``src/schwab_cli/notify/config.py``            (notification.json)
  - ``src/schwab_cli/order_policy/counters.py``    (order_counters.json)
  - ``src/schwab_cli/order_policy/loader.py``      (profiles/order/)
  - ``src/schwab_cli/commands/mcp.py``             (mcp.log default)
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_CONFIG_DIR = "SCHWAB_CLI_CONFIG_DIR"


def config_dir() -> Path:
    """Return the absolute path to the schwab_cli per-user config dir.

    See module docstring for resolution precedence.
    """
    override = os.environ.get(ENV_CONFIG_DIR)
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "schwab_cli"
