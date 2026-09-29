#!/usr/bin/env python3
"""Thin entry shim for /swarm-collab: see swarmcollab/cli.py for the real CLI."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from swarmcollab.cli import main  # noqa: E402

raise SystemExit(main())
