"""角色化配置 + Review 环境（protocol §5.23.3/§5.23.4，v3，子任务 C）。

覆盖：
- get/set-review-config：未保存 → config=null/defaults mode=inherit；
  inherit 保存忽略其他字段；custom 缺字段/非法 provider/model/effort →
  bad_request 旧值不变；保存成功 + event review_config_updated；
  executor_runner --stage review 在 custom 时加 --model/--thinking，
  inherit 不加（fake pi_bin 记录 argv）。
- exec_config backend 扩展：旧四字段配置 load 得 backend="pi"；
  set backend=pi_desktop 无 pi_desktop 块 → bad_request；permission_mode
  缺省/inherit → bad_request；两分支共存互不清字段；snapshot 双分支
  形状正确。
- check_available：backend=pi_desktop 且 pi_desktop_backend 模块缺失
  → (False, 中文原因)，不回落 pi。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import unittest.mock

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import (  # noqa: E402
    cli, cmd_config, db, exec_config, executor_runner, review_config, util,
)

DEFAULTS = {
    "provider": "CloseAI",
    "model": "deepseek-flash",
    "reasoning_effort": "xhigh",
    "agent_preset": "default",
}
PI_DESKTOP_BLOCK = {
    "connection_file": "/tmp/pi-desktop/mcp-control.json",
    "provider_id": "closeai",
    "model_id": "deepseek-flash",
    "thinking_level": "high",
    "permission_mode": "auto",
}
# 与本文件报告给主 agent 的 cli.py 注册条目一致（主 agent 接入后
# self.call 即走真实命令注册与 payload 校验）。
REVIEW_COMMANDS = {
    "get-review-config": ({}, cmd_config.cmd_get_review_config),
    "set-review-config": (
        {
            "mode": (True, "str"),
            "provider": (False, "str"),
            "model": (False, "str"),
            "reasoning_effort": (False, "str"),
        },
        cmd_config.cmd_set_review_config,
    ),
}


class _ReviewCommandsCase(KanpilotTestCase):
    """把新命令挂进 cli.COMMANDS（主 agent 注册前的等价路径）。"""

    def setUp(self):
        super().setUp()
        cli.COMMANDS.update(REVIEW_COMMANDS)
        self.addCleanup(self._unregister_review)

    def _unregister_review(self):
        for name in REVIEW_COMMANDS:
            cli.COMMANDS.pop(name, None)


class ReviewConfigCommands(_ReviewCommandsCase):
    # ------------------------------------------------------------- get/set
    def test_get_unsaved_returns_null_and_inherit_default(self):
        resp = self.call("get-review-config", {})
        self.assertEqual(set(resp), {"config", "defaults"})
        self.assertIsNone(resp["config"])
        self.assertEqual(resp["defaults"], {"mode": "inherit"})

    def test_inherit_save_ignores_other_fields(self):
        saved = self.call("set-review-config", {
            "mode": "inherit",
            # inherit 下这些字段被忽略（即便值非法也不校验）。
            "provider": "ghost", "model": "ghost", "reasoning_effort": "bogus",
        })
        config = saved["config"]
        self.assertEqual(config["mode"], "inherit")
        for key in ("provider", "model", "reasoning_effort"):
            self.assertNotIn(key, config)
        self.assertIsInstance(config["updated_at"], str)
        got = self.call("get-review-config", {})
        self.assertEqual(got["config"], config)

    def test_custom_missing_fields_bad_request_keeps_old(self):
        self.call("set-review-config", {"mode": "inherit"})
        cases = [
            {"mode": "custom"},
            {"mode": "custom", "provider": "CloseAI"},
            {"mode": "custom", "provider": "CloseAI",
             "model": "deepseek-flash"},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                exc = self.call_err("set-review-config", payload)
                self.assertEqual(exc.code, "bad_request")
                self.assertIn("缺少必填字段", str(exc))
        # 旧值不变（仍为 inherit）。
        got = self.call("get-review-config", {})
        self.assertEqual(got["config"]["mode"], "inherit")

    def test_custom_invalid_provider_model_effort_bad_request(self):
        cases = [
            ({"mode": "custom", "provider": "ghost",
              "model": "deepseek-flash", "reasoning_effort": "low"},
             "provider 不存在"),
            ({"mode": "custom", "provider": "CloseAI",
              "model": "ghost", "reasoning_effort": "low"},
             "模型不存在"),
            ({"mode": "custom", "provider": "CloseAI",
              "model": "deepseek-flash", "reasoning_effort": "off"},
             "不支持思考等级 off"),
        ]
        for payload, part in cases:
            with self.subTest(payload=payload):
                exc = self.call_err("set-review-config", payload)
                self.assertEqual(exc.code, "bad_request")
                self.assertIn("Review 配置校验失败", str(exc))
                self.assertIn(part, str(exc))
        # 任何失败都不落库。
        self.assertIsNone(self.call("get-review-config", {})["config"])

    def test_bad_mode_bad_request(self):
        exc = self.call_err("set-review-config", {"mode": "ghost"})
        self.assertEqual(exc.code, "bad_request")
        self.assertIn("mode", str(exc))

    def test_custom_save_success_event_and_redaction(self):
        saved = self.call("set-review-config", {
            "mode": "custom", "provider": "CloseAI",
            "model": "deepseek-flash", "reasoning_effort": "low",
        })
        config = saved["config"]
        self.assertEqual(config["mode"], "custom")
        self.assertEqual(config["provider"], "CloseAI")
        self.assertEqual(config["model"], "deepseek-flash")
        self.assertEqual(config["reasoning_effort"], "low")
        self.assertIsInstance(config["updated_at"], str)

        events = self.call("list-events", {"limit": 50})["events"]
        hits = [e for e in events if e["kind"] == "review_config_updated"]
        self.assertEqual(len(hits), 1)
        detail = hits[0]["detail"]
        self.assertEqual(detail, {
            "mode": "custom", "provider": "CloseAI",
            "model": "deepseek-flash", "reasoning_effort": "low",
        })
        blob = json.dumps(hits[0], ensure_ascii=False) + json.dumps(
            saved, ensure_ascii=False)
        self.assertNotIn("apiKey", blob)
        self.assertNotIn("test-key-placeholder", blob)

        # 回退 inherit 覆盖旧 custom——custom 字段被清掉。
        self.call("set-review-config", {"mode": "inherit"})
        got = self.call("get-review-config", {})["config"]
        self.assertEqual(got["mode"], "inherit")
        self.assertNotIn("provider", got)

    # ------------------------------------------------------------- runner
    def _argv_logging_pi(self):
        """fake pi：把 argv 逐行写日志后 exit 0。返回 argv 日志路径。"""
        argv_log = self.root / "pi-argv.log"
        pi_bin = self.root / "pi-argv-bin"
        pi_bin.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$@\" > '{argv_log}'\n"
            "exit 0\n",
            encoding="utf-8",
        )
        pi_bin.chmod(0o755)
        os.environ["KANPILOT_PI_BIN"] = str(pi_bin)
        os.environ["KANPILOT_DB"] = str(self.db_path)
        self.addCleanup(lambda: os.environ.pop("KANPILOT_DB", None))
        return argv_log

    def _run_review(self):
        argv_log = self._argv_logging_pi()
        rc = executor_runner.run_stage("review")
        self.assertEqual(rc, 0)
        return argv_log.read_text(encoding="utf-8").splitlines()

    def test_runner_custom_adds_model_and_thinking(self):
        self.call("set-review-config", {
            "mode": "custom", "provider": "CloseAI",
            "model": "deepseek-flash", "reasoning_effort": "low",
        })
        argv = self._run_review()
        self.assertIn("--model", argv)
        index = argv.index("--model")
        self.assertEqual(argv[index + 1], "CloseAI/deepseek-flash")
        self.assertIn("--thinking", argv)
        self.assertEqual(argv[argv.index("--thinking") + 1], "low")
        # 脱敏快照写入 executor_runs["review"]（无凭据）。
        conn = db.connect(self.db_path)
        try:
            runs = db.get_setting(conn, "executor_runs")
        finally:
            conn.close()
        review = runs["review"]["review_config"]
        self.assertEqual(review, {
            "mode": "custom", "provider": "CloseAI",
            "model": "deepseek-flash", "reasoning_effort": "low",
        })

    def test_runner_inherit_keeps_pi_defaults(self):
        for mode_payload in (
            None,  # 未保存
            {"mode": "inherit"},
        ):
            if mode_payload is not None:
                self.call("set-review-config", mode_payload)
            argv = self._run_review()
            self.assertNotIn("--model", argv)
            self.assertNotIn("--thinking", argv)

    def test_runner_monitor_ignores_review_config(self):
        # §5.23.5：monitor/dispatch 控制端继承 PI 默认，不读 review_config。
        self.call("set-review-config", {
            "mode": "custom", "provider": "CloseAI",
            "model": "deepseek-flash", "reasoning_effort": "low",
        })
        argv_log = self._argv_logging_pi()
        rc = executor_runner.run_stage("monitor")
        self.assertEqual(rc, 0)
        argv = argv_log.read_text(encoding="utf-8").splitlines()
        self.assertNotIn("--model", argv)
        self.assertNotIn("--thinking", argv)


class ExecBackendConfig(_ReviewCommandsCase):
    """exec_config backend/pi_desktop 扩展（§5.23.3）。"""

    def _raw_save_exec(self, config):
        conn = db.connect(self.db_path)
        try:
            db.set_setting(conn, "exec_config", config)
        finally:
            conn.close()

    def test_legacy_four_field_config_loads_backend_pi(self):
        self._raw_save_exec({**DEFAULTS, "updated_at": "2026-01-01T00:00:00Z"})
        got = self.call("get-exec-config", {})
        config = got["config"]
        self.assertEqual(config["backend"], "pi")
        self.assertIsNone(config["pi_desktop"])
        self.assertEqual({k: config[k] for k in DEFAULTS}, DEFAULTS)

    def test_unknown_backend_stored_reads_as_pi(self):
        self._raw_save_exec({**DEFAULTS, "backend": "dsh"})
        conn = db.connect(self.db_path)
        try:
            self.assertEqual(exec_config.load(conn)["backend"], "pi")
        finally:
            conn.close()

    def test_set_backend_pi_desktop_without_block_bad_request(self):
        self.call("set-exec-config", {"reasoning_effort": "low"})
        exc = self.call_err("set-exec-config", {"backend": "pi_desktop"})
        self.assertEqual(exc.code, "bad_request")
        self.assertIn("pi_desktop", str(exc))
        # 旧值不变：backend 仍是 pi。
        config = self.call("get-exec-config", {})["config"]
        self.assertEqual(config["backend"], "pi")
        self.assertEqual(config["reasoning_effort"], "low")

    def test_unknown_backend_value_bad_request(self):
        exc = self.call_err("set-exec-config", {"backend": "bogus"})
        self.assertEqual(exc.code, "bad_request")
        self.assertIn("backend 不支持", str(exc))

    def test_pi_desktop_permission_mode_missing_or_inherit_bad_request(self):
        cases = [
            # permission_mode 缺省
            {k: v for k, v in PI_DESKTOP_BLOCK.items()
             if k != "permission_mode"},
            # permission_mode=inherit（不允许——必须显式保存）
            dict(PI_DESKTOP_BLOCK, permission_mode="inherit"),
            # connection_file 非绝对路径
            dict(PI_DESKTOP_BLOCK, connection_file="relative/mcp.json"),
            # thinking_level 越界
            dict(PI_DESKTOP_BLOCK, thinking_level="ultra"),
        ]
        for block in cases:
            with self.subTest(block=block):
                exc = self.call_err("set-exec-config", {
                    "backend": "pi_desktop", "pi_desktop": block,
                })
                self.assertEqual(exc.code, "bad_request")
        # 也覆盖「块给全但 permission_mode 值非法」之外的单独提交：
        exc = self.call_err("set-exec-config", {
            "pi_desktop": {"connection_file": "/tmp/x.json"},
        })
        self.assertEqual(exc.code, "bad_request")
        self.assertIn("缺少必填字段", str(exc))
        self.assertIsNone(self.call("get-exec-config", {})["config"])

    def test_two_branches_coexist_without_clearing(self):
        saved = self.call("set-exec-config", {
            "backend": "pi_desktop", "pi_desktop": PI_DESKTOP_BLOCK,
        })
        config = saved["config"]
        self.assertEqual(config["backend"], "pi_desktop")
        self.assertEqual(config["pi_desktop"], PI_DESKTOP_BLOCK)
        # pi 四字段保留（默认/已存值不因切换删除）。
        for key, value in DEFAULTS.items():
            self.assertEqual(config[key], value)

        # 切回 pi 不删除 pi_desktop 块。
        self.call("set-exec-config", {"backend": "pi"})
        got = self.call("get-exec-config", {})["config"]
        self.assertEqual(got["backend"], "pi")
        self.assertEqual(got["pi_desktop"], PI_DESKTOP_BLOCK)
        # pi 分支字段照常更新。
        self.call("set-exec-config", {"reasoning_effort": "low"})
        got = self.call("get-exec-config", {})["config"]
        self.assertEqual(got["reasoning_effort"], "low")
        self.assertEqual(got["pi_desktop"], PI_DESKTOP_BLOCK)

    def test_snapshot_branch_shapes(self):
        conn = db.connect(self.db_path)
        try:
            # pi 分支快照：{backend:"pi", 四字段}。
            snap = exec_config.snapshot(conn)
            self.assertEqual(snap, {"backend": "pi", **DEFAULTS})
        finally:
            conn.close()
        self.call("set-exec-config", {
            "backend": "pi_desktop", "pi_desktop": PI_DESKTOP_BLOCK,
        })
        conn = db.connect(self.db_path)
        try:
            snap = exec_config.snapshot(conn)
        finally:
            conn.close()
        self.assertEqual(snap, {"backend": "pi_desktop", **PI_DESKTOP_BLOCK})
        blob = json.dumps(snap, ensure_ascii=False)
        self.assertNotIn("token", blob.lower())

    def test_claim_freezes_pi_snapshot_with_backend(self):
        fixture = self.make_ready_task()
        self.enqueue_dispatch(fixture["remote"]["id"])
        claim = self.call(
            "claim", {"stage": "dispatch", "executor": "kaneo-dispatch"}
        )
        self.assertTrue(claim["claimed"])
        self.assertEqual(claim["context"]["backend"], "pi")
        self.assertEqual(
            claim["context"]["exec_config"], {"backend": "pi", **DEFAULTS}
        )

    def test_capabilities_uses_pi_round_snapshot_when_current_is_valid(self):
        """当前配置 A 有效、轮次快照 B 无效时必须检查 B。"""
        self._raw_save_exec({"backend": "pi", **DEFAULTS})
        seen = []

        def fake_auth(provider, model, timeout=15):
            seen.append((provider, model))
            return (provider, model) == ("CloseAI", "deepseek-flash"), "fake"

        conn = db.connect(self.db_path)
        try:
            with unittest.mock.patch.object(
                exec_config, "_probe_auth", side_effect=fake_auth,
            ):
                caps = exec_config.capabilities(
                    conn,
                    config_snapshot={
                        "backend": "pi", "provider": "provider-b",
                        "model": "model-b", "reasoning_effort": "low",
                        "agent_preset": "default",
                    },
                )
        finally:
            conn.close()
        self.assertEqual(seen, [("provider-b", "model-b")])
        self.assertFalse(next(c for c in caps["checks"]
                              if c["name"] == "credential")["ok"])

    def test_capabilities_uses_pi_round_snapshot_when_current_is_invalid(self):
        """当前配置 A 无效、轮次快照 B 有效时仍必须检查 B。"""
        self._raw_save_exec({"backend": "pi", "provider": "provider-b",
                             "model": "model-b", "reasoning_effort": "low",
                             "agent_preset": "default"})
        seen = []

        def fake_auth(provider, model, timeout=15):
            seen.append((provider, model))
            return (provider, model) == ("provider-a", "model-a"), "fake"

        conn = db.connect(self.db_path)
        try:
            with unittest.mock.patch.object(
                exec_config, "_probe_auth", side_effect=fake_auth,
            ):
                caps = exec_config.capabilities(
                    conn,
                    config_snapshot={
                        "backend": "pi", "provider": "provider-a",
                        "model": "model-a", "reasoning_effort": "low",
                        "agent_preset": "default",
                    },
                )
        finally:
            conn.close()
        self.assertEqual(seen, [("provider-a", "model-a")])
        self.assertTrue(next(c for c in caps["checks"]
                             if c["name"] == "credential")["ok"])

    def test_capabilities_exposes_pi_desktop_branch(self):
        caps = self.call("exec-capabilities", {})
        self.assertIn("pi_desktop", caps)
        branch = caps["pi_desktop"]
        self.assertEqual(set(branch),
                         {"available", "detail", "tools_ok", "version"})
        # 未保存 → available False + 中文原因；不透出内部字段。
        self.assertFalse(branch["available"])
        self.assertIn("未保存", branch["detail"])
        blob = json.dumps(caps, ensure_ascii=False)
        self.assertNotIn("_providers_detail", blob)
        self.assertNotIn("test-key-placeholder", blob)

    def _force_backend_import_error(self):
        """模拟 pi_desktop_backend 模块缺失（ImportError 路径）。

        ``from . import pi_desktop_backend`` 实际调用
        ``__import__("kanpilot", ..., ["pi_desktop_backend"], 1)``——
        需按 fromlist 拦截。"""
        import builtins
        real_import = builtins.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(),
                        level=0):
            if (
                name in ("kanpilot.pi_desktop_backend",
                         "pi_desktop_backend")
                or (fromlist and "pi_desktop_backend" in fromlist)
            ):
                raise ImportError("No module named 'pi_desktop_backend'")
            return real_import(
                name, globals, locals, fromlist, level
            )

        return unittest.mock.patch.object(
            builtins, "__import__", fake_import
        )

    def test_check_available_pi_desktop_missing_module_no_fallback(self):
        self.call("set-exec-config", {
            "backend": "pi_desktop", "pi_desktop": PI_DESKTOP_BLOCK,
        })
        # pi_desktop_backend 惰性导入失败路径（无论 B 模块是否已落地）。
        sys.modules.pop("kanpilot.pi_desktop_backend", None)
        conn = db.connect(self.db_path)
        try:
            with self._force_backend_import_error():
                available, reason = exec_config.check_available(conn)
            self.assertFalse(available)
            self.assertIn("pi_desktop", reason)
            self.assertIn("缺失", reason)
            # 不自动切回 pi：生效 backend 保持 pi_desktop。
            self.assertEqual(exec_config.load(conn)["backend"], "pi_desktop")
        finally:
            conn.close()
        # exec-capabilities 同样报分支不可用（惰性导入失败口径）。
        with self._force_backend_import_error():
            caps = self.call("exec-capabilities", {})
        self.assertFalse(caps["pi_desktop"]["available"])
        self.assertIn("缺失", caps["pi_desktop"]["detail"])

    def test_check_available_pi_desktop_probe_failure_no_fallback(self):
        """后端模块存在但探测失败（连接文件不可读）→ (False, 中文原因)，
        不回落 pi。"""
        self.call("set-exec-config", {
            "backend": "pi_desktop", "pi_desktop": PI_DESKTOP_BLOCK,
        })
        conn = db.connect(self.db_path)
        try:
            available, reason = exec_config.check_available(conn)
        finally:
            conn.close()
        self.assertFalse(available)
        self.assertIn("pi_desktop", reason)
        # 生效 backend 未被改动（不自动切回 pi）。
        conn = db.connect(self.db_path)
        try:
            self.assertEqual(exec_config.load(conn)["backend"], "pi_desktop")
        finally:
            conn.close()

    def test_pi_desktop_probe_passes_model_and_required_tools(self):
        """能力探测把保存的型号和超时交给后端，不在 C 重复实现型号查询。"""
        calls = []

        def fake_probe(*args, **kwargs):
            calls.append((args, kwargs))
            return {
                "ok": True,
                "detail": "fake ready",
                "tools": set(exec_config.constants.PI_DESKTOP_DISPATCH_TOOLS),
                "version": "1",
            }

        conn = db.connect(self.db_path)
        try:
            with unittest.mock.patch(
                "kanpilot.pi_desktop_backend.probe_connection",
                side_effect=fake_probe,
            ):
                available, reason = exec_config.check_available(
                    conn, timeout=7,
                    config_snapshot={"backend": "pi_desktop", **PI_DESKTOP_BLOCK},
                )
        finally:
            conn.close()
        self.assertTrue(available)
        self.assertIsNone(reason)
        self.assertEqual(len(calls), 1)
        args, kwargs = calls[0]
        self.assertEqual(args, (PI_DESKTOP_BLOCK["connection_file"],))
        self.assertEqual(kwargs["provider_id"], PI_DESKTOP_BLOCK["provider_id"])
        self.assertEqual(kwargs["model_id"], PI_DESKTOP_BLOCK["model_id"])
        self.assertEqual(kwargs["timeout"], 7)
        self.assertEqual(
            tuple(kwargs["required_tools"]),
            exec_config.constants.PI_DESKTOP_DISPATCH_TOOLS,
        )

    def test_check_available_uses_flat_round_snapshot(self):
        """全局设置改变后，显式轮次快照仍按自身配置复核。"""
        old_block = dict(PI_DESKTOP_BLOCK, provider_id="old-provider")
        round_block = dict(PI_DESKTOP_BLOCK, provider_id="round-provider")
        self.call("set-exec-config", {
            "backend": "pi_desktop", "pi_desktop": old_block,
        })
        calls = []

        def fake_probe(*args, **kwargs):
            calls.append((args, kwargs))
            return {
                "ok": True,
                "detail": "fake ready",
                "tools": set(exec_config.constants.PI_DESKTOP_DISPATCH_TOOLS),
                "version": "1",
            }

        conn = db.connect(self.db_path)
        try:
            with unittest.mock.patch(
                "kanpilot.pi_desktop_backend.probe_connection",
                side_effect=fake_probe,
            ):
                available, reason = exec_config.check_available(
                    conn,
                    config_snapshot={"backend": "pi_desktop", **round_block},
                )
        finally:
            conn.close()
        self.assertTrue(available)
        self.assertIsNone(reason)
        self.assertEqual(calls[0][1]["provider_id"], "round-provider")


if __name__ == "__main__":  # pragma: no cover
    import unittest

    unittest.main()
