"""Lock-file, download and unpack checks for the Mac App's Python runtime. No network, no real archive."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

ROOT = Path(__file__).parents[2]
SPEC = importlib.util.spec_from_file_location("fetch_python_runtime", ROOT / "scripts/fetch_python_runtime.py")
fetch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fetch)
LOCK_PATH = ROOT / "scripts/python-runtime.lock.json"
GOOD = (("python", "dir", None), ("python/bin", "dir", None), ("python/bin/python3.13", "exec", b"interpreter"),
        ("python/bin/python3", "sym", "python3.13"), ("python/lib", "dir", None),
        ("python/lib/python3.13", "dir", None), ("python/lib/python3.13/os.py", "file", b"# os"),
        ("python/lib/libpython3.13.dylib", "file", b"library"), ("python/bin/py", "hard", "python/bin/python3.13"))


def archive_bytes(members):
    """A .tar.gz from (name, kind, payload): dir, file, exec, sym, hard or fifo."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, kind, payload in members:
            info = tarfile.TarInfo(name)
            if kind in ("file", "exec"):
                info.size, info.mode = len(payload), 0o755 if kind == "exec" else 0o644
                tar.addfile(info, io.BytesIO(payload))
                continue
            info.type = {"dir": tarfile.DIRTYPE, "sym": tarfile.SYMTYPE, "hard": tarfile.LNKTYPE,
                         "fifo": tarfile.FIFOTYPE}[kind]
            info.mode = 0o755
            if kind in ("sym", "hard"):
                info.linkname = payload
            tar.addfile(info)
    return buffer.getvalue()


def lock_for(data, **changes):
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    lock.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
    lock.update(changes)
    return lock


class FakeResponse:
    def __init__(self, data, status=200):
        self.stream, self.status = io.BytesIO(data), status

    def read(self, size=-1):
        return self.stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, *outcomes):
        self.outcomes, self.requests = list(outcomes), []

    def open(self, request, timeout=None):
        self.requests.append((request.full_url, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, FakeResponse):
            return outcome
        return FakeResponse(outcome)


class LockTests(unittest.TestCase):
    def test_the_checked_in_lock_is_consistent(self):
        lock = fetch.load_lock(LOCK_PATH)
        self.assertTrue(lock["python_version"].startswith("3.13."))
        self.assertEqual(lock["asset"], "cpython-%s+%s-aarch64-apple-darwin-install_only_stripped.tar.gz"
                         % (lock["python_version"], lock["release_tag"]))
        self.assertIn("%2B", lock["url"])
        self.assertNotIn("+", lock["url"])
        self.assertEqual((lock["archs"], lock["minos_max"]), (["arm64"], "14.0"))
        self.assertEqual(len(lock["sha256"]), 64)

    def test_every_tampered_lock_is_refused(self):
        base = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        asset = base["asset"]
        variants = {
            "schema": {"schema": 2}, "source": {"source": "elsewhere"}, "repository": {"repository": "someone/else"},
            "tag": {"release_tag": "2026-10-26"}, "version": {"python_version": "3.13"},
            "intel asset": {"asset": asset.replace("aarch64", "x86_64")},
            "freethreaded": {"asset": asset.replace("install_only_stripped", "freethreaded+pgo-full")},
            "http": {"url": base["url"].replace("https://", "http://")},
            "host": {"url": base["url"].replace("github.com", "example.com")},
            "plus": {"url": base["url"].replace("%2B", "+")},
            "size zero": {"size": 0}, "size bool": {"size": True}, "size text": {"size": "1"},
            "size huge": {"size": 10 ** 12}, "sha upper": {"sha256": base["sha256"].upper()},
            "sha short": {"sha256": base["sha256"][:63]}, "universal archs": {"archs": ["arm64", "x86_64"]},
            "intel only": {"archs": ["x86_64"]}, "minos": {"minos_max": "14"}, "minos text": {"minos_max": "fourteen"}}
        with tempfile.TemporaryDirectory() as directory:
            for label, change in variants.items():
                with self.subTest(label=label):
                    path = Path(directory) / "lock.json"
                    path.write_text(json.dumps(dict(base, **change)), encoding="utf-8")
                    with self.assertRaises(fetch.FetchError):
                        fetch.load_lock(path)
            for label, mutate in (("missing field", lambda lock: lock.pop("sha256")),
                                  ("extra field", lambda lock: lock.update(extra=1))):
                with self.subTest(label=label):
                    lock = dict(base)
                    mutate(lock)
                    path = Path(directory) / "lock.json"
                    path.write_text(json.dumps(lock), encoding="utf-8")
                    with self.assertRaises(fetch.FetchError):
                        fetch.load_lock(path)
            (Path(directory) / "broken.json").write_text("{", encoding="utf-8")
            for name in ("broken.json", "absent.json"):
                with self.subTest(name=name), self.assertRaises(fetch.FetchError):
                    fetch.load_lock(Path(directory) / name)


class RedirectTests(unittest.TestCase):
    def hop(self, url):
        request = urllib.request.Request(fetch.load_lock(LOCK_PATH)["url"])
        return fetch.StrictRedirects().redirect_request(request, None, 302, "Found", {}, url)

    def test_only_the_release_cdn_over_https_is_followed(self):
        for url in ("https://release-assets.githubusercontent.com/github-production-release-asset/1?token=x",
                    "https://objects.githubusercontent.com/github-production-release-asset-2/2"):
            with self.subTest(url=url):
                self.assertEqual(self.hop(url).full_url, url)
        for url in ("http://release-assets.githubusercontent.com/x", "https://example.com/x",
                    "https://github.com/other/file", "https://github.com.evil.example/x",
                    "https://user:secret@release-assets.githubusercontent.com/x",
                    "https://release-assets.githubusercontent.com:8443/x",
                    "https://release-assets.githubusercontent.com.evil.example/x", "ftp://objects.githubusercontent.com/x"):
            with self.subTest(url=url), self.assertRaises(fetch.FetchError):
                self.hop(url)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.target = Path(self.temp.name) / "runtime.tar.gz"
        self.data = archive_bytes(GOOD)
        self.lock = lock_for(self.data)

    def test_a_matching_archive_is_saved(self):
        opener = FakeOpener(self.data)
        self.assertEqual(fetch.download(self.lock, self.target, opener=opener), self.target)
        self.assertEqual(self.target.read_bytes(), self.data)
        self.assertEqual([url for url, _ in opener.requests], [self.lock["url"]])
        self.assertEqual(opener.requests[0][1], fetch.TIMEOUT)

    def test_a_wrong_size_or_hash_is_deleted_and_fails(self):
        cases = {"short": self.data[:-1], "long": self.data + b"x", "same length, other bytes": b"x" * len(self.data)}
        for label, body in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(fetch.FetchError):
                    fetch.download(self.lock, self.target, opener=FakeOpener(body))
                self.assertFalse(self.target.exists())

    def test_a_transport_failure_is_retried_but_a_bad_archive_is_not(self):
        sleeps = []
        opener = FakeOpener(urllib.error.URLError("reset"), OSError("timed out"), self.data)
        fetch.download(self.lock, self.target, opener=opener, sleep=sleeps.append)
        self.assertEqual((len(opener.requests), sleeps), (3, [5, 10]))
        self.target.unlink()
        opener = FakeOpener(urllib.error.URLError("a"), urllib.error.URLError("b"), urllib.error.URLError("c"))
        with self.assertRaisesRegex(fetch.FetchError, "已重试"):
            fetch.download(self.lock, self.target, opener=opener, sleep=lambda _: None)
        self.assertFalse(self.target.exists())
        opener = FakeOpener(b"x" * len(self.data), self.data)
        with self.assertRaises(fetch.FetchError):
            fetch.download(self.lock, self.target, opener=opener, sleep=lambda _: None)
        self.assertEqual(len(opener.requests), 1)

    def test_a_non_200_answer_and_a_foreign_host_are_refused(self):
        with self.assertRaises(fetch.FetchError):
            fetch.download(self.lock, self.target, opener=FakeOpener(FakeResponse(self.data, status=206)))
        self.assertFalse(self.target.exists())
        foreign = dict(self.lock, url=self.lock["url"].replace("github.com", "example.com"))
        opener = FakeOpener(self.data)
        with self.assertRaises(fetch.FetchError):
            fetch.download(foreign, self.target, opener=opener)
        self.assertEqual(opener.requests, [])

    def test_an_endless_body_stops_at_the_locked_size(self):
        class Endless(FakeResponse):
            reads = 0

            def read(self, size=-1):
                Endless.reads += 1
                return b"x" * size

        small = dict(self.lock, size=3 * 1024 * 1024)
        with self.assertRaises(fetch.FetchError):
            fetch.download(small, self.target, opener=FakeOpener(Endless(b"")))
        self.assertEqual(Endless.reads, 4)
        self.assertFalse(self.target.exists())


class ExtractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def unpack(self, members, name="out"):
        archive = self.root / (name + ".tar.gz")
        archive.write_bytes(archive_bytes(members))
        return fetch.extract(archive, self.root / name)

    def test_a_good_archive_keeps_modes_and_links(self):
        runtime = self.unpack(GOOD)
        self.assertEqual(runtime, (self.root / "out" / "python").resolve())
        self.assertEqual((runtime / "bin/python3.13").read_bytes(), b"interpreter")
        self.assertTrue(os.access(runtime / "bin/python3.13", os.X_OK))
        self.assertFalse(os.access(runtime / "lib/python3.13/os.py", os.X_OK))
        self.assertTrue(os.access(runtime / "lib/python3.13/os.py", os.W_OK))
        self.assertEqual(os.readlink(runtime / "bin/python3"), "python3.13")
        self.assertEqual((runtime / "bin/py").stat().st_ino, (runtime / "bin/python3.13").stat().st_ino)

    def test_unsafe_members_are_refused(self):
        base = GOOD[:4]
        cases = {
            "parent reference": base + (("python/../evil", "file", b"x"),),
            "absolute name": base + (("/etc/passwd", "file", b"x"),),
            "other top level": base + (("other/file", "file", b"x"),),
            "absolute link": base + (("python/lib", "dir", None), ("python/lib/x", "sym", "/etc")),
            "link out of the runtime": base + (("python/lib", "dir", None), ("python/lib/x", "sym", "../../..")),
            "hard link out of the runtime": base + (("other", "file", b"x"), ("python/h", "hard", "other")),
            "hard link to a link": base + (("python/h", "hard", "python/bin/python3"),),
            "device-like member": base + (("python/pipe", "fifo", None),),
            "duplicate file": base + (("python/a", "file", b"1"), ("python/a", "file", b"2")),
            "no interpreter": (("python", "dir", None), ("python/lib", "dir", None)),
        }
        for label, members in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(fetch.FetchError):
                    self.unpack(members, name=label.replace(" ", "-"))

    def test_a_link_cannot_be_used_to_write_outside(self):
        members = GOOD[:4] + (("python/lib", "dir", None), ("python/lib/up", "sym", ".."),
                              ("python/lib/up/escaped", "file", b"x"))
        self.unpack(members, name="inside")
        self.assertTrue((self.root / "inside" / "python" / "escaped").exists())
        self.assertFalse((self.root / "inside" / "escaped").exists())
        outside = GOOD[:4] + (("python/up", "sym", ".."), ("python/up/escaped", "file", b"x"))
        with self.assertRaises(fetch.FetchError):
            self.unpack(outside, name="outside")

    def test_the_unpacked_size_is_capped(self):
        with patch.object(fetch, "MAX_UNPACKED_SIZE", 5):
            with self.assertRaisesRegex(fetch.FetchError, "大小上限"):
                self.unpack(GOOD, name="capped")


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "python-runtime"
        self.data = archive_bytes(GOOD)
        self.lock = lock_for(self.data)

    def test_fetch_downloads_verifies_and_unpacks(self):
        runtime = fetch.fetch(self.lock, self.output, opener=FakeOpener(self.data))
        self.assertEqual(runtime.name, "python")
        self.assertTrue((runtime / "bin" / "python3").exists())
        self.assertEqual(json.loads((self.output / "runtime.json").read_text())["sha256"], self.lock["sha256"])
        with self.assertRaisesRegex(fetch.FetchError, "已存在"):
            fetch.fetch(self.lock, self.output, opener=FakeOpener(self.data))

    def test_a_failed_fetch_leaves_nothing_behind(self):
        with self.assertRaises(fetch.FetchError):
            fetch.fetch(self.lock, self.output, opener=FakeOpener(b"x" * len(self.data)))
        self.assertFalse(self.output.exists())
        bad = archive_bytes(GOOD[:4] + (("python/../evil", "file", b"x"),))
        with self.assertRaises(fetch.FetchError):
            fetch.fetch(lock_for(bad), self.output, opener=FakeOpener(bad))
        self.assertFalse(self.output.exists())

    def test_the_command_refuses_to_run_outside_actions_and_exports_the_folder_inside(self):
        environment = {key: value for key, value in os.environ.items()
                       if key not in ("GITHUB_ACTIONS", "RUNNER_ENVIRONMENT", "GITHUB_ENV")}
        with patch.dict(os.environ, environment, clear=True):
            self.assertEqual(fetch.main(["--output", str(self.output)]), 1)
        self.assertFalse(self.output.exists())
        github_env = Path(self.temp.name) / "github-env"
        hosted = dict(environment, GITHUB_ACTIONS="true", RUNNER_ENVIRONMENT="github-hosted", GITHUB_ENV=str(github_env))
        runtime = Path(self.temp.name) / "python"
        with patch.dict(os.environ, hosted, clear=True), patch.object(fetch, "fetch", return_value=runtime):
            self.assertEqual(fetch.main(["--output", str(self.output)]), 0)
        self.assertEqual(github_env.read_text(), "PYTHON_RUNTIME_DIR=%s\n" % runtime)


class BuildWiringTests(unittest.TestCase):
    """The build script and the smoke script agree on where the runtime lives and which interpreter judges the core."""

    def setUp(self):
        self.build = (ROOT / "scripts/build_app.sh").read_text(encoding="utf-8")
        self.smoke = (ROOT / "scripts/smoke_bundled_python.sh").read_text(encoding="utf-8")

    def test_the_runtime_is_copied_prepared_and_used_before_the_app_is_signed(self):
        steps = ('RUNTIME_SRC="${PYTHON_RUNTIME_DIR:?', 'RUNTIME_DST="$APP_DST/Contents/Resources/python"',
                 'ditto "$RUNTIME_SRC" "$RUNTIME_DST"', 'scripts/prepare_python_runtime.py" "$RUNTIME_DST"',
                 '[ "$BUNDLED_PYTHON" = "$EXPECTED_PYTHON" ]', '"$RUNTIME_DST/bin/python3" -I -B - "$ROOT/core" "$RES"',
                 "codesign --force --deep --sign -")
        positions = [self.build.index(step) for step in steps]
        self.assertEqual(positions, sorted(positions))
        # Next to the core it would lose its bytecode to the __pycache__ clean-up.
        self.assertNotIn("$RES/python", self.build)

    def test_the_core_is_judged_by_the_bundled_interpreter_not_the_runners(self):
        self.assertNotIn("PYTHONDONTWRITEBYTECODE=1 python3 -", self.build)
        self.assertIn('"python_version"', self.build)
        self.assertIn('find "$RES/_vendor" -name "*.so" -type f -delete', self.build)

    def test_the_smoke_script_reads_the_path_the_build_writes_and_runs_like_a_user_mac(self):
        self.assertIn('PY="$APP/Contents/Resources/python/bin/python3"', self.smoke)
        self.assertIn("env -i ", self.smoke)
        self.assertIn("PATH=/usr/bin:/bin", self.smoke)
        self.assertNotIn(" -I ", self.smoke.split("== bundled interpreter in an empty environment ==")[1])

    def test_the_smoke_never_waits_for_a_payload_on_stdin(self):
        # `kanpilot.py doctor` without --json reads a piped stdin until it closes; seen hanging for minutes.
        doctor = next(line for line in self.smoke.splitlines() if "kanpilot.py" in line and "doctor" in line)
        self.assertIn("doctor --json '{}' < /dev/null", doctor)


if __name__ == "__main__":
    unittest.main()
