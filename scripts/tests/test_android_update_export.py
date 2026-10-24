"""Exercise the actual acceptance metadata exporter without compiling Android code."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class AndroidUpdateExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="kp-update-export-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "output"
        self.output.mkdir()
        self.sdk = self.root / "sdk" / "build-tools" / "36.0.0"
        self.sdk.mkdir(parents=True)
        self.environment = dict(os.environ, ANDROID_HOME=str(self.root / "sdk"),
                                BUILD_NUMBER="25", SOURCE_SHA="a" * 40,
                                KANPILOT_ANDROID_UPDATE_OUTPUT=str(self.output))
        # Only aapt/apksigner are replaced. The exporter itself is read from source and executed.
        stub = ("#!/usr/bin/env python3\n"
                "from pathlib import Path\nimport sys\n"
                "tool = Path(__file__)\nrole = Path(sys.argv[-1]).stem\n"
                "print((tool.parent / (tool.name + '-' + role + '.txt')).read_text(), end='')\n")
        for tool in ("aapt", "apksigner"):
            path = self.sdk / tool
            path.write_text(stub)
            path.chmod(0o700)
        for role, version, code in (("baseline", "0.2.9", "2510"),
                                    ("newer", "0.2.10", "2511"), ("test", "", "")):
            (self.output / (role + ".apk")).write_bytes(("isolated " + role).encode())
            package = "com.sakurasep.kaneopilot" + (".test" if role == "test" else "")
            badging = "package: name='%s' versionCode='%s' versionName='%s'\n" % (package, code, version)
            if role == "test":
                badging += ("instrumentation: name='androidx.test.runner.AndroidJUnitRunner' "
                            "targetPackage='com.sakurasep.kaneopilot'\n")
            (self.sdk / ("aapt-" + role + ".txt")).write_text(badging)
            (self.sdk / ("apksigner-" + role + ".txt")).write_text(
                "Signer #1 certificate SHA-256 digest: " + "b" * 64 + "\n")

    def export(self):
        script = (ROOT / "android/tests/export-update-acceptance.sh").read_text()
        marker = "python3 - <<'PY'\n"
        self.assertEqual(script.count(marker), 1)
        body = script.split(marker, 1)[1].rsplit("\nPY", 1)[0]
        return subprocess.run([sys.executable, "-"], input=body, text=True,
                              env=self.environment, capture_output=True, timeout=15)

    def test_unversioned_instrumentation_preserves_real_metadata_and_checksums(self):
        result = self.export()
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest_path = self.output / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        self.assertIsNone(manifest["artifacts"]["test"]["version_code"])
        self.assertEqual(manifest["artifacts"]["test"]["version_name"], "")
        self.assertEqual(manifest["artifacts"]["baseline"]["version_code"], 2510)
        self.assertEqual(manifest["artifacts"]["newer"]["version_code"], 2511)
        self.assertEqual(manifest["artifacts"]["test"]["target_package"], "com.sakurasep.kaneopilot")
        self.assertTrue(manifest_path.read_bytes().endswith(b"\n"))
        checksums = (self.output / "SHA256SUMS").read_text().splitlines()
        self.assertEqual(len(checksums), 4)
        for line in checksums:
            expected, name = line.split("  ")
            self.assertEqual(hashlib.sha256((self.output / name).read_bytes()).hexdigest(), expected)

    def test_target_apk_without_numeric_version_is_rejected(self):
        path = self.sdk / "aapt-baseline.txt"
        path.write_text(path.read_text().replace("versionCode='2510'", "versionCode=''"))
        result = self.export()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("baseline: versionCode must be a positive integer", result.stderr)
        self.assertFalse((self.output / "manifest.json").exists())

    def test_mismatched_instrumentation_signer_is_rejected(self):
        (self.sdk / "apksigner-test.txt").write_text(
            "Signer #1 certificate SHA-256 digest: " + "c" * 64 + "\n")
        result = self.export()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Acceptance APKs were not signed by the same key", result.stderr)
        self.assertFalse((self.output / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
