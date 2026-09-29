#!/usr/bin/env python3
"""A scripted fake `claude -p`, used only by tests as a `launch_turn` substitute.

Reads a JSONL script from COLLAB_MOCK_SCRIPT (each line an intended board
entry) and copies those lines verbatim to the drop file named by COLLAB_DROP,
then prints a single JSON line to stdout mimicking `claude -p
--output-format json`'s cost/usage summary so the launcher can record it.

stdlib only: this file is run as a subprocess, never imported.
"""

import json
import os


def main():
    script_path = os.environ["COLLAB_MOCK_SCRIPT"]
    drop_path = os.environ["COLLAB_DROP"]

    with open(script_path) as fh:
        lines = [line.strip() for line in fh if line.strip()]

    with open(drop_path, "w") as out:
        for line in lines:
            out.write(line + "\n")

    result = {
        "total_cost_usd": 0.01,
        "num_turns": 1,
        "usage": {"output_tokens": max(1, len(lines) * 10)},
    }
    print(json.dumps(result))


if __name__ == "__main__":
    main()
