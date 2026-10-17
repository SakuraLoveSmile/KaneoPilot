"""Frozen verify commands versus a Plus Goal report's ``checks`` (protocol §5.18.6)."""

import unittest

from kanpilot import verify_evidence

COMMANDS = ["dart format --output=none lib/a.dart", "flutter test --no-pub test/a_test.dart"]


def check(command, exit_code=0, result="passed"):
    return {"id": "check", "command": command, "exitCode": exit_code, "result": result, "detail": ""}


class GoalCheckCoverageTests(unittest.TestCase):
    def test_every_frozen_command_needs_a_passing_record(self):
        coverage = verify_evidence.match_goal_checks(COMMANDS, [check(command) for command in COMMANDS])

        self.assertTrue(coverage["ok"])
        self.assertEqual(coverage["matched"], COMMANDS)
        self.assertEqual((coverage["missing"], coverage["failed"]), ([], []))
        self.assertEqual([row["command"] for row in coverage["rows"]], COMMANDS)

    def test_missing_record_is_unconfirmed_not_a_failure(self):
        coverage = verify_evidence.match_goal_checks(COMMANDS, [check(COMMANDS[0])])

        self.assertFalse(coverage["ok"])
        self.assertEqual(coverage["missing"], [COMMANDS[1]])
        self.assertEqual([item["exit_code"] for item in coverage["failed"]], [None])

    def test_inline_environment_prefix_and_trailing_note_are_normalized(self):
        checks = [check("FLUTTER_ROOT=/opt/flutter " + COMMANDS[0]), check(COMMANDS[1] + "（Flutter 3.22）")]

        self.assertTrue(verify_evidence.match_goal_checks(COMMANDS, checks)["ok"])

    def test_whitespace_folds_only_outside_quotes(self):
        key = verify_evidence.goal_command_key
        self.assertEqual(key("dart  format \t --output=none  lib/a.dart"), COMMANDS[0])
        self.assertNotEqual(key('echo "a  b"'), key('echo "a b"'))
        self.assertNotEqual(key("pytest -k alpha"), key("pytest -k beta"))

    def test_non_integer_or_contradictory_records_stay_unconfirmed(self):
        records = (check(COMMANDS[1], exit_code=None), check(COMMANDS[1], exit_code=True),
                   check(COMMANDS[1], exit_code="0"), check(COMMANDS[1], result="failed"))
        for record in records:
            with self.subTest(record=record):
                coverage = verify_evidence.match_goal_checks(COMMANDS, [check(COMMANDS[0]), record])

                self.assertFalse(coverage["ok"])
                self.assertEqual(coverage["missing"], [COMMANDS[1]])
                self.assertEqual([item["exit_code"] for item in coverage["failed"]], [None])

    def test_real_nonzero_exit_is_a_failure(self):
        coverage = verify_evidence.match_goal_checks(
            COMMANDS, [check(COMMANDS[0]), check(COMMANDS[1], exit_code=1, result="failed")])

        self.assertFalse(coverage["ok"])
        self.assertEqual(coverage["missing"], [])
        self.assertEqual([row["exit_code"] for row in coverage["failed"]], [1])

    def test_last_record_for_a_command_decides(self):
        recovered = verify_evidence.match_goal_checks(
            COMMANDS[:1], [check(COMMANDS[0], exit_code=1, result="failed"), check(COMMANDS[0])])
        regressed = verify_evidence.match_goal_checks(
            COMMANDS[:1], [check(COMMANDS[0]), check(COMMANDS[0], exit_code=1, result="failed")])

        self.assertTrue(recovered["ok"])
        self.assertFalse(regressed["ok"])
        self.assertEqual([row["exit_code"] for row in regressed["failed"]], [1])

    def test_absent_or_malformed_checks_match_nothing(self):
        for checks in (None, [], ["text"], [None]):
            with self.subTest(checks=checks):
                coverage = verify_evidence.match_goal_checks(COMMANDS, checks)

                self.assertFalse(coverage["ok"])
                self.assertEqual(coverage["missing"], COMMANDS)

    def test_declaration_without_commands_never_passes(self):
        self.assertFalse(verify_evidence.match_goal_checks([], [check(COMMANDS[0])])["ok"])


if __name__ == "__main__":
    unittest.main()
