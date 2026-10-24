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
            return self.processes, b""
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
        package = self.root / "KaneoPilot-1.2.3-macos-universal.dmg"
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
            self.install(job)
        self.assertEqual(updater.target_snapshot(self.target), self.original)
        self.assertFalse(any(Path(argv[0]).name == "open" for argv in self.tools.calls))

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
