"""Pure evidence matching tests; no command or service is executed."""
import copy
import unittest
from unittest.mock import patch

from kanpilot import stage_evidence, util


SOURCE = "a" * 64


def check(command="python3 -m unittest", **changes):
    row = {"id": "check-1", "command": command, "exit_code": 0,
           "result": "passed", "detail": "验证成功", "provenance": "model_declared",
           "source_sha256": SOURCE}
    row.update(changes)
    return row


class MatchChecksTests(unittest.TestCase):
    command = "python3 -m unittest"

    def match(self, records, **kwargs):
        return stage_evidence.match_checks([self.command], records, **kwargs)

    def assert_unconfirmed(self, result, reason):
        self.assertFalse(result["ok"])
        self.assertEqual(result["matched"], [])
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["missing"], [self.command])
        self.assertEqual(result["rows"][0]["unconfirmed_reason"], reason)

    def test_each_declared_provenance_is_retained_without_upgrade(self):
        for provenance in ("model_declared", "harness_observed", "core_verified"):
            with self.subTest(provenance=provenance):
                result = self.match([check(provenance=provenance)], expected_source_sha256=SOURCE)
                self.assertEqual(set(result), {"ok", "matched", "missing", "failed", "rows"})
                self.assertTrue(result["ok"])
                self.assertEqual(result["matched"], [self.command])
                self.assertEqual(result["rows"][0]["provenance"], provenance)
                self.assertEqual(result["rows"][0]["source_sha256"], SOURCE)
                self.assertEqual(result["rows"][0]["report_check_id"], "check-1")

    def test_shell_prefix_notes_and_whitespace_cannot_substitute_for_original(self):
        alternatives = [" " + self.command, self.command + " ", "python3  -m unittest",
                        "PYTHONPATH=core " + self.command, "cd core && " + self.command,
                        self.command + " # unit tests", self.command + "（单元测试）",
                        self.command + "\n", "echo '" + self.command + "'"]
        for command in alternatives:
            with self.subTest(command=command):
                self.assert_unconfirmed(self.match([check(command)]), "missing_record")
        # Leading whitespace in a frozen command is also frozen, never stripped.
        result = stage_evidence.match_checks([" " + self.command], [check(" " + self.command)])
        self.assertTrue(result["ok"])

    def test_duplicates_remain_unconfirmed_in_both_orders(self):
        passed = check(id="passed", provenance="harness_observed")
        failed = check(id="failed", exit_code=7, result="failed", source_sha256="b" * 64)
        for records in ([passed, failed], [failed, passed], [passed, passed]):
            result = self.match(records)
            self.assert_unconfirmed(result, "duplicate_records")
            self.assertEqual(result["rows"][0]["records"], records)
            self.assertIsNone(result["rows"][0]["exit_code"])
            self.assertIsNone(result["rows"][0]["provenance"])

    def test_exit_codes_require_exact_integer_type(self):
        for code in (False, True, 0.0, 1.0, "0", "1", [], {}):
            with self.subTest(code=code):
                result = self.match([check(exit_code=code)])
                self.assert_unconfirmed(result, "invalid_exit_code")
                self.assertIsNone(result["rows"][0]["exit_code"])
        self.assert_unconfirmed(self.match([check(exit_code=None)]), "missing_exit_code")

    def test_missing_unknown_and_contradictory_results_are_not_true_failures(self):
        for code, status, reason in ((0, None, "missing_result"),
                                     (1, None, "missing_result"),
                                     (0, "unknown", "unknown_result"),
                                     (7, "unknown", "unknown_result"),
                                     (0, "failed", "contradictory_result"),
                                     (7, "passed", "contradictory_result")):
            with self.subTest(code=code, status=status):
                self.assert_unconfirmed(self.match([check(exit_code=code, result=status)]), reason)

    def test_unknown_source_or_provenance_cannot_confirm_even_nonzero_exit(self):
        for provenance in (None, "host", "trusted", "HARNESS_OBSERVED", True, {}):
            for code, status in ((0, "passed"), (7, "failed")):
                with self.subTest(provenance=provenance, code=code):
                    self.assert_unconfirmed(self.match([check(provenance=provenance,
                                                             exit_code=code, result=status)]),
                                            "unknown_provenance")
        for source in (None, "b" * 64, True):
            for code, status in ((0, "passed"), (7, "failed")):
                with self.subTest(source=source, code=code):
                    result = self.match([check(source_sha256=source, exit_code=code, result=status)],
                                        expected_source_sha256=SOURCE)
                    self.assert_unconfirmed(result, "source_mismatch")
                    self.assertEqual(result["rows"][0]["source_sha256"], source)

    def test_optional_fields_may_be_missing_without_inventing_them(self):
        minimal = {"command": self.command, "exit_code": 0, "result": "passed",
                   "provenance": "model_declared"}
        result = self.match([minimal])
        self.assertTrue(result["ok"])
        self.assertIsNone(result["rows"][0]["report_check_id"])
        self.assertIsNone(result["rows"][0]["source_sha256"])
        self.assert_unconfirmed(self.match([minimal], expected_source_sha256=SOURCE), "source_mismatch")
        for field, reason in (("exit_code", "missing_exit_code"), ("result", "missing_result"),
                              ("provenance", "unknown_provenance"), ("command", "missing_record")):
            missing_field = dict(minimal)
            del missing_field[field]
            self.assert_unconfirmed(self.match([missing_field]), reason)

    def test_absent_or_unrecognized_records_are_missing_evidence(self):
        for records in (None, [], [None, 0, "passed", {}], [check("unrelated command")]):
            with self.subTest(records=records):
                self.assert_unconfirmed(self.match(records), "missing_record")

    def test_mixed_coverage_keeps_failures_and_missing_separate_in_frozen_order(self):
        commands = ["test-pass", "test-fail", "test-missing", "test-conflict"]
        records = [check("test-fail", exit_code=-9, result="failed", detail="killed"),
                   check("test-pass"), check("test-conflict", exit_code=5, result="passed")]
        result = stage_evidence.match_checks(commands, records)
        self.assertFalse(result["ok"])
        self.assertEqual(result["matched"], ["test-pass"])
        self.assertEqual(result["missing"], ["test-missing", "test-conflict"])
        self.assertEqual([row["command"] for row in result["rows"]], commands)
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(result["failed"][0]["command"], "test-fail")
        self.assertEqual(result["failed"][0]["exit_code"], -9)
        self.assertEqual(result["failed"][0]["detail"], "killed")
        self.assertNotIn("unconfirmed_reason", result["failed"][0])

    def test_invalid_frozen_commands_and_outer_shapes_are_validation_errors(self):
        invalid_commands = (None, [], (), "test", [""], [" "], [False], [{}],
                            ["test", "test"], ["test\n"], ["test\r"], ["test\x00"])
        for commands in invalid_commands:
            with self.subTest(commands=commands):
                with self.assertRaises(util.KanpilotError) as raised:
                    stage_evidence.match_checks(commands, [])
                self.assertEqual(raised.exception.code, "validation_failed")
        for checks in ({}, "checks", ()):
            with self.assertRaises(util.KanpilotError):
                self.match(checks)
        for digest in ("", "not-a-digest", "A" * 64, True, []):
            with self.assertRaises(util.KanpilotError):
                self.match([], expected_source_sha256=digest)

    def test_no_io_and_no_mutation_or_aliasing_of_records(self):
        commands = [self.command]
        records = [check(detail={"nested": ["fixture"]})]
        before = copy.deepcopy(records)
        with patch("builtins.open", side_effect=AssertionError("unexpected file IO")), \
                patch("subprocess.run", side_effect=AssertionError("unexpected execution")):
            result = stage_evidence.match_checks(commands, records)
        self.assertEqual(records, before)
        self.assertEqual(commands, [self.command])
        result["rows"][0]["detail"]["nested"].append("changed")
        self.assertEqual(records, before)


if __name__ == "__main__":
    unittest.main()
