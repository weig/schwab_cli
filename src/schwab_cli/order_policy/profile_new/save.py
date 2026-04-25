"""Atomic profile-file save."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class ProfileExistsError(FileExistsError):
    """Raised when the target profile filename already exists.

    `profile new` strictly creates — no overwrite, no prompt to
    confirm. Users who want to replace a profile delete the file
    first.
    """


def atomic_save(
    *, profile_name: str, profile_data: dict[str, Any], base_dir: Path,
) -> Path:
    """Write ``profile_data`` to ``base_dir / f"{profile_name}.json"``
    atomically (temp + ``os.replace``) with ``0600`` perms.

    Raises :class:`ProfileExistsError` if the target already exists.
    The temp-file approach guarantees we never leave a partially-
    written file at the target path.
    """
    base_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(base_dir, 0o700)
    except OSError:
        pass

    target = base_dir / f"{profile_name}.json"
    if target.exists():
        raise ProfileExistsError(str(target))

    tmp = target.with_suffix(target.suffix + ".tmp")
    payload = json.dumps(profile_data, indent=2, sort_keys=True) + "\n"
    fd = os.open(
        str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600,
    )
    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, target)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return target
