#!/usr/bin/env python3
"""Print one balanced shard of a suite's pytest files for a CI matrix job.

Usage: python3 scripts/pytest_shards.py {core,integration} --shard N --shards M

Files come from pytest's default patterns (test_*.py, *_test.py) under the
suite's tests/ directory. Every file lands in exactly one shard: files are
assigned longest-first to the currently lightest shard, weighted by the seconds
recorded in pytest_shard_durations.json (taken from the junit reports of
Packages run 40363126875). Files missing from the table weigh the suite's mean.
Printed paths are relative to the directory pytest runs from.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parent.parent
DURATIONS = Path(__file__).resolve().parent / "pytest_shard_durations.json"
# Repository-relative directory that each suite's pytest run starts from.
SUITES = {"core": "core", "integration": "."}
TEST_PATTERNS = ("test_*.py", "*_test.py")


def discover(base: Path) -> list[str]:
    found = []
    for dirpath, dirnames, filenames in os.walk(base / "tests"):
        dirnames[:] = sorted(name for name in dirnames if name != "__pycache__" and not name.startswith("."))
        for name in filenames:
            if any(fnmatch.fnmatch(name, pattern) for pattern in TEST_PATTERNS):
                found.append(Path(dirpath, name).relative_to(base).as_posix())
    return sorted(found)


def plan(files: list[str], seconds: dict[str, float], shards: int) -> tuple[list[list[str]], list[float]]:
    mean = sum(seconds.values()) / len(seconds) if seconds else 1.0
    weight = {name: seconds.get(name, mean) for name in files}
    buckets: list[list[str]] = [[] for _ in range(shards)]
    loads = [0.0] * shards
    for name in sorted(files, key=lambda name: (-weight[name], name)):
        lightest = min(range(shards), key=lambda index: (loads[index], index))
        buckets[lightest].append(name)
        loads[lightest] += weight[name]
    return [sorted(bucket) for bucket in buckets], loads


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=sorted(SUITES))
    parser.add_argument("--shard", type=int, required=True, help="1-based shard number")
    parser.add_argument("--shards", type=int, required=True, help="total shard count")
    args = parser.parse_args(argv)
    if not 1 <= args.shard <= args.shards:
        parser.error("--shard must be between 1 and --shards")
    seconds = json.loads(DURATIONS.read_text(encoding="utf-8"))[args.suite]
    buckets, loads = plan(discover(REPO / SUITES[args.suite]), seconds, args.shards)
    selected = buckets[args.shard - 1]
    if not selected:
        print(f"{args.suite}: shard {args.shard}/{args.shards} has no files", file=sys.stderr)
        return 1
    print(f"{args.suite}: shard {args.shard}/{args.shards} runs {len(selected)} files, planned {loads[args.shard - 1]:.0f}s", file=sys.stderr)
    print("\n".join(selected))
    return 0


if __name__ == "__main__":
    sys.exit(main())
