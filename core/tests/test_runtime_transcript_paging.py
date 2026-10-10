"""Transcript size limits through real local MCP HTTP transports, without models.

The existing isolated Plus HTTP fixture serves physical transcript slices and a
size-limited MCP result. RuntimeClient calls the real project MCP server, which
dispatches to harness_plus_adapter and then the Plus HTTP fixture. Historical
reads use the same adapter through their separate read-only facade.
"""

import base64
import hashlib
import json

from kanpilot import (dsh_events, harness_plus_adapter, pi_desktop_controller,
                      pi_desktop_worker, runtime_client)
from test_pi_desktop_backend import PiDesktopCase as _PiDesktopCase


class TestRuntimeTranscriptPaging(_PiDesktopCase):
    def setUp(self):
        super().setUp()
        self.result_byte_limit = 22000
        self.break_page_chain = False
        self.chunk_fault = None
        self.missing_session = False
        original_call = self.server.tool_call

        def limited_transcript(name, args):
            if name == "pi_session_get_chunk":
                end = args.get("messageBefore", len(self.server.messages))
                start = max(0, end - 1)
                payload = {"session": {
                    "id": self.server.session_id,
                    "messages": self.server.messages[start:end],
                    "messageStart": start, "messageEnd": end,
                    "hasMoreBefore": start > 0,
                    "hasMoreAfter": end < len(self.server.messages),
                }}
                if self.chunk_fault == "page_identity":
                    payload["session"]["id"] = "other-session"
                if self.chunk_fault == "page_boundary":
                    payload["session"]["messageEnd"] += 1
                if self.chunk_fault == "page_more":
                    payload["session"]["hasMoreBefore"] = not (start > 0)
                if self.chunk_fault == "empty_page":
                    payload["session"]["messages"] = []
                    payload["session"]["messageStart"] = end
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                digest = hashlib.sha256(body).hexdigest()
                offset = args.get("offset", 0)
                if offset:
                    self.assertEqual(args["expectedSha256"],
                                     "0" * 64 if self.chunk_fault == "hash_mismatch" else digest)
                end_offset = min(offset + 64 * 1024, len(body))
                result = {
                    "sessionId": self.server.session_id,
                    "offset": offset,
                    "nextOffset": end_offset if end_offset < len(body) else None,
                    "totalBytes": len(body), "sha256": digest,
                    "encoding": "base64",
                    "chunk": base64.b64encode(body[offset:end_offset]).decode("ascii"),
                }
                if self.chunk_fault == "changed_hash" and offset:
                    result["sha256"] = "0" * 64
                if self.chunk_fault == "hash_mismatch":
                    result["sha256"] = "0" * 64
                if self.chunk_fault == "identity":
                    result["sessionId"] = "other-session"
                if self.chunk_fault == "offset":
                    result["offset"] += 1
                if self.chunk_fault == "total":
                    result["totalBytes"] = 16 * 1024 * 1024 + 1
                if self.chunk_fault == "next_offset":
                    result["nextOffset"] = offset
                if self.chunk_fault == "encoding":
                    result["chunk"] = "not-base64!"
                return {"structuredContent": result}
            if name != "pi_session_get":
                return original_call(name, args)
            if self.missing_session:
                return {"structuredContent": {"session": None}}
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

    def enable_chunks(self):
        self.server.tools.append("pi_session_get_chunk")

    def test_oversized_single_message_complete_chunks_through_both_transports(self):
        self.enable_chunks()
        self.messages(count=12)
        # Tool output can be larger than the control-plane limit even alone.
        # Multibyte text is deliberately split across UTF-8 byte boundaries.
        self.server.messages[3]["content"] = "大工具输出🌸" * 15000
        self.server.messages[3]["toolResult"] = {"text": "完整输出" * 18000}
        for client in (self.runtime("review"),
                       runtime_client.HistoricalReadClient(str(self.connection_file))):
            self.server.calls.clear()
            rows, complete, detail = pi_desktop_controller.read_transcript(
                client, self.server.session_id,
            )
            self.assertTrue(complete, detail)
            self.assertEqual(rows, self.server.messages)
            calls = [args for name, args in self.server.calls
                     if name == "pi_session_get_chunk"]
            self.assertGreater(len(calls), 1)
            self.assertEqual(calls[0]["offset"], 0)
            self.assertNotIn("expectedSha256", calls[0])
            self.assertTrue(all("expectedSha256" in args for args in calls[1:]))
            self.assertTrue(all("contentLimit" not in args for args in calls))
            collector, _ = self.collector(client, "complete-chunks")
            collector.poll()
            self.assertFalse(collector.truncated)
            self.assertIsNone(collector.read_error)
            self.assertEqual(collector.verified_end, 12)
            self.assertEqual(self.server.prompt_count, 0)

    def test_invalid_or_changed_chunks_never_settle_complete_transcript(self):
        self.enable_chunks()
        self.messages(count=2)
        self.server.messages[1]["content"] = "x" * 90000
        client = runtime_client.HistoricalReadClient(str(self.connection_file))
        for fault in ("changed_hash", "hash_mismatch", "identity", "offset", "total",
                      "next_offset", "encoding", "page_identity", "page_boundary",
                      "page_more", "empty_page"):
            with self.subTest(fault=fault):
                self.chunk_fault = fault
                if fault == "page_more":
                    # start=0 with hasMoreBefore=true must never mean complete.
                    self.server.messages = self.server.messages[-1:]
                self.server.calls.clear()
                rows, complete, detail = pi_desktop_controller.read_transcript(
                    client, self.server.session_id,
                )
                self.assertFalse(complete)
                self.assertEqual(rows, [])
                self.assertIn("transcript_read_failed", detail)
                self.assertEqual(self.server.prompt_count, 0)
                chunk_calls = [args for name, args in self.server.calls
                               if name == "pi_session_get_chunk"]
                self.assertEqual(len(chunk_calls), 2 if fault in (
                    "changed_hash", "hash_mismatch", "page_identity", "page_boundary", "page_more"
                ) else 1)

    def test_one_oversized_tail_does_not_exhaust_long_history_page_budgets(self):
        self.enable_chunks()
        for count in (100, 241):  # beyond controller's 60 and worker's 200 pages
            with self.subTest(count=count):
                self.messages(count=count)
                self.server.messages[-1]["content"] = "完整大输出🌸" * 15000
                client = self.runtime("review")
                self.server.calls.clear()
                rows, complete, detail = pi_desktop_controller.read_transcript(
                    client, self.server.session_id,
                )
                self.assertTrue(complete, detail)
                self.assertEqual(rows, self.server.messages)
                self.assertEqual(next(args["messageLimit"] for args in self.transcript_calls()
                                      if args.get("messageBefore") == count - 1), 40)
                self.assertLess(len([args for args in self.transcript_calls()
                                     if args["messageLimit"] == 1]), 3)
                self.server.calls.clear()
                collector, _ = self.collector(client, f"long-chunks-{count}")
                collector.poll()
                self.assertFalse(collector.truncated)
                self.assertIsNone(collector.read_error)
                self.assertEqual(collector.verified_end, count)
                self.assertEqual(len(collector.seen), count)
                self.assertEqual(next(args["messageLimit"] for args in self.transcript_calls()
                                      if args.get("messageBefore") == count - 1), 40)
                self.assertEqual(self.server.prompt_count, 0)

    def test_nonminimum_tail_shrink_does_not_exhaust_ordinary_history_budget(self):
        self.messages(count=794, text_size=100)
        # These fit as a two-message page but not a five-message page. The older
        # ordinary rows fit at 40; retaining 2 would exceed both page budgets.
        for row in self.server.messages[-5:]:
            row["content"] = "x" * 8000
        client = self.runtime("review")
        rows, complete, detail = pi_desktop_controller.read_transcript(
            client, self.server.session_id,
        )
        self.assertTrue(complete, detail)
        self.assertEqual(rows, self.server.messages)
        self.assertEqual(next(args["messageLimit"] for args in self.transcript_calls()
                              if args.get("messageBefore") == 792), 40)
        self.server.calls.clear()
        collector, _ = self.collector(client, "nonminimum-tail")
        collector.poll()
        self.assertFalse(collector.truncated)
        self.assertIsNone(collector.read_error)
        self.assertEqual(collector.verified_end, 794)
        self.assertEqual(len(collector.seen), 794)
        self.assertEqual(next(args["messageLimit"] for args in self.transcript_calls()
                              if args.get("messageBefore") == 792), 40)
        self.assertEqual(self.server.prompt_count, 0)

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
            [(None, 40), (None, 20), (48, 40), (48, 20), (28, 40)],
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
                             [40, 20, 40, 20, 40])
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
                             [40, 20, 10, 5, 2, 1])

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

    def test_missing_session_is_not_found_never_an_empty_complete_transcript(self):
        client = self.runtime("review")
        self.missing_session = True
        for facade in (client, runtime_client.HistoricalReadClient(str(self.connection_file))):
            rows, complete, detail = pi_desktop_controller.read_transcript(
                facade, self.server.session_id,
            )
            self.assertEqual(rows, [])
            self.assertFalse(complete)
            self.assertIn("会话已不存在", detail)
            collector, _ = self.collector(facade, "missing-session")
            collector.poll()
            self.assertIn("会话已不存在", collector.read_error)
            self.assertEqual(collector.verified_end, 0)
            self.assertEqual(self.server.prompt_count, 0)

    def test_broken_physical_chain_still_fails_after_page_shrink(self):
        self.messages()
        self.break_page_chain = True
        collector, _ = self.collector(self.runtime())
        collector.poll()
        self.assertTrue(collector.truncated)
        self.assertEqual(collector.verified_end, 0)
        self.assertEqual(len(collector.seen), 20)  # already read evidence retained
        self.assertEqual(self.server.prompt_count, 0)
