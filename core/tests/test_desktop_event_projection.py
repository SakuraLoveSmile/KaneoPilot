"""Desktop transcript revisions must update the existing reader message."""

import unittest

from helpers import CORE  # noqa: F401 - installs isolated core import path
from kanpilot import dsh_events, headless_adapter


class DesktopEventProjectionTests(unittest.TestCase):
    def event(self, seq, text, revision, kind="assistant_message"):
        return {
            "seq": seq, "ts": "2026-09-26T00:00:00Z", "type": kind,
            "content": text, "source_message_id": "message-uuid",
            "source_revision": revision,
        }

    def test_string_identity_replaces_content_and_survives_old_cursor(self):
        first = self.event(2, "partial", 1)
        final = self.event(500, "complete", 2)
        messages = dsh_events.to_messages([first, final])
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["id"], "desktop:message-uuid:assistant_message")
        self.assertEqual(messages[0]["content"], "complete")
        self.assertEqual(messages[0]["revision"], 2)
        page = headless_adapter.slice_messages(messages, 499, 10, {})
        self.assertEqual(page["messages"][0]["content"], "complete")

    def test_tool_input_and_result_have_distinct_stable_identities(self):
        call = self.event(2, "", 1, "tool_call")
        call.update(name="read", arguments={"path": "INPUT.txt"})
        result = self.event(3, "", 2, "tool_result")
        result.update(name="read", output="read result")
        messages = dsh_events.to_messages([call, result])
        self.assertEqual(len(messages), 2)
        self.assertNotEqual(messages[0]["id"], messages[1]["id"])
        self.assertEqual(messages[1]["tool"]["output"], "read result")

    def test_old_pi_events_keep_original_identity_and_revision(self):
        event = {"seq": 7, "type": "assistant_message", "content": "old"}
        message = dsh_events.to_messages([event])[0]
        self.assertEqual(message["id"], "round:7")
        self.assertEqual(message["revision"], dsh_events._revision("assistant_message", "old"))

    def test_stale_source_revision_does_not_overwrite_new_content(self):
        messages = dsh_events.to_messages([
            self.event(2, "new", 2), self.event(3, "stale", 1),
        ])
        self.assertEqual(messages[0]["content"], "new")


if __name__ == "__main__":
    unittest.main()
