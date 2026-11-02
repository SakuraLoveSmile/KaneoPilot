"""Mach-O audit and runtime preparation checks on crafted files and recorded tool output.

Nothing here signs, compiles or runs the bundled interpreter. The one test that calls the real `lipo` and
`otool` reads a copy of a system binary and only runs on a Mac.
"""
import importlib.util
import io
import os
from pathlib import Path
import re
import shutil
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS))
import app_macho  # noqa: E402

SPEC = importlib.util.spec_from_file_location("prepare_python_runtime", SCRIPTS / "prepare_python_runtime.py")
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)

# `otool -l` output as printed by the tool on macOS: a relocatable build (shape of the bundled runtime) ...
OTOOL_BUNDLED = """\
Load command 8
      cmd LC_BUILD_VERSION
  cmdsize 32
 platform 1
    minos 14.0
      sdk 26.5
   ntools 1
     tool 3
  version 1235.2
Load command 14
          cmd LC_LOAD_DYLIB
      cmdsize 56
         name @executable_path/../lib/libpython3.13.dylib (offset 24)
   time stamp 2 Thu Jan  1 08:00:02 1970
      current version 3.13.0
compatibility version 3.13.0
Load command 15
          cmd LC_LOAD_DYLIB
      cmdsize 56
         name /usr/lib/libSystem.B.dylib (offset 24)
   time stamp 2 Thu Jan  1 08:00:02 1970
      current version 1356.0.0
compatibility version 1.0.0
"""
# ... a Homebrew build (absolute dependency, newer deployment target) ...
OTOOL_HOMEBREW = """\
      cmd LC_BUILD_VERSION
  cmdsize 32
 platform 1
    minos 26.0
      sdk 26.5
   ntools 1
     tool 3
          cmd LC_LOAD_DYLIB
      cmdsize 120
         name /opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/Versions/3.14/Python (offset 24)
   time stamp 2 Thu Jan  1 08:00:02 1970
      current version 3.14.0
compatibility version 3.14.0
          cmd LC_LOAD_DYLIB
      cmdsize 56
         name /usr/lib/libSystem.B.dylib (offset 24)
"""
# ... a dylib with an absolute install name and an rpath ...
OTOOL_DYLIB = """\
          cmd LC_ID_DYLIB
      cmdsize 104
         name /install/lib/libpython3.13.dylib (offset 24)
   time stamp 1 Thu Jan  1 08:00:01 1970
      current version 3.13.0
          cmd LC_BUILD_VERSION
 platform 1
    minos 11.0
          cmd LC_RPATH
      cmdsize 32
         path /opt/homebrew/lib (offset 12)
          cmd LC_LOAD_WEAK_DYLIB
         name @rpath/libweak.dylib (offset 24)
"""
# ... and a fat file whose slices use both ways of naming the deployment target.
OTOOL_FAT = """\
Mach header
      cmd LC_VERSION_MIN_MACOSX
  cmdsize 16
  version 10.9
      sdk 12.1
      cmd LC_SOURCE_VERSION
  version 0.0
      cmd LC_BUILD_VERSION
  cmdsize 32
 platform 1
    minos 11.0
      sdk 12.1
   ntools 1
"""


def macho(filetype, magic=0xFEEDFACF):
    return struct.pack("<IiiIIIII", magic, 0x0100000C, 0, filetype, 0, 0, 0, 0)


FAT = b"\xca\xfe\xba\xbe" + struct.pack(">I", 2) + bytes(24)


class FakeTools:
    """Answers `lipo -archs` and `otool -l` from tables keyed by file name, and records every call."""

    def __init__(self, archs=None, otool=None, default_archs="arm64", default_otool=OTOOL_BUNDLED):
        self.archs, self.otool = archs or {}, otool or {}
        self.default_archs, self.default_otool = default_archs, default_otool
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        name = os.path.basename(argv[-1])
        if argv[0] == "lipo":
            value = self.archs.get(name, self.default_archs)
        else:
            value = self.otool.get(name, self.default_otool)
        if isinstance(value, Exception):
            raise value
        return value


class MachoKindTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def kind(self, data):
        path = self.root / "file"
        path.write_bytes(data)
        return app_macho.macho_kind(str(path))

    def test_thin_images_are_classified_by_file_type(self):
        self.assertEqual([self.kind(macho(number)) for number in (1, 2, 6, 8, 10)],
                         ["object", "execute", "dylib", "bundle", "other"])
        self.assertEqual(self.kind(macho(6, magic=0xFEEDFACE)), "dylib")

    def test_fat_images_are_found_but_class_files_and_text_are_not(self):
        self.assertEqual(self.kind(FAT), "fat")
        self.assertEqual(self.kind(b"\xca\xfe\xba\xbf" + struct.pack(">I", 1) + bytes(24)), "fat")
        self.assertIsNone(self.kind(b"\xca\xfe\xba\xbe\x00\x00\x00\x34" + bytes(24)))
        self.assertIsNone(self.kind(b"#!/bin/sh\nexit 0\n# padding to sixteen bytes"))
        self.assertIsNone(self.kind(b"\xcf\xfa\xed"))
        self.assertIsNone(app_macho.macho_kind(str(self.root / "missing")))


class ScanTests(unittest.TestCase):
    def test_only_regular_mach_o_files_are_listed_in_a_stable_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {"bin/python3.13": macho(2), "lib/libpython3.13.dylib": macho(6),
                     "lib/python3.13/lib-dynload/_ssl.so": macho(8), "lib/python3.13/os.py": b"# not mach-o, but long enough",
                     "lib/python3.13/config/python.o": macho(1), "docs/a.class": b"\xca\xfe\xba\xbe\x00\x00\x00\x34" + bytes(16)}
            for relative, data in files.items():
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                (root / relative).write_bytes(data)
            os.symlink("python3.13", root / "bin/python3")
            os.symlink("../lib", root / "bin/lib-link")
            found = [(os.path.relpath(path, root), kind) for path, kind in app_macho.scan(str(root))]
            self.assertEqual(found, [("bin/python3.13", "execute"), ("lib/libpython3.13.dylib", "dylib"),
                                     ("lib/python3.13/config/python.o", "object"),
                                     ("lib/python3.13/lib-dynload/_ssl.so", "bundle")])


class ParseTests(unittest.TestCase):
    def test_a_relocatable_executable(self):
        info = app_macho.parse_load_commands(OTOOL_BUNDLED)
        self.assertEqual(info, {"minos": ["14.0"], "id": None, "rpaths": [],
                                "dependencies": ["@executable_path/../lib/libpython3.13.dylib", "/usr/lib/libSystem.B.dylib"]})

    def test_install_name_rpath_and_weak_dependency(self):
        info = app_macho.parse_load_commands(OTOOL_DYLIB)
        self.assertEqual((info["id"], info["rpaths"], info["dependencies"], info["minos"]),
                         ("/install/lib/libpython3.13.dylib", ["/opt/homebrew/lib"], ["@rpath/libweak.dylib"], ["11.0"]))

    def test_both_deployment_target_commands_of_every_slice(self):
        self.assertEqual(app_macho.parse_load_commands(OTOOL_FAT)["minos"], ["10.9", "11.0"])

    def test_versions_compare_numerically(self):
        self.assertLess(app_macho.version_tuple("9.0"), app_macho.version_tuple("14.0"))
        self.assertLess(app_macho.version_tuple("14.0"), app_macho.version_tuple("14.0.1"))
        self.assertGreater(app_macho.version_tuple("26.0"), app_macho.version_tuple("14.0"))


class AuditTests(unittest.TestCase):
    def problems(self, **tables):
        return app_macho.audit_file("/app/x", "execute", "14.0", FakeTools(**tables))

    def test_a_relocatable_arm64_image_passes(self):
        self.assertEqual(self.problems(), [])
        self.assertEqual(self.problems(default_otool=OTOOL_FAT.replace("10.9", "11.0")), [])

    def test_other_architectures_are_refused(self):
        for archs in ("x86_64", "x86_64 arm64", "arm64 x86_64", "arm64e", "arm64 arm64e", ""):
            with self.subTest(archs=archs):
                self.assertTrue(any("architectures" in problem for problem in self.problems(default_archs=archs)))

    def test_a_newer_deployment_target_or_none_at_all_is_refused(self):
        self.assertTrue(any("26.0" in problem for problem in self.problems(default_otool=OTOOL_HOMEBREW)))
        self.assertEqual(self.problems(default_otool="      cmd LC_SYMTAB\n"), ["no macOS deployment target"])

    def test_object_files_need_the_right_architecture_but_no_deployment_target(self):
        bare = "      cmd LC_SYMTAB\n"
        audit = lambda kind, **tables: app_macho.audit_file("/app/x", kind, "14.0", FakeTools(**tables))  # noqa: E731
        self.assertEqual(audit("object", default_otool=bare), [])
        self.assertTrue(any("architectures" in problem for problem in audit("object", default_archs="x86_64", default_otool=bare)))
        self.assertTrue(any("26.0" in problem for problem in audit("object", default_otool=OTOOL_HOMEBREW)))
        # An image that is loaded still needs a deployment target.
        for kind in ("dylib", "bundle", "execute"):
            self.assertEqual(audit(kind, default_otool=bare), ["no macOS deployment target"], kind)

    def test_dependencies_outside_the_app_and_the_system_are_refused(self):
        problems = self.problems(default_otool=OTOOL_HOMEBREW)
        self.assertEqual(sum("links /opt/homebrew/Cellar" in problem for problem in problems), 1)
        self.assertFalse(any("libSystem" in problem for problem in problems))
        for dependency in ("/usr/local/lib/libssl.dylib", "/Applications/Xcode.app/x.dylib", "libfoo.dylib", "/opt/x"):
            with self.subTest(dependency=dependency):
                text = OTOOL_BUNDLED + "          cmd LC_LOAD_DYLIB\n         name %s (offset 24)\n" % dependency
                self.assertEqual(len(self.problems(default_otool=text)), 1)
        allowed = "".join("          cmd LC_LOAD_DYLIB\n         name %s (offset 24)\n" % name for name in (
            "@rpath/libx.dylib", "@loader_path/../y.dylib", "/usr/lib/libz.1.dylib",
            "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation"))
        self.assertEqual(self.problems(default_otool=OTOOL_BUNDLED + allowed), [])

    def test_a_tool_failure_is_a_problem_not_a_pass(self):
        error = app_macho.ToolError("lipo failed: not a Mach-O file")
        self.assertEqual(self.problems(archs={"x": error}), [str(error)])

    def test_a_tree_is_audited_file_by_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative, data in {"Contents/MacOS/KaneoPilot": macho(2), "Contents/Resources/python/lib/libpython3.13.dylib": macho(6),
                                   "Contents/Resources/python/lib/python3.13/lib-dynload/_ssl.so": macho(8),
                                   "Contents/Resources/core/kanpilot.py": b"print('not mach-o, long enough')"}.items():
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                (root / relative).write_bytes(data)
            tools = FakeTools(archs={"_ssl.so": "x86_64 arm64"}, otool={"libpython3.13.dylib": OTOOL_HOMEBREW})
            report = app_macho.audit(str(root), "14.0", tools)
            self.assertEqual((report["images"], report["kinds"]), (3, {"dylib": 1, "bundle": 1, "execute": 1}))
            self.assertEqual(sorted((item["path"], item["problem"].split()[0]) for item in report["violations"]),
                             [("Contents/Resources/python/lib/libpython3.13.dylib", "deployment"),
                              ("Contents/Resources/python/lib/libpython3.13.dylib", "links"),
                              ("Contents/Resources/python/lib/python3.13/lib-dynload/_ssl.so", "architectures")])
            self.assertEqual(len(tools.calls), 6)
            self.assertTrue(all(call[0] in ("lipo", "otool") and call[-1].startswith(str(root)) for call in tools.calls))

    def test_the_command_reports_and_exits_by_the_findings(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                self.assertEqual(app_macho.main(["audit", directory]), 0)
            self.assertIn('"images": 0', stdout.getvalue())
            self.assertIn('"minos_max": "14.0"', stdout.getvalue())
            with patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(app_macho.main(["audit", os.path.join(directory, "missing")]), 1)

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("lipo") and shutil.which("otool"),
                         "needs the macOS command-line tools")
    def test_the_real_tools_flag_a_system_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            shutil.copy("/bin/ls", os.path.join(directory, "ls"))
            report = app_macho.audit(directory, "14.0")
        self.assertEqual(report["images"], 1)
        self.assertTrue(any("architectures" in item["problem"] for item in report["violations"]), report)


def tree(root, files):
    for relative, data in files.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_bytes(data)


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name) / "python"
        tree(self.runtime, {
            "bin/python3.13": macho(2), "lib/libpython3.13.dylib": macho(6),
            "lib/python3.13/lib-dynload/_ssl.cpython-313-darwin.so": macho(8),
            "lib/python3.13/lib-dynload/_json.cpython-313-darwin.so": macho(8),
            "lib/python3.13/os.py": b"# standard library module, long enough to be text",
            "lib/python3.13/config-3.13-darwin/python.o": macho(1)})
        os.symlink("python3.13", self.runtime / "bin/python3")
        self.calls = []

    def runner(self, argv):
        self.calls.append(list(argv))
        return ""

    def test_the_library_folder_must_be_unique(self):
        self.assertEqual(prepare.library_folder(self.runtime).name, "python3.13")
        (self.runtime / "lib/python3.14").mkdir()
        with self.assertRaises(prepare.PrepareError):
            prepare.library_folder(self.runtime)
        shutil.rmtree(self.runtime / "lib/python3.13")
        shutil.rmtree(self.runtime / "lib/python3.14")
        with self.assertRaises(prepare.PrepareError):
            prepare.library_folder(self.runtime)

    def test_bytecode_is_rewritten_with_unchecked_hashes_by_the_bundled_interpreter(self):
        prepare.compile_bytecode(self.runtime, self.runner)
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertEqual(call[:4], [str(self.runtime / "bin/python3"), "-I", "-m", "compileall"])
        self.assertIn("-f", call)
        self.assertEqual(call[call.index("--invalidation-mode") + 1], "unchecked-hash")
        self.assertEqual(call[-1], str(self.runtime / "lib/python3.13"))
        skipped = re.compile(call[call.index("-x") + 1])
        for path in ("/a/lib/python3.13/test/test_os.py", "/a/lib/python3.13/idlelib/run.py", "/a/lib/python3.13/tkinter/x.py"):
            self.assertTrue(skipped.search(path), path)
        for path in ("/a/lib/python3.13/os.py", "/a/lib/python3.13/unittest/mock.py", "/a/lib/python3.13/site-packages/pip/x.py"):
            self.assertFalse(skipped.search(path), path)

    def test_libraries_are_signed_before_executables_and_everything_is_verified(self):
        signed = prepare.sign_machos(self.runtime, self.runner)
        relative = [os.path.relpath(path, self.runtime) for path in signed]
        self.assertEqual(relative, ["lib/libpython3.13.dylib",
                                    "lib/python3.13/lib-dynload/_json.cpython-313-darwin.so",
                                    "lib/python3.13/lib-dynload/_ssl.cpython-313-darwin.so", "bin/python3.13"])
        signing = [call for call in self.calls if call[:4] == ["codesign", "--force", "--sign", "-"]]
        verifying = [call for call in self.calls if call[:3] == ["codesign", "--verify", "--strict"]]
        self.assertEqual([call[-1] for call in signing], signed)
        self.assertEqual([call[-1] for call in verifying], signed)
        self.assertEqual(self.calls, signing + verifying)

    def test_a_fat_image_in_the_runtime_stops_the_build(self):
        (self.runtime / "lib/fat.dylib").write_bytes(FAT)
        with self.assertRaisesRegex(prepare.PrepareError, "fat"):
            prepare.sign_machos(self.runtime, self.runner)
        self.assertEqual(self.calls, [])

    def test_the_size_report_names_the_big_parts(self):
        (self.runtime / "lib/python3.13/test").mkdir()
        (self.runtime / "lib/python3.13/test/data.bin").write_bytes(bytes(3_000_000))
        report = prepare.size_report(self.runtime)
        self.assertIn("runtime total: 3.", report)
        self.assertIn("lib/", report)
        self.assertRegex(report, r"test\s+3\.0 MB")

    def test_the_notice_report_lists_shallow_license_texts_and_counts_the_rest(self):
        tree(self.runtime, {"LICENSE.txt": b"top", "licenses/openssl.txt": b"x", "lib/python3.13/LICENSE.txt": b"psf",
                            "share/doc/NOTICE": b"n", "lib/python3.13/site-packages/pip/_vendor/rich/LICENSE": b"deep",
                            "lib/python3.13/test/COPYING": b"deep too", "lib/python3.13/licensing.py": b"not a notice"})
        lines = prepare.notice_report(self.runtime).splitlines()
        self.assertEqual(lines[0], "license and notice texts within 3 levels: 4")
        self.assertEqual(lines[1:-1], ["  LICENSE.txt", "  lib/python3.13/LICENSE.txt", "  licenses", "  share/doc/NOTICE"])
        self.assertEqual(lines[-1], "  ... and 2 deeper (bundled packages, tests)")

    def test_the_command_runs_only_in_actions_and_in_order(self):
        outside = {key: value for key, value in os.environ.items() if key not in ("GITHUB_ACTIONS", "RUNNER_ENVIRONMENT")}
        with patch.dict(os.environ, outside, clear=True), patch.object(prepare, "compile_bytecode") as compile_bytecode:
            self.assertEqual(prepare.main([str(self.runtime)]), 1)
        compile_bytecode.assert_not_called()
        hosted = dict(outside, GITHUB_ACTIONS="true", RUNNER_ENVIRONMENT="github-hosted")
        order = []
        with patch.dict(os.environ, hosted, clear=True), \
                patch.object(prepare, "compile_bytecode", side_effect=lambda runtime: order.append("compile")), \
                patch.object(prepare, "sign_machos", side_effect=lambda runtime: order.append("sign") or []), \
                patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(prepare.main([str(self.runtime)]), 0)
        self.assertEqual(order, ["compile", "sign"])
        with patch.dict(os.environ, hosted, clear=True), patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(prepare.main([str(self.runtime / "nothing")]), 1)
        self.assertIn("bin/python3", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
