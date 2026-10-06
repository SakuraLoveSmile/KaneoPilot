"""规划配置快照链路（protocol §5.22.6，planner-fix T2）：

- ``start-plans`` 把 ``planner_config.snapshot(plan_kind)`` 写进请求
  ``payload.model_config``；新配置只影响之后创建的请求，幂等命中的已排队
  请求不回写；未授权旧请求被用户确认复用时写入新快照。
- ``publish-plan``（§5.6）按请求快照校验 frontmatter 的
  planner_model/reasoning_effort；``dispatch`` claim（§4.5）按任务登记值
  校验；旧记录缺登记（NULL）时回落历史固定常量规则。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import constants, db, planfile, planner_config, util  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
