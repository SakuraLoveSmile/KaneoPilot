"""pi 轮次 worker 测试（protocol §5.18.1/§5.18.2/§5.18.4，v1.6）。

覆盖契约条款：
- KANPILOT_PI_FAKE 六种分支语义（pi_worker 模块 docstring 冻结）：
  finished / failed / crash / running / 未知值→finished / 未设置→真实分支；
- run_real 生命周期（伪 pi 可执行走真实 ``pi -p --mode json`` JSONL 事件流）：
  job.json 生命周期（running→finished/failed、exit_code、ended_at）、
  events.jsonl 映射与首 session/末 terminal、协议帧不透传 worker stdout、
  超时分支（timeout）、子进程崩溃（crash）、exit 0 但未见 agent_end
  （early_exit）、cwd 缺失（cwd_missing）、身份三字段 + backend=pi；
- §5.18.2 半写容忍：截断 events.jsonl 尾行 → iter_events 仍返回此前有效事件。

隔离：全程不触真实 pi/模型调用——伪 pi 是本文件自写的 sh 假脚本
（按 ``--mode json`` 契约吐 JSONL 事件行）；KANPILOT_PI_BIN 指向它，
KANPILOT_PI_FAKE 在 run_real 用例中清空。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

from helpers import CORE, KanpilotTestCase  # noqa: E402
from kanpilot import dsh_events, pi_worker  # noqa: E402

TASK_ID = "taskpiworker00000000000001"
RUN_ID = "runpifake0001"
INSTRUCTION = "读取并执行计划文件，完成验证并报告结果。"

# 伪 pi 可执行：按 `pi -p --mode json` 契约吐 JSONL 事件行（无 JSON-RPC，
# 事件即 stdout 行）。模式经 FAKE_PI_MODE：ok（默认）| crash | hang |
# earlyexit；--version / auth check 只报就绪（exec-capabilities 兼容）。
FAKE_PI_SOURCE = r'''#!/bin/sh
# 伪 pi：--mode json 事件流假端（测试专用，绝不发起真实模型调用）。
if [ "$1" = "--version" ]; then echo "0.0.0-test"; exit 0; fi
if [ "$1" = "auth" ] && [ "$2" = "check" ]; then exit 0; fi
mode=${FAKE_PI_MODE:-ok}
pid_file=${FAKE_PI_PID_FILE:-}
if [ -n "$pid_file" ]; then echo $$ > "$pid_file"; fi
if [ "$mode" = "hang" ]; then sleep 120; exit 0; fi
# 非协议 stdout 噪声：worker 必须忽略（事件行与噪声的区分）。
echo "pi: boot noise, not a protocol frame"
echo '{"note": "valid JSON but not a pi event"}'
echo '{"type": "mystery_event", "note": "out of vocabulary"}'
if [ "$mode" = "crash" ]; then
  printf '%s\n' '{"type": "session", "id": "s-crash", "name": "Fake pi · 崩溃", "timestamp": 1758000000000}'
  exit 3
fi
printf '%s\n' '{"type": "session", "id": "s-fake-pi", "name": "Fake pi · ok", "timestamp": 1758000000000}'
printf '%s\n' '{"type": "message_end", "message": {"role": "user", "timestamp": 1758000001000, "content": [{"type": "text", "text": "INSTRUCTION_PLACEHOLDER"}]}}'
if [ "$mode" = "earlyexit" ]; then exit 0; fi
printf '%s\n' '{"type": "message_end", "message": {"role": "assistant", "timestamp": 1758000002000, "model": "CloseAI/deepseek-flash", "stopReason": "stop", "content": [{"type": "thinking", "thinking": "先想"}, {"type": "text", "text": "执行完成。"}]}}'
printf '%s\n' '{"type": "tool_execution_start", "timestamp": 1758000003000, "toolName": "bash", "args": {"command": "ls"}}'
printf '%s\n' '{"type": "tool_execution_end", "timestamp": 1758000004000, "toolName": "bash", "args": {"command": "ls"}, "result": {"content": [{"type": "text", "text": "a\nb"}], "exitCode": 0}}'
printf '%s\n' '{"type": "tool_execution_end", "timestamp": 1758000005000, "toolName": "read", "result": {"content": [{"type": "text", "text": "1|hello"}]}}'
printf '%s\n' '{"type": "agent_end", "timestamp": 1758000006000, "willRetry": false}'
exit 0
'''


class PiWorkerCase(KanpilotTestCase):
    """共享接缝：spec 构造、env 覆盖、伪 pi 可执行、事件读取断言。"""

    # ---------------------------------------------------------- 构造

    def make_spec(self, run_dir, cwd, timeout=30.0) -> dict:
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        return {
            "v": 1,
            "task_id": TASK_ID,
            "run_id": RUN_ID,
            "session_marker": f"{TASK_ID}/{RUN_ID}",
            "config": {
                "provider": "CloseAI", "model": "deepseek-flash",
                "reasoning_effort": "max", "agent_preset": "default",
            },
            "cwd": str(cwd),
            "worktree_mode": "shared",
            "repository": str(self.repos_root / "Feedback"),
            "plan_path": str(self.root / "plan.md"),
            "instruction": INSTRUCTION,
            "runs_dir": str(run_dir),
            "pi_home": str(self.root / "pi-home"),
            "agent_md": str(self.root / "agents" / "default.md"),
            "timeout_seconds": timeout,
        }

    def write_spec(self, spec) -> Path:
        path = Path(spec["runs_dir"]) / "spec.json"
        path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        return path

    def write_fake_pi(self) -> Path:
        """伪 pi：sh 脚本按 --mode json 契约吐事件行。"""
        path = self.root / "fake-pi-bin" / "pi"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            FAKE_PI_SOURCE.replace("INSTRUCTION_PLACEHOLDER", INSTRUCTION),
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    @contextlib.contextmanager
    def env(self, **overrides):
        saved = {}
        for key, value in overrides.items():
            saved[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        try:
            yield
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    # ---------------------------------------------------------- 读取断言

    def read_events(self, run_dir):
        events, detail = dsh_events.iter_events(
            Path(run_dir) / dsh_events.EVENT_LOG_NAME
        )
        self.assertIsNone(detail, f"事件日志应可完整解析：{detail}")
        return events

    def read_job(self, run_dir, backend="pi"):
        job, error = dsh_events.read_job(
            Path(run_dir), TASK_ID, RUN_ID, backend=backend
        )
        self.assertIsNone(error, f"job.json 身份核验应通过：{error}")
        return job

    def assert_event_log_contract(self, events):
        """§5.18.2：v=1、seq 单调递增、ts/type 形状。"""
        self.assertTrue(events, "events.jsonl 不应为空")
        self.assertEqual([e["v"] for e in events], [1] * len(events))
        seqs = [e["seq"] for e in events]
        self.assertEqual(seqs, sorted(seqs), "seq 必须单调递增")
        self.assertEqual(len(set(seqs)), len(seqs), "seq 必须唯一")
        for event in events:
            self.assertIsInstance(event["seq"], int)
            self.assertIsInstance(event["ts"], str)
            self.assertIsInstance(event["type"], str)

    def run_real_with_fake(self, mode="ok", timeout=30.0, cwd=None):
        """run_real（真实分支）+ 伪 pi 可执行；返回 (rc, run_dir, spec)。"""
        run_dir = self.root / "runs" / RUN_ID
        worktree = Path(cwd) if cwd else (self.root / "worktree")
        if cwd is None:
            worktree.mkdir(parents=True, exist_ok=True)
        spec = self.make_spec(run_dir, worktree, timeout=timeout)
        fake = self.write_fake_pi()
        pid_file = self.root / "fake-pi.pid"
        if pid_file.exists():
            pid_file.unlink()
        captured = io.StringIO()
        with self.env(KANPILOT_PI_FAKE=None, KANPILOT_PI_BIN=str(fake),
                      FAKE_PI_MODE=mode, FAKE_PI_PID_FILE=str(pid_file)):
            with contextlib.redirect_stdout(captured), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = pi_worker.run_real(spec)
        # 协议帧绝不透传到 worker stdout（docstring 冻结）。
        self.assertEqual(
            captured.getvalue(), "",
            f"run_real 不得向 stdout 输出任何内容：{captured.getvalue()!r}",
        )
        return rc, run_dir, spec


# ---------------------------------------------------------------- KANPILOT_PI_FAKE


class TestFakeModes(PiWorkerCase):
    def _run_main_fake(self, mode):
        """经 ``main --spec`` 走 KANPILOT_PI_FAKE 分支（spec 契约 v1）。"""
        run_dir = self.root / "runs" / RUN_ID
        spec = self.make_spec(run_dir, self.root / "worktree")
        spec_path = self.write_spec(spec)
        with self.env(KANPILOT_PI_FAKE=mode):
            rc = pi_worker.main(["--spec", str(spec_path)])
        return rc, run_dir

    def test_fake_finished_writes_job_and_terminal(self):
        rc, run_dir = self._run_main_fake("finished")
        self.assertEqual(rc, 0)
        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "finished")
        self.assertEqual(job["exit_code"], 0)
        self.assertIsNotNone(job["ended_at"])
        self.assertIsNotNone(job["started_at"])
        self.assertIsNone(job["detail"])
        self.assertTrue(job["session_id"].startswith("fake-session-"))
        self.assertTrue(job["session_title"].startswith("Fake pi"))
        # 身份三字段 + backend=pi（§5.18.1，read_job 已核验，此处再显式断言）。
        self.assertEqual(job["task_id"], TASK_ID)
        self.assertEqual(job["run_id"], RUN_ID)
        self.assertEqual(job["backend"], "pi")

        events = self.read_events(run_dir)
        self.assert_event_log_contract(events)
        self.assertEqual(events[0]["type"], "session")  # 首事件必须是 session
        self.assertEqual(events[-1]["type"], "terminal")
        self.assertEqual(events[-1]["outcome"], "completed")
        self.assertEqual(
            dsh_events.terminal_event(events),
            {"outcome": "completed", "reason": "ok"},
        )
        self.assertEqual(
            dsh_events.session_event(events)["session_id"], job["session_id"]
        )

    def test_fake_failed_marks_job_and_terminal_failed(self):
        rc, run_dir = self._run_main_fake("failed")
        self.assertEqual(rc, 1)
        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["exit_code"], 1)
        self.assertEqual(job["detail"], "fake failure")
        self.assertIsNotNone(job["ended_at"])
        self.assertTrue(job["session_title"].startswith("Fake pi"))

        events = self.read_events(run_dir)
        self.assert_event_log_contract(events)
        self.assertEqual(events[0]["type"], "session")
        self.assertEqual(events[-1]["type"], "terminal")
        self.assertEqual(events[-1]["outcome"], "failed")
        self.assertEqual(events[-1]["reason"], "fake failure")

    def test_fake_unknown_mode_falls_back_to_finished(self):
        """未知 fake 值 → 按 finished 语义（docstring 冻结分支）。"""
        rc, run_dir = self._run_main_fake("mystery-mode")
        self.assertEqual(rc, 0)
        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "finished")
        self.assertEqual(job["exit_code"], 0)
        events = self.read_events(run_dir)
        self.assertEqual(events[-1]["type"], "terminal")
        self.assertEqual(events[-1]["outcome"], "completed")

    def test_fake_crash_leaves_running_scene_without_terminal(self):
        """crash：job 停在 running、无 terminal → §5.18.4 崩溃/截断现场。"""
        rc, run_dir = self._run_main_fake("crash")
        self.assertEqual(rc, 3)
        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "running", "crash 不得回写终态")
        self.assertIsNone(job["ended_at"])
        self.assertIsNone(job["exit_code"])

        events = self.read_events(run_dir)
        self.assert_event_log_contract(events)
        self.assertEqual(events[0]["type"], "session")
        self.assertTrue(events[0]["session_title"].startswith("Fake pi"))
        self.assertIsNone(
            dsh_events.terminal_event(events),
            "崩溃现场不得有 terminal 事件（读取层据此判异常）",
        )
        self.assertIsNotNone(dsh_events.session_event(events))

    def test_fake_running_writes_session_then_killed_by_test(self):
        """running：写 job(running)+session/user 后挂起；测试负责杀进程，
        不留僵尸。"""
        run_dir = self.root / "runs" / RUN_ID
        spec = self.make_spec(run_dir, self.root / "worktree")
        spec_path = self.write_spec(spec)
        env = dict(os.environ)
        env["KANPILOT_PI_FAKE"] = "running"
        env["PYTHONPATH"] = str(CORE)
        proc = subprocess.Popen(
            [sys.executable, "-m", "kanpilot.pi_worker",
             "--spec", str(spec_path)],
            cwd=str(CORE), env=env, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        saw_user = False
        detail = None
        deadline = time.monotonic() + 10.0
        try:
            while time.monotonic() < deadline:
                events, detail = dsh_events.iter_events(
                    Path(run_dir) / dsh_events.EVENT_LOG_NAME
                )
                if any(e.get("type") == "user" for e in events):
                    saw_user = True
                    break
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
        finally:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    proc.kill()
            _, stderr = proc.communicate(timeout=10)
        self.assertIsNotNone(
            proc.returncode, "挂起的 worker 必须被杀掉并回收（不留僵尸）"
        )
        self.assertTrue(
            saw_user,
            f"worker 未写 user 事件（rc={proc.returncode} "
            f"detail={detail} stderr={stderr.decode('utf-8', 'replace')}）",
        )

        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "running")
        self.assertIsNone(job["ended_at"])
        events = self.read_events(run_dir)
        self.assert_event_log_contract(events)
        self.assertEqual(events[0]["type"], "session")
        self.assertTrue(events[0]["session_title"].startswith("Fake pi"))
        self.assertEqual(
            [e["type"] for e in events], ["session", "user"],
            "running 模式只写 session/user，不写 terminal",
        )
        self.assertIsNone(dsh_events.terminal_event(events))

    def test_unset_fake_uses_real_branch_with_cwd_missing(self):
        """未设置 KANPILOT_PI_FAKE → run_real；cwd 缺失 →
        job(failed, detail=cwd_missing) + session + terminal(failed)，
        且不越权创建目录。"""
        run_dir = self.root / "runs" / RUN_ID
        missing = self.root / "missing-worktree"
        spec = self.make_spec(run_dir, missing)
        spec_path = self.write_spec(spec)
        with self.env(KANPILOT_PI_FAKE=None):
            rc = pi_worker.main(["--spec", str(spec_path)])
        self.assertEqual(rc, 1)
        self.assertFalse(missing.exists(), "worker 不得创建缺失的 cwd")

        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["exit_code"], 1)
        self.assertTrue(job["detail"].startswith("cwd_missing"))
        self.assertIsNotNone(job["ended_at"])

        events = self.read_events(run_dir)
        self.assert_event_log_contract(events)
        self.assertEqual(events[0]["type"], "session")  # 首事件恒为 session
        self.assertEqual(events[-1]["type"], "terminal")
        self.assertEqual(events[-1]["outcome"], "failed")
        self.assertEqual(events[-1]["reason"], "cwd_missing")


# ---------------------------------------------------------------- run_real


class TestRunRealLifecycle(PiWorkerCase):
    def test_full_protocol_finished_maps_all_event_types(self):
        rc, run_dir, spec = self.run_real_with_fake(mode="ok", timeout=30.0)
        self.assertEqual(rc, 0)

        # job.json 生命周期：running→finished、exit_code、ended_at、身份三字段。
        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "finished")
        self.assertEqual(job["exit_code"], 0)
        self.assertIsNotNone(job["started_at"])
        self.assertIsNotNone(job["ended_at"])
        self.assertIsNone(job["detail"])
        self.assertEqual(job["backend"], "pi")
        self.assertEqual(job["task_id"], TASK_ID)
        self.assertEqual(job["run_id"], RUN_ID)
        self.assertEqual(job["config"], spec["config"])
        self.assertGreater(job["pid"], 0)
        # 会话身份来自伪 pi 的 session 事件（{"type":"session","id","name"}）。
        self.assertEqual(job["session_id"], "s-fake-pi")
        self.assertEqual(job["session_title"], "Fake pi · ok")

        # events.jsonl 映射（pi --mode json 词汇全覆盖）+ 首 session/末 terminal。
        events = self.read_events(run_dir)
        self.assert_event_log_contract(events)
        self.assertEqual(events[0]["type"], "session")
        self.assertEqual(events[0]["session_id"], job["session_id"])
        self.assertEqual(events[-1]["type"], "terminal")
        self.assertEqual(events[-1]["outcome"], "completed")
        self.assertEqual(events[-1]["reason"], "completed")
        seqs = list(range(1, len(events) + 1))
        self.assertEqual([e["seq"] for e in events], seqs, "seq 从 1 连续单调")

        types = [e["type"] for e in events]
        for expected in (
            "session", "user", "assistant_message", "tool_call",
            "command_output", "tool_result", "turn_end", "terminal",
        ):
            self.assertIn(expected, types, f"缺少事件类型 {expected}")
        self.assertNotIn("mystery_event", types, "词汇外事件必须被丢弃")
        # user 事件回显 prompt 指令（pi 的首条 user message_end）。
        user = next(e for e in events if e["type"] == "user")
        self.assertEqual(user["content"], spec["instruction"])
        command = next(e for e in events if e["type"] == "command_output")
        self.assertEqual(command["name"], "bash")
        self.assertEqual(command["exit_code"], 0)
        self.assertEqual(command["output"], "a\nb")

        # 非协议 stdout 噪声行与词汇外事件不落盘：恰好 8 条映射事件
        #（session/user/assistant/tool_call/command_output/tool_result/
        # turn_end/terminal）。
        self.assertEqual(len(events), 8, types)

    def test_child_nonzero_exit_marks_crash(self):
        """子进程非 0 退出 → job(failed, detail=crash...) + terminal(failed)。"""
        rc, run_dir, _spec = self.run_real_with_fake(mode="crash")
        self.assertEqual(rc, 1)
        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["exit_code"], 3)
        self.assertEqual(job["detail"], "crash: exit code 3")
        self.assertEqual(job["session_id"], "s-crash")
        events = self.read_events(run_dir)
        self.assert_event_log_contract(events)
        self.assertEqual(events[0]["type"], "session")
        self.assertEqual(events[-1]["type"], "terminal")
        self.assertEqual(events[-1]["outcome"], "failed")
        self.assertEqual(events[-1]["reason"], "crash")

    def test_early_exit_without_agent_end_marks_failed(self):
        """exit 0 但未见 agent_end → job(failed, detail=early_exit)
        + terminal(failed)（疑似启动失败，§5.18.4）。"""
        rc, run_dir, _spec = self.run_real_with_fake(mode="earlyexit")
        self.assertEqual(rc, 1)
        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["exit_code"], 0)
        self.assertIn("agent_end", job["detail"])
        events = self.read_events(run_dir)
        self.assert_event_log_contract(events)
        self.assertEqual(events[0]["type"], "session")
        self.assertEqual(events[-1]["type"], "terminal")
        self.assertEqual(events[-1]["outcome"], "failed")
        self.assertEqual(events[-1]["reason"], "early_exit")

    def test_timeout_terminates_child_and_marks_failed(self):
        """spec.timeout_seconds 极小 → job failed + detail timeout +
        terminal(failed, reason=timeout)，子进程被回收（无残留）。"""
        pid_file = self.root / "fake-pi.pid"
        rc, run_dir, _spec = self.run_real_with_fake(
            mode="hang", timeout=1.5
        )
        self.assertEqual(rc, 1)
        job = self.read_job(run_dir)
        self.assertEqual(job["status"], "failed")
        self.assertIn("timeout", job["detail"], job["detail"])
        self.assertIsNotNone(job["ended_at"])

        events = self.read_events(run_dir)
        self.assert_event_log_contract(events)
        self.assertEqual(events[0]["type"], "session")
        self.assertEqual(events[-1]["type"], "terminal")
        self.assertEqual(events[-1]["outcome"], "failed")
        self.assertEqual(events[-1]["reason"], "timeout")

        # 子进程组已终止并回收。
        self.assertTrue(pid_file.is_file(), "伪 pi 应已启动并写 pid 文件")
        child_pid = int(pid_file.read_text(encoding="utf-8").strip())
        deadline = time.monotonic() + 5.0
        alive = True
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.05)
        self.assertFalse(alive, f"超时后子进程 {child_pid} 仍存活")

    def test_half_written_tail_keeps_prior_events(self):
        """§5.18.2 半写容忍：人为截断 events.jsonl 尾行 →
        iter_events 仍返回此前有效事件（不当空会话）。"""
        rc, run_dir, _spec = self.run_real_with_fake(mode="ok")
        self.assertEqual(rc, 0)
        path = Path(run_dir) / dsh_events.EVENT_LOG_NAME
        before = self.read_events(run_dir)
        self.assertGreater(len(before), 3)

        with open(path, "a", encoding="utf-8") as stream:
            stream.write('{"v": 1, "seq": 999, "ts": "2026-01-01T00:00:00')

        after, detail = dsh_events.iter_events(path)
        self.assertIsNone(detail, "尾行截断不报错，保留此前有效内容")
        self.assertEqual(after, before, "截断尾行前的事件必须原样保留")

        # 读取层仍能关联（inspect 不因半写尾行失败）。
        result = dsh_events.inspect(run_dir, TASK_ID, RUN_ID)
        self.assertEqual(result["status"], "associated")
        self.assertEqual(len(result["messages"]), len(before))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
