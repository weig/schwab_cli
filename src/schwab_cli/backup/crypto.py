"""Client-side encryption via openssl (ships with macOS; zero python deps).

AES-256-CBC with PBKDF2 (200k iterations). The passphrase lives only in
``backup_passphrase`` (0600) on this machine + the user's 1Password entry —
it is deliberately excluded from every backup archive.
"""
from __future__ import annotations

import secrets
import subprocess
from pathlib import Path

_ARGS = ["-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt"]


def ensure_passphrase(path: Path) -> bool:
    """Create the passphrase file if missing. Returns True when newly created
    (caller should remind the user to store it in 1Password)."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600)
    path.write_text(secrets.token_urlsafe(48) + "\n")
    path.chmod(0o600)
    return True


def _run(direction: list[str], src: Path, dst: Path, passfile: Path) -> None:
    r = subprocess.run(
        ["openssl", "enc", *direction, *_ARGS,
         "-in", str(src), "-out", str(dst), "-pass", f"file:{passfile}"],
        capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"openssl: {r.stderr.strip()[:200]}")


def encrypt(src: Path, dst: Path, passfile: Path) -> Path:
    _run(["-e"], src, dst, passfile)
    return dst


def decrypt(src: Path, dst: Path, passfile: Path) -> Path:
    _run(["-d"], src, dst, passfile)
    return dst
