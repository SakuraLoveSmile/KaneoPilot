"""dispatch 校验链与发送链测试。

覆盖契约条款：protocol §4.5（dispatch claim 校验与上下文）、§4.6（发送证据）、
§4.10（resolve dispatch 类动作）、§4.11（错误码）、§5.6（publish-plan 校验）、
§5.10（dispatch-prepare/begin-send/confirm/fail）；state-model §2（转换表）、
§5（dispatch/lifecycle 串行位）。

每个「校验失败」断言三点：claim 返回 guard_failed + request_id；
task 按确定性/暂时性区分 needs_user(validation_failed) / 保持原状(remote_unavailable)。
"""

import json
import os

import pytest

try:
    from conftest import (
        EXECUTORS, PROJECTS, assert_iso8601, db_exec, set_task_send_state,
        ui_evidence, write_plan_file, write_review_report, write_source_snapshot,
    )
except ImportError:  # unittest discover 包上下文
    from tests.conftest import (
        EXECUTORS, PROJECTS, assert_iso8601, db_exec, set_task_send_state,
        ui_evidence, write_plan_file, write_review_report, write_source_snapshot,
    )


def _queued_dispatch(factory, cli, ready):
    # v2 §5.22.4：ready+hold 任务需 confirm_plan 放行后才能排队
    factory.enqueue_dispatch(ready.task.task_id)
    return factory.find_request(ready.task.task_id, "dispatch")


def _claim_dispatch(factory, cli, ready):
    _queued_dispatch(factory, cli, ready)
    return cli("claim", {"stage": "dispatch", "executor": EXECUTORS["dispatch"]})


# ---------------------------------------------------------------------------
# §4.5 claim 上下文与 §4.2-3 串行位
# ---------------------------------------------------------------------------


class TestDispatchClaimContext:
    def test_context_fields(self, factory, cli):
        ready = factory.make_ready()
        task = ready.task
        c = _claim_dispatch(factory, cli, ready)
        assert c["claimed"] is True
        ctx = c["context"]
        assert ctx["task_id"] == task.task_id
        assert ctx["plan_path"] == str(ready.plan.path)
        assert ctx["attachment_name"] == ready.plan.path.name
        assert ctx["repository"] == str(task.repository)
        assert ctx["project_id"] == task.project_id
        assert ctx["plan_kind"] == "standard"
        # session_marker = <task_id>/<run_id>
        assert ctx["session_marker"] == f"{task.task_id}/{c['run_id']}"
        assert c["run_id"] in ctx["session_marker"]
        # instruction 含任务标识
        assert task.task_id in ctx["instruction"]
        assert c["run_id"] in ctx["instruction"]
        assert ctx["original_todo_slug"] in ("to-do", "todo")
        assert ctx["requirements_sha256"] == task.requirements_sha256
        assert ctx["source_sha256"] == task.source_sha256
        assert ctx["lease_seconds"] > 0
        # claim 成功后 task → dispatching，写 run_id/send_state
        t = factory.get_task(task.task_id)
        assert t["status"] == "dispatching"
        assert t["run_id"] == c["run_id"]
        assert t["session_marker"] == ctx["session_marker"]
        assert t["send_state"] == "not_sent"
        assert t["sent"] in (False, 0, None)

    def test_dispatch_serial_blocks_second_claim(self, factory, cli):
        """同一时间至多一个 claimed dispatch（§4.2-3 / state-model §5）。"""
        r1 = factory.make_ready(title="派发一")
        r2 = factory.make_ready(project="Music", title="派发二")
        _queued_dispatch(factory, cli, r1)
        _queued_dispatch(factory, cli, r2)
        c1 = cli("claim", {"stage": "dispatch",
                           "executor": EXECUTORS["dispatch"]})
        assert c1["claimed"] is True
        c2 = cli("claim", {"stage": "dispatch",
                           "executor": EXECUTORS["dispatch"]})
        assert c2["claimed"] is False
        assert c2["reason"] == "stage_busy"

    def test_serial_released_after_confirm(self, factory, cli):
        """dispatch-confirm 后串行位释放，下一个可领。"""
        running = factory.make_running()
        r2 = factory.make_ready(project="Music", title="派发二")
        _queued_dispatch(factory, cli, r2)
        c = cli("claim", {"stage": "dispatch",
                          "executor": EXECUTORS["dispatch"]})
        assert c["claimed"] is True
        assert c["context"]["task_id"] == r2.task.task_id


# ---------------------------------------------------------------------------
# §4.5 校验链失败分支（确定性 → needs_user validation_failed）
# ---------------------------------------------------------------------------


class TestClaimValidation:
    def _expect_guard_fail(self, factory, cli, ready, request_id=None):
        payload = {"stage": "dispatch", "executor": EXECUTORS["dispatch"]}
        if request_id:
            payload["request_id"] = request_id
        c = cli("claim", payload)
        assert c["claimed"] is False
        assert c["reason"] == "guard_failed"
        assert c["request_id"]
        return c

    def test_plan_hash_tampered(self, factory, cli):
        """校验 1：plan_sha256 重算不等于登记值 → needs_user。"""
        ready = factory.make_ready()
        _queued_dispatch(factory, cli, ready)
        with open(ready.plan.path, "a", encoding="utf-8") as f:
            f.write("\n篡改内容\n")
        self._expect_guard_fail(factory, cli, ready)
        t = factory.get_task(ready.task.task_id)
        assert t["status"] == "needs_user"
        assert t["failure_code"] == "validation_failed"

    def test_source_snapshot_missing(self, factory, cli):
        """校验 2：来源快照不存在 → needs_user。"""
        ready = factory.make_ready()
        _queued_dispatch(factory, cli, ready)
        os.remove(ready.task.source_path)
        self._expect_guard_fail(factory, cli, ready)
        t = factory.get_task(ready.task.task_id)
        assert t["status"] == "needs_user"
        assert t["failure_code"] == "validation_failed"

    def test_source_snapshot_tampered(self, factory, cli):
        """校验 2/3：快照被改 → source/requirements 重算不符 → needs_user。"""
        ready = factory.make_ready()
        _queued_dispatch(factory, cli, ready)
        with open(ready.task.source_path, "a", encoding="utf-8") as f:
            f.write("tamper")
        self._expect_guard_fail(factory, cli, ready)
        assert factory.get_task(ready.task.task_id)["status"] == "needs_user"

    def test_remote_requirements_drift(self, factory, cli):
        """校验 4：远端需求指纹漂移 → needs_user。"""
        ready = factory.make_ready()
        _queued_dispatch(factory, cli, ready)
        factory.kaneo.set_description(ready.task.task_id, "远端已改需求 v2")
        self._expect_guard_fail(factory, cli, ready)
        t = factory.get_task(ready.task.task_id)
        assert t["status"] == "needs_user"
        assert t["failure_code"] == "validation_failed"

    def test_remote_not_todo(self, factory, cli):
        """校验 4：远端不在 to-do → needs_user。"""
        ready = factory.make_ready()
        _queued_dispatch(factory, cli, ready)
        factory.kaneo.set_status(ready.task.task_id, "in-progress")
        self._expect_guard_fail(factory, cli, ready)
        assert factory.get_task(ready.task.task_id)["status"] == "needs_user"

    def test_remote_unavailable_keeps_task(self, factory, cli):
        """校验 4：远端不可达 → remote_unavailable，task 保持原状（不算阻塞）。"""
        ready = factory.make_ready()
        _queued_dispatch(factory, cli, ready)
        factory.kaneo.fail_next(500, count=20)
        c = cli("claim", {"stage": "dispatch",
                          "executor": EXECUTORS["dispatch"]})
        assert c["claimed"] is False
        assert c["reason"] == "guard_failed"
        t = factory.get_task(ready.task.task_id)
        assert t["status"] == "ready"          # 不转 needs_user
        # 恢复后可正常领取
        factory.kaneo.clear_failures()
        req = _queued_dispatch(factory, cli, ready)
        c2 = cli("claim", {"stage": "dispatch",
                           "executor": EXECUTORS["dispatch"],
                           "request_id": req["request_id"]})
        assert c2["claimed"] is True

    def test_remote_401_unavailable(self, factory, cli):
        ready = factory.make_ready()
        _queued_dispatch(factory, cli, ready)
        factory.kaneo.fail_next(401, count=20)
        c = cli("claim", {"stage": "dispatch",
                          "executor": EXECUTORS["dispatch"]})
        assert c["claimed"] is False
        assert c["reason"] == "guard_failed"
        # 401 属暂时性远端故障 → 不阻塞 task
        assert factory.get_task(ready.task.task_id)["status"] == "ready"

    def test_standard_plan_rejects_review_marker(self, factory, cli):
        """校验 5：standard 计划要求远端描述不含 codex-kaneo-review 标记。"""
        task = factory.sync_task(description="需求 <!-- codex-kaneo-review:RK1 -->")
        factory.publish(task)
        # publish-plan 已结束 plan 请求，不再 report
        ready = type("R", (), {"task": task})()  # 轻包装
        _queued_dispatch(factory, cli, ready)
        c2 = cli("claim", {"stage": "dispatch",
                           "executor": EXECUTORS["dispatch"]})
        assert c2["claimed"] is False
        assert c2["reason"] == "guard_failed"
        assert factory.get_task(task.task_id)["status"] == "needs_user"

    def test_review_fix_requires_marker(self, factory, cli):
        """校验 5：review_fix 计划要求远端描述含 review 标记；缺失 → 失败。"""
        ir = factory.make_in_review()
        # 造 fix task（带标记）
        fix = cli("create-review-fix", {
            "source_task_id": ir.task.task_id,
            "title": "修复任务", "description": "修复描述",
            "priority": "high", "dedupe_key": "RK-nomarker"})
        fix_id = fix["fix_task_id"]
        detail = factory.get_task(fix_id)
        assert detail["plan_kind"] == "review_fix"
        # 造 review_fix 计划并发布（v2 §5.22.4：先发布再移除远端标记——
        # 移除会改需求指纹，publish-plan 的远端复验需要登记值仍匹配）
        task_view = type("T", (), {
            "task_id": fix_id, "project_id": detail["project_id"],
            "project_name": detail["project_name"],
            "repository": detail["repository"],
            "source_sha256": detail["source_sha256"],
            "requirements_sha256": detail["requirements_sha256"],
            "title": "修复任务"})()
        report = write_review_report(
            factory.env.app_support, task_id=ir.task.task_id,
            conclusion="changes_requested")
        fm = {"review_report_path": str(report.path),
              "review_report_sha256": report.sha256,
              "review_source_task_id": ir.task.task_id,
              "review_key": "RK-nomarker"}
        plan = write_plan_file(
            factory.env.plan_root, task_id=fix_id,
            project_id=task_view.project_id,
            project_name=task_view.project_name,
            repository=task_view.repository,
            source_sha256=task_view.source_sha256,
            requirements_sha256=task_view.requirements_sha256,
            plan_kind="review_fix", title="修复任务",
            extra_frontmatter=fm)
        pub = factory.publish(task_view, plan_kind="review_fix",
                              plan_file=plan)
        assert pub.resp["task_status"] == "ready"
        # 移除远端标记 → review_fix 校验应失败
        remote = factory.kaneo.get_task(fix_id)
        factory.kaneo.set_description(
            fix_id, (remote["description"] or "").replace(
                "<!-- codex-kaneo-review:RK-nomarker -->", ""))
        factory.enqueue_dispatch(fix_id)
        c = cli("claim", {"stage": "dispatch",
                          "executor": EXECUTORS["dispatch"]})
        assert c["claimed"] is False
        assert c["reason"] == "guard_failed"
        assert factory.get_task(fix_id)["status"] == "needs_user"

    def test_send_conflict_blocks_claim(self, factory, cli, tmp_env):
        """校验 6：send_state=unknown 的任务不得直接重派 → needs_user。"""
        ready = factory.make_ready()
        # 构造 send_state=unknown 的 ready（经 DB 后门；正常路径只能经 needs_user+resolve）
        set_task_send_state(tmp_env.db, ready.task.task_id, "unknown", sent=0)
        _queued_dispatch(factory, cli, ready)
        c = cli("claim", {"stage": "dispatch",
                          "executor": EXECUTORS["dispatch"]})
        assert c["claimed"] is False
        assert c["reason"] == "guard_failed"
        t = factory.get_task(ready.task.task_id)
        assert t["status"] == "needs_user"
        # 须走 resolve：restore 认定未发送
        cli("resolve", {"task_id": ready.task.task_id, "action": "restore"})
        t2 = factory.get_task(ready.task.task_id)
        assert t2["status"] == "ready"
        assert t2["send_state"] == "not_sent"


# ---------------------------------------------------------------------------
# §5.10 prepare / begin-send / confirm / fail
# ---------------------------------------------------------------------------


class TestDispatchChain:
    def _claimed(self, factory, cli):
        ready = factory.make_ready()
        _queued_dispatch(factory, cli, ready)
        c = cli("claim", {"stage": "dispatch",
                          "executor": EXECUTORS["dispatch"]})
        assert c["claimed"] is True
        return ready, c

    def _evidence(self, c, **over):
        ev = ui_evidence(
            session_marker=c["context"]["session_marker"],
            repository=c["context"]["repository"],
            attachment_name=c["context"]["attachment_name"])
        ev.update(over)
        return ev

    def test_happy_chain_state_transitions(self, factory, cli):
        """dispatching → prepared → submitting → running（§5.10 / state-model §2）。"""
        ready, c = self._claimed(factory, cli)
        tid = ready.task.task_id
        r = cli("dispatch-prepare", {"task_id": tid, "run_id": c["run_id"],
                                     "evidence": self._evidence(c)})
        assert r["status"] == "prepared"
        assert factory.get_task(tid)["status"] == "prepared"
        # begin-send 前远端仍 to-do
        assert factory.kaneo.get_task(tid)["status"] == "to-do"
        r = cli("dispatch-begin-send", {"task_id": tid, "run_id": c["run_id"]})
        assert r["status"] == "submitting"
        assert "Send" in r["action"] or "send" in r["action"].lower()
        # 远端已转 in-progress（bridge update-status 生效）
        assert factory.kaneo.get_task(tid)["status"] == "in-progress"
        assert factory.get_task(tid)["status"] == "submitting"
        ev2 = self._evidence(c, message_visible=True,
                             session_title="S", session_id="sess-1")
        r = cli("dispatch-confirm", {"task_id": tid, "run_id": c["run_id"],
                                     "evidence": ev2})
        assert r["status"] == "running"
        t = factory.get_task(tid)
        assert t["status"] == "running"
        assert t["sent"] in (True, 1)
        assert t["send_state"] == "sent"
        assert t["session_id"] == "sess-1"
        assert t["session_title"] == "S"
        # 自动建 monitor 请求
        assert factory.find_request(tid, "monitor") is not None
        # 事件链
        kinds = {e["kind"] for e in
                 cli("list-events", {"task_id": tid})["events"]}
        assert {"dispatch_prepared", "kaneo_transition",
                "dispatch_sent"} <= kinds

    def test_prepare_requires_instruction_visible(self, factory, cli):
        ready, c = self._claimed(factory, cli)
        out = cli("dispatch-prepare", {
            "task_id": ready.task.task_id, "run_id": c["run_id"],
            "evidence": self._evidence(c, instruction_visible=False)},
            expect=1)
        assert out["code"] == "evidence_invalid"

    def test_prepare_wrong_marker(self, factory, cli):
        ready, c = self._claimed(factory, cli)
        ev = self._evidence(c, session_marker="bogus/marker")
        out = cli("dispatch-prepare", {"task_id": ready.task.task_id,
                                       "run_id": c["run_id"],
                                       "evidence": ev}, expect=1)
        assert out["code"] == "evidence_invalid"

    def test_prepare_stale_run(self, factory, cli):
        ready, c = self._claimed(factory, cli)
        out = cli("dispatch-prepare", {
            "task_id": ready.task.task_id, "run_id": "wrong-run",
            "evidence": self._evidence(c)}, expect=1)
        assert out["code"] == "stale_run"

    def test_begin_send_requires_prepared(self, factory, cli):
        ready, c = self._claimed(factory, cli)
        out = cli("dispatch-begin-send", {
            "task_id": ready.task.task_id, "run_id": c["run_id"]}, expect=1)
        assert out["code"] in ("conflict", "bad_request")

    def test_begin_send_remote_conflict(self, factory, cli):
        """begin-send 时远端已非 original_todo_slug → remote_conflict → needs_user。"""
        ready, c = self._claimed(factory, cli)
        tid = ready.task.task_id
        cli("dispatch-prepare", {"task_id": tid, "run_id": c["run_id"],
                                 "evidence": self._evidence(c)})
        factory.kaneo.set_status(tid, "in-review")  # 人工动了远端
        out = cli("dispatch-begin-send", {"task_id": tid, "run_id": c["run_id"]},
                  expect=1)
        assert out["code"] == "remote_conflict"
        t = factory.get_task(tid)
        assert t["status"] == "needs_user"
        assert t["failure_code"] == "remote_conflict"
        assert t["failure_stage"]

    def test_confirm_requires_message_visible(self, factory, cli):
        ready, c = self._claimed(factory, cli)
        tid = ready.task.task_id
        cli("dispatch-prepare", {"task_id": tid, "run_id": c["run_id"],
                                 "evidence": self._evidence(c)})
        cli("dispatch-begin-send", {"task_id": tid, "run_id": c["run_id"]})
        out = cli("dispatch-confirm", {
            "task_id": tid, "run_id": c["run_id"],
            "evidence": self._evidence(c, message_visible=False)}, expect=1)
        assert out["code"] == "evidence_invalid"

    def test_confirm_detects_plan_tamper(self, factory, cli):
        """confirm 重算计划哈希：begin-send 后篡改计划 → 拒绝（§4.6）。"""
        ready, c = self._claimed(factory, cli)
        tid = ready.task.task_id
        cli("dispatch-prepare", {"task_id": tid, "run_id": c["run_id"],
                                 "evidence": self._evidence(c)})
        cli("dispatch-begin-send", {"task_id": tid, "run_id": c["run_id"]})
        with open(ready.plan.path, "a", encoding="utf-8") as f:
            f.write("\n发送期间篡改\n")
        ev = self._evidence(c, message_visible=True,
                            session_title="S", session_id="sess-1")
        out = cli("dispatch-confirm", {"task_id": tid, "run_id": c["run_id"],
                                       "evidence": ev}, expect=1)
        assert out["code"] in ("evidence_invalid", "validation_failed",
                               "conflict")
        assert factory.get_task(tid)["status"] == "submitting"  # 未推进

    def test_fail_not_sent_releases(self, factory, cli):
        """dispatch-fail not_sent → needs_user；resolve retry → ready + dispatch。"""
        ready, c = self._claimed(factory, cli)
        tid = ready.task.task_id
        r = cli("dispatch-fail", {"task_id": tid, "run_id": c["run_id"],
                                  "send_state": "not_sent",
                                  "reason": "找不到编辑器窗口"})
        assert r["status"] == "needs_user"
        assert r["send_state"] == "not_sent"
        t = factory.get_task(tid)
        assert t["status"] == "needs_user"
        assert t["send_state"] == "not_sent"
        assert t["failure_stage"]
        # retry 合法（send_state=not_sent）
        cli("resolve", {"task_id": tid, "action": "retry"})
        t2 = factory.get_task(tid)
        assert t2["status"] == "ready"
        assert factory.find_request(tid, "dispatch") is not None

    def test_fail_unknown_retains_and_resolves(self, factory, cli):
        """dispatch-fail unknown → 只给 restore/accept_duplicate/mark_sent。"""
        ready, c = self._claimed(factory, cli)
        tid = ready.task.task_id
        r = cli("dispatch-fail", {"task_id": tid, "run_id": c["run_id"],
                                  "send_state": "unknown",
                                  "reason": "点击发送后锁屏"})
        assert r["status"] == "needs_user"
        t = factory.get_task(tid)
        assert t["send_state"] == "unknown"
        # 普通 retry 不允许（前置 send_state=not_sent）
        out = cli("resolve", {"task_id": tid, "action": "retry"}, expect=1)
        assert out["code"] == "bad_request"
        # allowed_actions 不含 retry，含三专用键
        actions = set(t["allowed_actions"])
        assert {"restore", "accept_duplicate", "mark_sent"} <= actions
        assert "retry" not in actions
        # restore → ready + dispatch 请求
        cli("resolve", {"task_id": tid, "action": "restore"})
        t2 = factory.get_task(tid)
        assert t2["status"] == "ready"
        assert t2["send_state"] == "not_sent"
        assert factory.find_request(tid, "dispatch") is not None

    def test_fail_unknown_mark_sent(self, factory, cli):
        """mark_sent → running + monitor 请求。"""
        ready, c = self._claimed(factory, cli)
        tid = ready.task.task_id
        cli("dispatch-fail", {"task_id": tid, "run_id": c["run_id"],
                              "send_state": "unknown", "reason": "不明"})
        cli("resolve", {"task_id": tid, "action": "mark_sent"})
        t = factory.get_task(tid)
        assert t["status"] == "running"
        assert t["sent"] in (True, 1)
        assert t["send_state"] == "sent"
        assert factory.find_request(tid, "monitor") is not None

    def test_fail_unknown_accept_duplicate(self, factory, cli):
        """accept_duplicate → ready + dispatch 请求 + resolution 记录。"""
        ready, c = self._claimed(factory, cli)
        tid = ready.task.task_id
        cli("dispatch-fail", {"task_id": tid, "run_id": c["run_id"],
                              "send_state": "unknown", "reason": "不明"})
        cli("resolve", {"task_id": tid, "action": "accept_duplicate",
                        "note": "知情接受"})
        t = factory.get_task(tid)
        assert t["status"] == "ready"
        assert factory.find_request(tid, "dispatch") is not None
        evs = cli("list-events", {"task_id": tid})["events"]
        resolved = next(e for e in evs if e["kind"] == "resolved")
        assert resolved["detail"]


# ---------------------------------------------------------------------------
# §5.6 publish-plan 校验
# ---------------------------------------------------------------------------


class TestPublishPlanValidation:
    def _planned_task(self, factory, cli):
        """v2：sync → queue_plan → claim plan；返回
        (task, claim_resp, planner_input_sha256)。"""
        task = factory.sync_task()
        req_id, _i, input_sha = factory.queue_plan(task.task_id)
        c = factory.claim("plan", request_id=req_id)
        assert c["claimed"] is True
        return task, c, input_sha

    def _publish_payload(self, task, plan_path, generated_at, c, input_sha):
        return {
            "task_id": task.task_id, "plan_path": str(plan_path),
            "plan_kind": "standard", "source_sha256": task.source_sha256,
            "requirements_sha256": task.requirements_sha256,
            "generated_at": generated_at,
            "planner_model": "gpt-6-astra", "reasoning_effort": "medium",
            "request_id": c["request_id"], "run_id": c["run_id"],
            "planner_input_sha256": input_sha}

    def test_plan_outside_root_rejected(self, factory, cli, tmp_env):
        task, c, input_sha = self._planned_task(factory, cli)
        # 写到 Plan 根外
        outside = write_plan_file(
            tmp_env.root / "elsewhere", task_id=task.task_id,
            project_id=task.project_id, project_name=task.project_name,
            repository=task.repository, source_sha256=task.source_sha256,
            requirements_sha256=task.requirements_sha256)
        out = cli("publish-plan",
                  self._publish_payload(task, outside.path,
                                        outside.frontmatter["generated_at"],
                                        c, input_sha),
                  expect=1)
        assert out["code"] in ("bad_request", "validation_failed", "conflict",
                               "not_found")

    def test_symlink_plan_rejected(self, factory, cli, tmp_env):
        task, c, input_sha = self._planned_task(factory, cli)
        outside = write_plan_file(
            tmp_env.root / "elsewhere", task_id=task.task_id,
            project_id=task.project_id, project_name=task.project_name,
            repository=task.repository, source_sha256=task.source_sha256,
            requirements_sha256=task.requirements_sha256)
        link = tmp_env.plan_root / task.project_name / "linked.md"
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(outside.path, link)
        out = cli("publish-plan",
                  self._publish_payload(task, link,
                                        "2025-01-01T00:00:00+00:00",
                                        c, input_sha),
                  expect=1)
        assert out["code"] in ("bad_request", "validation_failed", "conflict")

    def test_frontmatter_task_id_mismatch(self, factory, cli):
        task, c, input_sha = self._planned_task(factory, cli)
        plan = write_plan_file(
            factory.env.plan_root, task_id="different-task-id",
            project_id=task.project_id, project_name=task.project_name,
            repository=task.repository, source_sha256=task.source_sha256,
            requirements_sha256=task.requirements_sha256)
        out = cli("publish-plan",
                  self._publish_payload(task, plan.path,
                                        plan.frontmatter["generated_at"],
                                        c, input_sha),
                  expect=1)
        assert out["code"] in ("bad_request", "validation_failed")

    def test_frontmatter_implementation_status(self, factory, cli):
        task, c, input_sha = self._planned_task(factory, cli)
        plan = write_plan_file(
            factory.env.plan_root, task_id=task.task_id,
            project_id=task.project_id, project_name=task.project_name,
            repository=task.repository, source_sha256=task.source_sha256,
            requirements_sha256=task.requirements_sha256,
            extra_frontmatter={"implementation_status": "已完成"})
        out = cli("publish-plan",
                  self._publish_payload(task, plan.path,
                                        plan.frontmatter["generated_at"],
                                        c, input_sha),
                  expect=1)
        assert out["code"] in ("bad_request", "validation_failed")

    def test_publish_on_dispatching_conflict(self, factory, cli):
        # task 处于派发中 → publish-plan conflict（§5.6）；v2 顺序：
        # 远端复验（任务 in-progress → conflict）先于请求/状态检查。
        running = factory.make_running()
        task = running.task
        plan = write_plan_file(
            factory.env.plan_root, task_id=task.task_id,
            project_id=task.project_id, project_name=task.project_name,
            repository=task.repository, source_sha256=task.source_sha256,
            requirements_sha256=task.requirements_sha256)
        out = cli("publish-plan", {
            "task_id": task.task_id, "plan_path": str(plan.path),
            "plan_kind": "standard", "source_sha256": task.source_sha256,
            "requirements_sha256": task.requirements_sha256,
            "generated_at": plan.frontmatter["generated_at"],
            "planner_model": "gpt-6-astra", "reasoning_effort": "medium",
            "request_id": "req-bogus", "run_id": "run-bogus",
            "planner_input_sha256": "0" * 64},
            expect=1)
        assert out["code"] == "conflict"


# ---------------------------------------------------------------------------
# unittest discover 收集入口（本机无 pytest；见 tests/unittest_compat.py）


def load_tests(loader, standard_tests, pattern):
    import sys

    try:
        import unittest_compat
    except ImportError:
        from tests import unittest_compat
    return unittest_compat.collect_module(sys.modules[__name__])
