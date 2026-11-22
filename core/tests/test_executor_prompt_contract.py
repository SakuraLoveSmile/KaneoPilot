"""执行端 prompt 契约（protocol.md §5.24.4/§5.24.5，v4 T6.3）。

kp-e02 之后只剩 review（与它的 _preamble）走 prompt 路径；dispatch/monitor 是确定性流程。

- 角色流程 + 控制端后端分支都写明（``pi`` 分支遵守核心命令契约；``pi_desktop`` 分支在
  Plus ``mode="agent"`` 控制会话里跑同一角色 prompt，**不**拉起 ``pi -p``）。
- Plus 控制轮的固定包装器与显式宿主环境（六个契约键）。
- Review 的版本化评审文本、同任务修复登记与交付门控、报告根
  ``<task_id>/<run_id>.md``、登记身份字段；意外 proposal 只 reject 关闭。
- 不越权语义：控制轮 ``completed`` ≠ 请求成功、权限等待不算成功、
  「已排队 ≠ 已开始」、发送不明不重发。
"""

from pathlib import Path
import unittest

PROMPTS = Path(__file__).resolve().parents[1] / "executor_prompts"

HOST_ENV_KEYS = (
    "KANPILOT_DB", "KANPILOT_APP_SUPPORT", "KANPILOT_PLAN_ROOT",
    "KANPILOT_REPOS_ROOT", "KANPILOT_LEGACY_ROOT", "KANEO_CREDENTIALS_PATH",
)


def read(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


class ReviewPromptContractTests(unittest.TestCase):
    def test_plus_branch_requires_raw_markdown_transport(self):
        prompt = read("review.md")
        self.assertIn("pi_desktop", prompt)
        self.assertIn("review_text_v1", prompt)
        self.assertIn("**不要输出 JSON**", prompt)
        self.assertIn("pass/changes_requested/blocked", prompt)
        self.assertIn("no-priority/low/medium/high/urgent", prompt)
        self.assertIn("两段修复正文留空", prompt)
        self.assertIn("各出现一次", prompt)
        # Plus 分支不 SubmitPlan / 不代批契约。
        self.assertIn("SubmitPlan", prompt)
        self.assertIn("SubmitGoal", prompt)
        self.assertIn("reject", prompt)

    def test_changes_requested_registers_same_task_repair_without_plan(self):
        prompt = read("review.md")
        self.assertIn('"fix_mode":"same_task"', prompt)
        self.assertIn('"fix":{"title":', prompt)
        self.assertNotIn("create-review-fix", prompt)
        self.assertNotIn("fix_task_source_sha256", prompt)
        self.assertNotIn("needs_plan", prompt)
        self.assertIn("不创建 Kaneo 子任务", prompt)
        self.assertIn("不生成新计划", prompt)
        self.assertIn("report", prompt)

    def test_pass_keeps_automated_delivery_gate(self):
        prompt = read("review.md")
        self.assertIn("自动管理任务", prompt)
        self.assertIn("只登记 pass", prompt)
        self.assertIn("交付流程", prompt)
        self.assertIn("不直接推进 done", prompt)

    def test_report_root_and_registration_identity(self):
        prompt = read("review.md")

        self.assertIn("<task_id>/<run_id>.md", prompt)
        for field in ("request_id", "run_id", "expected_execution_run_id",
                      "review_key", "fix_mode"):
            self.assertIn(field, prompt, field)
        self.assertIn("review_blocked", prompt)
        self.assertIn("stale_run", prompt)


class PromptRenderingContractTests(unittest.TestCase):
    """渲染层：review prompt 仍能展开 {PREAMBLE} 与固定路径占位符（kp-e02 后只剩它）。"""

    def setUp(self):
        import sys
        core = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(core))
        from kanpilot import executor_runner
        self.executor_runner = executor_runner

    def test_render_expands_preamble_for_every_stage(self):
        for stage in ("review",):
            prompt = self.executor_runner.render_prompt(stage)
            self.assertNotIn("{PREAMBLE}", prompt, stage)
            self.assertNotIn("{PYTHON}", prompt, stage)
            self.assertNotIn("{KANPILOT}", prompt, stage)
            self.assertIn("## 固定助手", prompt, stage)
            self.assertIn("KANPILOT_DB", prompt, stage)

    def test_core_command_uses_the_cores_own_interpreter(self):
        import shlex
        import sys
        from unittest import mock

        core_script = str(self.executor_runner.constants.core_root() / "kanpilot.py")
        for executable in (sys.executable,
                           "/Applications/Kaneo Pilot.app/Contents/Resources/python/bin/python3"):
            with mock.patch.object(sys, "executable", executable):
                prompt = self.executor_runner.render_prompt("review")
            command = f"{shlex.quote(executable)} {core_script} <command>"
            self.assertIn(command, prompt, executable)
        # 路径含空格时必须带引号，否则执行端 shell 会把它拆成两个词。
        self.assertIn("'/Applications/Kaneo Pilot.app/", prompt)

    def test_core_command_never_falls_back_to_path_python(self):
        import sys
        from unittest import mock

        for empty in ("", None):
            with mock.patch.object(sys, "executable", empty):
                with self.assertRaises(self.executor_runner.util.KanpilotError) as ctx:
                    self.executor_runner.render_prompt("review")
            self.assertEqual(ctx.exception.code, "validation_failed")
        self.assertNotIn("python3 {KANPILOT}", read("_preamble.md"))


if __name__ == "__main__":
    unittest.main()
