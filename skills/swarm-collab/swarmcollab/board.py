"""The blackboard: an append-only JSONL log of typed entries at <run-dir>/board.jsonl."""

import json

from swarmcollab import util
from swarmcollab.util import CollabError

ENTRY_TYPES = {
    "hypothesis", "proposal", "result", "critique", "deadend", "message", "council", "digest",
}


def _board_path(r):
    return r.dir / "board.jsonl"


def _iter_entries(r):
    path = _board_path(r)
    if not path.is_file():
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def max_id(r) -> int:
    last_id = 0
    for entry in _iter_entries(r):
        last_id = entry["id"]
    return last_id


def append(r, *, round, role, author_agent, type, body, refs=None, to=None) -> dict:
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
