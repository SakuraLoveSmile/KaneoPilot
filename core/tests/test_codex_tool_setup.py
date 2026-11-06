"""Task-private offline cache seeds, using only isolated filesystem fixtures."""
import copy
import hashlib
import os
import stat
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from kanpilot import codex_tool_setup as setup, util


class CodexToolSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="kp-tool-setup-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.cwd = self.root / "task"
        self.cwd.mkdir()
        self.pub = self.root / "host-pub"
        self.pub.mkdir()
        self.gradle = self.root / "host-gradle"
        self.gradle.mkdir()
        self.private = self.cwd / ".pi/kanpilot/codex/run-1"
        self.spec = {"cwd": str(self.cwd), "run_id": "run-1", "tool_environment": {
            "v": 2, "environment": {"PUB_CACHE": str(self.private / "pub-cache"),
                                      "GRADLE_USER_HOME": str(self.private / "gradle")},
            "setup": {"flutter": str(self.root / "flutter/bin/flutter"),
                      "pub_cache_source": str(self.pub), "gradle_cache_source": str(self.gradle),
                      "pub_offline": True, "android": False}}}
        self.packages = {}
        self.write_lock()

    def write_lock(self):
        (self.cwd / "pubspec.lock").write_text(util.yaml.safe_dump({"packages": self.packages}), encoding="utf-8")

    def hosted(self, name="needed", version="1.2.3", url="https://pub.dev"):
        self.packages[name] = {"version": version, "source": "hosted", "description": {"name": name, "url": url}}
        self.write_lock()
        host = setup._hosted_directory(url)
        directory = self.pub / "hosted" / host / (name + "-" + version)
        directory.mkdir(parents=True)
        (directory / "pubspec.yaml").write_text("name: " + name + "\nversion: " + version + "\n")
        metadata = directory.parent / ".cache"
        metadata.mkdir(exist_ok=True)
        (metadata / (name + "-versions.json")).write_text('{"versions":[]}')
        (metadata / "unrelated-versions.json").write_text("must not copy")
        hashes = self.pub / "hosted-hashes" / host
        hashes.mkdir(parents=True, exist_ok=True)
        (hashes / (name + "-" + version + ".sha256")).write_text("a" * 64)
        return directory

    def git(self):
        name, url, revision = "feedback", "https://github.com/example/feedback.git", "a" * 40
        self.packages["feedback"] = {"version": "0.8.6", "source": "git",
                                     "description": {"url": url, "resolved-ref": revision, "path": "flutter/feedback"}}
        self.write_lock()
        checkout = self.pub / "git" / (name + "-" + revision)
        checkout.mkdir(parents=True)
        (checkout / "pubspec.yaml").write_text("name: feedback\n")
        (checkout / ".git").mkdir()
        (checkout / ".git/pub-packages").write_text("flutter/feedback\n")
        cache = self.pub / "git/cache" / (name + "-" + hashlib.sha1(url.encode()).hexdigest())
        (cache / "objects/ab").mkdir(parents=True)
        (cache / "objects/ab" / ("c" * 38)).write_bytes(b"object")
        (cache / "refs/heads").mkdir(parents=True)
        (cache / "refs/heads/main").write_text(revision + "\n")
        (cache / "HEAD").write_text("ref: refs/heads/main\n")
        (cache / "packed-refs").write_text("# pack-refs with: peeled fully-peeled\n")
        (cache / "config").write_text('[include]\npath = /secret/config\n[credential]\nhelper = !dangerous\n')
        (cache / "hooks").mkdir()
        (cache / "hooks/post-checkout").write_text("do not copy")
        return checkout, cache

    def gradle_distribution(self):
        self.spec["tool_environment"]["setup"]["android"] = True
        wrapper = self.cwd / "android/gradle/wrapper"
        wrapper.mkdir(parents=True)
        url = "https://services.gradle.org/distributions/gradle-9.3.1-all.zip"
        (wrapper / "gradle-wrapper.properties").write_text(
            "distributionBase=GRADLE_USER_HOME\ndistributionPath=wrapper/dists\n"
            "zipStoreBase=GRADLE_USER_HOME\nzipStorePath=wrapper/dists\n"
            "distributionUrl=https\\://services.gradle.org/distributions/gradle-9.3.1-all.zip\n")
        hash_value = setup._base36(int.from_bytes(hashlib.md5(url.encode()).digest(), "big"))
        self.assertEqual(hash_value, "9ot9r568e8zfvvd4mn8rbu1j0")
        cache = self.gradle / "wrapper/dists/gradle-9.3.1-all" / hash_value
        expanded = cache / "gradle-9.3.1"
        (expanded / "bin").mkdir(parents=True)
        (expanded / "bin/gradle").write_text("#!/bin/sh\n")
        (expanded / "bin/gradle").chmod(0o755)
        (expanded / "lib").mkdir()
        (expanded / "lib/gradle-launcher-9.3.1.jar").write_bytes(b"runtime")
        (expanded / "docs").mkdir()
        (expanded / "docs/not-needed").write_bytes(b"docs")
        (expanded / "init.d").mkdir()
        (expanded / "init.d/host-script.gradle").write_bytes(b"do not copy")
        (cache / "gradle-9.3.1-all.zip.ok").write_bytes(b"")
        (cache / "gradle-9.3.1-all.zip.lck").write_bytes(b"host lock")
        (cache / "gradle-9.3.1-all.zip").write_bytes(b"archive")
        (self.gradle / "gradle.properties").write_text("password=secret\n")
        (self.gradle / "init.d").mkdir()
        (self.gradle / "init.d/host.gradle").write_text("do not copy")
        return cache

    def fingerprint(self, root):
        return sorted((str(path.relative_to(root)), stat.S_IMODE(path.lstat().st_mode),
                       os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else None)
                      for path in root.rglob("*"))

    def test_unrestricted_v3_keeps_private_seed_and_offline_setup_contract(self):
        self.spec["tool_environment"]["v"] = 3
        package = self.hosted()
        self.gradle_distribution()
        before = (self.fingerprint(self.pub), self.fingerprint(self.gradle))
        receipt = setup.seed_caches(self.spec)
        self.assertEqual(receipt, {"pub_packages": 1, "git_checkouts": 0, "gradle_distributions": 1})
        self.assertEqual((self.private / "pub-cache/hosted/pub.dev/needed-1.2.3").resolve(), package)
        self.assertEqual(setup.setup_argv(self.spec), [self.spec["tool_environment"]["setup"]["flutter"],
                                                    "pub", "get", "--offline", "--enforce-lockfile"])
        self.assertEqual((self.fingerprint(self.pub), self.fingerprint(self.gradle)), before)

    def gradle_compatibility_fixture(self, version="9.3.1"):
        cached = self.gradle_distribution()
        sdk = self.root / "flutter"
        plugin = sdk / "packages/flutter_tools/gradle"
        plugin.mkdir(parents=True)
        android = self.cwd / "android"
        (android / "local.properties").write_text("flutter.sdk=" + str(sdk) + "\n")
        (android / "settings.gradle.kts").write_text('''
pluginManagement {
    val flutterSdkPath = run {
        val properties = java.util.Properties()
        file("local.properties").inputStream().use { properties.load(it) }
        val flutterSdkPath = properties.getProperty("flutter.sdk")
        require(flutterSdkPath != null) { "flutter.sdk not set in local.properties" }
        flutterSdkPath
    }
    includeBuild("$flutterSdkPath/packages/flutter_tools/gradle")
    repositories { google() mavenCentral() gradlePluginPortal() }
}
plugins {
    id("dev.flutter.flutter-plugin-loader") version "1.0.0"
    id("com.android.application") version "8.11.1" apply false
    id("org.jetbrains.kotlin.android") version "2.2.20" apply false
}
include(":app")
''')
        jar = cached / "gradle-9.3.1/lib/gradle-base-services-9.3.1.jar"
        with zipfile.ZipFile(jar, "w") as archive:
            archive.writestr("org/gradle/build-receipt.properties", "versionNumber=" + version + "\n")
        setup.seed_caches(self.spec)
        private_jar = self.private / "gradle" / jar.relative_to(self.gradle)
        return plugin, private_jar

    def test_gradle_sdk_requirement_uses_selected_jar_receipt_not_distribution_name(self):
        plugin, jar = self.gradle_compatibility_fixture()
        expected = {"gradle_version": "9.3.1", "project_root": str(plugin)}
        self.assertEqual(setup.android_gradle_sdk_requirement(self.spec), expected)
        for version, applicable in (("8.14", False), ("8.14.5", False), ("9.0.0", True),
                                    ("10.0.1", True), ("9.0.0-rc-1", False),
                                    ("9.3.1-SNAPSHOT", False), ("unknown", False)):
            with zipfile.ZipFile(jar, "w") as archive:
                archive.writestr("org/gradle/build-receipt.properties", "versionNumber=" + version + "\n")
            with self.subTest(version=version):
                actual = setup.android_gradle_sdk_requirement(self.spec)
                self.assertEqual(actual, {**expected, "gradle_version": version} if applicable else None)
        # A missing/invalid receipt is unknown, even though the URL says 9.3.1.
        jar.write_bytes(b"not a ZIP")
        self.assertIsNone(setup.android_gradle_sdk_requirement(self.spec))

    def test_gradle_sdk_requirement_confirms_standard_settings_and_matching_sdk(self):
        plugin, jar = self.gradle_compatibility_fixture()
        path = self.cwd / "android/settings.gradle.kts"
        original = path.read_text()
        for source in ("// " + original.replace("\n", "\n// "),
                       original + '\nflutterSdkPath = "different"\n',
                       original.replace('includeBuild("$flutterSdkPath/packages/flutter_tools/gradle")',
                                        '// includeBuild("$flutterSdkPath/packages/flutter_tools/gradle")'),
                       original.replace('file("local.properties")', 'file("other.properties")')):
            path.write_text(source)
            with self.subTest(source=source):
                self.assertIsNone(setup.android_gradle_sdk_requirement(self.spec))
        path.write_text("// Standard Flutter project\n" + original)
        self.assertIsNotNone(setup.android_gradle_sdk_requirement(self.spec))
        local = self.cwd / "android/local.properties"
        local.write_text("flutter.sdk=/other/sdk\n")
        self.assertIsNone(setup.android_gradle_sdk_requirement(self.spec))
        local.unlink()
        self.assertIsNone(setup.android_gradle_sdk_requirement(self.spec))
        for version in (1, 3):
            self.spec["tool_environment"]["v"] = version
            self.assertIsNone(setup.android_gradle_sdk_requirement(self.spec))

    def test_gradle_sdk_requirement_supports_standard_groovy_without_custom_logic(self):
        plugin, jar = self.gradle_compatibility_fixture()
        (self.cwd / "android/settings.gradle.kts").unlink()
        (self.cwd / "android/settings.gradle").write_text('''
pluginManagement {
    def flutterSdkPath = {
        def properties = new Properties()
        file("local.properties").withInputStream { properties.load(it) }
        def flutterSdkPath = properties.getProperty("flutter.sdk")
        assert flutterSdkPath != null, "flutter.sdk not set in local.properties"
        return flutterSdkPath
    }()
    includeBuild("$flutterSdkPath/packages/flutter_tools/gradle")
    repositories { google() mavenCentral() gradlePluginPortal() }
}
plugins {
    id "dev.flutter.flutter-plugin-loader" version "1.0.0"
    id "com.android.application" version "8.11.1" apply false
    id "org.jetbrains.kotlin.android" version "2.2.20" apply false
}
include ":app"
''')
        self.assertEqual(setup.android_gradle_sdk_requirement(self.spec),
                         {"gradle_version": "9.3.1", "project_root": str(plugin)})

    def test_ambiguous_or_duplicate_gradle_versions_remain_unknown(self):
        plugin, jar = self.gradle_compatibility_fixture()
        other = jar.with_name("gradle-base-services-other.jar")
        with zipfile.ZipFile(other, "w") as archive:
            archive.writestr("org/gradle/build-receipt.properties", "versionNumber=8.14\n")
        self.assertIsNone(setup.android_gradle_sdk_requirement(self.spec))
        other.unlink()
        with zipfile.ZipFile(jar, "w") as archive:
            archive.writestr("org/gradle/build-receipt.properties", "versionNumber=9.3.1\nversionNumber=8.14\n")
        self.assertIsNone(setup.android_gradle_sdk_requirement(self.spec))

    def test_standard_settings_and_local_properties_are_bounded_regular_files(self):
        self.gradle_compatibility_fixture()
        for name in ("settings.gradle.kts", "local.properties"):
            path = self.cwd / "android" / name
            saved = path.with_suffix(path.suffix + ".saved")
            path.rename(saved)
            path.symlink_to(saved)
            try:
                with self.subTest(name=name), self.assertRaises(util.KanpilotError):
                    setup.android_gradle_sdk_requirement(self.spec)
            finally:
                path.unlink()
            path.write_bytes(b"x" * 65537)
            with self.subTest(name=name, oversized=True), self.assertRaises(util.KanpilotError):
                setup.android_gradle_sdk_requirement(self.spec)
            path.unlink()
            saved.rename(path)

    def test_gradle_version_receipt_rejects_symlink_special_and_oversized_metadata(self):
        plugin, jar = self.gradle_compatibility_fixture()
        saved = jar.with_suffix(".saved")
        jar.rename(saved)
        jar.symlink_to(saved)
        try:
            with self.assertRaises(util.KanpilotError):
                setup.android_gradle_sdk_requirement(self.spec)
        finally:
            jar.unlink()
        os.mkfifo(jar)
        try:
            with self.assertRaises(util.KanpilotError):
                setup.android_gradle_sdk_requirement(self.spec)
        finally:
            jar.unlink()
        for size in (65537,):
            with zipfile.ZipFile(jar, "w") as archive:
                archive.writestr("org/gradle/build-receipt.properties", b"x" * size)
            with self.assertRaises(util.KanpilotError):
                setup.android_gradle_sdk_requirement(self.spec)
        with zipfile.ZipFile(jar, "w") as archive:
            entry = zipfile.ZipInfo("org/gradle/build-receipt.properties")
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(entry, "versionNumber=9.3.1\n")
        with self.assertRaises(util.KanpilotError):
            setup.android_gradle_sdk_requirement(self.spec)
        jar.write_bytes(b"x")
        with jar.open("r+b") as stream:
            stream.truncate(16 * 1024 * 1024 + 1)
        with self.assertRaises(util.KanpilotError):
            setup.android_gradle_sdk_requirement(self.spec)

    def test_only_locked_hosted_packages_are_linked_and_metadata_is_private(self):
        package = self.hosted()
        unrelated = package.parent / "unrelated-9.0.0"
        unrelated.mkdir()
        (unrelated / "secret").write_text("not needed")
        before = self.fingerprint(self.pub)
        receipt = setup.seed_caches(self.spec)
        self.assertEqual(receipt, {"pub_packages": 1, "git_checkouts": 0, "gradle_distributions": 0})
        target = self.private / "pub-cache/hosted/pub.dev/needed-1.2.3"
        self.assertTrue(target.is_symlink())
        self.assertEqual(target.resolve(), package)
        self.assertFalse(target.with_name("unrelated-9.0.0").exists())
        metadata = target.parent / ".cache/needed-versions.json"
        self.assertFalse(metadata.is_symlink())
        self.assertEqual(metadata.read_text(), '{"versions":[]}')
        self.assertFalse(metadata.with_name("unrelated-versions.json").exists())
        self.assertEqual((self.private / "pub-cache/hosted-hashes/pub.dev/needed-1.2.3.sha256").read_text(), "a" * 64)
        self.assertEqual(before, self.fingerprint(self.pub))
        self.assertEqual(setup.seed_caches(self.spec), receipt)
        self.assertEqual(before, self.fingerprint(self.pub))

    def test_custom_host_url_uses_pub_compatible_decimal_encoding(self):
        url = "https://packages.example.com:8443/pub/"
        package = self.hosted(url=url)
        self.assertEqual(package.parent.name, "packages.example.com%588443%47pub%47")
        self.assertEqual(setup._hosted_directory("http://127.0.0.1:1234"), "localhost%581234")
        self.assertEqual(setup._hosted_directory("http://[::1]:1234"), "localhost%581234")
        self.assertEqual(setup._hosted_directory("http://example.com"), "http%58%47%47example.com")
        setup.seed_caches(self.spec)
        self.assertTrue((self.private / "pub-cache/hosted" / package.parent.name / package.name).is_symlink())

    def test_only_locked_git_data_is_seeded_without_host_config_or_hooks(self):
        checkout, cache = self.git()
        before = self.fingerprint(self.pub)
        receipt = setup.seed_caches(self.spec)
        self.assertEqual(receipt["git_checkouts"], 1)
        self.assertEqual((self.private / "pub-cache/git" / checkout.name).resolve(), checkout)
        copied = self.private / "pub-cache/git/cache" / cache.name
        self.assertEqual((copied / "objects/ab" / ("c" * 38)).read_bytes(), b"object")
        self.assertEqual((copied / "HEAD").read_text(), "ref: refs/heads/main\n")
        self.assertIn('url = "https://github.com/example/feedback.git"', (copied / "config").read_text())
        self.assertNotIn("credential", (copied / "config").read_text())
        self.assertNotIn("/secret", (copied / "config").read_text())
        self.assertFalse((copied / "hooks").exists())
        self.assertEqual(before, self.fingerprint(self.pub))
        self.assertEqual(setup.seed_caches(self.spec), receipt)

    def test_gradle_runtime_is_private_writable_and_host_settings_are_not_copied(self):
        source = self.gradle_distribution()
        before = self.fingerprint(self.gradle)
        receipt = setup.seed_caches(self.spec)
        self.assertEqual(receipt["gradle_distributions"], 1)
        target = self.private / "gradle" / source.relative_to(self.gradle)
        launcher = target / "gradle-9.3.1/bin/gradle"
        self.assertFalse(launcher.is_symlink())
        self.assertEqual(stat.S_IMODE(launcher.stat().st_mode), 0o755)
        self.assertEqual((target / "gradle-9.3.1-all.zip.lck").read_bytes(), b"")
        launcher.write_text("private runtime change")
        (target / "gradle-9.3.1-all.zip.lck").write_text("private lock")
        self.assertFalse((target / "gradle-9.3.1/docs").exists())
        self.assertFalse((target / "gradle-9.3.1/init.d").exists())
        self.assertFalse((target / "gradle-9.3.1-all.zip").exists())
        self.assertFalse((self.private / "gradle/gradle.properties").exists())
        self.assertFalse((self.private / "gradle/init.d").exists())
        self.assertEqual(before, self.fingerprint(self.gradle))
        self.assertEqual(setup.seed_caches(self.spec), receipt)
        self.assertEqual(launcher.read_text(), "private runtime change")
        self.assertEqual(before, self.fingerprint(self.gradle))

    def test_old_git_checkout_missing_subpath_is_left_for_a_private_local_clone(self):
        checkout, cache = self.git()
        (checkout / ".git/pub-packages").write_text(".\n")
        before = self.fingerprint(self.pub)
        self.assertEqual(setup.seed_caches(self.spec)["git_checkouts"], 0)
        self.assertFalse((self.private / "pub-cache/git" / checkout.name).exists())
        self.assertTrue((self.private / "pub-cache/git/cache" / cache.name / "config").is_file())
        self.assertEqual(before, self.fingerprint(self.pub))

    def test_additional_git_subpath_removes_only_the_expected_private_link(self):
        checkout, _ = self.git()
        setup.seed_caches(self.spec)
        link = self.private / "pub-cache/git" / checkout.name
        self.assertTrue(link.is_symlink())
        self.packages["other"] = copy.deepcopy(self.packages["feedback"])
        self.packages["other"]["description"]["path"] = "other/package"
        self.write_lock()
        before = self.fingerprint(self.pub)
        self.assertEqual(setup.seed_caches(self.spec)["git_checkouts"], 0)
        self.assertFalse(link.exists())
        self.assertEqual(before, self.fingerprint(self.pub))

    def test_seeded_real_bare_repository_supports_local_clone_and_pinned_revision(self):
        repository = self.root / "source-repository"
        environment = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                           GIT_TERMINAL_PROMPT="0")
        def git(*arguments):
            return subprocess.run(["git", *arguments], env=environment, check=True,
                                  capture_output=True, text=True).stdout.strip()
        git("init", str(repository))
        (repository / "pubspec.yaml").write_text("name: feedback\n")
        git("-C", str(repository), "add", "pubspec.yaml")
        git("-C", str(repository), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "commit", "-m", "fixture")
        revision = git("-C", str(repository), "rev-parse", "HEAD")
        url = repository.as_uri()
        name = repository.name + "-" + hashlib.sha1(url.encode()).hexdigest()
        original = self.pub / "git/cache" / name
        original.parent.mkdir(parents=True)
        git("clone", "--mirror", str(repository), str(original))
        self.packages["feedback"] = {"source": "git", "version": "1.0.0",
                                     "description": {"url": url, "resolved-ref": revision, "path": "."}}
        self.write_lock()
        before = self.fingerprint(self.pub)
        setup.seed_caches(self.spec)
        bare = self.private / "pub-cache/git/cache" / name
        self.assertEqual(git("--git-dir=" + str(bare), "rev-parse", revision), revision)
        target = self.private / "pub-cache/git/feedback" / revision
        git("clone", "--no-checkout", "--", str(bare), str(target))
        git("-C", str(target), "checkout", revision)
        self.assertEqual((target / "pubspec.yaml").read_text(), "name: feedback\n")
        self.assertEqual(before, self.fingerprint(self.pub))

    def test_missing_gradle_distribution_fails_before_execution_with_version(self):
        cached = self.gradle_distribution()
        (cached / "gradle-9.3.1-all.zip.ok").unlink()
        with self.assertRaises(util.KanpilotError) as raised:
            setup.seed_caches(self.spec)
        self.assertEqual(raised.exception.code, "codex_environment_unavailable")
        self.assertIn("gradle-9.3.1-all", raised.exception.message)
        self.assertIn("缓存缺失", raised.exception.message)

    def test_no_pub_source_leaves_a_cold_private_cache_for_offline_native_error(self):
        self.spec["tool_environment"]["setup"]["pub_cache_source"] = str(self.root / "missing-pub-cache")
        self.assertEqual(setup.seed_caches(self.spec)["pub_packages"], 0)
        self.assertTrue((self.private / "pub-cache").is_dir())
        self.assertEqual(setup.setup_argv(self.spec), [self.spec["tool_environment"]["setup"]["flutter"],
                                                     "pub", "get", "--offline", "--enforce-lockfile"])

    def test_old_specs_and_non_native_new_spec_do_not_seed_or_set_up(self):
        for environment in (None, {"v": 1}, {"v": 2, "environment": {},
                                               "setup": {"pub_offline": False, "android": False}}):
            spec = copy.deepcopy(self.spec)
            if environment is None:
                spec.pop("tool_environment")
            else:
                spec["tool_environment"] = environment
            self.assertEqual(setup.seed_caches(spec), {"pub_packages": 0, "git_checkouts": 0, "gradle_distributions": 0})
            self.assertIsNone(setup.setup_argv(spec))
            self.assertFalse((self.private / "pub-cache").exists())

    def test_replaced_private_directory_components_and_destinations_are_rejected(self):
        self.hosted()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "sentinel").write_text("untouched")
        for relative in (".pi", ".pi/kanpilot", ".pi/kanpilot/codex", ".pi/kanpilot/codex/run-1",
                         ".pi/kanpilot/codex/run-1/pub-cache",
                         ".pi/kanpilot/codex/run-1/pub-cache/hosted",
                         ".pi/kanpilot/codex/run-1/pub-cache/hosted/pub.dev"):
            path = self.cwd / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(outside, target_is_directory=True)
            try:
                with self.subTest(path=relative), self.assertRaises(util.KanpilotError):
                    setup.seed_caches(self.spec)
                self.assertEqual(sorted(item.name for item in outside.iterdir()), ["sentinel"])
            finally:
                path.unlink()
        self.assertEqual((outside / "sentinel").read_text(), "untouched")

    def test_replaced_package_links_metadata_and_gradle_locks_do_not_write_outside(self):
        package = self.hosted()
        cached = self.gradle_distribution()
        setup.seed_caches(self.spec)
        outside = self.root / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_text("untouched")
        link = self.private / "pub-cache/hosted/pub.dev" / package.name
        link.unlink()
        link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(util.KanpilotError):
            setup.seed_caches(self.spec)
        link.unlink()
        link.symlink_to(package, target_is_directory=True)
        metadata = link.parent / ".cache/needed-versions.json"
        metadata.unlink()
        metadata.symlink_to(sentinel)
        with self.assertRaises(util.KanpilotError):
            setup.seed_caches(self.spec)
        metadata.unlink()
        setup.seed_caches(self.spec)
        lock = self.private / "gradle" / cached.relative_to(self.gradle) / "gradle-9.3.1-all.zip.lck"
        lock.unlink()
        lock.symlink_to(sentinel)
        with self.assertRaises(util.KanpilotError):
            setup.seed_caches(self.spec)
        self.assertEqual(sentinel.read_text(), "untouched")

    def test_host_source_symlink_and_git_alternates_are_rejected(self):
        checkout, cache = self.git()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "pubspec.yaml").write_text("name: outside\n")
        checkout.rename(checkout.with_name("original-checkout"))
        checkout.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(util.KanpilotError):
            setup.seed_caches(self.spec)
        checkout.unlink()
        checkout.with_name("original-checkout").rename(checkout)
        (cache / "objects/info").mkdir()
        (cache / "objects/info/alternates").write_text("/outside/object-store\n")
        with self.assertRaises(util.KanpilotError) as raised:
            setup.seed_caches(self.spec)
        self.assertIn("外部对象", raised.exception.message)

    def test_private_cache_environment_cannot_escape_run(self):
        for key in ("PUB_CACHE", "GRADLE_USER_HOME"):
            spec = copy.deepcopy(self.spec)
            spec["tool_environment"]["environment"][key] = str(self.root)
            with self.subTest(key=key), self.assertRaises(util.KanpilotError):
                setup.seed_caches(spec)

    def test_unsupported_gradle_layout_and_credential_urls_fail_explicitly(self):
        self.gradle_distribution()
        properties = self.cwd / "android/gradle/wrapper/gradle-wrapper.properties"
        original = properties.read_text()
        properties.write_text(original.replace("distributionBase=GRADLE_USER_HOME", "distributionBase=PROJECT"))
        with self.assertRaises(util.KanpilotError):
            setup.seed_caches(self.spec)
        properties.write_text(original.replace("https\\://services", "https\\://user:password@services"))
        with self.assertRaises(util.KanpilotError):
            setup.seed_caches(self.spec)
        self.spec["tool_environment"]["setup"]["android"] = False
        self.hosted(url="https://user:password@packages.example.com")
        with self.assertRaises(util.KanpilotError):
            setup.seed_caches(self.spec)


if __name__ == "__main__":
    unittest.main()
