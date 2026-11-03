"""doctor 的 python_runtime 检查：内置运行时带不带得上系统证书（离线，不联网）。"""
from types import SimpleNamespace
from unittest import mock

from helpers import KanpilotTestCase
from kanpilot import cmd_read

BUNDLED = "/Applications/KaneoPilot.app/Contents/Resources/python/bin/python3"
EXTERNAL = "/usr/local/bin/python3"


def _verify_paths(cafile=None, capath=None):
    return SimpleNamespace(cafile=cafile, capath=capath)


class DoctorPythonRuntimeCheck(KanpilotTestCase):
    def _run(self, executable, cafile=None, capath=None):
        with mock.patch.object(cmd_read.sys, "executable", executable), \
                mock.patch("ssl.get_default_verify_paths",
                           return_value=_verify_paths(cafile, capath)):
            return cmd_read._doctor_python_runtime()

    def test_bundled_with_system_cafile_is_ok(self):
        cafile = self.root / "cert.pem"
        cafile.write_text("fixture", encoding="utf-8")
        check = self._run(BUNDLED, cafile=str(cafile))
        self.assertEqual(check["name"], "python_runtime")
        self.assertTrue(check["ok"], check)
        self.assertIn("bundled", check["detail"])
        self.assertIn(BUNDLED, check["detail"])
        self.assertIn(f"cafile {cafile} (exists)", check["detail"])

    def test_bundled_without_any_certificate_source_fails_with_repair_hint(self):
        check = self._run(BUNDLED, cafile=str(self.root / "missing.pem"),
                          capath=str(self.root / "missing-dir"))
        self.assertFalse(check["ok"], check)
        self.assertIn("(missing)", check["detail"])
        self.assertIn("重新安装 KaneoPilot", check["detail"])

    def test_bundled_capath_alone_is_enough(self):
        capath = self.root / "certs"
        capath.mkdir()
        check = self._run(BUNDLED, cafile=str(self.root / "missing.pem"),
                          capath=str(capath))
        self.assertTrue(check["ok"], check)
        self.assertIn(f"capath {capath} (exists)", check["detail"])
        self.assertIn("(missing)", check["detail"])  # cafile 仍如实标缺失

    def test_bundled_without_ssl_defaults_fails(self):
        with mock.patch.object(cmd_read.sys, "executable", BUNDLED), \
                mock.patch("ssl.get_default_verify_paths",
                           side_effect=RuntimeError("no ssl")):
            check = cmd_read._doctor_python_runtime()
        self.assertFalse(check["ok"], check)
        self.assertIn("cafile none (missing)", check["detail"])

    def test_external_interpreter_is_informational_only(self):
        check = self._run(EXTERNAL, cafile=str(self.root / "missing.pem"),
                          capath=str(self.root / "missing-dir"))
        self.assertTrue(check["ok"], check)
        self.assertIn("external", check["detail"])
        self.assertNotIn("bundled", check["detail"])

    def test_detail_reports_interpreter_version(self):
        check = self._run(EXTERNAL)
        version = ".".join(str(part) for part in cmd_read.sys.version_info[:3])
        self.assertTrue(check["detail"].startswith(version + " external"), check)

    def test_doctor_lists_it_right_after_python(self):
        out = self.call("doctor", {})
        names = [c["name"] for c in out["checks"]]
        self.assertIn("python_runtime", names)
        self.assertEqual(names.index("python_runtime"), names.index("python") + 1)
        check = next(c for c in out["checks"] if c["name"] == "python_runtime")
        self.assertIsInstance(check["ok"], bool)
        self.assertTrue(check["detail"])
