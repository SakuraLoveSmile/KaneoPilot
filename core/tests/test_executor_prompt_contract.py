"""执行端 prompt 契约（protocol.md §5.24.4/§5.24.5，v4 T6.3）。

四个 prompt（_preamble/dispatch/monitor/review）必须与冻结契约一致：

- 角色流程 + 控制端后端分支都写明（``pi`` 分支命令不变；``pi_desktop`` 分支在
  Plus ``mode="agent"`` 控制会话里跑同一角色 prompt，**不**拉起 ``pi -p``）。
- Plus 控制轮的固定包装器与显式宿主环境（六个契约键）。
- Review 的冻结 JSON 结论结构、登记顺序（**先** create 后 register）、报告根
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


class DispatchPromptBackendContractTests(unittest.TestCase):
    def test_prepare_and_confirm_use_claimed_backend_and_config_snapshot(self):
        prompt = read("dispatch.md")

        self.assertIn("pi_desktop", prompt)
        self.assertIn('"backend":<context.backend>', prompt)
        self.assertIn("context.exec_config 原样", prompt)
        self.assertNotIn('"backend":"pi"', prompt)

    def test_controller_backend_branches_are_documented(self):
        prompt = read("dispatch.md")

        self.assertIn("controller_backend", prompt)
        self.assertIn('`mode="agent"`', prompt)
        self.assertIn("`pi -p --mode json`", prompt)
        self.assertIn("{KANPILOT}", prompt)
        self.assertIn("queued", prompt)

    def test_plus_branch_keeps_same_role_flow_and_stop_policy(self):
        prompt = read("dispatch.md")

        # 角色流程与命令一字未变（Plus 分支只改控制轮，不改流程）。
        for token in ("dispatch-prepare", "dispatch-begin-send",
                      "dispatch-start", "dispatch-confirm", "dispatch-fail"):
            self.assertIn(token, prompt, token)
        self.assertIn("stale_run", prompt)
        self.assertIn("不**重发", prompt)


class PreambleContractTests(unittest.TestCase):
    def test_explicit_host_environment_and_wrapper(self):
        preamble = read("_preamble.md")

        for key in HOST_ENV_KEYS:
            self.assertIn(key, preamble, key)
        self.assertIn("包装器", preamble)
        self.assertIn("{KANPILOT}", preamble)
        self.assertIn("不继承", preamble)

    def test_queue_control_round_and_permission_semantics(self):
        preamble = read("_preamble.md")

        self.assertIn("已排队 ≠ 已开始", preamble)
        self.assertIn("控制模型回合", preamble)
        self.assertIn("权限", preamble)
        self.assertIn("send_state=unknown", preamble)
        self.assertIn("**不重发**", preamble)

    def test_report_root_is_task_and_run_scoped(self):
        preamble = read("_preamble.md")

        self.assertIn("<task_id>/<run_id>.md", preamble)
        self.assertIn("{APP_SUPPORT}/reports", preamble)


class MonitorPromptContractTests(unittest.TestCase):
    def test_monitor_never_creates_execution_sessions(self):
        prompt = read("monitor.md")

        self.assertIn("新建任务执行会话", prompt)
        self.assertIn("round-observe", prompt)
        self.assertIn("session-messages", prompt)
        self.assertIn("控制模型回合结束", prompt)


class ReviewPromptContractTests(unittest.TestCase):
    def test_plus_branch_freezes_json_contract(self):
        prompt = read("review.md")

        self.assertIn('"conclusion":"pass|changes_requested|blocked"', prompt)
        self.assertIn(
            '"priority":"no-priority|low|medium|high|urgent"', prompt
        )
        self.assertIn("report_markdown", prompt)
        self.assertIn("fix", prompt)
        # Plus 分支不 SubmitPlan / 不代批契约。
        self.assertIn("SubmitPlan", prompt)
        self.assertIn("SubmitGoal", prompt)
        self.assertIn("reject", prompt)

    def test_changes_requested_creates_before_register(self):
        prompt = read("review.md")

        create_at = prompt.index("create-review-fix")
        register_at = prompt.index("register-review", create_at + 1)
        self.assertLess(
            create_at, register_at,
            "changes_requested 必须先 create-review-fix 再 register-review",
        )
        self.assertIn("**先**", prompt)
        self.assertIn("fix_task_source_sha256", prompt)

    def test_report_root_and_registration_identity(self):
        prompt = read("review.md")

        self.assertIn("<task_id>/<run_id>.md", prompt)
        for field in ("request_id", "run_id", "expected_execution_run_id",
                      "review_key", "dedupe_key"):
            self.assertIn(field, prompt, field)
        self.assertIn("review_blocked", prompt)
        self.assertIn("stale_run", prompt)


class PromptRenderingContractTests(unittest.TestCase):
    """渲染层：四种 prompt 仍能展开 {PREAMBLE} 与固定路径占位符。"""

    def setUp(self):
        import sys
        core = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(core))
        from kanpilot import executor_runner
        self.executor_runner = executor_runner

    def test_render_expands_preamble_for_every_stage(self):
        for stage in ("dispatch", "monitor", "review"):
            prompt = self.executor_runner.render_prompt(stage)
            self.assertNotIn("{PREAMBLE}", prompt, stage)
            self.assertNotIn("{KANPILOT}", prompt, stage)
            self.assertIn("## 固定助手", prompt, stage)
            self.assertIn("KANPILOT_DB", prompt, stage)


if __name__ == "__main__":
    unittest.main()
