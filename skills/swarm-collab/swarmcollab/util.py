"""Small stdlib-only helpers shared across swarm-collab."""

import datetime as dt
import hashlib
import re
import subprocess
from pathlib import Path


class CollabError(Exception):
    """A condition the human must see; never swallowed."""


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def run(cmd, cwd=None, env=None, check=True, timeout=None) -> subprocess.CompletedProcess:
    shell = isinstance(cmd, str)
    proc = subprocess.run(
        f"set -o pipefail; {cmd}" if shell else cmd, cwd=cwd, env=env, capture_output=True, text=True,
        timeout=timeout, shell=shell, executable="/bin/bash" if shell else None,
    )
    if check and proc.returncode != 0:
        shown = cmd if isinstance(cmd, str) else " ".join(map(str, cmd))
        raise CollabError(f"command failed ({proc.returncode}): {shown}\n{proc.stderr.strip()}")
    return proc


def git(repo, *args, check=True) -> str:
    return run(["git", "-C", str(repo), *args], check=check).stdout.strip()


def render(template: str, values: dict) -> str:
    return re.sub(r"\{([A-Za-z0-9_]+)\}",
                  lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0),
                  template)
