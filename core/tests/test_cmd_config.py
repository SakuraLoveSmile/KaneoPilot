"""get/set/exec-capabilities 命令层（protocol §5.17.2 v1.6 + §5.24.1/§5.24.2 v4）。

覆盖：默认 config=null + defaults；合并保存（已存值 > 默认值，payload 覆盖）；
非法 provider/model/effort/preset → bad_request 且中文原因含可用列表；
思考等级按模型支持子集校验（deepseek-flash 可选 max；Restricted/small
仅 low/medium 的模型选 max 拒绝——测试内临时改写 tmp 夹具 models.json）；
空 payload bad_request；响应绝不含 apiKey/密钥值；event exec_config_updated
落库（detail 四字段 + controller_backend、不含凭据）；set 不影响已 claim 轮次的
config_json 快照（§5.17.3）。

v4（§5.24.1/§5.24.2）另覆盖：``controller_backend`` 缺省 pi、非法值
bad_request、选择 Plus 需完整五字段块、两分支共存；``exec-capabilities`` 顶层
``controller`` = ``{backend, available, detail}``（控制器就绪态与任务执行就绪态
分开，控制器不要求 Goal 契约审批工具）；任务执行轮次快照不受 controller 影响。
"""

from __future__ import annotations

import json
import os
import sqlite3
import unittest.mock
from pathlib import Path

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import (  # noqa: E402
    cli, constants, db, exec_config, round_store, util,
)

DEFAULTS = {
    "provider": "CloseAI",
    "model": "deepseek-flash",
    "reasoning_effort": "xhigh",
    "agent_preset": "default",
}
# 测试夹具凭据值（绝不出现在任何响应里——pi auth check 只看退出码）。
CREDENTIAL_VALUE = "test-key-placeholder"
# v4（§5.24.1）：控制器后端与 PI-Desktop 分支五字段块。
PI_DESKTOP_BLOCK = {
    "model_source": "custom",
    "connection_file": "/tmp/pi-desktop/mcp-control.json",
    "provider_id": "closeai",
    "model_id": "deepseek-flash",
    "thinking_level": "high",
    "permission_mode": "auto",
}
PI_DESKTOP_SNAPSHOT = {
    k: PI_DESKTOP_BLOCK[k] for k in constants.EXEC_PI_DESKTOP_KEYS
}

# v4 字段注册（Lead 在 cli.py 落地前由本文件自行补登记，幂等）。
V4_ENTRIES = {
    "set-exec-config": ({"controller_backend": (False, "str")}, None),
}


def _install_v4_entries(entries):
    """把 v4 新增字段并进 cli.COMMANDS 的既有 schema；返回可恢复的原文。"""
    saved = {}
    for name, (spec, fn) in entries.items():
        current = cli.COMMANDS.get(name)
        if current is None:
            cli.COMMANDS[name] = (spec, fn)
            saved[name] = None
            continue
        merged = dict(current[0])
        merged.update(spec)
        if merged != current[0]:
            cli.COMMANDS[name] = (merged, current[1] or fn)
            saved[name] = current
    return saved


def _restore_v4_entries(saved):
    for name, original in saved.items():
        if original is None:
            cli.COMMANDS.pop(name, None)
        else:
            cli.COMMANDS[name] = original


class _V4FieldsCase(KanpilotTestCase):
    """补登记 v4 字段的命令入口（cli.py 已注册时为 no-op）。"""

    def setUp(self):
        super().setUp()
        saved = _install_v4_entries(V4_ENTRIES)
        self.addCleanup(_restore_v4_entries, saved)


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
        # v4：pi 分支 detail = 契约四字段 + controller_backend（保留旧字段）。
        self.assertEqual(event["detail"], dict(
            DEFAULTS, reasoning_effort="medium", controller_backend="pi"))
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
        # v3（§5.23.3）：快照含 backend 字段。
        self.assertEqual(claim["context"]["exec_config"],
                         {"backend": "pi", **DEFAULTS})
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
        # v3（§5.23.3）：pi 分支固化快照含 backend:"pi"。
        self.assertEqual(frozen, {"backend": "pi", **DEFAULTS})  # 旧轮次快照仍是旧值
        got = self.call("get-exec-config", {})
        self.assertEqual(got["config"]["reasoning_effort"], "low")


class ControllerBackendCommands(_V4FieldsCase):
    """v4（§5.24.1/§5.24.2）：控制器后端字段与 ``controller`` 就绪态。"""

    def test_legacy_config_defaults_controller_pi(self):
        """旧配置（无 controller_backend）零迁移按 pi 解释。"""
        self.call("get-exec-config", {})  # 建库
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                db.set_setting(conn, "exec_config", {
                    **DEFAULTS, "updated_at": "2026-01-01T00:00:00Z",
                })
        finally:
            conn.close()
        config = self.call("get-exec-config", {})["config"]
        self.assertEqual(config["controller_backend"], "pi")
        # 任务执行后端不受影响。
        self.assertEqual(config["backend"], "pi")
        conn = db.connect(self.db_path)
        try:
            self.assertEqual(exec_config.controller_backend(conn), "pi")
            self.assertEqual(exec_config.controller_snapshot(conn),
                             {"backend": "pi"})
            self.assertEqual(exec_config.snapshot(conn),
                             {"backend": "pi", **DEFAULTS})
        finally:
            conn.close()

    def test_unknown_controller_value_reads_as_pi(self):
        self.call("set-exec-config", {"reasoning_effort": "low"})
        conn = db.connect(self.db_path)
        try:
            payload = dict(db.get_setting(conn, "exec_config"))
        finally:
            conn.close()
        self.assertEqual(payload["controller_backend"], "pi")
        payload["controller_backend"] = "dsh"
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                db.set_setting(conn, "exec_config", payload)
        finally:
            conn.close()
        conn = db.connect(self.db_path)
        try:
            self.assertEqual(exec_config.controller_backend(conn), "pi")
            self.assertEqual(exec_config.load(conn)["controller_backend"], "pi")
        finally:
            conn.close()

    def test_controller_pi_desktop_requires_block_and_coexists(self):
        self.call("set-exec-config", {"reasoning_effort": "low"})
        # v5 controller_backend is retained for display; it no longer gates execution.
        saved_legacy = self.call("set-exec-config", {"controller_backend": "pi_desktop"})
        self.assertEqual(saved_legacy["config"]["controller_backend"], "pi_desktop")
        self.assertEqual(saved_legacy["config"]["backend"], "pi")

        saved = self.call("set-exec-config", {
            "controller_backend": "pi_desktop", "pi_desktop": PI_DESKTOP_BLOCK,
        })
        config = saved["config"]
        self.assertEqual(config["controller_backend"], "pi_desktop")
        self.assertEqual(config["pi_desktop"], PI_DESKTOP_BLOCK)
        # 任务执行后端仍是 pi（两项独立），pi 分支字段保留。
        self.assertEqual(config["backend"], "pi")
        self.assertEqual(config["reasoning_effort"], "low")
        # 任务执行轮次快照不含 controller（控制轮与执行轮身份不混用）。
        conn = db.connect(self.db_path)
        try:
            self.assertEqual(
                exec_config.snapshot(conn),
                {"backend": "pi", "provider": "CloseAI",
                 "model": "deepseek-flash", "reasoning_effort": "low",
                 "agent_preset": "default"},
            )
            self.assertEqual(
                exec_config.controller_snapshot(conn),
                {"backend": "pi_desktop", **PI_DESKTOP_SNAPSHOT},
            )
        finally:
            conn.close()
        # 切回 pi 控制器不删除 Plus 块。
        self.call("set-exec-config", {"controller_backend": "pi"})
        got = self.call("get-exec-config", {})["config"]
        self.assertEqual(got["controller_backend"], "pi")
        self.assertEqual(got["pi_desktop"], PI_DESKTOP_BLOCK)

    def test_invalid_controller_backend_bad_request(self):
        exc = self.call_err(
            "set-exec-config", {"controller_backend": "bogus"})
        self.assertEqual(exc.code, "bad_request")
        self.assertIn("controller_backend 不支持", str(exc))
        self.assertIsNone(self.call("get-exec-config", {})["config"])

    def test_controller_capabilities_shape_pi_branch(self):
        caps = self.call("exec-capabilities", {})
        self.assertIn("controller", caps)
        controller = caps["controller"]
        self.assertEqual(set(controller), {"backend", "available", "detail"})
        self.assertEqual(controller["backend"], "pi")
        # 夹具 pi --version 可用 → 控制器 pi 分支就绪。
        self.assertTrue(controller["available"], controller)
        self.assertIn("Pi CLI", controller["detail"])
        blob = json.dumps(caps, ensure_ascii=False)
        self.assertNotIn(CREDENTIAL_VALUE, blob)
        self.assertNotIn("token", blob.lower())

    def test_controller_pi_desktop_probe_uses_controller_tools(self):
        """控制端只要求基础工具（不建 Goal 会话，故不要契约审批工具）。"""
        self.call("set-exec-config", {
            "controller_backend": "pi_desktop", "pi_desktop": PI_DESKTOP_BLOCK,
        })
        self.assertNotIn("pi_plans_pending", constants.PI_DESKTOP_CONTROLLER_TOOLS)
        self.assertNotIn("pi_plans_resolve", constants.PI_DESKTOP_CONTROLLER_TOOLS)
        calls = []

        def fake_probe(*args, **kwargs):
            calls.append((args, kwargs))
            return {
                "ok": True, "detail": "fake ready",
                "tools": set(constants.PI_DESKTOP_CONTROLLER_TOOLS),
                "version": "1",
            }

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            with unittest.mock.patch(
                "kanpilot.pi_desktop_backend.probe_connection",
                side_effect=fake_probe,
            ):
                available, reason = exec_config.check_controller_available(
                    conn, timeout=7)
        finally:
            conn.close()
        self.assertTrue(available, reason)
        self.assertEqual(len(calls), 1)
        args, kwargs = calls[0]
        self.assertEqual(args, (PI_DESKTOP_BLOCK["connection_file"],))
        self.assertEqual(kwargs["provider_id"], PI_DESKTOP_BLOCK["provider_id"])
        self.assertEqual(kwargs["model_id"], PI_DESKTOP_BLOCK["model_id"])
        self.assertEqual(kwargs["timeout"], 7)
        self.assertEqual(tuple(kwargs["required_tools"]),
                         constants.PI_DESKTOP_CONTROLLER_TOOLS)

    def test_controller_probe_failure_never_falls_back_to_pi(self):
        self.call("set-exec-config", {
            "controller_backend": "pi_desktop", "pi_desktop": PI_DESKTOP_BLOCK,
        })

        def fake_fail(*args, **kwargs):
            return {"ok": False, "detail": "连接文件不存在：/tmp/x.json",
                    "tools": set(), "version": None}

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            with unittest.mock.patch(
                "kanpilot.pi_desktop_backend.probe_connection",
                side_effect=fake_fail,
            ):
                available, reason = exec_config.check_controller_available(conn)
            self.assertFalse(available)
            self.assertIn("连接文件不存在", reason)
            # 不自动回落 pi：生效控制器后端保持 pi_desktop。
            self.assertEqual(
                exec_config.controller_backend(conn), "pi_desktop")
        finally:
            conn.close()

    def test_controller_module_missing_reports_chinese_reason(self):
        self.call("set-exec-config", {
            "controller_backend": "pi_desktop", "pi_desktop": PI_DESKTOP_BLOCK,
        })
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            with unittest.mock.patch.object(
                exec_config, "_probe_pi_desktop",
                side_effect=lambda *a, **k: {
                    "available": False, "detail": "pi_desktop 后端模块缺失",
                    "tools_ok": False, "version": None,
                },
            ):
                available, reason = exec_config.check_controller_available(conn)
        finally:
            conn.close()
        self.assertFalse(available)
        self.assertIn("缺失", reason)


if __name__ == "__main__":  # pragma: no cover
    import unittest

    unittest.main()
