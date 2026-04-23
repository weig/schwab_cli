"""Local storage helpers.

Provides :func:`storage_dir` — the directory where persistent, non-config
artefacts live (currently only the vol-history SQLite DB). Follows the
same override/override-chain pattern we use for ``config.json``:

  1. ``SCHWAB_CLI_STORAGE`` env var — absolute path override for scripting
     and ad-hoc runs.
  2. Otherwise, a ``storage/`` directory sibling to ``config.json``.

The directory is created on demand by callers (see
:mod:`schwab_cli.storage.vol_history`).
"""

from __future__ import annotations

import os
from pathlib import Path


def storage_dir() -> Path:
    """Return the directory for persistent local storage."""
    override = os.environ.get("SCHWAB_CLI_STORAGE")
    if override:
        return Path(override)
    from schwab_cli.config import config_path
    return config_path().parent / "storage"
