#!/usr/bin/env python3
"""Make the unpacked Python runtime inside KaneoPilot.app ready to ship (GitHub Actions only).

Run by scripts/build_app.sh after the runtime was copied into Contents/Resources/python and before the
app itself is signed: it (1) rewrites the standard library's bytecode so that it stays valid wherever the
app is copied, (2) signs every Mach-O image of the runtime from the inside out, and (3) prints where the
bytes are, so that the next release can decide what to prune. Standard library only.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
# Not needed at run time and never imported by the App; their bytecode is skipped, not their files.
SKIPPED_BYTECODE = r"/(test|tests|idlelib|turtledemo|tkinter)/"
# License and notice texts that come with the runtime; a public release has to carry them (docs/build-release.md).
NOTICE_NAME = re.compile(r"^(licen[cs]es?|copying|notice|copyright)([._-].*)?$", re.IGNORECASE)
NOTICE_DEPTH = 3


class PrepareError(Exception):
    pass


def run_tool(argv, timeout=600):
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, timeout=timeout, check=False)
    if result.returncode:
        raise PrepareError("%s failed (exit %d):\n%s" % (" ".join(argv[:3]), result.returncode, result.stdout[-2000:]))
    return result.stdout


def library_folder(runtime):
    """`lib/pythonX.Y` of the runtime; there must be exactly one."""
    folders = sorted(path for path in (runtime / "lib").glob("python3.*") if path.is_dir() and not path.is_symlink())
    if len(folders) != 1:
        raise PrepareError("expected exactly one lib/python3.* folder, found %d" % len(folders))
    return folders[0]


def compile_bytecode(runtime, runner=run_tool):
    """Every .pyc is rewritten as an unchecked-hash file: valid whatever the modification times become.

    The App runs with PYTHONDONTWRITEBYTECODE, so a timestamp-based file that goes stale (a copy, a DMG or an
    update changes modification times) would be compiled again by every process and never saved.
    """
    interpreter = str(runtime / "bin" / "python3")
    runner([interpreter, "-I", "-m", "compileall", "-q", "-f", "--invalidation-mode", "unchecked-hash",
            "-x", SKIPPED_BYTECODE, str(library_folder(runtime))])


def sign_machos(runtime, runner=run_tool):
    """Ad-hoc sign every Mach-O image: libraries and extension modules first, executables last."""
    import app_macho  # the helper that lives next to this script

    images = app_macho.scan(str(runtime))
    unexpected = [path for path, kind in images if kind == "fat"]
    if unexpected:
        raise PrepareError("fat Mach-O files in the runtime: %s" % ", ".join(unexpected))
    order = {kind: index for index, kind in enumerate(app_macho.SIGNED_KINDS)}
    signed = sorted(((order[kind], path) for path, kind in images if kind in order))
    for _, path in signed:
        runner(["codesign", "--force", "--sign", "-", path])
    for _, path in signed:
        runner(["codesign", "--verify", "--strict", path])
    return [path for _, path in signed]


def size_report(runtime, top=12):
    """Bytes below each top-level folder and the largest entries of the standard library."""
    def total(path):
        if path.is_symlink():
            return 0
        if path.is_file():
            return path.stat().st_size
        return sum(total(child) for child in path.iterdir())

    lines = ["runtime total: %.1f MB" % (total(runtime) / 1e6)]
    for child in sorted(runtime.iterdir()):
        lines.append("  %-28s %8.1f MB" % (child.name + "/", total(child) / 1e6))
    sizes = sorted(((total(child), child.name) for child in library_folder(runtime).iterdir()), reverse=True)
    lines.append("largest in %s:" % library_folder(runtime).name)
    lines.extend("  %-28s %8.1f MB" % (name, size / 1e6) for size, name in sizes[:top])
    return "\n".join(lines)


def notice_report(runtime, depth=NOTICE_DEPTH):
    """Where the runtime's license and notice texts are: every one within `depth` folders, and a count of the rest."""
    shallow, deeper = [], 0
    for directory, folders, names in os.walk(runtime):
        for name in folders + names:
            if not NOTICE_NAME.match(name):
                continue
            relative = os.path.relpath(os.path.join(directory, name), runtime)
            if relative.count(os.sep) < depth:
                shallow.append(relative)
            else:
                deeper += 1
    lines = ["license and notice texts within %d levels: %d" % (depth, len(shallow))]
    lines.extend("  " + path for path in sorted(shallow))
    lines.append("  ... and %d deeper (bundled packages, tests)" % deeper)
    return "\n".join(lines)


def main(argv=None):
    # The Actions gate comes first: signing a bundle is a build step.
    if subprocess.call(["sh", str(ROOT / "scripts/require_actions.sh")]):
        return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime", help="Contents/Resources/python of the app being built")
    args = parser.parse_args(argv)
    runtime = Path(args.runtime)
    try:
        if not (runtime / "bin" / "python3").exists():
            raise PrepareError("%s has no bin/python3" % runtime)
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        compile_bytecode(runtime)
        signed = sign_machos(runtime)
        print("signed %d Mach-O images of the runtime" % len(signed))
        print(size_report(runtime))
        print(notice_report(runtime))
    except PrepareError as error:
        print("ERROR: %s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
