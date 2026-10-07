"""Transcript size limits through real local MCP HTTP transports, without models.

The existing isolated Plus HTTP fixture serves physical transcript slices and a
size-limited MCP result. RuntimeClient calls the real project MCP server, which
dispatches to harness_plus_adapter and then the Plus HTTP fixture. Historical
reads use the same adapter through their separate read-only facade.
"""

import json

from kanpilot import (dsh_events, harness_plus_adapter, pi_desktop_controller,
                      pi_desktop_worker, runtime_client)
from test_pi_desktop_backend import PiDesktopCase as _PiDesktopCase


class TestRuntimeTranscriptPaging(_PiDesktopCase):
    def setUp(self):
        super().setUp()
        self.result_byte_limit = 22000
        self.break_page_chain = False
        original_call = self.server.tool_call

        def limited_transcript(name, args):
            if name != "pi_session_get":
                return original_call(name, args)
            end = args.get("messageBefore", len(self.server.messages))
            start = max(0, end - args.get("messageLimit", 200))
            payload = {"session": {
                "id": self.server.session_id,
                "messages": self.server.messages[start:end],
                "messageStart": start, "messageEnd": end,
                "hasMoreBefore": start > 0,
                "hasMoreAfter": end < len(self.server.messages),
            }}
            if len(json.dumps(payload).encode("utf-8")) > self.result_byte_limit:
                payload = {
                    "truncated": True,
                    "reason": f"MCP_RESULT_LIMIT {self.server.token}",
                    "preview": "PRIVATE_TRANSCRIPT_PREVIEW",
                }
            elif self.break_page_chain and args.get("messageBefore") is not None:
                payload["session"]["messageEnd"] -= 1
            return {"structuredContent": payload}

        # Override the server's result producer, never either MCP client or adapter.
        self.server.tool_call = limited_transcript

    def messages(self, count=68, text_size=700):
        self.server.messages = [{
            "id": f"m-{index}", "role": "user" if index == 0 else "assistant",
            "content": "task-anchor" if index == 0 else "x" * text_size,
            "timestamp": "2026-10-07T01:00:00Z",
        } for index in range(count)]

    def runtime(self, role="planner"):
        client = runtime_client.RuntimeClient(
            role=role, connection_file=str(self.connection_file),
            project_path=str(self.repos_root / "Feedback"),
            runtime_config={"permission_mode": "ask"},
            invocation_id=f"paging_{role}",
        )
        client.connect()
        session = client.session_create(
            str(self.repos_root / "Feedback"),
            mode="goal" if role == "execution" else "plan",
        )
        self.assertEqual(session["session"]["id"], self.server.session_id)
        return client

    def collector(self, client, name="events"):
        path = self.root / f"{name}.jsonl"
        collector = pi_desktop_worker._Collector(
            client, self.server.session_id, pi_desktop_worker._EventLog(path),
            "task-anchor",
        )
        collector.drain()
        return collector, path

    def transcript_calls(self):
        return [args for name, args in self.server.calls if name == "pi_session_get"]

    def assert_complete_chain(self, client, name):
        self.server.calls.clear()
        collector, path = self.collector(client, name)
        collector.poll()
        self.assertFalse(collector.truncated)
        self.assertIsNone(collector.read_error)
        self.assertFalse(collector.unrecognized)
        self.assertEqual(collector.verified_end, 68)
        self.assertEqual(len(collector._prefix_hashes), 68)
        self.assertNotIn(None, collector._prefix_hashes)
        self.assertEqual(len(collector.seen), 68)
        self.assertEqual(collector.first_user_index, 0)
        self.assertEqual(collector.first_user_message, "task-anchor")
        self.assertEqual(
            [(args.get("messageBefore"), args["messageLimit"])
             for args in self.transcript_calls()],
            [(None, 40), (None, 20), (48, 20), (28, 20), (8, 20)],
        )
        events, error = dsh_events.iter_events(path)
        self.assertIsNone(error)
        self.assertEqual(len(events), 68)
        self.assertNotIn("PRIVATE_TRANSCRIPT_PREVIEW", path.read_text())
        # Re-reading the verified tail does not duplicate transcript events.
        collector.poll()
        repeated_events, error = dsh_events.iter_events(path)
        self.assertIsNone(error)
        self.assertEqual(repeated_events, events)
        self.assertEqual(self.server.prompt_count, 0)

    def test_runtime_planner_review_execution_shrink_to_complete_transcript(self):
        self.messages()
        for role in ("planner", "review", "execution"):
            with self.subTest(role=role):
                self.assert_complete_chain(self.runtime(role), role)

    def test_historical_reader_shrinks_to_complete_transcript(self):
        self.messages()
        client = runtime_client.HistoricalReadClient(str(self.connection_file))
        client.connect()
        self.assert_complete_chain(client, "historical")

    def test_review_controller_reader_shrinks_and_rejects_irreducible_pages(self):
        for client in (self.runtime("review"),
                       runtime_client.HistoricalReadClient(str(self.connection_file))):
            self.messages()
            self.server.calls.clear()
            messages, complete, detail = pi_desktop_controller.read_transcript(
                client, self.server.session_id,
            )
            self.assertTrue(complete)
            self.assertIsNone(detail)
            self.assertEqual(messages, self.server.messages)
            self.assertEqual([args["messageLimit"] for args in self.transcript_calls()],
                             [40, 20, 20, 20, 20])
            self.server.messages[0]["content"] = "x" * 50000
            messages, complete, detail = pi_desktop_controller.read_transcript(
                client, self.server.session_id,
            )
            self.assertFalse(complete)
            self.assertEqual(messages, [])
            self.assertIn("transcript_incomplete", detail)
            self.assertEqual(self.server.prompt_count, 0)

    def test_truncation_contract_preserves_signal_redacts_reason_discards_preview(self):
        self.messages()
        client = self.runtime()
        result = client._call("kp_runtime_messages", {
            "session_id": self.server.session_id, "limit": 40,
        })
        self.assertEqual(set(result), {"session_id", "messages", "truncated", "reason"})
        self.assertEqual(result["session_id"], self.server.session_id)
        self.assertEqual(result["messages"], [])
        self.assertIs(result["truncated"], True)
        self.assertIn("MCP_RESULT_LIMIT", result["reason"])
        self.assertNotIn(self.server.token, result["reason"])
        self.assertNotIn("PRIVATE_TRANSCRIPT_PREVIEW", json.dumps(result))
        self.assertEqual(harness_plus_adapter.plus_messages(
            str(self.connection_file), session_id=self.server.session_id, limit=40,
        ), result)
        for facade in (client, runtime_client.HistoricalReadClient(str(self.connection_file))):
            self.assertEqual(facade.session_get(self.server.session_id, message_limit=40), {
                "truncated": True, "reason": result["reason"],
            })
        spec = next(tool for tool in client.client.list_tools()
                    if tool["name"] == "kp_runtime_messages")
        schema = spec["outputSchema"]["properties"]["data"]
        self.assertEqual(schema["required"], ["session_id", "messages"])
        self.assertEqual(schema["properties"]["truncated"], {"type": "boolean"})
        self.assertEqual(schema["properties"]["reason"], {"type": "string"})

    def test_minimum_page_still_truncated_never_verifies_empty_session(self):
        self.messages(count=1, text_size=50000)
        self.server.messages[0]["content"] = "x" * 50000
        for client in (self.runtime(), runtime_client.HistoricalReadClient(str(self.connection_file))):
            self.server.calls.clear()
            collector, _ = self.collector(client)
            collector.poll()
            self.assertTrue(collector.truncated)
            self.assertIsNone(collector.read_error)
            self.assertEqual(collector.verified_end, 0)
            self.assertEqual(collector.seen, {})
            self.assertEqual([args["messageLimit"] for args in self.transcript_calls()],
                             [40, 20, 10, 5])

    def test_normal_empty_page_keeps_valid_pagination_shape(self):
        self.messages(count=0)
        for client in (self.runtime(), runtime_client.HistoricalReadClient(str(self.connection_file))):
            result = client.session_get(self.server.session_id, message_limit=40)
            self.assertEqual(result, {"session": {
                "id": self.server.session_id, "messages": [],
                "messageStart": 0, "messageEnd": 0,
                "hasMoreBefore": False, "hasMoreAfter": False,
            }})
            collector, _ = self.collector(client)
            collector.poll()
            self.assertFalse(collector.truncated)
            self.assertIsNone(collector.read_error)
            self.assertEqual(collector.verified_end, 0)

    def test_broken_physical_chain_still_fails_after_page_shrink(self):
        self.messages()
        self.break_page_chain = True
        collector, _ = self.collector(self.runtime())
        collector.poll()
        self.assertTrue(collector.truncated)
        self.assertEqual(collector.verified_end, 0)
        self.assertEqual(len(collector.seen), 20)  # already read evidence retained
        self.assertEqual(self.server.prompt_count, 0)
