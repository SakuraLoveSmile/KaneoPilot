#!/usr/bin/env python3
"""Mach-O checks for a KaneoPilot.app: Apple Silicon only, macOS 14 or older, relocatable libraries.

`audit` only reads, so it also works on a package that was downloaded to a Mac. It uses the Xcode
command-line tools `lipo` and `otool`; the parsing is unit-tested on real `otool -l` output.
Standard library only.
"""
import argparse
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import sys

DEFAULT_LOCK = Path(__file__).resolve().with_name("python-runtime.lock.json")
# Mach-O file types (mach-o/loader.h) that are executed or loaded and therefore need a signature.
FILE_TYPES = {1: "object", 2: "execute", 6: "dylib", 8: "bundle"}
SIGNED_KINDS = ("dylib", "bundle", "execute")
DEPENDENCY_COMMANDS = {"LC_LOAD_DYLIB", "LC_LOAD_WEAK_DYLIB", "LC_REEXPORT_DYLIB",
                       "LC_LAZY_LOAD_DYLIB", "LC_LOAD_UPWARD_DYLIB"}
# What a relocatable app may link: its own libraries and the operating system.
ALLOWED_DEPENDENCIES = ("@rpath/", "@executable_path/", "@loader_path/", "/usr/lib/", "/System/Library/")
OFFSET_VALUE = re.compile(r"^(?:name|path) (.*) \(offset [0-9]+\)$")


class ToolError(Exception):
    pass


def run_tool(argv, timeout=120):
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ToolError("%s could not run: %s" % (argv[0], error)) from error
    if result.returncode:
        raise ToolError("%s failed: %s" % (" ".join(argv[:2]), result.stderr.strip()[:400]))
    return result.stdout


def macho_kind(path):
    """The kind of a regular file that is a Mach-O image, else None. A fat file is "fat"."""
    try:
        with open(path, "rb") as stream:
            header = stream.read(16)
    except OSError:
        return None
    if len(header) < 16:
        return None
    if header[:4] in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe"):
        return FILE_TYPES.get(struct.unpack("<I", header[12:16])[0], "other")
    if header[:4] in (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"):
        # A Java class file starts the same way; its next field is a version number, a fat file's an architecture count.
        return "fat" if 1 <= struct.unpack(">I", header[4:8])[0] <= 20 else None
    return None


def scan(root):
    """(path, kind) of every Mach-O regular file below `root`, in a stable order. Symbolic links are skipped."""
    found = []
    for directory, _, names in os.walk(root, followlinks=False):
        for name in names:
            path = os.path.join(directory, name)
            if not stat.S_ISREG(os.lstat(path).st_mode):
                continue
            kind = macho_kind(path)
            if kind:
                found.append((path, kind))
    return sorted(found)


def parse_load_commands(text):
    """Deployment targets, install name, dependencies and rpaths from `otool -l` output (all slices)."""
    result = {"minos": [], "id": None, "dependencies": [], "rpaths": []}
    command = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("cmd "):
            command = line.split(None, 1)[1]
        elif command == "LC_BUILD_VERSION" and line.startswith("minos "):
            result["minos"].append(line.split()[1])
        elif command == "LC_VERSION_MIN_MACOSX" and line.startswith("version "):
            result["minos"].append(line.split()[1])
        elif command in DEPENDENCY_COMMANDS and line.startswith("name "):
            match = OFFSET_VALUE.match(line)
            if match:
                result["dependencies"].append(match.group(1))
        elif command == "LC_ID_DYLIB" and line.startswith("name "):
            match = OFFSET_VALUE.match(line)
            result["id"] = match.group(1) if match else None
        elif command == "LC_RPATH" and line.startswith("path "):
            match = OFFSET_VALUE.match(line)
            if match:
                result["rpaths"].append(match.group(1))
    return result


def version_tuple(value):
    return tuple(int(part) for part in value.split("."))


def audit_file(path, kind, minos_max, runner=run_tool):
    """The reasons this image must not ship in the app; an empty list means it is fine."""
    problems = []
    try:
        archs = runner(["lipo", "-archs", path]).split()
        info = parse_load_commands(runner(["otool", "-l", path]))
    except ToolError as error:
        return [str(error)]
    if archs != ["arm64"]:
        problems.append("architectures %s, want arm64 only" % (" ".join(archs) or "none"))
    # Object files (static-link inputs such as config-*/python.o) are never loaded: they must be arm64, but a
    # missing deployment target is not a defect there. Every image that is loaded needs one.
    if not info["minos"] and kind != "object":
        problems.append("no macOS deployment target")
    for target in info["minos"]:
        if version_tuple(target) > version_tuple(minos_max):
            problems.append("deployment target %s is above %s" % (target, minos_max))
    for dependency in info["dependencies"]:
        if not dependency.startswith(ALLOWED_DEPENDENCIES):
            problems.append("links %s, which is not inside the app or the system" % dependency)
    return problems


def audit(root, minos_max, runner=run_tool):
    images = scan(root)
    violations = []
    kinds = {}
    for path, kind in images:
        kinds[kind] = kinds.get(kind, 0) + 1
        for problem in audit_file(path, kind, minos_max, runner):
            violations.append({"path": os.path.relpath(path, root), "problem": problem})
    return {"images": len(images), "kinds": kinds, "minos_max": minos_max, "violations": violations}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    auditing = actions.add_parser("audit", help="check every Mach-O image below a folder or app (read-only)")
    auditing.add_argument("root")
    auditing.add_argument("--minos-max", help="highest allowed deployment target; default: the runtime lock file")
    args = parser.parse_args(argv)
    root = Path(args.root)
    if not root.is_dir():
        print("ERROR: not a folder: %s" % root, file=sys.stderr)
        return 1
    minos_max = args.minos_max or json.loads(DEFAULT_LOCK.read_text(encoding="utf-8"))["minos_max"]
    report = audit(str(root), minos_max)
    print(json.dumps({key: value for key, value in report.items() if key != "violations"}, sort_keys=True))
    for item in report["violations"]:
        print("VIOLATION %s: %s" % (item["path"], item["problem"]), file=sys.stderr)
    if report["violations"]:
        print("ERROR: %d Mach-O problem(s) in %d image(s)" % (len(report["violations"]), report["images"]), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
