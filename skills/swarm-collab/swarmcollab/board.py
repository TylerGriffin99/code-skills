"""The blackboard: an append-only JSONL log of typed entries at <run-dir>/board.jsonl."""

import hashlib
import json

from swarmcollab import util
from swarmcollab.util import CollabError

ENTRY_TYPES = {
    "hypothesis", "proposal", "result", "critique", "deadend", "message", "council", "digest",
    "scored", "retro",
}

#: Types only the orchestrator writes. A `scored` entry carries the locked eval's own score
#: and commit sha, so an agent posting one would be forging a score; `digest` is the compactor's.
SYSTEM_ONLY_TYPES = {"scored", "digest"}

#: Types a view shows only when the ledger vouches for them (see `is_trusted`): the run's
#: record that every role reads first. An agent that writes one straight into board.jsonl
#: (it knows the run dir) gets no ledger hash, so its line is never shown.
TRUSTED_TYPES = {"scored", "retro"}

#: Which role may post a `retro` through its drop file
RETRO_ROLE = "retro"


def _board_path(r):
    return r.dir / "board.jsonl"


_ENVELOPE = {"id": int, "round": int, "type": str, "refs": list, "to": list}


def _well_formed(entry) -> bool:
    return isinstance(entry, dict) and all(isinstance(entry.get(k), t) and not isinstance(entry.get(k), bool)
                                           for k, t in _ENVELOPE.items())


def _iter_entries(r):
    """Every well-formed entry. A line that is not one (an agent can append to the file directly)
    is skipped here so it cannot crash the orchestrator; `bad_lines` counts them for an event."""
    for line in _raw_lines(r):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if _well_formed(entry):
            yield entry


def _raw_lines(r):
    path = _board_path(r)
    if not path.is_file():
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield line


def bad_lines(r) -> int:
    """How many board lines are not well-formed entries (and so are invisible to every reader)."""
    total = sum(1 for _ in _raw_lines(r))
    return total - sum(1 for _ in _iter_entries(r))


def max_id(r) -> int:
    """The highest id on the board (not the last line's: a forged last line must not rewind ids)."""
    top = 0
    for entry in _iter_entries(r):
        if isinstance(entry.get("id"), int) and entry["id"] > top:
            top = entry["id"]
    return top


def entry_hash(entry: dict) -> str:
    return hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()


def is_trusted(r, entry: dict) -> bool:
    """True if the ledger recorded exactly this entry (same id, same content) when it was written."""
    return (r.get("trusted_entries") or {}).get(str(entry.get("id"))) == entry_hash(entry)


def append(r, *, round, role, author_agent, type, body, refs=None, to=None, trusted=False) -> dict:
    """Append one entry. `trusted=True` records its hash in the ledger BEFORE the line is
    written, so a crash between the two can never leave a real entry unvouched."""
    if type not in ENTRY_TYPES:
        raise CollabError(f"unknown board entry type: {type!r}")
    next_id = max_id(r) + 1
    entry = {
        "id": next_id,
        "round": round,
        "role": role,
        "author_agent": author_agent,
        "ts": util.now(),
        "type": type,
        "refs": list(refs) if refs else [],
        "to": list(to) if to else [],
        "body": body,
    }
    if trusted:
        hashes = dict(r.get("trusted_entries") or {})
        hashes[str(next_id)] = entry_hash(entry)
        r.set("trusted_entries", hashes)
    with open(_board_path(r), "a") as fh:
        fh.write(json.dumps(entry) + "\n")
    return entry


def read(r, *, types=None, rounds=None, refs=None, to=None, since_id=0, limit=None) -> list:
    types_set = set(types) if types is not None else None
    rounds_set = set(rounds) if rounds is not None else None
    refs_set = set(refs) if refs is not None else None
    to_set = set(to) if to is not None else None

    entries = []
    for entry in _iter_entries(r):
        if entry["id"] <= since_id:
            continue
        if types_set is not None and entry["type"] not in types_set:
            continue
        if rounds_set is not None and entry["round"] not in rounds_set:
            continue
        if refs_set is not None and not (refs_set & set(entry["refs"])):
            continue
        if to_set is not None and not (to_set & set(entry["to"])):
            continue
        entries.append(entry)

    entries.sort(key=lambda e: e["id"])
    if limit is not None:
        entries = entries[-limit:]
    return entries


def all_lines_hash(r) -> str:
    path = _board_path(r)
    if not path.is_file():
        return util.sha256_bytes(b"")
    return util.sha256_file(path)
