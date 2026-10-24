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
        self.analyzer = self.root / "sdk" / "cmdline-tools" / "latest" / "bin"
        self.analyzer.mkdir(parents=True)
        self.reports = self.root / "reports"
        self.reports.mkdir()
        self.environment = dict(os.environ, ANDROID_HOME=str(self.root / "sdk"),
                                BUILD_NUMBER="25", SOURCE_SHA="a" * 40,
                                KANPILOT_ANDROID_UPDATE_OUTPUT=str(self.output),
                                KANPILOT_ANDROID_UPDATE_REPORTS=str(self.reports))
        # Only SDK tools are replaced. The exporter itself is read from source and executed.
        stub = ("#!/usr/bin/env python3\n"
                "from pathlib import Path\nimport sys\n"
                "tool = Path(__file__)\nrole = Path(sys.argv[-1]).stem\n"
                "print((tool.parent / (tool.name + '-' + role + '.txt')).read_text(), end='')\n")
        for tool in ("aapt", "apksigner"):
            path = self.sdk / tool
            path.write_text(stub)
            path.chmod(0o700)
        analyzer = self.analyzer / "apkanalyzer"
        analyzer.write_text(stub)
        analyzer.chmod(0o700)
        for role, version, code in (("preview", "0.0.0", "25"), ("preview-test", "", ""),
                                    ("baseline", "0.2.9", "2510"),
                                    ("newer", "0.2.10", "2511"), ("test", "", "")):
            (self.output / (role + ".apk")).write_bytes(("isolated " + role).encode())
            package = "com.sakurasep.kaneopilot.updateacceptance" + (".test" if role.endswith("test") else "")
            badging = "package: name='%s' versionCode='%s' versionName='%s'\n" % (package, code, version)
            # aapt badging does not supply the instrumentation declaration.
            (self.sdk / ("aapt-" + role + ".txt")).write_text(badging)
            (self.sdk / ("apksigner-" + role + ".txt")).write_text(
                "Signer #1 certificate SHA-256 digest: " + "b" * 64 + "\n")
        self.manifest = ("<?xml version='1.0' encoding='utf-8'?>\n"
                         "<manifest xmlns:android='http://schemas.android.com/apk/res/android' "
                         "package='com.sakurasep.kaneopilot.updateacceptance.test'>\n"
                         "  <instrumentation android:name='androidx.test.runner.AndroidJUnitRunner' "
                         "android:targetPackage='com.sakurasep.kaneopilot.updateacceptance'/>\n</manifest>\n")
        for role in ("test", "preview-test"):
            (self.analyzer / ("apkanalyzer-" + role + ".txt")).write_text(self.manifest)

    def export(self, script=None):
        if script is None:
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
        self.assertEqual(manifest["package_name"], "com.sakurasep.kaneopilot.updateacceptance")
        self.assertEqual(set(manifest["artifacts"]), {"preview", "preview-test", "baseline", "newer", "test"})
        self.assertEqual(manifest["artifacts"]["preview"]["version_name"], "0.0.0")
        self.assertEqual(manifest["artifacts"]["preview"]["version_code"], 25)
        for role in ("test", "preview-test"):
            self.assertIsNone(manifest["artifacts"][role]["version_code"])
            self.assertEqual(manifest["artifacts"][role]["version_name"], "")
            self.assertEqual(manifest["artifacts"][role]["target_package"], "com.sakurasep.kaneopilot.updateacceptance")
            self.assertEqual(manifest["artifacts"][role]["runner"], "androidx.test.runner.AndroidJUnitRunner")
            self.assertEqual((self.reports / (role + "-manifest.xml")).read_text(), self.manifest)
        self.assertEqual(manifest["artifacts"]["baseline"]["version_code"], 2510)
        self.assertEqual(manifest["artifacts"]["newer"]["version_code"], 2511)
        self.assertTrue(manifest_path.read_bytes().endswith(b"\n"))
        checksums = (self.output / "SHA256SUMS").read_text().splitlines()
        self.assertEqual(len(checksums), 6)
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

    def test_preview_identity_is_frozen(self):
        path = self.sdk / "aapt-preview.txt"
        path.write_text(path.read_text().replace("versionName='0.0.0'", "versionName='0.0.1'"))
        result = self.export()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("preview: version identity drift", result.stderr)
        self.assertFalse((self.output / "manifest.json").exists())

    def test_mismatched_instrumentation_signer_is_rejected(self):
        for role in ("test", "preview-test", "preview"):
            with self.subTest(role=role):
                path = self.sdk / ("apksigner-" + role + ".txt")
                original = path.read_text()
                path.write_text("Signer #1 certificate SHA-256 digest: " + "c" * 64 + "\n")
                result = self.export()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Acceptance APKs were not signed by the same key", result.stderr)
                self.assertFalse((self.output / "manifest.json").exists())
                path.write_text(original)

    def test_wrong_instrumentation_target_is_rejected_with_manifest_evidence(self):
        changed = self.manifest.replace("targetPackage='com.sakurasep.kaneopilot.updateacceptance'",
                                        "targetPackage='com.example.other'")
        for role in ("test", "preview-test"):
            with self.subTest(role=role):
                path = self.analyzer / ("apkanalyzer-" + role + ".txt")
                path.write_text(changed)
                result = self.export()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Acceptance instrumentation targets another package", result.stderr)
                self.assertFalse((self.output / "manifest.json").exists())
                self.assertEqual((self.reports / (role + "-manifest.xml")).read_text(), changed)
                path.write_text(self.manifest)

    def test_wrong_instrumentation_runner_is_rejected_with_manifest_evidence(self):
        changed = self.manifest.replace("androidx.test.runner.AndroidJUnitRunner", "com.example.OtherRunner")
        for role in ("test", "preview-test"):
            with self.subTest(role=role):
                path = self.analyzer / ("apkanalyzer-" + role + ".txt")
                path.write_text(changed)
                result = self.export()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Acceptance test runner missing", result.stderr)
                self.assertFalse((self.output / "manifest.json").exists())
                self.assertEqual((self.reports / (role + "-manifest.xml")).read_text(), changed)
                path.write_text(self.manifest)

    def test_missing_or_duplicate_instrumentation_is_rejected(self):
        declaration = ("<instrumentation android:name='androidx.test.runner.AndroidJUnitRunner' "
                       "android:targetPackage='com.sakurasep.kaneopilot.updateacceptance'/>")
        for role in ("test", "preview-test"):
            for count in (0, 2):
                with self.subTest(role=role, instrumentation_count=count):
                    changed = self.manifest.replace(declaration, declaration * count)
                    path = self.analyzer / ("apkanalyzer-" + role + ".txt")
                    path.write_text(changed)
                    result = self.export()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse((self.output / "manifest.json").exists())
                    self.assertEqual((self.reports / (role + "-manifest.xml")).read_text(), changed)
                    path.write_text(self.manifest)


if __name__ == "__main__":
    unittest.main()
