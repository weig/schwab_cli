"""Remote storage backends: local dir (tests) and R2 via aws cli (prod)."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


class LocalDirRemote:
    """Filesystem-backed remote for tests and dry runs."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _p(self, key: str) -> Path:
        return self.root / key

    def put(self, local: Path, key: str) -> None:
        dst = self._p(key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local, dst)

    def get(self, key: str, local: Path) -> None:
        shutil.copyfile(self._p(key), local)

    def list(self, prefix: str) -> list[str]:
        base = self.root
        if not base.exists():
            return []
        return sorted(
            str(p.relative_to(base))
            for p in base.rglob("*")
            if p.is_file() and str(p.relative_to(base)).startswith(prefix)
        )

    def delete(self, key: str) -> None:
        p = self._p(key)
        if p.exists():
            p.unlink()


def _resolve_aws(override: str | None = None) -> str:
    """Absolute path to the aws CLI.

    The daemon runs under launchd with a minimal PATH that excludes Homebrew,
    so a bare ``"aws"`` raises FileNotFoundError there even though it resolves
    in an interactive shell. Resolve an absolute path up front: an explicit
    R2_AWS_BIN wins, else PATH, else the common install locations.
    """
    if override:
        return override
    found = shutil.which("aws")
    if found:
        return found
    for cand in ("/opt/homebrew/bin/aws", "/usr/local/bin/aws",
                 "/usr/bin/aws"):
        if Path(cand).exists():
            return cand
    return "aws"  # last resort — will raise a clear FileNotFoundError


class R2Remote:
    """Cloudflare R2 through the aws CLI (S3-compatible endpoint).

    Credentials/endpoint/bucket come from backup_r2.env (0600). The token is
    scoped to object read/write on the single backup bucket — ListBuckets is
    expected to be denied; we only ever operate inside the bucket.
    """

    def __init__(self, env_file: Path) -> None:
        self.env = dict(os.environ)
        for line in Path(env_file).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                self.env[k] = v
        self.bucket = self.env["R2_BUCKET"]
        self.endpoint = self.env["R2_ENDPOINT"]
        self.aws = _resolve_aws(self.env.get("R2_AWS_BIN"))

    def _aws(self, *args: str) -> subprocess.CompletedProcess:
        cmd = [self.aws, *args, "--endpoint-url", self.endpoint]
        r = subprocess.run(cmd, env=self.env, capture_output=True, text=True,
                           timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"aws {' '.join(args[:3])}: {r.stderr.strip()[:300]}")
        return r

    def put(self, local: Path, key: str) -> None:
        self._aws("s3", "cp", str(local), f"s3://{self.bucket}/{key}")

    def get(self, key: str, local: Path) -> None:
        self._aws("s3", "cp", f"s3://{self.bucket}/{key}", str(local))

    def list(self, prefix: str) -> list[str]:
        r = self._aws("s3api", "list-objects-v2", "--bucket", self.bucket,
                      "--prefix", prefix, "--query", "Contents[].Key",
                      "--output", "text")
        out = r.stdout.strip()
        if not out or out == "None":
            return []
        return sorted(out.split())

    def delete(self, key: str) -> None:
        self._aws("s3", "rm", f"s3://{self.bucket}/{key}")
