"""publish-plan 的模型快照解析（protocol §5.22.6 codex / §5.24.3 Plus，v4）。

背景：``cmd_publish_plan`` 用 ``planfile.check_plan_meta(expected_model=…)``
校验 frontmatter 的 ``planner_model/reasoning_effort``。Plus 规划请求
（``planner_backend="pi_desktop"``）按 §5.24.3 固化 ``runtime_config`` 五字段，
**不写** codex 的 ``model_config``；若核心只看 ``model_config``，Plus 计划就会
被迫回落 §6 历史常量、在计划文件里声称一个从未运行的 Codex 模型。

本测试锁定修复后的解析规则（``cmd_exec._publish_expected_model``）：

- codex 请求：用 ``model_config``；
- Plus 请求：用 ``runtime_config`` 的 ``model_id``/``thinking_level``；
- 两者都缺/快照不完整：None（沿用旧记录的历史常量兼容路径）。
"""
from __future__ import annotations

import unittest

from kanpilot import cmd_exec, constants  # noqa: E402


class PublishExpectedModel(unittest.TestCase):
    def test_codex_request_uses_model_config_snapshot(self):
        payload = {
            "planner_backend": "codex",
            "model_config": {"planner_model": "gpt-6-astra",
                             "reasoning_effort": "medium"},
        }
        self.assertEqual(
            cmd_exec._publish_expected_model(payload),
            {"planner_model": "gpt-6-astra", "reasoning_effort": "medium"},
        )

    def test_plus_request_uses_runtime_snapshot_not_codex_constants(self):
        payload = {
            "planner_backend": "pi_desktop",
            "runtime_config": {
                "connection_file": "/Users/x/.pi-desktop-plus/mcp-control.json",
                "provider_id": "prov-1",
                "model_id": "deepseek/deepseek-v4.1-flash",
                "thinking_level": "high",
                "permission_mode": "ask",
            },
        }
        self.assertEqual(
            cmd_exec._publish_expected_model(payload),
            {"planner_model": "deepseek/deepseek-v4.1-flash",
             "reasoning_effort": "high"},
        )
        # 明确不得回落到 codex 常量（那会让计划文件声称未运行的模型）。
        self.assertNotEqual(
            cmd_exec._publish_expected_model(payload),
            dict(constants.STANDARD_PLANNER),
        )

    def test_plus_snapshot_incomplete_falls_back_to_none(self):
        base = {"planner_backend": "pi_desktop"}
        for runtime in (
            None,
            {},
            {"model_id": "m"},                       # 缺 thinking_level
            {"thinking_level": "high"},              # 缺 model_id
            {"model_id": "", "thinking_level": "high"},
            {"model_id": "m", "thinking_level": ""},
            {"model_id": 5, "thinking_level": "high"},
        ):
            payload = dict(base)
            if runtime is not None:
                payload["runtime_config"] = runtime
            with self.subTest(runtime=runtime):
                self.assertIsNone(cmd_exec._publish_expected_model(payload))

    def test_missing_backend_keeps_legacy_behaviour(self):
        # 旧请求缺 planner_backend/model_config → None（历史常量兼容）。
        self.assertIsNone(cmd_exec._publish_expected_model({}))
        self.assertIsNone(cmd_exec._publish_expected_model({"model_config": None}))
        # 非 dict / 缺 planner_model 的 model_config 不算快照。
        self.assertIsNone(
            cmd_exec._publish_expected_model({"model_config": "gpt-6-astra"})
        )
        self.assertIsNone(
            cmd_exec._publish_expected_model({"model_config": {}})
        )

    def test_codex_snapshot_wins_over_runtime_block(self):
        payload = {
            "planner_backend": "codex",
            "model_config": {"planner_model": "gpt-6-astra",
                             "reasoning_effort": "medium"},
            "runtime_config": {"model_id": "x", "thinking_level": "high"},
        }
        self.assertEqual(
            cmd_exec._publish_expected_model(payload),
            {"planner_model": "gpt-6-astra", "reasoning_effort": "medium"},
        )


if __name__ == "__main__":
    unittest.main()
