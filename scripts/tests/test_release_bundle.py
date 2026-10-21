"""Isolated GitHub-release crash/retry and identity contract checks. No network."""
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
import urllib.parse
import urllib.request

SPEC = importlib.util.spec_from_file_location("release_bundle", Path(__file__).parents[1] / "release_bundle.py")
release_bundle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_bundle)
SHA = "a" * 40
TAG = "v1.2.3"
BUILD = 42


class FakeGitHub:
    repository = "test/private"

    def __init__(self):
        self.tag = {"type": "commit", "sha": SHA}
        self.release = None
        self.assets = []
        self.content = {}
        self.writes = []
        self.interrupt_after = None
        self.hide_draft_from_tag = False

    def add(self, name, data):
        record = {"name": name, "id": len(self.assets) + 1}
        self.assets.append(record)
        self.content[record["id"]] = data
        return record

    def request(self, method, path, data=None, binary=False, upload=None):
        if method != "GET":
            self.writes.append((method, path))
        if method == "GET" and "/git/ref/" in path:
            return {"object": self.tag}
        if method == "GET" and "/releases/tags/" in path:
            return None if self.hide_draft_from_tag and self.release and self.release["draft"] else self.release
        if method == "GET" and "/releases?" in path:
            return [self.release] if self.release else []
        if method == "GET" and "/releases/assets/" in path:
            return self.content[int(path.rsplit("/", 1)[1])]
        if method == "GET" and "/assets?" in path:
            page = int(urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["page"][0])
            return self.assets[(page - 1) * 100:page * 100]
        if method == "POST" and path.endswith("/releases"):
            self.release = dict(data, id=7, html_url="https://github.com/test/private/releases/tag/" + TAG)
            return self.release
        if method == "POST" and upload:
            if self.interrupt_after == 0:
                raise RuntimeError("simulated connection loss")
            if self.interrupt_after is not None:
                self.interrupt_after -= 1
            name = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["name"][0]
            return self.add(name, Path(upload).read_bytes())
        if method == "PATCH":
            self.release.update(data)
            return self.release
        raise AssertionError((method, path))


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.api = FakeGitHub()
        for platform, extension in (("android", "apk"), ("macos", "dmg")):
            package = self.root / ("KaneoPilot-1.2.3-" + platform + "." + extension)
            package.write_bytes((platform + " signed fixture").encode())
            release_bundle.metadata(types.SimpleNamespace(
                package=package, platform=platform, fingerprint="b" * 64,
                version="1.2.3", source_sha=SHA, build_number=BUILD,
                architectures="any" if platform == "android" else "arm64,x86_64",
                output=self.root / (platform + "-metadata.json")))
        release_bundle.prepare(self.root, TAG, SHA, BUILD)

    def tearDown(self):
        self.tmp.cleanup()

    def publish(self):
        return release_bundle.publish(self.api, self.root, TAG, SHA, BUILD)

    def test_new_release_hashes_every_uploaded_asset_then_publishes(self):
        self.publish()
        self.assertFalse(self.api.release["draft"])
        self.assertEqual(len(self.api.assets), 4)
        self.assertEqual(self.api.writes[-1][0], "PATCH")
        sums = (self.root / "SHA256SUMS").read_text()
        self.assertIn("manifest.json", sums)
        manifest = json.loads((self.root / "manifest.json").read_text())
        self.assertEqual(manifest["artifacts"][1]["architectures"], ["arm64", "x86_64"])

    def test_completed_release_rerun_is_read_only(self):
        self.publish()
        self.api.writes.clear()
        self.publish()
        self.assertEqual(self.api.writes, [])

    def test_interrupted_draft_recovers_only_missing_assets_and_preserves_extra(self):
        self.api.interrupt_after = 1
        with self.assertRaisesRegex(RuntimeError, "connection loss"):
            self.publish()
        self.assertTrue(self.api.release["draft"])
        first = self.api.assets[0].copy()
        self.api.add("operator-note.txt", b"keep me")
        self.api.hide_draft_from_tag = True
        self.api.interrupt_after = None
        self.publish()
        self.assertEqual(self.api.assets[0], first)
        self.assertEqual(len(self.api.assets), 5)
        self.assertFalse(self.api.release["draft"])

    def test_conflicting_existing_asset_aborts_before_any_write(self):
        self.publish()
        self.api.release["draft"] = True
        self.api.content[self.api.assets[-1]["id"]] = b"conflicting checksum"
        self.api.assets.pop(0)  # A missing APK must not be uploaded before checking later conflicts.
        self.api.writes.clear()
        with self.assertRaisesRegex(ValueError, "differs"):
            self.publish()
        self.assertEqual(self.api.writes, [])

    def test_other_builds_draft_is_never_modified(self):
        self.api.release = {"id": 9, "draft": True, "body": "human-created", "tag_name": TAG}
        with self.assertRaisesRegex(ValueError, "another build"):
            self.publish()
        self.assertEqual(self.api.writes, [])

    def test_moved_remote_tag_aborts_before_writes(self):
        self.api.tag["sha"] = "c" * 40
        with self.assertRaisesRegex(ValueError, "tag no longer"):
            self.publish()
        self.assertEqual(self.api.writes, [])

    def test_source_content_change_rejected_before_network_writes(self):
        next(self.root.glob("*.apk")).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "content mismatch"):
            self.publish()
        self.assertEqual(self.api.writes, [])

    def test_published_missing_asset_is_never_repaired_silently(self):
        self.publish()
        self.api.assets.pop()
        self.api.writes.clear()
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.publish()
        self.assertEqual(self.api.writes, [])

    def test_other_platform_version_mismatch_is_rejected(self):
        path = self.root / "macos-metadata.json"
        metadata = json.loads(path.read_text())
        metadata["version"] = "1.2.4"
        path.write_text(json.dumps(metadata))
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            release_bundle.prepare(self.root, TAG, SHA, BUILD)

    def test_prerelease_and_noncanonical_tags_rejected(self):
        for tag in ("v1.2.3-rc.1", "v01.2.3", "v1.2", "1.2.3"):
            with self.subTest(tag=tag), self.assertRaisesRegex(ValueError, "stable"):
                release_bundle.prepare(self.root, tag, SHA, BUILD)

    def test_redirect_does_not_forward_token_to_asset_storage(self):
        request = urllib.request.Request("https://api.github.com/repos/test/private/releases/assets/1", headers={"Authorization": "Bearer secret"})
        redirected = release_bundle.SafeRedirect().redirect_request(request, None, 302, "Found", {}, "https://release-assets.githubusercontent.com/signed-asset")
        self.assertFalse(redirected.has_header("Authorization"))
        with self.assertRaisesRegex(ValueError, "non-HTTPS"):
            release_bundle.SafeRedirect().redirect_request(request, None, 302, "Found", {}, "http://example.com/asset")

    def test_asset_pagination_preserves_unrelated_assets(self):
        self.publish()
        for number in range(101):
            self.api.add("note-%d" % number, b"unrelated")
        self.api.writes.clear()
        self.publish()
        self.assertEqual(self.api.writes, [])
        self.assertEqual(len(self.api.assets), 105)


if __name__ == "__main__":
    unittest.main()
