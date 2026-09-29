"""Hash-locking for control files, target (locked) files, and blackboard integrity.

Three independent guarantees, all backed by `<run-dir>/locks.json`:

- control: the orchestrator's own inputs (manifest, verdict, role prompts, adapters,
  the `swarmcollab` package itself) must not change mid-run.
- target: files the manifest names as off-limits to agents (`locked_files`) must not
  drift from their content at `baseline_commit`.
- board_prefix: the blackboard (`board.jsonl`) is append-only; entries recorded before
  a given point must never be rewritten, though new entries may always be appended.
"""

import json
import subprocess
from pathlib import Path

from swarmcollab.util import CollabError, sha256_bytes, sha256_file

# swarmcollab/locks.py -> swarmcollab/ -> skill root (sibling of tests/, roles/)
_PACKAGE_DIR = Path(__file__).resolve().parent
SKILL_DIR = _PACKAGE_DIR.parent


def _locks_path(r):
    return r.dir / "locks.json"


def _read_locks(r) -> dict:
    path = _locks_path(r)
    if not path.is_file():
        raise CollabError(f"no locks.json in run dir (run write_locks first): {r.dir}")
    return json.loads(path.read_text())


def _write_locks(r, data: dict):
    _locks_path(r).write_text(json.dumps(data, indent=2))


def _board_path(r):
    return r.dir / "board.jsonl"


def _board_lines(r) -> list:
    """Raw lines (bytes, newline included) of board.jsonl, or [] if absent."""
    path = _board_path(r)
    if not path.is_file():
        return []
    with open(path, "rb") as fh:
        return fh.readlines()


def _board_prefix_state(lines: list, count: int) -> dict:
    prefix = lines[:count]
    return {"count": count, "sha": sha256_bytes(b"".join(prefix))}


def control_files(r) -> list:
    """Files whose content the orchestrator must not see change mid-run.

    Always: swarm.json, verdict.md, and every .py file in the swarmcollab package.
    Conditionally (only when present on disk, since neither exists until Task 12):
    role prompts under <skill-root>/roles/*.md, and every file under
    <run-dir>/adapters/ (mirrors swarm.py's optional-adapters-dir glob).
    """
    files = [r.dir / "swarm.json", r.dir / "verdict.md"]

    roles_dir = SKILL_DIR / "roles"
    if roles_dir.is_dir():
        files += sorted(roles_dir.glob("*.md"))

    adapters_dir = r.dir / "adapters"
    if adapters_dir.is_dir():
        files += sorted(
            p for p in adapters_dir.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
        )

    files += sorted(
        p for p in _PACKAGE_DIR.glob("*.py")
        if "__pycache__" not in p.parts
    )
    return files


def write_locks(r):
    """Snapshot control-file hashes, target-file hashes (at baseline_commit), and
    the current board prefix into <run-dir>/locks.json."""
    base = r.m["baseline_commit"]
    workdir = r.m.get("workdir", ".")

    target = {}
    for rel in r.m.get("locked_files", []):
        path = rel if workdir in ("", ".") else f"{workdir}/{rel}"
        # Kept on raw subprocess (not util.run) to hash the exact bytes git emits,
        # but no longer relies on check=True's bare CalledProcessError traceback:
        # a failure raises a clean CollabError the human can act on.
        proc = subprocess.run(
            ["git", "-C", str(r.repo), "show", f"{base}:{path}"],
            capture_output=True,
        )
        if proc.returncode != 0:
            raise CollabError(
                f"git show {base}:{path} failed ({proc.returncode}): "
                f"{proc.stderr.decode(errors='replace').strip()[-300:]}"
            )
        target[rel] = sha256_bytes(proc.stdout)

    control = {str(p): sha256_file(p) for p in control_files(r)}

    lines = _board_lines(r)
    board_prefix = _board_prefix_state(lines, len(lines))

    _write_locks(r, {"target": target, "control": control, "board_prefix": board_prefix})


def check_control_locks(r) -> list:
    """Return the control paths (as strings) whose content no longer matches the lock."""
    locks = _read_locks(r)
    bad = []
    for path, digest in locks["control"].items():
        p = Path(path)
        if not p.is_file() or sha256_file(p) != digest:
            bad.append(path)
    return bad


def check_target_locks(r, worktree) -> list:
    """Return the locked_files entries (relative paths) that changed in `worktree`."""
    locks = _read_locks(r)
    workdir = r.m.get("workdir", ".")
    worktree = Path(worktree)
    wd = worktree if workdir in ("", ".") else worktree / workdir
    bad = []
    for rel, digest in locks["target"].items():
        p = wd / rel
        if not p.is_file() or sha256_file(p) != digest:
            bad.append(rel)
    return bad


def check_board_append_only(r):
    """Return a reason string if the previously-locked board prefix was rewritten
    (either a recorded line changed, or the file now has fewer lines), else None."""
    locks = _read_locks(r)
    prefix = locks.get("board_prefix", {"count": 0, "sha": sha256_bytes(b"")})
    count = prefix["count"]
    if count == 0:
        return None

    lines = _board_lines(r)
    if len(lines) < count:
        return (
            f"board.jsonl has {len(lines)} line(s), fewer than the "
            f"{count} locked at last check: history was rewritten"
        )

    current = _board_prefix_state(lines, count)
    if current["sha"] != prefix["sha"]:
        return f"board.jsonl's first {count} line(s) no longer match their locked hash: history was rewritten"

    return None


def advance_board_prefix(r):
    """Record the full current board (count + hash) as the new locked prefix."""
    locks = _read_locks(r)
    lines = _board_lines(r)
    locks["board_prefix"] = _board_prefix_state(lines, len(lines))
    _write_locks(r, locks)
