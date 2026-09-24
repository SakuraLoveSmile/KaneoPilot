"""pi 后端（kanpilot.pi_backend）测试（protocol §5.18.1/§5.18.3，v1.6）。

覆盖契约条款：
- launcher_argv 形状：``pi -p --mode json --session-dir <run>/pi-sessions
  --no-extensions --no-skills --no-context-files [--model p/m]
  [--thinking l] [--system-prompt <md 内容>] <instruction 尾参>``——
  system_prompt 传 agent.md 文件**内容**（非路径），spec.system_prompt 优先；
- child_env：透传父进程环境 + PI_HOME 指向 spec.pi_home；不注入凭据值；
- map_event 事件映射：``pi --mode json`` JSONL 行 → (etype, ts, fields)：
  session / message_end(user→user, assistant→assistant_message) /
  tool_execution_start→tool_call / tool_execution_end→tool_result
  （bash 类→command_output 带 exit_code）/ agent_end→turn_end；
  未知 type、toolResult 角色、非 dict 行 → None（worker 丢弃不污染
  events.jsonl——端到端见 test_pi_worker 的伪 pi 用例）。
"""

from __future__ import annotations

import contextlib
import json
import os
import unittest
from pathlib import Path

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import dsh_events, pi_backend  # noqa: E402

INSTRUCTION = "读取并执行计划文件，完成验证并报告结果。"


class PiBackendCase(KanpilotTestCase):
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

    def make_spec(self, run_dir=None, **overrides) -> dict:
        run_dir = Path(run_dir or (self.root / "runs" / "runpi0001"))
        run_dir.mkdir(parents=True, exist_ok=True)
        spec = {
            "v": 1,
            "task_id": "taskbackend000000000001",
            "run_id": "runpi0001",
            "session_marker": "taskbackend000000000001/runpi0001",
            "config": {
                "provider": "CloseAI", "model": "deepseek-flash",
                "reasoning_effort": "max", "agent_preset": "default",
            },
            "cwd": str(self.root / "worktree"),
            "worktree_mode": "shared",
            "repository": str(self.repos_root / "Feedback"),
            "plan_path": str(self.root / "plan.md"),
            "instruction": INSTRUCTION,
            "runs_dir": str(run_dir),
            "pi_home": str(self.root / "pi-home"),
            "agent_md": str(self.root / "agents" / "default.md"),
            "timeout_seconds": 30,
        }
        spec.update(overrides)
        return spec

    def write_agent_md(self, content="# test agent\n") -> Path:
        path = self.root / "agents" / "default.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path


# ------------------------------------------------------------ launcher_argv


class TestLauncherArgv(PiBackendCase):
    def test_launcher_argv_shape(self):
        agent_md = self.write_agent_md("# test agent\n")
        spec = self.make_spec()
        argv = pi_backend.launcher_argv(spec, agent_md)

        # argv[0] = pi_bin（KANPILOT_PI_BIN 指向夹具 sh 桩）。
        self.assertEqual(argv[0], os.environ["KANPILOT_PI_BIN"])
        # 冻结的非交互 + JSONL 事件模式 + 隔离开关。
        for flag in (
            "-p", "--mode", "json", "--no-extensions", "--no-skills",
            "--no-context-files",
        ):
            self.assertIn(flag, argv, f"缺少参数 {flag}")
        self.assertLess(argv.index("--mode"), argv.index("json"))
        # 会话目录落在轮次目录下（随轮次归档）。
        session_dir = argv[argv.index("--session-dir") + 1]
        self.assertEqual(
            session_dir, str(Path(spec["runs_dir"]) / "pi-sessions")
        )
        # 模型 = provider/model；思考等级 = reasoning_effort。
        self.assertEqual(
            argv[argv.index("--model") + 1], "CloseAI/deepseek-flash"
        )
        self.assertEqual(argv[argv.index("--thinking") + 1], "max")
        # --system-prompt 传 agent.md 文件内容（pi 接受文本，不接受路径）。
        self.assertEqual(
            argv[argv.index("--system-prompt") + 1], "# test agent\n"
        )
        # 指令是尾参（argv 最后一个元素）。
        self.assertEqual(argv[-1], INSTRUCTION)

    def test_system_prompt_from_spec_wins_over_agent_md(self):
        agent_md = self.write_agent_md("# test agent\n")
        spec = self.make_spec(system_prompt="inline prompt")
        argv = pi_backend.launcher_argv(spec, agent_md)
        self.assertEqual(
            argv[argv.index("--system-prompt") + 1], "inline prompt"
        )

    def test_no_system_prompt_without_agent_md(self):
        spec = self.make_spec(agent_md="")
        argv = pi_backend.launcher_argv(spec, None)
        self.assertNotIn("--system-prompt", argv)
        self.assertEqual(argv[-1], INSTRUCTION)

    def test_missing_agent_md_file_is_skipped(self):
        spec = self.make_spec()
        argv = pi_backend.launcher_argv(
            spec, self.root / "agents" / "nonexistent.md"
        )
        self.assertNotIn("--system-prompt", argv)

    def test_optional_model_and_thinking_omitted(self):
        spec = self.make_spec(config={})
        argv = pi_backend.launcher_argv(spec, None)
        self.assertNotIn("--model", argv)
        self.assertNotIn("--thinking", argv)
        # model 缺省时 provider 不单独成参。
        self.assertNotIn("CloseAI/", argv)

    def test_provider_empty_model_bare(self):
        spec = self.make_spec(config={"provider": "", "model": "m-only"})
        argv = pi_backend.launcher_argv(spec, None)
        self.assertEqual(argv[argv.index("--model") + 1], "m-only")


# -------------------------------------------------------------- child_env


class TestChildEnv(PiBackendCase):
    def test_pi_home_injected_from_spec(self):
        spec = self.make_spec()
        env = pi_backend.child_env(spec)
        self.assertEqual(env["PI_HOME"], spec["pi_home"])

    def test_spec_pi_home_overrides_parent(self):
        spec = self.make_spec()
        with self.env(PI_HOME="/parent/pi-home"):
            env = pi_backend.child_env(spec)
        self.assertEqual(env["PI_HOME"], spec["pi_home"])

    def test_parent_env_passthrough_and_no_credential_injection(self):
        spec = self.make_spec()
        before = dict(os.environ)
        with self.env(KANPILOT_PI_FAKE="finished",
                      KANPILOT_PI_BIN="/tmp/fake/pi",
                      KANPILOT_MARKER_X="x1"):
            env = pi_backend.child_env(spec)
            # 不注入凭据值：除 PI_HOME 外与父进程环境一致（凭据由 pi 自身
            # PI_HOME/agent/auth.json 管理）。
            parent = dict(os.environ)
            parent["PI_HOME"] = spec["pi_home"]
        # 接缝/标记变量透传（父进程环境原样继承 + PI_HOME 覆盖）。
        self.assertEqual(env["KANPILOT_PI_FAKE"], "finished")
        self.assertEqual(env["KANPILOT_PI_BIN"], "/tmp/fake/pi")
        self.assertEqual(env["KANPILOT_MARKER_X"], "x1")
        self.assertEqual(env, parent)
        self.assertNotIn("CLOSEAI_API_KEY", set(env) - set(before))
        # 返回独立 dict，不改本进程 os.environ。
        self.assertIsNot(env, os.environ)

    def test_missing_pi_home_leaves_env_untouched(self):
        spec = self.make_spec()
        spec.pop("pi_home")
        with self.env(PI_HOME=None):
            env = pi_backend.child_env(spec)
        self.assertNotIn("PI_HOME", env)


# --------------------------------------------------------------- map_event


class TestMapEvent(PiBackendCase):
    def test_session_event(self):
        got = pi_backend.map_event({
            "type": "session", "id": "s1", "name": "T1 轮次",
            "timestamp": 1758000000000,
        })
        self.assertIsNotNone(got)
        etype, ts, fields = got
        self.assertEqual(etype, "session")
        self.assertTrue(ts.startswith("2025-09-16"), ts)  # ms → ISO
        self.assertEqual(fields["session_id"], "s1")
        self.assertEqual(fields["session_title"], "T1 轮次")

    def test_session_title_falls_back_to_cwd(self):
        _etype, _ts, fields = pi_backend.map_event(
            {"type": "session", "id": "s1", "cwd": "/work/tree"}
        )
        self.assertEqual(fields["session_title"], "/work/tree")

    def test_user_message_end(self):
        got = pi_backend.map_event({
            "type": "message_end",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "请修复登录 bug"}],
            },
        })
        etype, _ts, fields = got
        self.assertEqual(etype, "user")
        self.assertEqual(fields["content"], "请修复登录 bug")

    def test_user_message_string_content(self):
        etype, _ts, fields = pi_backend.map_event({
            "type": "message_end",
            "message": {"role": "user", "content": "直接文本"},
        })
        self.assertEqual(etype, "user")
        self.assertEqual(fields["content"], "直接文本")

    def test_assistant_message_end_joins_text_and_thinking(self):
        got = pi_backend.map_event({
            "type": "message_end",
            "message": {
                "role": "assistant", "timestamp": 1758000002000,
                "model": "CloseAI/deepseek-flash", "stopReason": "stop",
                "content": [
                    {"type": "thinking", "thinking": "先想"},
                    {"type": "text", "text": "执行完成。"},
                ],
            },
        })
        etype, ts, fields = got
        self.assertEqual(etype, "assistant_message")
        self.assertTrue(ts.startswith("2025-"))
        self.assertEqual(fields["content"], "[thinking] 先想\n执行完成。")
        self.assertEqual(fields["model"], "CloseAI/deepseek-flash")
        self.assertEqual(fields["stop_reason"], "stop")

    def test_tool_result_role_not_duplicated(self):
        """message_end role=toolResult → None（tool_execution_end 已产出）。"""
        self.assertIsNone(pi_backend.map_event({
            "type": "message_end",
            "message": {"role": "toolResult", "content": "x"},
        }))
        # 其他未知角色同样丢弃。
        self.assertIsNone(pi_backend.map_event({
            "type": "message_end", "message": {"role": "system"},
        }))
        self.assertIsNone(pi_backend.map_event({"type": "message_end"}))

    def test_tool_execution_start(self):
        got = pi_backend.map_event({
            "type": "tool_execution_start", "toolName": "bash",
            "args": {"command": "ls"}, "timestamp": 1758000003000,
        })
        etype, _ts, fields = got
        self.assertEqual(etype, "tool_call")
        self.assertEqual(fields["name"], "bash")
        self.assertEqual(json.loads(fields["arguments"]), {"command": "ls"})

    def test_tool_execution_end_bash_is_command_output(self):
        got = pi_backend.map_event({
            "type": "tool_execution_end", "toolName": "bash",
            "args": {"command": "ls"},
            "result": {
                "content": [{"type": "text", "text": "a\nb"}],
                "stderr": "warn",
                "exitCode": 3,
            },
        })
        etype, _ts, fields = got
        self.assertEqual(etype, "command_output")
        self.assertEqual(fields["name"], "bash")
        self.assertEqual(json.loads(fields["input"]), {"command": "ls"})
        self.assertEqual(fields["output"], "a\nb\nwarn")
        self.assertEqual(fields["exit_code"], 3)

    def test_tool_execution_end_other_command_shells(self):
        for name in ("pwsh", "shell"):
            etype, _ts, fields = pi_backend.map_event({
                "type": "tool_execution_end", "toolName": name,
                "result": {"content": [{"type": "text", "text": "ok"}],
                           "exitCode": 0},
            })
            self.assertEqual(etype, "command_output", name)
            self.assertEqual(fields["exit_code"], 0)

    def test_tool_execution_end_non_command_is_tool_result(self):
        got = pi_backend.map_event({
            "type": "tool_execution_end", "toolName": "read",
            "result": {"content": [{"type": "text", "text": "1|hello"}]},
        })
        etype, _ts, fields = got
        self.assertEqual(etype, "tool_result")
        self.assertEqual(fields["name"], "read")
        self.assertEqual(fields["output"], "1|hello")
        self.assertNotIn("exit_code", fields)

    def test_tool_execution_end_nonint_exit_code(self):
        _etype, _ts, fields = pi_backend.map_event({
            "type": "tool_execution_end", "toolName": "bash",
            "result": {"content": [], "exitCode": "0"},
        })
        self.assertIsNone(fields["exit_code"])
        _etype, _ts, fields = pi_backend.map_event({
            "type": "tool_execution_end", "toolName": "bash",
            "result": {"content": [], "exitCode": True},
        })
        self.assertIsNone(fields["exit_code"])

    def test_agent_end_maps_turn_end(self):
        got = pi_backend.map_event({"type": "agent_end", "willRetry": False})
        etype, _ts, fields = got
        self.assertEqual(etype, "turn_end")
        self.assertEqual(fields["will_retry"], False)

    def test_unknown_and_malformed_lines_rejected(self):
        """未知 type / 增量事件 / 非 dict → None（worker 丢弃，不落盘）。"""
        for line in (
            {"type": "message_update"},
            {"type": "thinking_delta", "delta": "x"},
            {"type": "progress", "pct": 1},
            {"type": 5},
            {"content": "no type"},
            None,
            ["session"],
            "session",
            42,
            {"jsonrpc": "2.0", "id": 1, "result": {}},
        ):
            self.assertIsNone(pi_backend.map_event(line), repr(line))

    def test_mapped_types_within_reader_vocabulary(self):
        """map_event 产出的 etype 必须全部在读取层词汇表内。"""
        produced = set()
        for line in (
            {"type": "session", "id": "s"},
            {"type": "message_end", "message": {"role": "user",
                                              "content": "x"}},
            {"type": "message_end", "message": {"role": "assistant",
                                              "content": "y"}},
            {"type": "tool_execution_start", "toolName": "bash"},
            {"type": "tool_execution_end", "toolName": "bash", "result": {}},
            {"type": "tool_execution_end", "toolName": "read", "result": {}},
            {"type": "agent_end"},
        ):
            got = pi_backend.map_event(line)
            self.assertIsNotNone(got, line)
            produced.add(got[0])
        self.assertTrue(produced <= set(dsh_events._EVENT_TO_MESSAGE),
                        f"词汇外类型：{produced - set(dsh_events._EVENT_TO_MESSAGE)}")

    def test_missing_timestamp_gives_empty_ts(self):
        """无 timestamp → ts=""（worker 补当前时刻，不落空字段）。"""
        _etype, ts, _fields = pi_backend.map_event({"type": "agent_end"})
        self.assertEqual(ts, "")
        _etype, ts, _fields = pi_backend.map_event(
            {"type": "session", "id": "s", "timestamp": "not-a-date"}
        )
        self.assertEqual(ts, "not-a-date")  # 字符串原样透出


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
