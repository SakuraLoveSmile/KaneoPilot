"""v3 施工规划方式测试（protocol §5.23.2，planner-method T2）。

覆盖：
- ``start-plans.items[].planner_method``：缺省/显式 builtin、spec_plan 固化
  ``plugin_id``/``plugin_version``、非法值 → bad_request；
- 幂等：同 input_sha256 同方式 → deduplicated；同输入不同方式 → conflict；
  旧 payload 缺 planner_method 按 builtin 解释；
- ``planner-capabilities.planner_methods``：fake ``codex plugin list``
  （KANPILOT_CODEX_BIN stub）驱动 spec_plan available true/false；
- 领取时插件缺失 → 请求 failed + failure_code=plan_failed，不降级 builtin；
- ``launcher_argv``/``build_prompt`` 的方式分支（spec_plan 去
  ``--ignore-user-config``、不加 provider -c 覆写、prompt 带 $spec-plan）。
"""

from __future__ import annotations

import json
import os
import stat
import sys
import unittest
from pathlib import Path

# 供 `python3 -m unittest tests.test_planner_method` 与 pytest 两种入口工作。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import (  # noqa: E402
    cli, cmd_planner, codex_planner, codex_provider, constants, db,
    dsh_events, planner_config, planner_runner, util,
)

FAKE_PLUGIN_VERSION = "9.9.9-test"


def _plugin_json(plugin_dir: Path, version=FAKE_PLUGIN_VERSION,
                 enabled=True, installed=True) -> str:
    return json.dumps({
        "installed": [{
            "pluginId": codex_planner.SPEC_PLUGIN_ID,
            "name": "spec-planner",
            "marketplaceName": "personal",
            "version": version,
            "installed": installed,
            "enabled": enabled,
            "source": {"source": "local", "path": str(plugin_dir)},
            "installPolicy": "AVAILABLE",
            "authPolicy": "ON_INSTALL",
        }],
        "available": [],
    })


class _MethodCase(KanpilotTestCase):
    """公共夹具：Feedback 任务 + planner_inputs 材料行 + codex plugin stub。"""

    def _new_task(self, task_id):
        task = self.fake.add_task(
            task_id, "example-project-05", status="to-do")
        self.make_repo("Feedback")
        self.sync_task(task, project_name="Feedback")
        return task

    def _prepare_input(self, task_id):
        return self.make_planner_input(
            task_id,
            manifest_extra={
                "task": {"task_id": task_id, "project_name": "Feedback"},
            },
        )

    def _start_plans(self, info, method=None):
        item = {"input_id": info["input_id"],
                "input_sha256": info["input_sha256"]}
        if method is not None:
            item["planner_method"] = method
        return self.call("start-plans", {"items": [item]})

    def _request_payload(self, request_id):
        return self.call("get-request", {"request_id": request_id})["payload"]

    def _make_plugin_dir(self):
        """伪造插件安装源：.codex-plugin/plugin.json + skills/spec-plan/SKILL.md。"""
        plugin_dir = self.root / "spec-plugin"
        (plugin_dir / ".codex-plugin").mkdir(parents=True, exist_ok=True)
        (plugin_dir / ".codex-plugin" / "plugin.json").write_text(
            json.dumps({"name": "spec-planner"}), encoding="utf-8")
        skill = plugin_dir / "skills" / "spec-plan"
        skill.mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text("# spec-plan\n", encoding="utf-8")
        return plugin_dir

    def _use_plugin_list(self, body: str, name="plugin-stub",
                         exit_code=0):
        """codex bin stub：`plugin list --json` 输出 body；其余退出 0。"""
        json_path = self.root / f"{name}.json"
        json_path.write_text(body, encoding="utf-8")
        stub = self.root / name
        stub.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"plugin\" ]; then\n"
            f"  cat '{json_path}'\n"
            f"  exit {exit_code}\n"
            "fi\n"
            "if [ \"$1\" = \"--version\" ]; then echo \"codex-cli 0.0.0\"; fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
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

    def _use_installed_plugin(self):
        plugin_dir = self._make_plugin_dir()
        self._use_plugin_list(_plugin_json(plugin_dir))
        return plugin_dir


class TestStartPlansMethod(_MethodCase):
    """start-plans 的 planner_method 入口与 payload 固化（§5.23.2）。"""

    def test_default_is_builtin(self):
        task = self._new_task("taskmethod00000000001")
        info = self._prepare_input(task["id"])
        result = self._start_plans(info)
        entry = result["results"][0]
        self.assertTrue(entry["request_id"], f"提交失败: {entry}")
        payload = self._request_payload(entry["request_id"])
        self.assertEqual(payload["planner_method"], "builtin")
        self.assertNotIn("plugin_id", payload)
        self.assertNotIn("plugin_version", payload)

    def test_explicit_builtin(self):
        task = self._new_task("taskmethod00000000002")
        info = self._prepare_input(task["id"])
        result = self._start_plans(info, method="builtin")
        entry = result["results"][0]
        self.assertTrue(entry["request_id"], f"提交失败: {entry}")
        payload = self._request_payload(entry["request_id"])
        self.assertEqual(payload["planner_method"], "builtin")

    def test_spec_plan_fixates_plugin(self):
        self._use_installed_plugin()
        task = self._new_task("taskmethod00000000003")
        info = self._prepare_input(task["id"])
        result = self._start_plans(info, method="spec_plan")
        entry = result["results"][0]
        self.assertTrue(entry["request_id"], f"提交失败: {entry}")
        payload = self._request_payload(entry["request_id"])
        self.assertEqual(payload["planner_method"], "spec_plan")
        self.assertEqual(payload["plugin_id"],
                         codex_planner.SPEC_PLUGIN_ID)
        self.assertEqual(payload["plugin_version"], FAKE_PLUGIN_VERSION)
        # 模型快照与材料哈希照常固化。
        self.assertEqual(
            payload["model_config"],
            dict(planner_config.DEFAULTS["standard"]))
        self.assertEqual(payload["planner_input_sha256"],
                         info["input_sha256"])

    def test_spec_plan_unavailable_fails_item(self):
        # personal 市场无记录 → validation_failed（不建请求，不降级）。
        self._use_plugin_list('{"installed": [], "available": []}')
        task = self._new_task("taskmethod00000000004")
        info = self._prepare_input(task["id"])
        result = self._start_plans(info, method="spec_plan")
        entry = result["results"][0]
        self.assertEqual(entry["code"], "validation_failed")
        self.assertIn("spec_plan", entry["error"])
        self.assertIsNone(entry.get("request_id"))
        self.assertFalse(self.requests(status="queued"))

    def test_invalid_method_bad_request(self):
        task = self._new_task("taskmethod00000000005")
        info = self._prepare_input(task["id"])
        result = self._start_plans(info, method="mystery")
        entry = result["results"][0]
        self.assertEqual(entry["code"], "bad_request")
        self.assertEqual(entry["task_id"], task["id"])
        # 不生成任何请求。
        self.assertFalse(self.requests(status="queued"))


class TestMethodIdempotency(_MethodCase):
    """§5.23.2 幂等：planner_method 属于请求身份。"""

    def test_same_input_same_method_deduplicates(self):
        task = self._new_task("taskidem000000000001")
        info = self._prepare_input(task["id"])
        first = self._start_plans(info)["results"][0]
        again = self._start_plans(info)["results"][0]
        self.assertTrue(again["deduplicated"])
        self.assertEqual(again["request_id"], first["request_id"])

    def test_same_input_different_method_conflicts(self):
        task = self._new_task("taskidem000000000002")
        info = self._prepare_input(task["id"])
        first = self._start_plans(info, method="builtin")["results"][0]
        self.assertTrue(first["request_id"])
        # 同材料换 spec_plan → conflict（dedup 判定先于插件探测，无需 stub）。
        again = self._start_plans(info, method="spec_plan")["results"][0]
        self.assertEqual(again["code"], "conflict")
        self.assertIn("方式", again["error"])
        # 原请求仍在队列。
        self.assertTrue(self.requests(status="queued"))

    def test_legacy_payload_missing_method_is_builtin(self):
        """旧已授权 payload 缺 planner_method → 按 builtin：同材料 builtin
        提交 → deduplicated；spec_plan 提交 → conflict。"""
        task = self._new_task("taskidem000000000003")
        info = self._prepare_input(task["id"])
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                db.create_request(
                    conn, util.now_iso(), "plan", f"plan:{task['id']}",
                    task_id=task["id"],
                    payload={
                        "manual_authorized": True,
                        "input_id": info["input_id"],
                        "planner_input_sha256": info["input_sha256"],
                        "manifest": info["manifest"],
                        "model_config": {
                            "planner_model": "gpt-6-astra",
                            "reasoning_effort": "medium",
                        },
                        "confirmed_at": util.now_iso(),
                    },
                    requested_by="test",
                )
        finally:
            conn.close()
        builtin = self._start_plans(info)["results"][0]
        self.assertTrue(builtin["deduplicated"])
        spec = self._start_plans(info, method="spec_plan")["results"][0]
        self.assertEqual(spec["code"], "conflict")
        self.assertIn("方式", spec["error"])


class TestCapabilities(_MethodCase):
    """planner-capabilities.planner_methods（§5.22.6 v3 形状）。"""

    def test_builtin_and_spec_plan_available(self):
        self._use_installed_plugin()
        result = self.call("planner-capabilities", {})
        methods = result["planner_methods"]
        self.assertTrue(methods["builtin"]["available"])
        self.assertIsNone(methods["builtin"]["reason"])
        spec = methods["spec_plan"]
        self.assertTrue(spec["available"])
        self.assertIsNone(spec["reason"])
        self.assertEqual(spec["plugin_id"], codex_planner.SPEC_PLUGIN_ID)
        self.assertEqual(spec["version"], FAKE_PLUGIN_VERSION)

    def test_spec_plan_not_installed(self):
        self._use_plugin_list('{"installed": [], "available": []}')
        result = self.call("planner-capabilities", {})
        methods = result["planner_methods"]
        self.assertTrue(methods["builtin"]["available"])
        spec = methods["spec_plan"]
        self.assertFalse(spec["available"])
        self.assertIn("未安装", spec["reason"])
        self.assertNotIn("version", spec)

    def test_spec_plan_disabled(self):
        plugin_dir = self._make_plugin_dir()
        self._use_plugin_list(_plugin_json(plugin_dir, enabled=False))
        result = self.call("planner-capabilities", {})
        spec = result["planner_methods"]["spec_plan"]
        self.assertFalse(spec["available"])
        self.assertIn("禁用", spec["reason"])

    def test_spec_plan_missing_skill_file(self):
        plugin_dir = self.root / "spec-plugin-no-skill"
        plugin_dir.mkdir(parents=True, exist_ok=True)  # 无 manifest/SKILL.md
        self._use_plugin_list(_plugin_json(plugin_dir))
        result = self.call("planner-capabilities", {})
        spec = result["planner_methods"]["spec_plan"]
        self.assertFalse(spec["available"])
        self.assertIn("manifest", spec["reason"])

    def test_spec_plan_command_failure(self):
        self._use_plugin_list("garbage", exit_code=3)
        result = self.call("planner-capabilities", {})
        spec = result["planner_methods"]["spec_plan"]
        self.assertFalse(spec["available"])
        self.assertIn("退出码 3", spec["reason"])

    def test_spec_plan_output_not_json(self):
        self._use_plugin_list("not-json")
        result = self.call("planner-capabilities", {})
        spec = result["planner_methods"]["spec_plan"]
        self.assertFalse(spec["available"])
        self.assertIn("JSON", spec["reason"])


class TestLauncherArgv(_MethodCase):
    """launcher_argv/build_prompt 的方式分支（§5.23.2 执行路径差异）。"""

    def _provider(self):
        return codex_provider.resolve_provider(constants.codex_home())

    def _job(self, method):
        return {
            "model_config": {"planner_model": "gpt-6-astra",
                             "reasoning_effort": "medium"},
            "repository": str(self.repos_root / "Feedback"),
            "planner_method": method,
        }

    def test_builtin_argv_keeps_isolation_and_provider(self):
        argv = codex_planner.launcher_argv(
            self._job("builtin"), Path("/tmp/s"), Path("/tmp/o"), [],
            provider=self._provider())
        self.assertIn("--ignore-user-config", argv)
        self.assertIn("--ephemeral", argv)
        self.assertIn("read-only", argv)
        self.assertIn('--output-schema', argv)
        # provider -c 覆写（fixture custom/Sub2API）在 model 覆写之前。
        self.assertIn('model_providers.custom.base_url="http://127.0.0.1:9/v1"',
                      argv)
        self.assertIn('model="gpt-6-astra"', argv)
        self.assertIn('model_reasoning_effort="medium"', argv)

    def test_spec_plan_argv_drops_isolation_and_provider(self):
        argv = codex_planner.launcher_argv(
            self._job("spec_plan"), Path("/tmp/s"), Path("/tmp/o"), [],
            provider=self._provider())
        self.assertNotIn("--ignore-user-config", argv)
        self.assertIn("--ephemeral", argv)
        self.assertIn("read-only", argv)
        self.assertIn('--output-schema', argv)
        # provider -c 覆写不再出现（user config 未被忽略，自行解析）；
        # 模型/思考等级快照覆写保留。
        joined = " ".join(argv)
        self.assertNotIn("model_providers.custom", joined)
        self.assertIn('model="gpt-6-astra"', argv)
        self.assertIn('model_reasoning_effort="medium"', argv)
        # 缺 planner_method 字段 → 与 builtin 同形（向后兼容）。
        job = self._job("builtin")
        del job["planner_method"]
        argv_old = codex_planner.launcher_argv(
            job, Path("/tmp/s"), Path("/tmp/o"), [],
            provider=self._provider())
        self.assertIn("--ignore-user-config", argv_old)

    def test_spec_plan_prompt_prefix(self):
        task = {"title": "t", "task_id": "tid", "repository": "/r"}
        manifest = {"task": {}, "files": [], "supplement": ""}
        prompt = codex_planner.build_prompt(
            task, manifest, None, method="spec_plan")
        self.assertTrue(prompt.startswith("$spec-plan"))
        self.assertIn("plan_markdown", prompt)
        builtin = codex_planner.build_prompt(task, manifest, None)
        self.assertFalse(builtin.startswith("$spec-plan"))


class TestClaimTimePluginGate(_MethodCase):
    """领取时 spec_plan 复检（§5.23.2）：插件缺失 → report failed，不降级。"""

    def _queue_spec_plan(self, task_id):
        """插件仍可用时正常准备并授权，领取前再模拟卸载。"""
        self._use_installed_plugin()
        info = self.call("prepare-plan-input", {"task_id": task_id})
        self.assertTrue(info["can_start"])
        result = self._start_plans(info, method="spec_plan")["results"][0]
        self.assertIn("request_id", result)
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                db.set_setting(conn, "session_runs_root",
                               str(self.app_support / "runs"))
        finally:
            conn.close()
        return result["request_id"]

    def test_plugin_removed_at_claim_fails(self):
        os.environ.pop("KANPILOT_CODEX_FAKE", None)
        self.addCleanup(os.environ.pop, "KANPILOT_CODEX_FAKE", None)
        task = self._new_task("taskclaim00000000001")
        request_id = self._queue_spec_plan(task["id"])
        # 领取时 personal 市场已无插件记录 → 明确失败，不降级 builtin。
        self._use_plugin_list('{"installed": [], "available": []}')
        progressed = planner_runner.run_one(self.db_path)
        self.assertTrue(progressed, "run_one 未领取到 plan 请求")

        view = self.task(task["id"])
        self.assertEqual(view["status"], "needs_plan")  # 失败回待规划
        self.assertEqual(view["failure_code"], "plan_failed")
        self.assertIn("plugin_unavailable", view["failure_reason"])

        request = next(r for r in self.requests()
                       if r["request_id"] == request_id)
        self.assertEqual(request["status"], "failed")

        sessions = self.call("list-sessions", {"task_id": task["id"]})
        self.assertTrue(sessions["sessions"], "无 codex 轮次记录")
        session = sessions["sessions"][0]
        run_dir = (self.app_support / "runs" / session["task_id"]
                   / session["run_id"])
        job = json.loads((run_dir / dsh_events.JOB_NAME).read_text())
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["planner_method"], "spec_plan")
        self.assertEqual(job["plugin_version"], FAKE_PLUGIN_VERSION)
        self.assertIn("plugin_unavailable", job["detail"])
        events, error = dsh_events.iter_events(run_dir / dsh_events.EVENT_LOG_NAME)
        self.assertIsNone(error)
        self.assertEqual(dsh_events.terminal_event(events)["reason"], "plugin_unavailable")
        # 没有拉过 codex exec：无 ready 计划文件、材料保留。
        self.assertFalse((run_dir / "last-message.txt").exists())


class TestPlannerConfigDefaultMethod(_MethodCase):
    """planner_config.default_method 存取（§5.23.2 UI 预选）。"""

    def test_default_method_defaults_builtin(self):
        conn = db.connect(self.db_path)
        try:
            self.assertEqual(planner_config.default_method(conn), "builtin")
        finally:
            conn.close()
        result = self.call("get-planner-config", {})
        self.assertIsNone(result["config"])
        self.assertEqual(result["defaults"]["default_method"], "builtin")

    def test_save_and_get_default_method(self):
        ctx = cli.Context(self.db_path)
        try:
            saved = cmd_planner.cmd_set_planner_config(
                ctx, {"default_method": "spec_plan"})
        finally:
            ctx.close()
        self.assertEqual(saved["config"]["default_method"], "spec_plan")
        result = self.call("get-planner-config", {})
        self.assertEqual(result["config"]["default_method"], "spec_plan")
        # 模型组不受 default_method 单独保存影响（沿用默认）。
        self.assertEqual(result["config"]["standard"],
                         planner_config.DEFAULTS["standard"])
        # snapshot 不含 default_method（请求级字段不进快照）。
        conn = db.connect(self.db_path)
        try:
            snap = planner_config.snapshot(conn, "standard")
        finally:
            conn.close()
        self.assertNotIn("default_method", snap)

    def test_invalid_default_method_rejected(self):
        ctx = cli.Context(self.db_path)
        try:
            with self.assertRaises(util.KanpilotError) as ctx_err:
                cmd_planner.cmd_set_planner_config(
                    ctx, {"default_method": "mystery"})
        finally:
            ctx.close()
        self.assertEqual(ctx_err.exception.code, "bad_request")
        conn = db.connect(self.db_path)
        try:
            self.assertEqual(planner_config.default_method(conn), "builtin")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
