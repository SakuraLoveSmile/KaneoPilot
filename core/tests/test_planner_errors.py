"""规划错误链测试（protocol §5.22.6，planner-fix-implementation T3）。

覆盖：codex_planner.map_event 的 error/turn.failed/item.completed(error)
→ system 事件；dsh_events.to_messages 的 system 透出；planner_runner 真实
子进程路径的失败分类（model_unavailable / process_exit / timeout）、
去重与 util.redact 脱敏；KANPILOT_CODEX_FAKE 下 job["provider"] 摘要。

真实路径测试用 KANPILOT_CODEX_BIN 指向 shell stub（不拉起真 codex）。
"""

from __future__ import annotations

import json
import os
import stat
import sys
import unittest
from pathlib import Path

# 供 `python3 -m unittest tests.test_planner_errors` 与 pytest 两种入口工作：
# tests/ 非包模块，显式把本目录加入 sys.path 再 import helpers。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import FAKE_BEARER_TOKEN, KanpilotTestCase  # noqa: E402
from kanpilot import codex_planner, constants, db, dsh_events  # noqa: E402
from kanpilot import planner_runner  # noqa: E402


def sh_quote(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"


class TestMapEventErrors(unittest.TestCase):
    """map_event：错误帧 → system；普通事件不受影响。"""

    def test_top_level_error_frame(self):
        parsed = codex_planner.map_event({
            "type": "error",
            "error": {"detail": "boom", "code": "E_MODEL"},
        })
        self.assertIsNotNone(parsed)
        etype, _ts, fields = parsed
        self.assertEqual(etype, "system")
        self.assertEqual(fields["content"], "boom")
        self.assertEqual(fields["code"], "E_MODEL")

    def test_top_level_error_plain_message(self):
        _e, _t, fields = codex_planner.map_event(
            {"type": "error", "message": "plain message"})
        self.assertEqual(fields["content"], "plain message")
        # 无 code/http_status → code=None（emit 时省略该字段）。
        self.assertIsNone(fields["code"])

    def test_turn_failed_error_dict(self):
        parsed = codex_planner.map_event({
            "type": "turn.failed",
            "error": {
                "message": "The 'gpt-6-astra' model is not supported when "
                           "using Codex with a ChatGPT account.",
                "codex_error_info": "Other",
                "http_status": 400,
            },
        })
        etype, _ts, fields = parsed
        self.assertEqual(etype, "system")
        self.assertIn("not supported", fields["content"])
        self.assertIn("codex_error_info=Other", fields["content"])
        self.assertIn("http_status=400", fields["content"])
        self.assertEqual(fields["code"], 400)

    def test_turn_failed_error_string(self):
        _e, _t, fields = codex_planner.map_event(
            {"type": "turn.failed", "error": "oops"})
        self.assertEqual(fields["content"], "oops")

    def test_item_completed_error_variants(self):
        # item.type=error/turn_failed → system。
        for kind in ("error", "turn_failed"):
            etype, _t, fields = codex_planner.map_event({
                "type": "item.completed",
                "item": {"type": kind, "text": f"{kind} body"},
            })
            self.assertEqual(etype, "system")
            self.assertEqual(fields["content"], f"{kind} body")
        # 普通 item 带 error 字段 → system（优先于常规分流）。
        etype, _t, fields = codex_planner.map_event({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "ignored",
                     "error": {"msg": "embedded failure"}},
        })
        self.assertEqual(etype, "system")
        self.assertEqual(fields["content"], "embedded failure")

    def test_normal_events_unchanged(self):
        etype, _t, fields = codex_planner.map_event({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "hi"},
        })
        self.assertEqual(etype, "assistant_message")
        self.assertEqual(fields["content"], "hi")
        etype, _t, fields = codex_planner.map_event({
            "type": "item.completed",
            "item": {"type": "command_execution", "command": "ls",
                     "aggregated_output": "x", "exit_code": 0},
        })
        self.assertEqual(etype, "command_output")
        self.assertEqual(fields["exit_code"], 0)
        etype, _t, _f = codex_planner.map_event(
            {"type": "turn.completed", "usage": {"x": 1}})
        self.assertEqual(etype, "turn_end")
        # 未知类型 → None。
        self.assertIsNone(codex_planner.map_event({"type": "turn.started"}))
        self.assertIsNone(codex_planner.map_event({"type": "mystery"}))
        self.assertIsNone(codex_planner.map_event("not a dict"))


class TestSystemEventMessages(unittest.TestCase):
    """to_messages：system 事件透出为 type=system 消息（历史轮次可读）。"""

    def test_system_event_becomes_message(self):
        events = [
            {"v": 1, "seq": 1, "ts": "2026-09-20T00:00:00+00:00",
             "type": "session", "session_id": "t-1",
             "session_title": "codex plan"},
            {"v": 1, "seq": 2, "ts": "2026-09-20T00:00:01+00:00",
             "type": "system", "content": "bad model", "code": 400},
            {"v": 1, "seq": 3, "ts": "2026-09-20T00:00:02+00:00",
             "type": "terminal", "outcome": "failed",
             "reason": "model_unavailable"},
        ]
        messages = dsh_events.to_messages(events)
        kinds = [m["type"] for m in messages]
        self.assertEqual(kinds, ["system", "system", "system"])
        self.assertEqual(messages[1]["content"], "bad model")
        self.assertEqual(messages[1]["id"], "round:2")


class _RunnerCase(KanpilotTestCase):
    """真实 run_one 路径的公共夹具：needs_plan 任务 + 已授权 plan 请求。"""

    def _new_task(self, task_id):
        task = self.fake.add_task(
            task_id, "ryryo1reph1xfemqtwkjyiiz", status="to-do",
        )
        self.make_repo("Feedback")
        source_sha, req_sha, source_path = self.make_source_snapshot(task)
        self.sync_task(
            task, project_name="Feedback",
            requirements_sha256=req_sha, source_sha256=source_sha,
            source_path=source_path,
        )
        return task, source_sha, req_sha

    def _queue(self, task, source_sha, req_sha):
        return self.queue_plan(
            task["id"], source_sha256=source_sha,
            requirements_sha256=req_sha,
        )

    def _use_codex_stub(self, name, lines=None, raw_lines=None,
                        exit_code=1, sleep_seconds=0):
        """codex stub：``lines`` 为 JSON 帧列表（逐行输出），``raw_lines``
        为原样文本行；可选先 sleep 再输出。"""
        stub = self.root / f"codex-stub-{name}"
        parts = ["#!/bin/sh", f"sleep {sleep_seconds}"] if sleep_seconds \
            else ["#!/bin/sh"]
        for line in raw_lines or []:
            parts.append(f"printf '%s\\n' {sh_quote(line)}")
        for frame in lines or []:
            parts.append(f"printf '%s\\n' {sh_quote(json.dumps(frame))}")
        parts.append(f"exit {exit_code}")
        stub.write_text("\n".join(parts) + "\n", encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP
                   | stat.S_IXOTH)
        old = os.environ.get("KANPILOT_CODEX_BIN")
        os.environ["KANPILOT_CODEX_BIN"] = str(stub)

        def _restore(value=old):
            if value is None:
                os.environ.pop("KANPILOT_CODEX_BIN", None)
            else:
                os.environ["KANPILOT_CODEX_BIN"] = value

        self.addCleanup(_restore)

    def _prepare_run(self, settings=None):
        os.environ.pop("KANPILOT_CODEX_FAKE", None)
        self.addCleanup(os.environ.pop, "KANPILOT_CODEX_FAKE", None)
        conn = db.connect(self.db_path)
        try:
            db.set_setting(conn, "session_runs_root", str(self.runs_root))
            for key, value in (settings or {}).items():
                db.set_setting(conn, key, value)
            conn.commit()
        finally:
            conn.close()

    def _run_runner(self, task_id, settings=None):
        """同进程跑一轮真实 run_one。返回 run_dir（job.json/events.jsonl/
        codex.jsonl 所在目录）。"""
        self._prepare_run(settings)
        progressed = planner_runner.run_one(self.db_path)
        self.assertTrue(progressed, "run_one 未领取到 plan 请求")
        sessions = self.call("list-sessions", {"task_id": task_id})
        self.assertTrue(sessions["sessions"], "无 codex 轮次记录")
        session = sessions["sessions"][0]
        return self.runs_root / session["task_id"] / session["run_id"]

    def _read_events(self, run_dir: Path):
        events, detail = dsh_events.iter_events(
            run_dir / dsh_events.EVENT_LOG_NAME)
        self.assertIsNone(detail, f"events.jsonl 解析失败: {detail}")
        return events

    def _terminal_reason(self, events):
        terminal = dsh_events.terminal_event(events)
        self.assertIsNotNone(terminal, "缺 terminal 事件")
        return terminal["reason"]


class TestRunnerFailureClassification(_RunnerCase):
    def setUp(self):
        super().setUp()
        self.runs_root = self.app_support / "runs"
        self.runs_root.mkdir(parents=True, exist_ok=True)

    def test_http400_model_unavailable(self):
        """turn.failed + HTTP 400 → model_unavailable，note 含模型名与状态码。"""
        task, source_sha, req_sha = self._new_task("taskerr-400-model001")
        self._queue(task, source_sha, req_sha)
        self._use_codex_stub("m400", lines=[{
            "type": "turn.failed",
            "error": {
                "message": "The 'gpt-6-astra' model is not supported when "
                           "using Codex with a ChatGPT account.",
                "codex_error_info": "Other",
                "http_status": 400,
            },
        }])
        run_dir = self._run_runner(task["id"])

        view = self.task(task["id"])
        self.assertEqual(view["status"], "needs_plan")  # 失败回待规划
        self.assertEqual(view["failure_code"], "planner_failed")
        self.assertIn("gpt-6-astra", view["failure_reason"])
        self.assertIn("HTTP 400", view["failure_reason"])

        events = self._read_events(run_dir)
        self.assertEqual(self._terminal_reason(events), "model_unavailable")
        system = [e for e in events if e["type"] == "system"]
        self.assertTrue(
            any("not supported" in (e.get("content") or "")
                for e in system),
            f"system 事件缺错误内容: {system}")
        # system 事件带 code（http_status）字段。
        self.assertTrue(any(e.get("code") == 400 for e in system))
        # system 消息可经 to_messages 透出（历史日志可见）。
        messages = dsh_events.to_messages(events)
        self.assertTrue(any(
            m["type"] == "system" and "not supported" in (m["content"] or "")
            for m in messages))

        job = json.loads((run_dir / dsh_events.JOB_NAME).read_text())
        self.assertEqual(job["status"], "failed")
        self.assertTrue(job["detail"])
        self.assertNotIn("crash", str(job["detail"]))
        # 运行记录带脱敏 provider 摘要（fixture 是 custom/Sub2API）。
        self.assertEqual(job["provider"]["provider_id"], "custom")
        # 原始 JSONL 仍完整保留（脱敏后）。
        codex_log = (run_dir / constants.PLANNER_CODEX_LOG_NAME).read_text()
        self.assertIn("turn.failed", codex_log)

    def test_duplicate_error_shown_once(self):
        """同一错误帧重复出现 → events.jsonl 只有一条 system 事件。"""
        task, source_sha, req_sha = self._new_task("taskerr-dup00000001")
        self._queue(task, source_sha, req_sha)
        frame = {"type": "turn.failed", "error": {"message": "same failure"}}
        self._use_codex_stub("dup", lines=[frame, dict(frame)])
        run_dir = self._run_runner(task["id"])
        events = self._read_events(run_dir)
        system = [e for e in events if e["type"] == "system"]
        self.assertEqual(len(system), 1, f"重复错误未去重: {system}")

    def test_process_exit_without_error_frame(self):
        """非零退出且无错误帧 → process_exit，note 注明退出码。"""
        task, source_sha, req_sha = self._new_task("taskerr-exit20000001")
        self._queue(task, source_sha, req_sha)
        self._use_codex_stub("exit2", raw_lines=["some noise"], exit_code=2)
        run_dir = self._run_runner(task["id"])

        view = self.task(task["id"])
        self.assertIn("2", view["failure_reason"])
        events = self._read_events(run_dir)
        self.assertEqual(self._terminal_reason(events), "process_exit")
        job = json.loads((run_dir / dsh_events.JOB_NAME).read_text())
        self.assertEqual(job["exit_code"], 2)
        self.assertIn("退出码 2", job["detail"])

    def test_timeout_classification(self):
        """超过 codex_timeout_seconds → timeout 分类。"""
        task, source_sha, req_sha = self._new_task("taskerr-timeout00001")
        self._queue(task, source_sha, req_sha)
        self._use_codex_stub("sleepy", sleep_seconds=15, exit_code=0)
        run_dir = self._run_runner(
            task["id"], settings={"codex_timeout_seconds": 1})

        view = self.task(task["id"])
        self.assertEqual(view["failure_code"], "planner_timeout")
        self.assertIn("超时", view["failure_reason"])
        events = self._read_events(run_dir)
        self.assertEqual(self._terminal_reason(events), "timeout")
        job = json.loads((run_dir / dsh_events.JOB_NAME).read_text())
        self.assertEqual(job["status"], "failed")
        self.assertIn("timeout", job["detail"])

    def test_secrets_redacted_in_logs(self):
        """凭据形态文本进入 events.jsonl/codex.jsonl 前经 util.redact。"""
        task, source_sha, req_sha = self._new_task("taskerr-redact000001")
        self._queue(task, source_sha, req_sha)
        self._use_codex_stub(
            "redact",
            raw_lines=[f"plain stderr line bearer_token={FAKE_BEARER_TOKEN}"],
            lines=[{
                "type": "error",
                "message": f"request failed bearer_token={FAKE_BEARER_TOKEN}",
            }],
        )
        run_dir = self._run_runner(task["id"])
        events_text = (
            run_dir / dsh_events.EVENT_LOG_NAME).read_text()
        codex_log = (
            run_dir / constants.PLANNER_CODEX_LOG_NAME).read_text()
        # 假 token 无论经 secrets（provider.env）还是 _SECRET_PATTERNS
        # （bearer_token= 形态）都不得原值落盘。
        self.assertNotIn(FAKE_BEARER_TOKEN, events_text)
        self.assertNotIn(FAKE_BEARER_TOKEN, codex_log)
        self.assertIn("«redacted»", codex_log)

    def test_fake_path_records_provider(self):
        """KANPILOT_CODEX_FAKE 下 job.json 仍写 provider 摘要
        （fake=fail：不走到 publish-plan，provider 字段已先落盘）。"""
        task, source_sha, req_sha = self._new_task("taskerr-fake00000001")
        self._queue(task, source_sha, req_sha)
        self._prepare_run()
        os.environ["KANPILOT_CODEX_FAKE"] = "fail"
        self.addCleanup(os.environ.pop, "KANPILOT_CODEX_FAKE", None)
        self.assertTrue(planner_runner.run_one(self.db_path))
        sessions = self.call("list-sessions", {"task_id": task["id"]})
        session = sessions["sessions"][0]
        run_dir = self.runs_root / session["task_id"] / session["run_id"]
        job = json.loads((run_dir / dsh_events.JOB_NAME).read_text())
        self.assertIn("provider", job)
        # fixture 写入 model_provider=custom → 摘要为服务商脱敏视图。
        self.assertEqual(job["provider"]["provider_id"], "custom")
        # 脱敏：摘要不得含 token 值。
        self.assertNotIn("test-bearer-token", json.dumps(job["provider"]))


if __name__ == "__main__":
    unittest.main()
