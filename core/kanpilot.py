#!/usr/bin/env python3
"""KaneoPilot core CLI: `kanpilot.py <command> [--json '<payload>'] [--db <path>]`."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kanpilot.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
