"""Run the shared update-version vectors through both Python implementations. No network.

The macOS fixtures and Android unit tests read the same file, so a rule cannot change in one place only.
The `dev` section carries the dev channel: macOS and the Python side read it, while the Android client
ignores `-dev.N` releases until it grows that channel of its own.
"""
import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
VECTORS = json.loads((Path(__file__).parent / "fixtures" / "update-version-vectors.json").read_text(encoding="utf-8"))


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release_bundle = load("release_bundle", "scripts/release_bundle.py")
updater = load("app_update_installer", "core/kanpilot/app_update_installer.py")


def sections():
    """Every (versions, ordering, offers) triple: the shared core and the dev-only extension."""
    yield VECTORS["versions"], VECTORS["ordering"], VECTORS["offers"]
    yield VECTORS["dev"]["versions"], VECTORS["dev"]["ordering"], VECTORS["dev"]["offers"]


class VersionVectorTests(unittest.TestCase):
    def test_vector_file_is_well_formed(self):
        self.assertEqual(VECTORS["schema"], 1)
        self.assertTrue(any("-test." in text for text in VECTORS["versions"]["valid"]))
        self.assertTrue(any("-test." not in text for text in VECTORS["versions"]["valid"]))
        self.assertTrue(any("-dev." in text for text in VECTORS["dev"]["versions"]["valid"]))
        for versions, ordering, offers in sections():
            valid, invalid = versions["valid"], versions["invalid"]
            self.assertEqual(len(valid), len(set(valid)))
            self.assertEqual(len(invalid), len(set(invalid)))
            self.assertFalse(set(valid) & set(invalid))
            for sequence in ordering["ascending"]:
                self.assertGreaterEqual(len(sequence), 2)
                for text in sequence:
                    release_bundle.parse_version(text)
            for case in offers["cases"]:
                self.assertEqual(set(case) - {"platforms"},
                                 {"name", "installed", "installed_build", "candidate", "candidate_build", "offered"})
                self.assertIs(type(case["installed_build"]), str)
                self.assertIs(type(case["candidate_build"]), int)
                self.assertIs(type(case["offered"]), bool)
                for key in ("installed", "candidate"):
                    release_bundle.parse_version(case[key])

    def test_valid_versions_sort_identically_in_both_implementations(self):
        for versions, _ordering, _offers in sections():
            for text in versions["valid"]:
                with self.subTest(version=text):
                    self.assertEqual(release_bundle.version_key(text), updater.version_key(text))
                    self.assertEqual(updater.is_test_version(text), "-test." in text)

    def test_invalid_versions_are_rejected_by_both_implementations(self):
        for versions, _ordering, _offers in sections():
            for text in versions["invalid"]:
                with self.subTest(version=text):
                    with self.assertRaises(ValueError):
                        release_bundle.parse_version(text)
                    with self.assertRaises(updater.UpdateError):
                        updater.version_key(text)

    def test_tags_are_a_v_followed_by_a_valid_version(self):
        for versions, _ordering, _offers in sections():
            for text in versions["valid"]:
                channel, serial = release_bundle.parse_version(text)[3:]
                with self.subTest(tag="v" + text):
                    self.assertEqual(release_bundle.tag_channel("v" + text, serial or 7),
                                     "stable" if channel == "stable" else channel)
            rejected = ["v" + text for text in versions["invalid"]] + VECTORS["tags"]["invalid"]
            for tag in rejected:
                with self.subTest(tag=tag), self.assertRaisesRegex(ValueError, "stable"):
                    release_bundle.tag_channel(tag, 7)

    def test_a_prerelease_tag_serial_must_equal_the_build_number(self):
        for tag, channel in (("v0.2.1-test.70", "test"), ("v0.2.1-dev.70", "dev")):
            with self.subTest(tag=tag):
                self.assertEqual(release_bundle.tag_channel(tag, "70"), channel)
                with self.assertRaisesRegex(ValueError, "serial"):
                    release_bundle.tag_channel(tag, 71)

    def test_a_dev_tag_sorts_below_the_test_of_its_base(self):
        self.assertLess(release_bundle.version_key("0.2.1-dev.999"), release_bundle.version_key("0.2.1-test.1"))
        self.assertLess(updater.version_key("0.2.1-dev.999"), updater.version_key("0.2.1-test.1"))

    def test_ascending_sequences_are_strictly_ordered(self):
        for _versions, ordering, _offers in sections():
            for sequence in ordering["ascending"]:
                for index, lower in enumerate(sequence):
                    self.assertEqual(release_bundle.version_key(lower), release_bundle.version_key(lower))
                    for higher in sequence[index + 1:]:
                        with self.subTest(lower=lower, higher=higher):
                            self.assertLess(release_bundle.version_key(lower), release_bundle.version_key(higher))
                            self.assertLess(updater.version_key(lower), updater.version_key(higher))

    def test_update_offers_follow_the_shared_rule(self):
        for _versions, _ordering, offers in sections():
            for case in offers["cases"]:
                if "python" not in case.get("platforms", ["python", "macos", "android"]):
                    continue
                with self.subTest(case["name"]):
                    self.assertIs(updater.update_allowed(case["installed"], case["installed_build"],
                                                         case["candidate"], case["candidate_build"]), case["offered"])


if __name__ == "__main__":
    unittest.main()
