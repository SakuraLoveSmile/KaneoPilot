"""get/set/exec-capabilities 命令层（protocol §5.17.2，v1.6）。

覆盖：默认 config=null + defaults；合并保存（已存值 > 默认值，payload 覆盖）；
非法 provider/model/effort/preset → bad_request 且中文原因含可用列表；
思考等级按模型支持子集校验（deepseek-flash 可选 max；Restricted/small
仅 low/medium 的模型选 max 拒绝——测试内临时改写 tmp 夹具 models.json）；
空 payload bad_request；响应绝不含 apiKey/密钥值；event exec_config_updated
落库（detail 四字段、不含凭据）；set 不影响已 claim 轮次的 config_json
快照（§5.17.3）。
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import round_store, util  # noqa: E402

DEFAULTS = {
    "provider": "CloseAI",
    "model": "deepseek-flash",
    "reasoning_effort": "xhigh",
    "agent_preset": "default",
}
# 测试夹具凭据值（绝不出现在任何响应里——pi auth check 只看退出码）。
CREDENTIAL_VALUE = "test-key-placeholder"


class ExecConfigCommands(KanpilotTestCase):
    # ------------------------------------------------------------- get/set

    def test_get_defaults_before_any_save(self):
        resp = self.call("get-exec-config", {})
        self.assertEqual(set(resp), {"config", "defaults"})
        self.assertIsNone(resp["config"])  # 从未保存
        self.assertEqual(resp["defaults"], DEFAULTS)

    def test_set_merges_partial_then_get_returns_saved(self):
        saved = self.call("set-exec-config", {"reasoning_effort": "low"})
        self.assertEqual(set(saved), {"config"})
        config = saved["config"]
        # 合并语义：payload 只给一个字段，其余取默认。
        self.assertEqual(config["reasoning_effort"], "low")
        self.assertEqual(config["provider"], "CloseAI")
        self.assertEqual(config["model"], "deepseek-flash")
        self.assertEqual(config["agent_preset"], "default")
        self.assertIsInstance(config["updated_at"], str)
        # get 返回存过的完整配置（含 updated_at）。
        got = self.call("get-exec-config", {})
        self.assertEqual(got["config"], config)
        # 第二次部分保存保留其余已存值。
        self.call("set-exec-config", {"agent_preset": "default"})
        got2 = self.call("get-exec-config", {})
        self.assertEqual(got2["config"]["reasoning_effort"], "low")
        self.assertEqual(got2["config"]["agent_preset"], "default")
        self.assertEqual(set(got2["defaults"]), set(DEFAULTS))
        self.assertEqual(got2["defaults"], DEFAULTS)

    def test_set_invalid_values_bad_request_with_available_list(self):
        cases = [
            ({"provider": "ghost"}, "provider 不存在", "可用"),
            ({"model": "ghost"}, "模型不存在", "可用"),
            ({"reasoning_effort": "off"}, "不支持思考等级 off", "支持"),
            ({"agent_preset": "ghost"}, "预设不存在", "可用"),
        ]
        for payload, part1, part2 in cases:
            with self.subTest(payload=payload):
                exc = self.call_err("set-exec-config", payload)
                self.assertEqual(exc.code, "bad_request")
                message = str(exc)
                self.assertIn("执行配置校验失败", message)
                self.assertIn(part1, message)
                self.assertIn(part2, message)
        # 任一非法 payload 都不落库。
        self.assertIsNone(self.call("get-exec-config", {})["config"])

    def test_effort_max_legal_for_default_model(self):
        self.call("set-exec-config", {"reasoning_effort": "low"})
        saved = self.call("set-exec-config", {"reasoning_effort": "xhigh"})
        self.assertEqual(saved["config"]["reasoning_effort"], "xhigh")
        self.assertEqual(saved["config"]["provider"], "CloseAI")

    def test_effort_rejected_when_model_supports_only_low_medium(self):
        # 切到受限 provider（thinkingLevelMap 只有 low/medium）。
        saved = self.call(
            "set-exec-config",
            {"provider": "Restricted", "model": "small",
             "reasoning_effort": "low"},
        )
        self.assertEqual(saved["config"]["reasoning_effort"], "low")
        exc = self.call_err("set-exec-config", {"reasoning_effort": "max"})
        self.assertEqual(exc.code, "bad_request")
        message = str(exc)
        self.assertIn("不支持思考等级 max", message)
        self.assertIn("low, medium", message)

    def test_broken_preset_rejected(self):
        # agent_preset 必须对应 agents_dir 下存在的 <name>.md。
        exc = self.call_err(
            "set-exec-config", {"agent_preset": "ghost"}
        )
        self.assertEqual(exc.code, "bad_request")
        self.assertIn("预设不存在：ghost", str(exc))

    def test_empty_payload_bad_request(self):
        exc = self.call_err("set-exec-config", {})
        self.assertEqual(exc.code, "bad_request")
        self.assertIn("至少提供一个字段", str(exc))

    # ------------------------------------------------------------- 凭据红线

    def test_responses_never_contain_credentials(self):
        caps = self.call("exec-capabilities", {})
        cap_blob = json.dumps(caps, ensure_ascii=False)
        # 内部字段与凭据值绝不透出。
        self.assertNotIn("_providers_detail", cap_blob)
        self.assertNotIn("apiKey", cap_blob)
        self.assertNotIn(CREDENTIAL_VALUE, cap_blob)
        credential = next(
            c for c in caps["checks"] if c["name"] == "credential"
        )
        self.assertIn("凭据", credential["detail"])

        saved = self.call(
            "set-exec-config",
            {"provider": "CloseAI", "model": "deepseek-flash",
             "reasoning_effort": "xhigh", "agent_preset": "default"},
        )
        for blob in (
            json.dumps(saved, ensure_ascii=False),
            json.dumps(self.call("get-exec-config", {}),
                       ensure_ascii=False),
        ):
            self.assertNotIn(CREDENTIAL_VALUE, blob)
            self.assertNotIn("apiKey", blob)

    # ------------------------------------------------------------- 事件

    def test_event_written_without_credentials(self):
        self.call("set-exec-config", {"reasoning_effort": "medium"})
        events = self.call("list-events", {"limit": 50})["events"]
        hits = [e for e in events if e["kind"] == "exec_config_updated"]
        self.assertEqual(len(hits), 1)
        event = hits[0]
        self.assertEqual(event["actor"], "user")
        self.assertEqual(event["detail"], dict(DEFAULTS,
                                               reasoning_effort="medium"))
        blob = json.dumps(event, ensure_ascii=False)
        self.assertNotIn(CREDENTIAL_VALUE, blob)
        self.assertNotIn("apiKey", blob)

    # ------------------------------------------------------------- 快照隔离

    def test_set_does_not_affect_claimed_round_snapshot(self):
        fixture = self.make_ready_task()
        task_id = fixture["remote"]["id"]
        # v2 §5.22.4：发布后 hold → dispatch 排队须 confirm_plan。
        self.enqueue_dispatch(task_id)
        claim = self.call(
            "claim", {"stage": "dispatch", "executor": "kaneo-dispatch"}
        )
        self.assertTrue(claim["claimed"])
        run_id = claim["run_id"]
        self.assertEqual(claim["context"]["exec_config"], DEFAULTS)
        self.assertEqual(claim["context"]["backend"], "pi")

        # claim 后改配置（只影响新轮次）。
        saved = self.call("set-exec-config", {"reasoning_effort": "low"})
        self.assertEqual(saved["config"]["reasoning_effort"], "low")

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = round_store.get_round(conn, task_id, run_id)
            self.assertIsNotNone(row)
            frozen = util.jload(row["config_json"])
        finally:
            conn.close()
        self.assertEqual(frozen, DEFAULTS)  # 旧轮次快照仍是旧值
        got = self.call("get-exec-config", {})
        self.assertEqual(got["config"]["reasoning_effort"], "low")


if __name__ == "__main__":  # pragma: no cover
    import unittest

    unittest.main()
