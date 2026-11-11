"""KP-EP01：执行端一行进展（progress 事件）与 round_detail 投影。

只使用临时事件日志与普通字典行，不接触真实运行目录或数据库。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import CORE  # noqa: F401 - installs isolated core import path
from kanpilot import dsh_events
from kanpilot import pi_desktop_worker as worker


ROW = {
    "task_id": "task", "run_id": "run", "backend": "pi_desktop",
    "session_id": "session", "session_title": None, "run_status": "running",
    "config_json": "{}", "worktree_path": None, "started_at": None,
    "pid": None, "save_state": "active", "saved_at": None,
    "created_at": None, "updated_at": None,
}


class ProgressTextTests(unittest.TestCase):
    def test_first_readable_line_is_normalized(self):
        self.assertEqual(dsh_events.progress_text("## 已修复排序\n\n继续验证。"), "已修复排序")
        self.assertEqual(dsh_events.progress_text("\n\n- **正在构建** 尾部"), "正在构建 尾部")
        self.assertEqual(dsh_events.progress_text("   多   个   空格   "), "多 个 空格")

    def test_noise_is_not_reported(self):
        for value in ("", "   \n  ", None, 3,
                      '{"checks": []}', "[1, 2]", "<html>",
                      "```json\n{\"a\": 1}\n```", "```\nplain\n```"):
            with self.subTest(value=value):
                self.assertIsNone(dsh_events.progress_text(value))

    def test_long_line_is_capped(self):
        text = dsh_events.progress_text("字" * 400)
        self.assertEqual(len(text), 160)
        self.assertTrue(text.endswith("…"))


class CollectorProgressTests(unittest.TestCase):
    def collector(self, directory):
        log = worker._EventLog(Path(directory) / "events.jsonl")
        collector = worker._Collector(None, "session", log, "")
        collector.gate_open = True
        return collector, log

    def test_one_progress_per_content_and_revision_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            collector, log = self.collector(directory)
            message = {"id": "m1", "role": "assistant", "content": "先看布局。",
                       "timestamp": "2026-11-10T09:00:00Z", "turnId": "turn"}
            collector._emit(message)
            collector._emit(message)
            collector._emit(dict(message, content="已调整间距。"))
            events = [json.loads(line) for line in log.path.read_text().splitlines()]
            self.assertEqual([event["type"] for event in events],
                             ["assistant_message", "progress",
                              "assistant_message", "progress"])
            self.assertEqual(events[1]["text"], "先看布局。")
            self.assertEqual(events[-1]["text"], "已调整间距。")
            self.assertEqual(events[-1]["at"], "2026-11-10T09:00:00Z")

    def test_empty_and_tool_messages_do_not_report(self):
        with tempfile.TemporaryDirectory() as directory:
            collector, log = self.collector(directory)
            collector._emit({"id": "m2", "role": "assistant", "content": "   ",
                             "turnId": "turn"})
            collector._emit({"id": "m3", "role": "tool_result", "toolName": "Bash",
                             "toolCallId": "call", "turnId": "turn",
                             "toolResult": "output", "exitCode": 0})
            events = [json.loads(line) for line in log.path.read_text().splitlines()]
            self.assertNotIn("progress", [event["type"] for event in events])


class ProjectionTests(unittest.TestCase):
    def test_latest_progress_takes_last_non_empty(self):
        events = [
            {"type": "progress", "text": "第一步", "at": "2026-11-10T09:00:00Z"},
            {"type": "terminal", "outcome": "completed"},
            {"type": "progress", "text": "  第二步  ", "at": None},
        ]
        self.assertEqual(dsh_events.latest_progress(events),
                         {"text": "第二步", "at": None})
        self.assertIsNone(dsh_events.latest_progress([{"type": "progress", "text": " "}]))

    def test_old_log_falls_back_to_last_assistant_line(self):
        events = [
            {"seq": 1, "type": "assistant_message", "content": "旧进展", "ts": "2026-11-10T08:00:00Z"},
            {"seq": 2, "type": "assistant_message", "content": "```json\n{\"a\": 1}\n```", "ts": "2026-11-10T08:30:00Z"},
            {"seq": 3, "type": "assistant_message", "content": "## 已修复排序", "ts": "2026-11-10T09:00:00Z"},
        ]
        self.assertEqual(dsh_events.latest_progress(events),
                         {"text": "已修复排序", "at": "2026-11-10T09:00:00Z"})

    def test_progress_is_not_a_message(self):
        messages = dsh_events.to_messages([
            {"seq": 1, "type": "progress", "text": "进展"},
            {"seq": 2, "type": "assistant_message", "content": "正文"},
        ])
        self.assertEqual([message["content"] for message in messages], ["正文"])

    def test_round_detail_exposes_progress(self):
        events = [{"type": "session", "seq": 1, "session_id": "session"},
                  {"type": "progress", "seq": 2, "text": "正在构建",
                   "at": "2026-11-10T09:00:00Z"}]
        detail = dsh_events.round_detail(ROW, {"status": "running"}, events)
        self.assertEqual(detail["progress"],
                         {"text": "正在构建", "at": "2026-11-10T09:00:00Z"})



if __name__ == "__main__":
    unittest.main()
