"""The license texts that ship inside Contents/Resources/licenses/python/.

The Mac App bundles a Python runtime that statically links third-party libraries. The
`install_only_stripped` archive the build downloads only carries the PSF text, so the rest
are checked in under scripts/licenses/python/ and copied into the bundle by
scripts/build_app.sh (docs/build-release.md). These tests guard that directory: an empty
or partially deleted folder would silently ship a release without the required notices.
No network, no archive.
"""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[2]
FOLDER = ROOT / "scripts/licenses/python"
LOCK_PATH = ROOT / "scripts/python-runtime.lock.json"

SPEC = importlib.util.spec_from_file_location("fetch_python_runtime", ROOT / "scripts/fetch_python_runtime.py")
fetch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fetch)
LOCK = fetch.load_lock(LOCK_PATH)

# Every library the runtime links, taken from the release's PYTHON.json
# (`python_config_vars.MODLIBS`) plus `tcl_library_paths`. Left side is the name used in a
# failure message, right side is what the license file name has to contain. readline is
# built against libedit on macOS, so either spelling counts.
LINKED_LIBRARIES = {
    "cpython": ("cpython",),
    "openssl": ("openssl",),
    "sqlite": ("sqlite",),
    "libffi": ("ffi",),
    "expat": ("expat",),
    "mpdecimal": ("mpdec",),
    "bzip2": ("bzip2",),
    "liblzma": ("lzma",),
    "ncurses": ("ncurses",),
    "libuuid": ("uuid",),
    "tcl/tk": ("tcl",),
    "readline": ("readline", "libedit"),
}


class BundledPythonLicenseTests(unittest.TestCase):
    def test_the_folder_exists_and_holds_texts(self):
        self.assertTrue(FOLDER.is_dir(), "%s is missing" % FOLDER)
        files = sorted(path for path in FOLDER.iterdir() if path.is_file())
        self.assertTrue(files, "%s holds no license text" % FOLDER)
        for path in files:
            self.assertGreater(path.stat().st_size, 0, "%s is empty" % path.name)

    def test_every_linked_library_has_a_text(self):
        names = [path.name.lower() for path in FOLDER.iterdir() if path.is_file()]
        missing = sorted(
            "%s (no file matching %s)" % (library, " or ".join(repr(n) for n in needles))
            for library, needles in LINKED_LIBRARIES.items()
            if not any(needle in name for name in names for needle in needles)
        )
        self.assertEqual(missing, [], "the runtime links these, the bundle would not carry them: %s" % missing)

    def test_the_texts_are_license_texts(self):
        # A placeholder or a truncated file would pass the size check above; a real notice
        # always names a licence or a copyright holder.
        markers = ("license", "copyright", "redistribution", "public domain", "permission")
        for path in sorted(FOLDER.iterdir()):
            if not path.is_file():
                continue
            head = path.read_text(encoding="utf-8", errors="replace")[:4000].lower()
            self.assertTrue(
                any(marker in head for marker in markers),
                "%s does not look like a license text" % path.name)

    def test_the_lock_file_still_points_at_the_release_the_texts_came_from(self):
        # The texts were taken from python-build-standalone 20261026's `python/licenses/`.
        # Bumping the runtime version without re-taking them ships stale notices, so the
        # release is recorded next to the lock file and both have to move together.
        recorded = LOCK.get("licenses_release")
        self.assertIsNotNone(recorded, "the lock file does not record which release the license texts came from")
        self.assertEqual(recorded, LOCK["release_tag"])


if __name__ == "__main__":
    unittest.main()
