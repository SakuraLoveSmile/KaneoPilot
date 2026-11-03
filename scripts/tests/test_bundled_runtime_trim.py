"""What scripts/build_app.sh removes from the bundled Python runtime.

The App runs `bin/python3` as a subprocess and never links libpython itself, so the pinned
`install_only_stripped` archive carries about 20 MB the App cannot use: a libpython3.13.dylib
that nothing links, C headers, the config-3.13-darwin build directory and _pyrepl
(docs/build-release.md). Each entry was re-checked by importing the cloud smoke set and all 95
core modules with the trimmed runtime. These tests keep the list to entries that cost the App
and the core no capability: adding pip, Tcl/Tk, idlelib or pydoc_data would silently change what
the bundled interpreter can do, and that is a separate decision. No archive, no network.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[2]
BUILD_APP = ROOT / "scripts/build_app.sh"

# Paths below Contents/Resources/python whose removal costs nothing: a shared library that no
# image links, and build-time or interactive-only files. Anything outside this set changes what
# the bundled interpreter can do and needs its own decision.
SAFE_TO_TRIM = {
    "lib/libpython3.13.dylib",
    "include",
    "lib/python3.13/config-3.13-darwin",
    "lib/python3.13/_pyrepl",
}


def trim_paths():
    """The TRIM_PATHS list in scripts/build_app.sh, as relative runtime paths."""
    text = BUILD_APP.read_text(encoding="utf-8")
    match = re.search(r"^TRIM_PATHS=\(\n(.*?)^\)", text, re.S | re.M)
    if match is None:
        raise ValueError("scripts/build_app.sh has no TRIM_PATHS list")
    return re.findall(r'"([^"]+)"', match.group(1))


class BundledRuntimeTrimTests(unittest.TestCase):
    def test_the_list_is_not_empty(self):
        self.assertTrue(trim_paths(), "the bundled runtime is no longer trimmed")

    def test_the_unreferenced_libpython_is_trimmed(self):
        # The largest single win, and the one most likely to come back with a new runtime:
        # `otool -L bin/python3.13` does not reference it.
        self.assertIn("lib/libpython3.13.dylib", trim_paths())

    def test_every_entry_costs_no_capability(self):
        unexpected = sorted(set(trim_paths()) - SAFE_TO_TRIM)
        self.assertEqual(
            unexpected, [],
            "these entries change what the bundled interpreter can do: %s" % unexpected)

    def test_the_interpreter_and_the_standard_library_survive(self):
        for relative in trim_paths():
            self.assertFalse(
                relative.startswith("bin/"),
                "%s would remove the interpreter itself" % relative)
            self.assertNotEqual(relative, "lib/python3.13", "the standard library is not trimmable")
            self.assertFalse(
                relative.startswith("lib/python3.13/lib-dynload"),
                "%s would remove an extension module" % relative)


if __name__ == "__main__":
    unittest.main()
