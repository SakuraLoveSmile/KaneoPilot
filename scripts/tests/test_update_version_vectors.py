"""Run the shared update-version vectors through both Python implementations. No network.

The macOS fixtures and Android unit tests read the same file, so a rule cannot change in one place only.
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


class VersionVectorTests(unittest.TestCase):
    def test_vector_file_is_well_formed(self):
        self.assertEqual(VECTORS["schema"], 1)
        valid, invalid = VECTORS["versions"]["valid"], VECTORS["versions"]["invalid"]
        self.assertEqual(len(valid), len(set(valid)))
        self.assertEqual(len(invalid), len(set(invalid)))
        self.assertFalse(set(valid) & set(invalid))
        self.assertTrue(any("-test." in text for text in valid))
        self.assertTrue(any("-test." not in text for text in valid))
        for sequence in VECTORS["ordering"]["ascending"]:
            self.assertGreaterEqual(len(sequence), 2)
            for text in sequence:
                release_bundle.parse_version(text)
        for case in VECTORS["offers"]["cases"]:
            self.assertEqual(set(case) - {"platforms"}, {"name", "installed", "installed_build", "candidate", "candidate_build", "offered"})
            self.assertIs(type(case["installed_build"]), str)
            self.assertIs(type(case["candidate_build"]), int)
            self.assertIs(type(case["offered"]), bool)
            for key in ("installed", "candidate"):
                release_bundle.parse_version(case[key])

    def test_valid_versions_sort_identically_in_both_implementations(self):
        for text in VECTORS["versions"]["valid"]:
            with self.subTest(version=text):
                self.assertEqual(release_bundle.version_key(text), updater.version_key(text))
                self.assertEqual(updater.is_test_version(text), "-test." in text)

    def test_invalid_versions_are_rejected_by_both_implementations(self):
        for text in VECTORS["versions"]["invalid"]:
            with self.subTest(version=text):
                with self.assertRaises(ValueError):
                    release_bundle.parse_version(text)
                with self.assertRaises(updater.UpdateError):
                    updater.version_key(text)

    def test_tags_are_a_v_followed_by_a_valid_version(self):
        for text in VECTORS["versions"]["valid"]:
            serial = release_bundle.parse_version(text)[3]
            with self.subTest(tag="v" + text):
                self.assertEqual(release_bundle.tag_channel("v" + text, serial or 7), "stable" if serial is None else "test")
        rejected = ["v" + text for text in VECTORS["versions"]["invalid"]] + VECTORS["tags"]["invalid"]
        for tag in rejected:
            with self.subTest(tag=tag), self.assertRaisesRegex(ValueError, "stable"):
                release_bundle.tag_channel(tag, 7)

    def test_a_test_tag_serial_must_equal_the_build_number(self):
        self.assertEqual(release_bundle.tag_channel("v0.2.1-test.70", "70"), "test")
        with self.assertRaisesRegex(ValueError, "serial"):
            release_bundle.tag_channel("v0.2.1-test.70", 71)

    def test_ascending_sequences_are_strictly_ordered(self):
        for sequence in VECTORS["ordering"]["ascending"]:
            for index, lower in enumerate(sequence):
                self.assertEqual(release_bundle.version_key(lower), release_bundle.version_key(lower))
                for higher in sequence[index + 1:]:
                    with self.subTest(lower=lower, higher=higher):
                        self.assertLess(release_bundle.version_key(lower), release_bundle.version_key(higher))
                        self.assertLess(updater.version_key(lower), updater.version_key(higher))

    def test_update_offers_follow_the_shared_rule(self):
        for case in VECTORS["offers"]["cases"]:
            if "python" not in case.get("platforms", ["python", "macos", "android"]):
                continue
            with self.subTest(case["name"]):
                self.assertIs(updater.update_allowed(case["installed"], case["installed_build"],
                                                     case["candidate"], case["candidate_build"]), case["offered"])


if __name__ == "__main__":
    unittest.main()
