"""规划配置快照链路（protocol §5.22.6，planner-fix T2 + §5.24.1/§5.24.3 v4）：

- ``start-plans`` 把 ``planner_config.snapshot(plan_kind)`` 写进请求
  ``payload.model_config``（codex 分支）或 ``runtime_config``
  （pi_desktop 分支，五字段脱敏）；新配置只影响之后创建的请求，幂等命中的
  已排队请求不回写；未授权旧请求被用户确认复用时写入新快照。
- ``publish-plan``（§5.6）按请求快照校验 frontmatter 的
  planner_model/reasoning_effort；``dispatch`` claim（§4.5）按任务登记值
  校验；旧记录缺登记（NULL）时回落历史固定常量规则。
- v4：``backend`` 缺省 codex（旧配置零迁移）；两分支共存、服务离线可保存；
  Plus 分支要求五字段 + permission_mode=auto，只支持 builtin（spec_plan 明确
  禁用并给中文原因）；幂等判定包含 planner_backend（同材料换后端 → conflict）。
"""
from __future__ import annotations

import json
import unittest
import unittest.mock
from pathlib import Path

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import (  # noqa: E402
    cli, constants, db, planfile, planner_config, util,
)


CUSTOM = {"planner_model": "gpt-9-pluto", "reasoning_effort": "low"}
CUSTOM2 = {"planner_model": "gpt-8-nova", "reasoning_effort": "xhigh"}


def _meta(**overrides):
    meta = {
        "task_id": "taskmeta00000000000001",
        "project_id": "example-project-05",
        "project_name": "Feedback",
        "repository": str(constants.repo_path_for("Feedback")),
        "source_sha256": "a" * 64,
        "requirements_sha256": "b" * 64,
        "planner_model": "gpt-6-astra",
        "reasoning_effort": "medium",
        "implementation_status": "未开始",
        "generated_at": "2026-09-20T00:00:00+00:00",
    }
    meta.update(overrides)
    return meta


class CheckPlanMetaExpectedModel(unittest.TestCase):
    """planfile.check_plan_meta 的 expected_model 三态（§5.22.6 冻结签名）。"""

    def test_expected_model_match(self):
        meta = _meta(planner_model="m", reasoning_effort="r")
        planfile.check_plan_meta(
            meta, "standard",
            expected_model={"planner_model": "m", "reasoning_effort": "r"},
        )

    def test_expected_model_mismatch(self):
        expected = {"planner_model": "m", "reasoning_effort": "r"}
        with self.assertRaises(util.KanpilotError) as ctx:
            planfile.check_plan_meta(
                _meta(planner_model="other", reasoning_effort="r"),
                "standard", expected_model=expected,
            )
        self.assertEqual(ctx.exception.code, "validation_failed")
        self.assertIn("与本请求登记不符", ctx.exception.message)
        # reasoning_effort 单独不符同样拒绝。
        with self.assertRaises(util.KanpilotError):
            planfile.check_plan_meta(
                _meta(planner_model="m", reasoning_effort="other"),
                "standard", expected_model=expected,
            )

    def test_no_expected_model_uses_constants(self):
        # 缺省 → 历史固定常量规则（向后兼容：默认 frontmatter 通过）。
        planfile.check_plan_meta(_meta(), "standard")
        with self.assertRaises(util.KanpilotError) as ctx:
            planfile.check_plan_meta(
                _meta(planner_model="custom-model"), "standard"
            )
        self.assertIn("format mismatch", ctx.exception.message)


class PlannerConfigSnapshot(KanpilotTestCase):
    """start-plans 请求快照 + publish-plan/dispatch 校验路径。"""

    def _prepare_input(self, task_id):
        """登记 planner_inputs 行（manifest.task.project_name 供 start-plans
        项目白名单检查）。"""
        return self.make_planner_input(
            task_id,
            manifest_extra={
                "task": {"task_id": task_id, "project_name": "Feedback"},
            },
        )

    def _start_plans(self, info):
        return self.call("start-plans", {
            "items": [{"input_id": info["input_id"],
                       "input_sha256": info["input_sha256"]}],
        })

    def _request_payload(self, request_id):
        detail = self.call("get-request", {"request_id": request_id})
        return detail["payload"]

    def _task_field(self, task_id, field):
        conn = db.connect(self.db_path)
        try:
            return db.get_task(conn, task_id)[field]
        finally:
            conn.close()

    def test_start_plans_uses_saved_planner_config(self):
        task = self.fake.add_task(
            "taskcfg00000000000001", "example-project-05")
        self.make_repo("Feedback")
        self.sync_task(task, project_name="Feedback")
        # 未保存配置 → 默认快照（与旧常量一致）。
        info = self._prepare_input(task["id"])
        result = self._start_plans(info)
        self.assertTrue(result["results"][0]["request_id"])
        payload = self._request_payload(result["results"][0]["request_id"])
        self.assertEqual(
            payload["model_config"], dict(planner_config.DEFAULTS["standard"]))
        # set-planner-config 后 → 新请求携带新快照；review_fix 分组独立。
        self.call("set-planner-config", {
            "standard": dict(CUSTOM), "review_fix": dict(CUSTOM2)})
        task2 = self.fake.add_task(
            "taskcfg00000000000002", "example-project-05")
        self.sync_task(task2, project_name="Feedback")
        info2 = self._prepare_input(task2["id"])
        result2 = self._start_plans(info2)
        payload2 = self._request_payload(result2["results"][0]["request_id"])
        self.assertEqual(payload2["model_config"], CUSTOM)
        task3 = self.fake.add_task(
            "taskcfg00000000000003", "example-project-05")
        self.sync_task(task3, project_name="Feedback")
        self.set_task_status(task3["id"], "needs_plan",
                             plan_kind="review_fix")
        info3 = self._prepare_input(task3["id"])
        result3 = self._start_plans(info3)
        payload3 = self._request_payload(result3["results"][0]["request_id"])
        self.assertEqual(payload3["model_config"], CUSTOM2)
        self.assertEqual(payload3["plan_kind"], "review_fix")

    def test_queued_request_keeps_its_snapshot(self):
        """新配置只影响之后创建的请求：已排队请求的 model_config 不回写
        （幂等 deduplicated 路径也不改 payload）。"""
        task = self.fake.add_task(
            "taskcfg00000000000004", "example-project-05")
        self.make_repo("Feedback")
        self.sync_task(task, project_name="Feedback")
        self.call("set-planner-config", {"standard": dict(CUSTOM)})
        info = self._prepare_input(task["id"])
        result = self._start_plans(info)
        request_id = result["results"][0]["request_id"]
        self.assertEqual(
            self._request_payload(request_id)["model_config"], CUSTOM)
        # 改配置后：旧请求快照不变；同材料重复提交 → deduplicated 且不回写。
        self.call("set-planner-config", {"standard": dict(CUSTOM2)})
        self.assertEqual(
            self._request_payload(request_id)["model_config"], CUSTOM)
        again = self._start_plans(info)
        self.assertTrue(again["results"][0]["deduplicated"])
        self.assertEqual(again["results"][0]["request_id"], request_id)
        self.assertEqual(
            self._request_payload(request_id)["model_config"], CUSTOM)

    def test_unauthorized_reuse_writes_new_snapshot(self):
        """未授权旧请求被确认复用（等价新确认）→ 写回新快照（§5.22.2）。"""
        task = self.fake.add_task(
            "taskcfg00000000000005", "example-project-05")
        self.make_repo("Feedback")
        self.sync_task(task, project_name="Feedback")
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                db.create_request(
                    conn, util.now_iso(), "plan", f"plan:{task['id']}",
                    task_id=task["id"], payload={"legacy": True},
                    requested_by="test")
        finally:
            conn.close()
        self.call("set-planner-config", {"standard": dict(CUSTOM)})
        info = self._prepare_input(task["id"])
        result = self._start_plans(info)
        entry = result["results"][0]
        self.assertTrue(entry["request_id"])
        self.assertTrue(entry.get("reused"))
        payload = self._request_payload(entry["request_id"])
        self.assertEqual(payload["model_config"], CUSTOM)
        self.assertTrue(payload["manual_authorized"])

    def test_publish_plan_validates_against_request_snapshot(self):
        """publish-plan 按请求 payload.model_config 校验：登记 X 时
        frontmatter=X 通过、frontmatter=默认模型拒绝（§5.6/§5.22.6）。"""
        fixture = self.make_ready_task(plan_overrides=dict(CUSTOM))
        task_id = fixture["remote"]["id"]
        self.assertEqual(self._task_field(task_id, "planner_model"),
                         CUSTOM["planner_model"])
        # 另一任务：请求快照是 CUSTOM，frontmatter 写默认模型 → validation_failed。
        remote = self.fake.add_task(
            "taskcfg00000000000006", "example-project-05")
        self.make_repo("Feedback")
        source_sha, req_sha, source_path = self.make_source_snapshot(remote)
        self.sync_task(
            remote, project_name="Feedback",
            requirements_sha256=req_sha, source_sha256=source_sha,
            source_path=source_path)
        plan_path = self.write_plan("Feedback", remote, {
            "source_sha256": source_sha, "requirements_sha256": req_sha})
        request_id, input_sha = self.queue_plan(
            remote["id"], source_sha256=source_sha,
            requirements_sha256=req_sha, **dict(CUSTOM))
        claim = self.call("claim", {
            "stage": "plan", "executor": "test-planner",
            "request_id": request_id})
        self.assertTrue(claim["claimed"])
        exc = self.call_err("publish-plan", {
            "task_id": remote["id"], "plan_path": str(plan_path),
            "plan_kind": "standard",
            "source_sha256": source_sha, "requirements_sha256": req_sha,
            "generated_at": "2026-09-20T00:00:00+00:00",
            "planner_model": "gpt-6-astra", "reasoning_effort": "medium",
            "request_id": request_id, "run_id": claim["run_id"],
            "planner_input_sha256": input_sha,
        })
        self.assertEqual(exc.code, "validation_failed")
        self.assertIn("与本请求登记不符", exc.message)

    def test_dispatch_uses_registered_model(self):
        """dispatch claim 按任务登记的 planner_model/reasoning_effort 比对：
        frontmatter 与登记一致 → 通过；不一致 → validation_failed 且不影响
        其他任务登记值的领取（§4.5/§5.22.6）。"""
        fixture = self.make_ready_task(plan_overrides=dict(CUSTOM))
        task_id = fixture["remote"]["id"]
        # 发布后把 frontmatter 模型改成非登记值（登记哈希随文件同步变化）。
        path = Path(fixture["plan_path"])
        text = path.read_text(encoding="utf-8")
        self.assertIn("planner_model: gpt-9-pluto", text)
        path.write_text(
            text.replace("planner_model: gpt-9-pluto",
                         "planner_model: gpt-6-astra", 1),
            encoding="utf-8")
        self.enqueue_dispatch(task_id)
        result = self.call(
            "claim", {"stage": "dispatch", "executor": "kaneo-dispatch"})
        self.assertFalse(result["claimed"])
        self.assertEqual(result["reason"], "guard_failed")
        self.assertEqual(result["code"], "validation_failed")
        self.assertIn("与本请求登记不符", result["detail"])
        self.assertEqual(self.task(task_id)["status"], "needs_user")
        # frontmatter=登记值的任务照常领取。
        fixture2 = self.make_ready_task(
            task_id="taskcfg00000000000007", plan_overrides=dict(CUSTOM))
        self.enqueue_dispatch(fixture2["remote"]["id"])
        claim = self.call(
            "claim", {"stage": "dispatch", "executor": "kaneo-dispatch"})
        self.assertTrue(claim["claimed"])
        self.assertEqual(claim["context"]["task_id"],
                         fixture2["remote"]["id"])

    def test_legacy_task_without_registration_falls_back(self):
        """旧任务 planner_model=NULL → 回落历史常量规则，默认 frontmatter
        照常通过派发校验。"""
        fixture = self.make_ready_task()
        task_id = fixture["remote"]["id"]
        self.set_task_status(task_id, "ready",
                             planner_model=None, reasoning_effort=None)
        self.enqueue_dispatch(task_id)
        claim = self.call(
            "claim", {"stage": "dispatch", "executor": "kaneo-dispatch"})
        self.assertTrue(claim["claimed"])
        self.assertEqual(claim["context"]["task_id"], task_id)




# v4（§5.24.1/§5.24.3）：规划后端字段、请求快照与幂等。cli.py 尚未注册
# backend/pi_desktop 时由本文件补登记（并集，幂等）。
V4_ENTRIES = {
    "set-planner-config": (
        {"backend": (False, "str"), "pi_desktop": (False, "dict")},
        None,
    ),
}
PLUS_BLOCK = {
    "model_source": "custom",
    "connection_file": "/tmp/pi-desktop/mcp-control.json",
    "provider_id": "closeai",
    "model_id": "deepseek-flash",
    "thinking_level": "high",
    "permission_mode": "auto",
}
PLUS_SNAPSHOT = {
    k: PLUS_BLOCK[k] for k in constants.EXEC_PI_DESKTOP_KEYS
}


def _install_entries(entries):
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


def _restore_entries(saved):
    for name, original in saved.items():
        if original is None:
            cli.COMMANDS.pop(name, None)
        else:
            cli.COMMANDS[name] = original


class PlannerBackendConfig(PlannerConfigSnapshot):
    """v4（§5.24.1/§5.24.3）：规划后端字段与 start-plans 固化。"""

    def setUp(self):
        super().setUp()
        saved = _install_entries(V4_ENTRIES)
        self.addCleanup(_restore_entries, saved)

    def _new_task(self, task_id):
        task = self.fake.add_task(
            task_id, "example-project-05", status="to-do")
        self.make_repo("Feedback")
        self.sync_task(task, project_name="Feedback")
        return task

    def _start(self, info, **item_fields):
        item = {"input_id": info["input_id"],
                "input_sha256": info["input_sha256"]}
        item.update(item_fields)
        return self.call("start-plans", {"items": [item]})["results"][0]

    def _save_plus(self):
        self.call("set-planner-config", {
            "backend": "pi_desktop", "pi_desktop": dict(PLUS_BLOCK),
        })

    # ------------------------------------------------------------- 配置

    def test_legacy_config_reads_backend_codex(self):
        """旧 planner_config（无 backend）零迁移按 codex 解释。"""
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                db.set_setting(conn, "planner_config", {
                    "standard": dict(CUSTOM), "review_fix": dict(CUSTOM2),
                    "default_method": "builtin",
                    "updated_at": "2026-01-01T00:00:00Z",
                })
        finally:
            conn.close()
        result = self.call("get-planner-config", {})
        config = result["config"]
        self.assertEqual(config["backend"], "codex")
        self.assertIsNone(config["pi_desktop"])
        self.assertEqual(config["standard"], CUSTOM)
        self.assertEqual(result["defaults"]["backend"], "codex")
        conn = db.connect(self.db_path)
        try:
            self.assertEqual(planner_config.backend(conn), "codex")
            self.assertEqual(planner_config.snapshot(conn, "standard"), CUSTOM)
            self.assertEqual(planner_config.snapshot(conn, "review_fix"),
                             CUSTOM2)
        finally:
            conn.close()

    def test_two_branches_coexist_and_offline_save(self):
        self.call("set-planner-config", {"standard": dict(CUSTOM)})
        saved = self.call("set-planner-config", {
            "backend": "pi_desktop", "pi_desktop": dict(PLUS_BLOCK),
        })
        config = saved["config"]
        self.assertEqual(config["backend"], "pi_desktop")
        self.assertEqual(config["pi_desktop"], PLUS_BLOCK)
        # codex 分支字段保留（切回即生效）。
        self.assertEqual(config["standard"], CUSTOM)
        conn = db.connect(self.db_path)
        try:
            self.assertEqual(planner_config.snapshot(conn, "standard"),
                             PLUS_SNAPSHOT)
            self.assertEqual(planner_config.runtime_snapshot(conn), PLUS_SNAPSHOT)
        finally:
            conn.close()
        # 切回 codex：Plus 块保留，快照回到 codex 两组字段。
        self.call("set-planner-config", {"backend": "codex"})
        got = self.call("get-planner-config", {})["config"]
        self.assertEqual(got["backend"], "codex")
        self.assertEqual(got["pi_desktop"], PLUS_BLOCK)
        conn = db.connect(self.db_path)
        try:
            self.assertEqual(planner_config.snapshot(conn, "standard"), CUSTOM)
        finally:
            conn.close()

    def test_inherit_snapshot_fails_when_plus_default_cannot_be_resolved(self):
        block = {
            "model_source": "inherit",
            "connection_file": "/tmp/pi-desktop/mcp-control.json",
            "permission_mode": "auto",
        }
        self.call("set-planner-config", {
            "backend": "pi_desktop", "pi_desktop": block,
        })
        conn = db.connect(self.db_path)
        try:
            with unittest.mock.patch(
                "kanpilot.pi_desktop_backend.resolve_plus_default",
                return_value=(None, "Plus 离线"),
            ):
                with self.assertRaises(util.KanpilotError) as raised:
                    planner_config.runtime_snapshot(conn)
                self.assertIn("Plus 离线", raised.exception.message)
        finally:
            conn.close()

    def test_plus_block_requires_auto_and_absolute_path(self):
        for block in (
            dict(PLUS_BLOCK, permission_mode="ask"),
            dict(PLUS_BLOCK, permission_mode="accept-edits"),
            {k: v for k, v in PLUS_BLOCK.items() if k != "permission_mode"},
            dict(PLUS_BLOCK, connection_file="relative/mcp.json"),
            dict(PLUS_BLOCK, thinking_level="ultra"),
        ):
            with self.subTest(block=block):
                exc = self.call_err("set-planner-config", {
                    "backend": "pi_desktop", "pi_desktop": block,
                })
                self.assertEqual(exc.code, "bad_request")
        self.assertIsNone(self.call("get-planner-config", {})["config"])
        exc = self.call_err("set-planner-config", {
            "backend": "pi_desktop",
            "pi_desktop": dict(PLUS_BLOCK, permission_mode="ask"),
        })
        self.assertIn("auto", str(exc))

    def test_legacy_ask_settings_use_auto_for_new_request_without_resave(self):
        conn = db.connect(self.db_path)
        stored = {"backend": "pi_desktop", "pi_desktop": dict(PLUS_BLOCK, permission_mode="ask")}
        try:
            with db.write_txn(conn):
                db.set_setting(conn, "planner_config", stored)
        finally:
            conn.close()
        effective = self.call("get-planner-config", {})["config"]
        self.assertEqual(effective["pi_desktop"], PLUS_BLOCK)
        task = self._new_task("legacyasksettings001")
        entry = self._start(self._prepare_input(task["id"]), planner_backend="pi_desktop")
        self.assertEqual(self._request_payload(entry["request_id"])["runtime_config"], PLUS_SNAPSHOT)
        conn = db.connect(self.db_path)
        try:
            self.assertEqual(db.get_setting(conn, "planner_config"), stored)
        finally:
            conn.close()

    def test_inactive_plus_block_rejects_explicit_ask(self):
        error = self.call_err("set-planner-config", {
            "backend": "codex", "pi_desktop": dict(PLUS_BLOCK, permission_mode="ask"),
        })
        self.assertEqual(error.code, "bad_request")
        self.assertIn("auto", str(error))

    def test_plus_spec_plan_rejected_on_save(self):
        # 1) 同一次提交：Plus + spec_plan → bad_request。
        exc = self.call_err("set-planner-config", {
            "backend": "pi_desktop", "pi_desktop": dict(PLUS_BLOCK),
            "default_method": "spec_plan",
        })
        self.assertEqual(exc.code, "bad_request")
        self.assertIn("spec_plan", str(exc))
        # 2) 先存 spec_plan（codex 下合法），再切 Plus → 明确失败并指路。
        self.call("set-planner-config", {"default_method": "spec_plan"})
        exc = self.call_err("set-planner-config", {
            "backend": "pi_desktop", "pi_desktop": dict(PLUS_BLOCK),
        })
        self.assertEqual(exc.code, "bad_request")
        self.assertIn("builtin", str(exc))
        got = self.call("get-planner-config", {})["config"]
        self.assertEqual(got["backend"], "codex")  # 旧值不变
        self.assertEqual(got["default_method"], "spec_plan")
        # 3) 先把方式改回 builtin → 切 Plus 成功。
        self.call("set-planner-config", {"default_method": "builtin"})
        saved = self.call("set-planner-config", {
            "backend": "pi_desktop", "pi_desktop": dict(PLUS_BLOCK),
        })
        self.assertEqual(saved["config"]["backend"], "pi_desktop")

    # ------------------------------------------------------- start-plans

    def test_start_plans_fixates_backend_and_snapshot(self):
        self._save_plus()
        task = self._new_task("taskplus0000000000001")
        info = self._prepare_input(task["id"])
        codex_default = self._start(info)
        payload = self._request_payload(codex_default["request_id"])
        self.assertEqual(payload["planner_backend"], "codex")
        self.assertEqual(payload["model_config"],
                         dict(planner_config.DEFAULTS["standard"]))
        self.assertNotIn("runtime_config", payload)

        task2 = self._new_task("taskplus0000000000002")
        info2 = self._prepare_input(task2["id"])
        entry = self._start(info2, planner_backend="pi_desktop")
        self.assertTrue(entry["request_id"], entry)
        payload2 = self._request_payload(entry["request_id"])
        self.assertEqual(payload2["planner_backend"], "pi_desktop")
        self.assertEqual(payload2["planner_method"], "builtin")
        self.assertEqual(payload2["runtime_config"], PLUS_SNAPSHOT)
        self.assertNotIn("model_config", payload2)
        blob = json.dumps(payload2, ensure_ascii=False)
        self.assertNotIn("token", blob.lower())

    def test_idempotency_includes_backend(self):
        self._save_plus()
        task = self._new_task("taskplus0000000000011")
        info = self._prepare_input(task["id"])
        first = self._start(info)  # 缺省 codex
        self.assertTrue(first["request_id"], first)
        # 同材料换后端 → conflict（不暗换后端），原请求不变。
        again = self._start(info, planner_backend="pi_desktop")
        self.assertEqual(again["code"], "conflict")
        self.assertIn("后端", again["error"])
        payload = self._request_payload(first["request_id"])
        self.assertEqual(payload["planner_backend"], "codex")
        self.assertIn("model_config", payload)
        # 同材料同后端同方式 → deduplicated。
        same = self._start(info)
        self.assertTrue(same["deduplicated"])
        self.assertEqual(same["request_id"], first["request_id"])

    def test_legacy_request_without_backend_is_codex(self):
        """旧 payload 缺 planner_backend → 按 codex：codex 提交 dedup，
        Plus 提交 conflict。"""
        task = self._new_task("taskplus0000000000012")
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
                        "model_config": dict(
                            planner_config.DEFAULTS["standard"]),
                        "confirmed_at": util.now_iso(),
                    },
                    requested_by="test",
                )
        finally:
            conn.close()
        codex = self._start(info)
        self.assertTrue(codex["deduplicated"], codex)
        self._save_plus()
        plus = self._start(info, planner_backend="pi_desktop")
        self.assertEqual(plus["code"], "conflict")
        self.assertIn("后端", plus["error"])

    def test_plus_item_requires_builtin_method(self):
        self._save_plus()
        task = self._new_task("taskplus0000000000021")
        info = self._prepare_input(task["id"])
        entry = self._start(
            info, planner_backend="pi_desktop", planner_method="spec_plan")
        self.assertEqual(entry["code"], "validation_failed")
        self.assertIn("spec_plan", entry["error"])
        self.assertIn("Plus", entry["error"])
        self.assertIsNone(entry.get("request_id"))
        self.assertFalse(self.requests(status="queued"))

    def test_plus_item_without_saved_block_fails(self):
        task = self._new_task("taskplus0000000000022")
        info = self._prepare_input(task["id"])
        entry = self._start(info, planner_backend="pi_desktop")
        self.assertEqual(entry["code"], "validation_failed")
        self.assertIn("五字段", entry["error"])
        self.assertFalse(self.requests(status="queued"))

    def test_invalid_planner_backend_bad_request(self):
        task = self._new_task("taskplus0000000000031")
        info = self._prepare_input(task["id"])
        entry = self._start(info, planner_backend="plus")
        self.assertEqual(entry["code"], "bad_request")
        self.assertEqual(entry["task_id"], task["id"])
        self.assertFalse(self.requests(status="queued"))

    # ------------------------------------------------------ capabilities

    def test_planner_capabilities_backends(self):
        result = self.call("planner-capabilities", {})
        self.assertIn("backends", result)
        codex_keys = ("codex", "provider", "auth", "models", "models_source",
                      "models_verified", "planner_methods")
        self.assertEqual(result["backends"]["codex"],
                         {key: result[key] for key in codex_keys})
        plus = result["backends"]["pi_desktop"]
        self.assertEqual(set(plus),
                         {"available", "detail", "tools_ok", "version",
                          "planner_methods"})
        self.assertFalse(plus["available"])
        self.assertIn("未保存", plus["detail"])
        self.assertFalse(plus["planner_methods"]["builtin"]["available"])
        spec = plus["planner_methods"]["spec_plan"]
        self.assertFalse(spec["available"])
        self.assertIn("spec_plan", spec["reason"])
        self.assertIn("Codex", spec["reason"])
        # 顶层旧字段保留（planner_methods 仍是 codex 口径）。
        self.assertIn("builtin", result["planner_methods"])

    def test_planner_capabilities_plus_branch_probe(self):
        self._save_plus()
        calls = []

        def fake_probe(*args, **kwargs):
            calls.append((args, kwargs))
            return {
                "ok": True, "detail": "fake ready",
                "tools": set(constants.PI_DESKTOP_PLAN_TOOLS),
                "version": "2025-06-18",
            }

        with unittest.mock.patch(
            "kanpilot.pi_desktop_backend.probe_connection",
            side_effect=fake_probe,
        ):
            result = self.call("planner-capabilities", {})
        plus = result["backends"]["pi_desktop"]
        self.assertTrue(plus["available"], plus)
        self.assertTrue(plus["tools_ok"])
        self.assertEqual(plus["version"], "2025-06-18")
        self.assertTrue(plus["planner_methods"]["builtin"]["available"])
        self.assertIsNone(plus["planner_methods"]["builtin"]["reason"])
        # spec_plan 在 Plus 下始终禁用的中文原因。
        self.assertFalse(plus["planner_methods"]["spec_plan"]["available"])
        args, kwargs = calls[0]
        self.assertEqual(args, (PLUS_BLOCK["connection_file"],))
        self.assertEqual(kwargs["provider_id"], PLUS_BLOCK["provider_id"])
        self.assertEqual(kwargs["model_id"], PLUS_BLOCK["model_id"])
        self.assertEqual(tuple(kwargs["required_tools"]),
                         constants.PI_DESKTOP_PLAN_TOOLS)
        # Plus 分支不含 token/连接文件全文（codex 分支的 auth_detail 措辞
        # 里本来就有 "token" 一词，故只对 Plus 分支做红线检查）。
        plus_blob = json.dumps(plus, ensure_ascii=False)
        self.assertNotIn("bearer", plus_blob.lower())
        self.assertNotIn("authorization", plus_blob.lower())


if __name__ == "__main__":
    unittest.main()
