"""Isolated filesystem/process tests; no real app, DB, network or compiler."""
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).parents[2] / "core/kanpilot/app_update_installer.py"
SPEC = importlib.util.spec_from_file_location("app_update_installer", SCRIPT)
updater = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updater)


def app(path, version="1.2.2", build="40", bundle_id=updater.BUNDLE_ID):
    (path / "Contents/MacOS").mkdir(parents=True)
    binary = path / "Contents/MacOS/KaneoPilot"
    binary.write_bytes(b"#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    info = {"CFBundleIdentifier": bundle_id, "CFBundleExecutable": "KaneoPilot",
            "CFBundleShortVersionString": version, "CFBundleVersion": build}
    (path / "Contents/Info.plist").write_bytes(plistlib.dumps(info))


def portable_exchange(left, right):
    """Only test injection; production never falls back from atomic RENAME_SWAP."""
    temporary = right.parent / "exchanging.app"
    os.rename(left, temporary)
    os.rename(right, left)
    os.rename(temporary, right)


class Tools:
    def __init__(self):
        self.calls = []
        self.mounted_version = "1.2.3"
        self.mounted_build = "41"
        self.mounted_id = updater.BUNDLE_ID
        self.signature = "b" * 64
        self.processes = b""
        self.fail_new_open = False
        self.fail_stage_verify = False
        self.wrong_mount = False
        self.detach_fails = False
        self.open_timeout = False
        self.open_attempted = False
        self.running_after_failure = False
        self.process_outputs = None  # successive `ps` outputs; the last one repeats
        self.fail_clone = False
        self.fail_runtime_probe = False

    def __call__(self, argv):
        self.calls.append(argv)
        tool = Path(argv[0]).name
        if tool == "codesign":
            if "-d" in argv:
                return b"", ("CandidateCDHashFull sha256=" + self.signature + "\n").encode()
            if self.fail_stage_verify and ".KaneoPilot-backup-" in argv[-1]:
                raise updater.UpdateError("staged signature invalid")
            return b"", b""
        if tool == "hdiutil":
            if argv[1] == "attach":
                mount = Path(argv[argv.index("-mountpoint") + 1])
                app(mount / "KaneoPilot.app", self.mounted_version, self.mounted_build, self.mounted_id)
                return plistlib.dumps({"system-entities": [{"mount-point": "unexpected" if self.wrong_mount else str(mount)}]}), b""
            if self.detach_fails:
                raise updater.UpdateError("volume is busy")
            return b"", b""
        if tool == "ditto":
            shutil.copytree(argv[-2], argv[-1])
            return b"", b""
        if tool == "ps":
            if self.running_after_failure and self.open_attempted:
                return ("5720 " + self.calls[-2][-1] + "/Contents/MacOS/KaneoPilot\n").encode(), b""
            if self.process_outputs:
                return (self.process_outputs.pop(0) if len(self.process_outputs) > 1 else self.process_outputs[0]), b""
            return self.processes, b""
        if tool == "cp":
            if self.fail_clone:
                raise updater.UpdateError("clone failed")
            shutil.copytree(argv[-2], argv[-1], symlinks=True)
            return b"", b""
        if argv[1:2] == ["-c"]:  # the interpreter probe of a cloned runtime
            if self.fail_runtime_probe:
                raise updater.UpdateError("clone does not start")
            return b"", b""
        if tool == "open":
            self.open_attempted = True
            if self.open_timeout:
                raise subprocess.TimeoutExpired(argv, 90)
            if self.fail_new_open and updater.bundle_info(Path(argv[-1]))["CFBundleShortVersionString"] == "1.2.3":
                raise updater.UpdateError("LaunchServices refused candidate")
            return b"", b""
        raise AssertionError("Unexpected system tool: " + repr(argv))


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.target = self.root / "KaneoPilot.app"
        app(self.target)
        # A separate data directory makes accidental data changes observable.
        self.data = self.root / "user-data"
        self.data.mkdir()
        (self.data / "kanpilot.db").write_bytes(b"do not migrate or open this DB")
        (self.data / "pairing").write_bytes(b"keep pairing")
        package = self.root / "KaneoPilot-1.2.3-macos-arm64.dmg"
        package.write_bytes(b"isolated DMG fixture")
        self.payload = {"package_path": str(package), "target_path": str(self.target),
                        "cache_path": str(self.root / "cache"), "version": "1.2.3", "build_number": 41,
                        "source_sha": "a" * 40, "sha256": updater.digest(package), "size": package.stat().st_size,
                        "signature_sha256": "b" * 64}
        self.tools = Tools()
        self.original = updater.target_snapshot(self.target)

    def tearDown(self):
        self.assertEqual((self.data / "kanpilot.db").read_bytes(), b"do not migrate or open this DB")
        self.assertEqual((self.data / "pairing").read_bytes(), b"keep pairing")
        self.temporary.cleanup()

    def prepare(self):
        return updater.prepare(self.payload, runner=self.tools)

    def install(self, job, **kwargs):
        self.commit(job)
        return updater.install(Path(job["job_path"]), 1234567, runner=self.tools,
                               alive=lambda _: False, exchange=portable_exchange, **kwargs)

    def commit(self, job):
        updater.write_json(Path(job["job_path"]).parent / "commit.json",
                           {"parent_pid": 1234567, "job_path": job["job_path"]})

    def result(self, job):
        return json.loads((Path(job["job_path"]).parent / "result.json").read_text())

    def test_prepare_copies_verified_app_without_changing_target(self):
        job = self.prepare()
        self.assertEqual(updater.target_snapshot(self.target), self.original)
        self.assertEqual(updater.bundle_info(Path(job["backup_path"]))["CFBundleShortVersionString"], "1.2.3")
        self.assertEqual(Path(job["backup_path"]).parent.parent, self.target.parent)
        self.assertEqual(Path(job["helper_path"]).read_bytes(), SCRIPT.read_bytes())
        self.assertEqual(self.result(job)["status"], "prepared")
        attach = next(argv for argv in self.tools.calls if "attach" in argv)
        self.assertIn("-readonly", attach)
        self.assertIn("-nobrowse", attach)
        self.assertTrue(any("detach" in argv for argv in self.tools.calls))

    def test_distinct_jobs_cannot_exchange_the_same_target_concurrently(self):
        first, overlapping, stale = (self.prepare() for _ in range(3))
        for job in (first, overlapping, stale):
            self.commit(job)
        exchanges = []
        conflicts = []

        def recorded_exchange(left, right):
            exchanges.append(str(right))
            portable_exchange(left, right)

        def exchange_while_another_helper_attempts_install(left, right):
            # Both jobs were prepared from the same original bundle. The second
            # helper runs exactly after the first has checked identity/processes,
            # but before its exchange. No thread timing or production App is used.
            try:
                updater.install(Path(overlapping["job_path"]), 1234567, runner=self.tools,
                                alive=lambda _: False, exchange=recorded_exchange)
            except updater.UpdateError as error:
                conflicts.append(error.code)
            recorded_exchange(left, right)

        result = updater.install(Path(first["job_path"]), 1234567, runner=self.tools,
                                 alive=lambda _: False, exchange=exchange_while_another_helper_attempts_install)
        opens = [call for call in self.tools.calls if Path(call[0]).name == "open"]
        facts = {"conflicts": conflicts, "exchanges": len(exchanges), "launches": len(opens),
                 "first_backup_version": updater.bundle_info(Path(first["backup_path"]))["CFBundleShortVersionString"]}
        self.assertEqual(conflicts, ["update_busy"], json.dumps(facts, sort_keys=True))
        self.assertEqual(result["status"], "launch_requested")
        self.assertEqual(len(exchanges), 1)
        self.assertEqual(len(opens), 1)
        self.assertEqual(updater.target_snapshot(Path(first["backup_path"])), self.original)
        self.assertEqual(self.result(overlapping)["status"], "failed")
        self.assertEqual(updater.bundle_info(Path(overlapping["backup_path"]))["CFBundleShortVersionString"], "1.2.3")

        # The target lock must be released after launch. A later old job acquires
        # it, rejects its stale target identity, and never reopens the new App.
        with self.assertRaisesRegex(updater.UpdateError, "目标发生变化"):
            updater.install(Path(stale["job_path"]), 1234567, runner=self.tools,
                            alive=lambda _: False, exchange=recorded_exchange)
        self.assertEqual(len(exchanges), 1)
        self.assertEqual(len([call for call in self.tools.calls if Path(call[0]).name == "open"]), 1)

    def test_corrupt_or_truncated_download_never_mounts(self):
        for key, value in (("size", self.payload["size"] - 1), ("sha256", "c" * 64)):
            with self.subTest(key=key):
                request = dict(self.payload, **{key: value})
                with self.assertRaisesRegex(updater.UpdateError, "损坏"):
                    updater.prepare(request, runner=self.tools)
                self.assertFalse(self.tools.calls)
                self.assertEqual(updater.target_snapshot(self.target), self.original)

    def test_bad_parameters_are_rejected_before_any_tools(self):
        for key, value in (("version", "1.2.3-rc.1"), ("source_sha", "x" * 40),
                           ("sha256", "a" * 63), ("size", 2 ** 31), ("size", True),
                           ("build_number", True), ("build_number", 0)):
            with self.subTest(key=key):
                with self.assertRaises(updater.UpdateError):
                    updater.prepare(dict(self.payload, **{key: value}), runner=self.tools)
        with self.assertRaises(updater.UpdateError):
            updater.prepare(dict(self.payload, unexpected=True), runner=self.tools)
        self.assertFalse(self.tools.calls)

    def test_only_the_arm64_package_of_the_same_version_is_installable(self):
        # The Universal package of the retired scheme, another version's package and a bare name never mount.
        for name in ("KaneoPilot-1.2.3-macos-universal.dmg", "KaneoPilot-1.2.4-macos-arm64.dmg",
                     "KaneoPilot-1.2.3-macos.dmg"):
            with self.subTest(name=name):
                package = self.root / name
                package.write_bytes(b"isolated DMG fixture")
                request = dict(self.payload, package_path=str(package), sha256=updater.digest(package),
                               size=package.stat().st_size)
                with self.assertRaisesRegex(updater.UpdateError, "文件名"):
                    updater.prepare(request, runner=self.tools)
                self.assertFalse(self.tools.calls)
                self.assertEqual(updater.target_snapshot(self.target), self.original)

    def test_old_same_and_preview_versions_cannot_replace_app(self):
        for installed in ("1.2.3", "1.2.10", "0.0.0"):
            with self.subTest(installed=installed):
                info_path = self.target / "Contents/Info.plist"
                info = plistlib.loads(info_path.read_bytes())
                info["CFBundleShortVersionString"] = installed
                info_path.write_bytes(plistlib.dumps(info))
                with self.assertRaises(updater.UpdateError):
                    self.prepare()
        self.assertFalse(self.tools.calls)

    def use_installed(self, version, build):
        info_path = self.target / "Contents/Info.plist"
        info = plistlib.loads(info_path.read_bytes())
        info["CFBundleShortVersionString"], info["CFBundleVersion"] = version, build
        info_path.write_bytes(plistlib.dumps(info))
        self.original = updater.target_snapshot(self.target)

    def use_candidate(self, version, build):
        package = self.root / ("KaneoPilot-%s-macos-arm64.dmg" % version)
        package.write_bytes(("isolated DMG fixture " + version).encode())
        self.payload.update(package_path=str(package), version=version, build_number=build,
                            sha256=updater.digest(package), size=package.stat().st_size)
        self.tools.mounted_version, self.tools.mounted_build = version, str(build)

    def test_test_builds_can_replace_stable_and_test_builds_and_be_replaced_by_stable(self):
        cases = (("1.2.2", "40", "1.2.3-test.41", 41), ("1.2.3-test.41", "41", "1.2.3-test.42", 42),
                 ("1.2.3-test.41", "41", "1.2.3", 43), ("1.2.2", "abc", "1.2.3", 44))
        for installed, installed_build, candidate, build in cases:
            with self.subTest(installed=installed, candidate=candidate):
                self.tools = Tools()
                self.use_installed(installed, installed_build)
                self.use_candidate(candidate, build)
                job = self.prepare()
                self.assertEqual(updater.bundle_info(Path(job["backup_path"]))["CFBundleShortVersionString"], candidate)
                self.assertEqual(updater.target_snapshot(self.target), self.original)

    def test_update_between_test_builds_installs_and_keeps_the_previous_build(self):
        self.use_installed("1.2.3-test.41", "41")
        self.use_candidate("1.2.3-test.42", 42)
        job = self.prepare()
        result = self.install(job)
        self.assertEqual(result["status"], "launch_requested")
        self.assertEqual(updater.bundle_info(self.target)["CFBundleShortVersionString"], "1.2.3-test.42")
        self.assertEqual(updater.target_snapshot(Path(job["backup_path"])), self.original)

    def test_equal_lower_or_build_regressed_test_builds_are_rejected_before_mounting(self):
        cases = (("1.2.3-test.41", "41", "1.2.3-test.41", 41), ("1.2.3-test.42", "42", "1.2.3-test.41", 41),
                 ("1.2.3", "50", "1.2.3-test.60", 60), ("1.2.2", "90", "1.2.3-test.41", 41),
                 ("1.2.3-test.41", "41", "1.2.3", 40), ("1.2.3-test.41", "abc", "1.2.3-test.42", 42),
                 ("1.2.3-test.41", "41", "1.2.3-test.42", 41))
        for installed, installed_build, candidate, build in cases:
            with self.subTest(installed=installed, installed_build=installed_build, candidate=candidate, build=build):
                self.tools = Tools()
                self.use_installed(installed, installed_build)
                self.use_candidate(candidate, build)
                with self.assertRaisesRegex(updater.UpdateError, "更高"):
                    self.prepare()
                self.assertFalse(self.tools.calls)
                self.assertEqual(updater.target_snapshot(self.target), self.original)

    def test_malformed_test_versions_are_rejected_before_any_tools(self):
        for version in ("1.2.3-test", "1.2.3-test.0", "1.2.3-test.01", "1.2.3-rc.1", "1.2.3-TEST.5", "1.2.3-test.5+x", "1.2.3-test.1000000000"):
            with self.subTest(version=version), self.assertRaisesRegex(updater.UpdateError, "版本格式无效"):
                updater.prepare(dict(self.payload, version=version), runner=self.tools)
        self.assertFalse(self.tools.calls)

    def test_installed_test_build_is_a_valid_install_target_but_not_a_preview(self):
        self.use_installed("1.2.3-test.41", "41")
        self.assertEqual(updater.bundle_info(self.target)["CFBundleShortVersionString"], "1.2.3-test.41")
        self.use_installed("0.0.0", "41")
        with self.assertRaises(updater.UpdateError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, "update_preview")

    def test_symlink_target_and_package_are_rejected(self):
        for key in ("target_path", "package_path"):
            with self.subTest(key=key):
                link = self.root / (key + ".app")
                link.symlink_to(self.payload[key])
                with self.assertRaises(updater.UpdateError):
                    updater.prepare(dict(self.payload, **{key: str(link)}), runner=self.tools)
        self.assertFalse(self.tools.calls)

    def test_wrong_dmg_signature_never_mounts(self):
        self.tools.signature = "d" * 64
        with self.assertRaisesRegex(updater.UpdateError, "DMG 签名"):
            self.prepare()
        self.assertFalse(any("attach" in argv for argv in self.tools.calls))

    def test_writable_directory_named_volumes_is_not_mistaken_for_readonly_image(self):
        directory = self.root / "Volumes" / "External Apps"
        directory.mkdir(parents=True)
        target = directory / "KaneoPilot.app"
        app(target)
        self.payload["target_path"] = str(target)
        job = self.prepare()
        self.assertEqual(Path(job["backup_path"]).parent.parent, directory)
        self.assertEqual(updater.bundle_info(target)["CFBundleShortVersionString"], "1.2.2")

    def test_detach_failure_reports_retained_mountpoint_without_touching_target(self):
        self.tools.detach_fails = True
        with self.assertRaisesRegex(updater.UpdateError, "保留挂载位置"):
            self.prepare()
        self.assertEqual(updater.target_snapshot(self.target), self.original)
        self.assertFalse(list(self.root.glob(".KaneoPilot-backup-*")))

    def test_internal_bundle_identity_failures_detach_and_remove_staging(self):
        for attribute, value in (("mounted_version", "1.2.4"), ("mounted_build", "42"),
                                 ("mounted_id", "other.application"), ("wrong_mount", True)):
            with self.subTest(attribute=attribute):
                self.tools = Tools()
                setattr(self.tools, attribute, value)
                with self.assertRaises(updater.UpdateError):
                    self.prepare()
                self.assertEqual(updater.target_snapshot(self.target), self.original)
                self.assertFalse(list(self.root.glob(".KaneoPilot-backup-*")))
                self.assertTrue(any("detach" in argv for argv in self.tools.calls))

    def test_installer_waits_for_exit_then_swaps_and_keeps_old_app(self):
        job = self.prepare()
        self.commit(job)
        live = iter((True, True, False))
        def alive(_):
            self.assertEqual(updater.target_snapshot(self.target), self.original)
            return next(live)
        with patch.object(updater.time, "sleep"):
            result = updater.install(Path(job["job_path"]), 1234567, runner=self.tools,
                                     alive=alive, exchange=portable_exchange)
        self.assertEqual(result["status"], "launch_requested")
        self.assertEqual(updater.bundle_info(self.target)["CFBundleShortVersionString"], "1.2.3")
        self.assertEqual(updater.target_snapshot(Path(job["backup_path"])), self.original)
        with self.assertRaisesRegex(updater.UpdateError, "已执行"):
            self.install(job)
        self.assertEqual(updater.bundle_info(self.target)["CFBundleShortVersionString"], "1.2.3")

    def test_timeout_leaves_running_app_untouched(self):
        job = self.prepare()
        with self.assertRaisesRegex(updater.UpdateError, "尚未退出"):
            updater.install(Path(job["job_path"]), 1234567, runner=self.tools,
                            alive=lambda _: True, exchange=portable_exchange, wait_seconds=0)
        self.assertEqual(updater.target_snapshot(self.target), self.original)
        self.assertEqual(self.result(job)["status"], "failed")
        self.assertFalse(any(Path(argv[0]).name == "open" for argv in self.tools.calls))

    def test_target_changed_after_prepare_is_not_overwritten_or_relaunched(self):
        job = self.prepare()
        info = self.target / "Contents/Info.plist"
        info.write_bytes(info.read_bytes() + b"\n")
        modified = updater.target_snapshot(self.target)
        with self.assertRaisesRegex(updater.UpdateError, "发生变化"):
            self.install(job)
        self.assertEqual(updater.target_snapshot(self.target), modified)
        self.assertFalse(any(Path(argv[0]).name == "open" for argv in self.tools.calls))

    def test_changed_staged_signature_does_not_replace_target(self):
        job = self.prepare()
        self.tools.fail_stage_verify = True
        with self.assertRaisesRegex(updater.UpdateError, "signature invalid"):
            self.install(job)
        self.assertEqual(updater.target_snapshot(self.target), self.original)
        self.assertFalse(self.result(job)["restored"])

    def test_other_running_instance_blocks_exchange_without_launching_another(self):
        job = self.prepare()
        self.tools.processes = ("4820 " + str(self.target / "Contents/MacOS/KaneoPilot") + "\n").encode()
        with self.assertRaisesRegex(updater.UpdateError, "还有 KaneoPilot"):
            self.install(job, busy_wait_seconds=0)
        self.assertEqual(updater.target_snapshot(self.target), self.original)
        self.assertFalse(any(Path(argv[0]).name == "open" for argv in self.tools.calls))

    def add_runtime(self):
        """A stand-in for the app's bundled Python; returns the interpreter entry inside the target."""
        runtime = self.target / "Contents/Resources/python"
        (runtime / "bin").mkdir(parents=True)
        (runtime / "lib").mkdir()
        (runtime / "lib/stdlib.py").write_text("value = 1\n")
        (runtime / "bin/python3.13").write_bytes(b"#!/bin/sh\nexit 0\n")
        (runtime / "bin/python3.13").chmod(0o755)
        (runtime / "bin/python3").symlink_to("python3.13")
        return runtime / "bin/python3"

    def prepare_bundled(self, interpreter):
        return updater.prepare(self.payload, runner=self.tools, interpreter=str(interpreter))

    def test_bundled_interpreter_is_cloned_next_to_the_job_and_proven_to_start(self):
        interpreter = self.add_runtime()
        job = self.prepare_bundled(interpreter)
        clone = Path(job["job_path"]).parent / "runtime"
        clone_command = ["/bin/cp", "-c", "-p", "-R", str(self.target / "Contents/Resources/python"), str(clone)]
        probe = [job["python_path"], "-c", updater.RUNTIME_PROBE]
        self.assertEqual(job["python_path"], str(clone / "bin/python3"))
        self.assertEqual(os.readlink(clone / "bin/python3"), "python3.13")
        self.assertEqual((clone / "lib/stdlib.py").read_text(), "value = 1\n")
        self.assertIn(clone_command, self.tools.calls)
        self.assertIn(probe, self.tools.calls)
        self.assertLess(self.tools.calls.index(clone_command), self.tools.calls.index(probe))
        # The helper never runs from inside the app directory it is going to swap away.
        self.assertFalse(job["python_path"].startswith(str(self.target) + "/"))
        self.assertEqual(updater.target_snapshot(self.target), self.original)
        self.assertEqual(updater.bundle_info(Path(job["backup_path"]))["CFBundleShortVersionString"], "1.2.3")

    def test_interpreter_outside_the_target_is_used_as_is(self):
        job = self.prepare()
        self.assertEqual(job["python_path"], sys.executable)
        self.assertFalse(any(Path(argv[0]).name == "cp" for argv in self.tools.calls))
        self.assertFalse((Path(job["job_path"]).parent / "runtime").exists())

    def test_interpreter_reached_through_a_symlink_into_the_runtime_is_still_cloned(self):
        self.add_runtime()
        outside = self.root / "elsewhere"
        outside.mkdir()
        (outside / "python3").symlink_to(self.target / "Contents/Resources/python/bin/python3.13")
        job = self.prepare_bundled(outside / "python3")
        self.assertEqual(job["python_path"], str(Path(job["job_path"]).parent / "runtime/bin/python3.13"))

    def test_failed_clone_or_probe_stops_prepare_before_the_app_quits(self):
        interpreter = self.add_runtime()
        for attribute in ("fail_clone", "fail_runtime_probe"):
            with self.subTest(attribute=attribute):
                self.tools = Tools()
                setattr(self.tools, attribute, True)
                with self.assertRaises(updater.UpdateError):
                    self.prepare_bundled(interpreter)
                self.assertEqual(updater.target_snapshot(self.target), self.original)
                self.assertFalse(list(self.root.glob(".KaneoPilot-backup-*")))
                self.assertFalse(list((self.root / "cache").glob("install-*/runtime")))

    def test_next_prepare_drops_runtime_clones_of_jobs_that_have_already_run(self):
        interpreter = self.add_runtime()
        statuses = ("prepared", "waiting_for_exit", "installing", "launch_requested", "failed",
                    "launch_unconfirmed", "recovery_required")
        jobs = {status: Path(self.prepare_bundled(interpreter)["job_path"]).parent for status in statuses}
        unreadable = Path(self.prepare_bundled(interpreter)["job_path"]).parent
        for status, root in jobs.items():
            updater.write_json(root / "result.json", {"status": status})
        (unreadable / "result.json").write_text("not json")
        latest = Path(self.prepare_bundled(interpreter)["job_path"]).parent
        for status, root in jobs.items():
            self.assertEqual((root / "runtime").exists(), status in ("prepared", "waiting_for_exit", "installing"), status)
            self.assertTrue((root / "result.json").is_file(), status)  # only the clone goes, never the record
        self.assertTrue((unreadable / "runtime").exists())
        self.assertTrue((latest / "runtime").exists())

    def leftover(self, pid=4821):
        return (str(pid) + " " + str(self.target / "Contents/Resources/python/bin/python3") + "\n").encode()

    @staticmethod
    def fake_time():
        now = [0.0]
        return now, (lambda seconds: now.__setitem__(0, now[0] + seconds)), (lambda: now[0])

    def test_leftover_worker_gets_a_bounded_time_to_exit_before_the_swap(self):
        job = self.prepare()
        self.tools.process_outputs = [self.leftover(), self.leftover(), b""]
        now, sleep, clock = self.fake_time()
        result = self.install(job, busy_wait_seconds=20, sleep=sleep, clock=clock)
        self.assertEqual(result["status"], "launch_requested")
        self.assertEqual(updater.bundle_info(self.target)["CFBundleShortVersionString"], "1.2.3")
        self.assertTrue(0 < now[0] < 20, now[0])

    def test_leftover_worker_that_stays_is_refused_after_the_wait_and_the_old_app_is_reopened(self):
        job = self.prepare()
        self.tools.processes = self.leftover()
        now, sleep, clock = self.fake_time()
        with self.assertRaisesRegex(updater.UpdateError, "还有 KaneoPilot.*4821") as caught:
            self.install(job, busy_wait_seconds=20, sleep=sleep, clock=clock)
        self.assertEqual(caught.exception.code, "update_busy")
        self.assertTrue(20 <= now[0] < 21, now[0])  # waited the whole period, and not longer
        self.assertEqual(updater.target_snapshot(self.target), self.original)
        self.assertEqual([argv for argv in self.tools.calls if Path(argv[0]).name == "open"],
                         [["/usr/bin/open", "-n", str(self.target)]])
        recorded = self.result(job)
        self.assertEqual((recorded["status"], recorded["restored"]), ("failed", False))
        self.assertEqual(recorded["python"], sys.executable)

    def test_bundle_processes_match_the_bundle_prefix_and_skip_the_helper(self):
        lines = ["100 %s/Contents/MacOS/KaneoPilot" % self.target,
                 "200 %s/Contents/Resources/python/bin/python3" % self.target,
                 "%d %s/Contents/Resources/python/bin/python3" % (os.getpid(), self.target),
                 "300 %s.backup/Contents/MacOS/KaneoPilot" % self.target,  # same prefix, another directory
                 "400 /usr/bin/python3",
                 "x500 %s/Contents/MacOS/KaneoPilot" % self.target]
        output = ("\n".join(lines) + "\n").encode()
        self.assertEqual(updater.bundle_processes(self.target, lambda argv: (output, b"")), [100, 200])

    def test_results_record_which_interpreter_ran_the_helper(self):
        job = self.prepare()
        result = self.install(job)
        self.assertEqual(result["python"], sys.executable)
        self.assertEqual(self.result(job)["python"], sys.executable)

    def test_launch_failure_restores_old_app_and_requests_its_relaunch(self):
        job = self.prepare()
        self.tools.fail_new_open = True
        with self.assertRaisesRegex(updater.UpdateError, "LaunchServices"):
            self.install(job)
        self.assertEqual(updater.target_snapshot(self.target), self.original)
        self.assertTrue(self.result(job)["restored"])
        self.assertEqual(len([argv for argv in self.tools.calls if Path(argv[0]).name == "open"]), 2)

    def test_successful_launch_is_never_rolled_back_when_final_receipt_fails(self):
        job = self.prepare()
        original_write = updater.write_json
        def no_final_receipt(path, value):
            if value.get("status") == "launch_requested":
                raise OSError("cache is full")
            original_write(path, value)
        with patch.object(updater, "write_json", side_effect=no_final_receipt):
            result = self.install(job)
        self.assertEqual(result["status"], "launch_requested")
        self.assertIn("cache is full", result["receipt_error"])
        self.assertEqual(updater.bundle_info(self.target)["CFBundleShortVersionString"], "1.2.3")
        self.assertEqual(updater.target_snapshot(Path(job["backup_path"])), self.original)

    def test_launch_timeout_retains_new_target_and_old_backup_for_review(self):
        job = self.prepare()
        self.tools.open_timeout = True
        with self.assertRaisesRegex(updater.UpdateError, "启动结果待核实"):
            self.install(job)
        self.assertEqual(self.result(job)["status"], "launch_unconfirmed")
        self.assertEqual(updater.bundle_info(self.target)["CFBundleShortVersionString"], "1.2.3")
        self.assertEqual(updater.target_snapshot(Path(job["backup_path"])), self.original)

    def test_launch_failure_with_new_process_never_exchanges_running_bundle_back(self):
        job = self.prepare()
        self.tools.fail_new_open = True
        self.tools.running_after_failure = True
        with self.assertRaisesRegex(updater.UpdateError, "启动结果待核实"):
            self.install(job)
        self.assertEqual(self.result(job)["status"], "launch_unconfirmed")
        self.assertEqual(updater.bundle_info(self.target)["CFBundleShortVersionString"], "1.2.3")
        self.assertEqual(updater.target_snapshot(Path(job["backup_path"])), self.original)

    def test_swap_failure_preserves_target_and_reopens_it(self):
        job = self.prepare()
        self.commit(job)
        def denied(*_):
            raise PermissionError("volume does not support atomic exchange")
        with self.assertRaises(PermissionError):
            updater.install(Path(job["job_path"]), 1234567, runner=self.tools,
                            alive=lambda _: False, exchange=denied)
        self.assertEqual(updater.target_snapshot(self.target), self.original)
        self.assertEqual(self.result(job)["status"], "failed")

    def test_failed_rollback_reports_retained_recovery_path(self):
        job = self.prepare()
        self.commit(job)
        self.tools.fail_new_open = True
        count = [0]
        def exchange(left, right):
            count[0] += 1
            if count[0] == 2:
                raise OSError("volume unavailable")
            portable_exchange(left, right)
        with self.assertRaisesRegex(updater.UpdateError, "需从备份恢复"):
            updater.install(Path(job["job_path"]), 1234567, runner=self.tools,
                            alive=lambda _: False, exchange=exchange)
        result = self.result(job)
        self.assertEqual(result["status"], "recovery_required")
        self.assertEqual(updater.target_snapshot(Path(result["backup_path"])), self.original)

    def test_missing_or_wrong_exit_commit_never_replaces_or_reopens_app(self):
        for commit in (None, {"parent_pid": 999, "job_path": "another-job"}):
            with self.subTest(commit=commit):
                job = self.prepare()
                if commit is not None:
                    updater.write_json(Path(job["job_path"]).parent / "commit.json", commit)
                with self.assertRaisesRegex(updater.UpdateError, "安装.*确认"):
                    updater.install(Path(job["job_path"]), 1234567, runner=self.tools,
                                    alive=lambda _: False, exchange=portable_exchange)
                self.assertEqual(updater.target_snapshot(self.target), self.original)
                self.assertFalse(any(Path(argv[0]).name == "open" for argv in self.tools.calls))

    @unittest.skipUnless(sys.platform == "darwin", "macOS atomic exchange API")
    def test_native_atomic_exchange_preserves_both_bundles(self):
        candidate = self.root / "New.app"
        app(candidate, "1.2.3", "41")
        updater.exchange_bundles(self.target, candidate)
        self.assertEqual(updater.bundle_info(self.target)["CFBundleShortVersionString"], "1.2.3")
        self.assertEqual(updater.target_snapshot(candidate), self.original)
        updater.exchange_bundles(self.target, candidate)
        self.assertEqual(updater.target_snapshot(self.target), self.original)

    def test_cli_reports_machine_readable_failure_without_touching_app(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "prepare", "--json", "{}"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["code"], "update_invalid_package")
        self.assertEqual(updater.target_snapshot(self.target), self.original)

    def test_detached_helper_handshakes_and_cannot_install_without_exit_commit(self):
        job = self.prepare()
        parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        helper = subprocess.Popen([sys.executable, job["helper_path"], "install", "--job", job["job_path"],
                                   "--parent-pid", str(parent.pid)],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while self.result(job)["status"] == "prepared" and time.monotonic() < deadline and helper.poll() is None:
                time.sleep(0.02)
            self.assertEqual(self.result(job)["status"], "waiting_for_exit")
            self.assertEqual(self.result(job)["python"], sys.executable)
            self.assertEqual(updater.target_snapshot(self.target), self.original)
            parent.terminate()
            parent.wait(timeout=5)
            stdout, stderr = helper.communicate(timeout=5)
            self.assertEqual(helper.returncode, 1, stdout + stderr)
            self.assertEqual(json.loads(stdout)["code"], "update_cancelled")
            self.assertEqual(updater.target_snapshot(self.target), self.original)
            self.assertEqual(self.result(job)["status"], "failed")
        finally:
            for process in (helper, parent):
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
