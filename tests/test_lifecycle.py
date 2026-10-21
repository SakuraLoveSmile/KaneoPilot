"""端到端生命周期：sync→plan→publish→dispatch→running→monitor→in-review→review→done；
changes_requested→create-review-fix(dedupe)→review_fix plan 流程。

覆盖契约条款：protocol §4.5、§4.7、§5.4（sync-tasks）、§5.6（publish-plan）、
§5.7（register-review）、§5.8（create-review-fix）、§5.9（advance-task）、
§5.10（dispatch 子步骤）、§5.11（recheck）；state-model §2（task 转换表）、
§4（事件 kind 枚举）；automation-contract §2（各 stage 流程）。
"""

from pathlib import Path

import pytest

try:
    from conftest import (
        EXECUTORS, PROJECTS, assert_iso8601, ui_evidence, write_plan_file,
        write_review_report, write_source_snapshot,
    )
except ImportError:  # unittest discover 包上下文
    from tests.conftest import (
        EXECUTORS, PROJECTS, assert_iso8601, ui_evidence, write_plan_file,
        write_review_report, write_source_snapshot,
    )


def _events(cli, task_id):
    return cli("list-events", {"task_id": task_id})["events"]


def _kinds(cli, task_id):
    return {e["kind"] for e in _events(cli, task_id)}


# ---------------------------------------------------------------------------
# 全链 happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_full_chain(self, factory, cli):
        # 1) sync-tasks → needs_plan（v2 §5.22.2：不再自动建 plan 请求）
        task = factory.sync_task(title="端到端任务", description="做一件完整的事")
        tid = task.task_id
        assert task.sync["upserted"] == 1
        assert task.sync["created_requests"] == [], (
            "v2 起 sync 不再自动建请求，等规划面板手动提交")
        t = factory.get_task(tid)
        assert t["status"] == "needs_plan"
        assert t["display_status"] == "待规划"

        # 2) 规划面板链路：queue_plan → claim plan → planning
        plan_req_id, _input_id, _input_sha = factory.queue_plan(tid)
        c = factory.claim("plan", request_id=plan_req_id)
        assert c["claimed"] is True
        assert factory.get_task(tid)["status"] == "planning"
        assert factory.get_task(tid)["display_status"] == "规划中"

        # 3) publish-plan → ready + plan_execution_hold（请求随发布闭环，
        #    不再单独 report）
        pub = factory.publish(task)
        assert pub.resp["ok"] is True
        assert pub.resp["task_status"] == "ready"
        assert len(pub.resp["plan_sha256"]) == 64
        t = factory.get_task(tid)
        assert t["status"] == "ready"
        assert t["display_status"] == "待确认执行"  # v2 §5.22.4 计划确认闸

        # 4) 安排执行（confirm_plan 闸）→ 已排队；claim → dispatching
        en = factory.enqueue_dispatch(tid)
        assert en["request"]["kind"] == "dispatch"
        assert factory.get_task(tid)["display_status"] == "已排队"
        d = factory.claim("dispatch")
        assert d["claimed"] is True
        assert d["context"]["task_id"] == tid
        assert factory.get_task(tid)["status"] == "dispatching"

        # 5) prepare → prepared；begin-send → submitting（远端 in-progress）
        ev = ui_evidence(
            session_marker=d["context"]["session_marker"],
            repository=d["context"]["repository"],
            attachment_name=d["context"]["attachment_name"])
        assert cli("dispatch-prepare", {"task_id": tid, "run_id": d["run_id"],
                                        "evidence": ev})["status"] == "prepared"
        assert cli("dispatch-begin-send", {"task_id": tid,
                                           "run_id": d["run_id"]})["status"] == "submitting"
        assert factory.kaneo.get_task(tid)["status"] == "in-progress"

        # 6) confirm → running + monitor 请求
        ev2 = dict(ev, message_visible=True, session_title="E2E",
                   session_id="sess-e2e")
        assert cli("dispatch-confirm", {"task_id": tid, "run_id": d["run_id"],
                                        "evidence": ev2})["status"] == "running"
        t = factory.get_task(tid)
        assert t["status"] == "running"
        assert t["display_status"] == "执行中"
        mreq = factory.find_request(tid, "monitor")
        assert mreq is not None

        # 7) monitor claim → heartbeat → advance-task in-review → report
        m = factory.claim("monitor", request_id=mreq["request_id"])
        assert m["claimed"] is True
        hb = cli("heartbeat", {"request_id": m["request_id"],
                               "run_id": m["run_id"], "progress": "执行顺利"})
        assert hb["ok"] is True
        # §5.9 分支评审前置（claim→snapshot→commit→register）：任务工作区由
        # start-plans 登记为 workspace_version=1，推进 in-review 前必须先有
        # 本轮执行的核心本地提交（core.require_committed_review 要求
        # delivery.commits 中存在 execution_run_id==本轮执行 run 且
        # sha==delivery.head_sha 的登记）。这里沿用 tests/conftest.py
        # TaskFactory.make_in_review 的既有正确生命周期模式：在 tmp_env 的
        # 临时本地仓库工作树里造一处真实改动，再经核心命令 delivery-commit
        # （monitor 领取身份 + expected_execution_run_id=派发 run_id）由核心
        # 完成提交与登记——不手写 delivery 证据、不弱化守卫、不触真实远端。
        delivery = factory.get_task(tid).get("delivery") or {}
        assert (delivery.get("mode") == "pr"
                or delivery.get("workspace_version") == 1) and all(
            delivery.get(field) for field in ("branch", "base_branch",
                                              "base_sha", "head_sha",
                                              "worktree_path")), (
            f"{tid} 应已登记完整分支评审工作区：{delivery}")
        (Path(delivery["worktree_path"]) / "lifecycle-feature.py").write_text(
            "VALUE = 1\n", encoding="utf-8")
        committed = cli("delivery-commit", {
            "task_id": tid, "request_id": m["request_id"], "run_id": m["run_id"],
            "expected_execution_run_id": d["run_id"]})
        assert committed["outcome"] in ("committed", "already_committed"), committed
        assert committed["sha"] == factory.get_task(tid)["delivery"]["head_sha"], committed
        adv = cli("advance-task", {"task_id": tid, "run_id": m["run_id"],
                                   "target": "in-review",
                                   "expected_current": "in-progress"})
        assert adv["ok"] is True
        assert adv["task_status"] == "in_review"
        assert factory.kaneo.get_task(tid)["status"] == "in-review"
        cli("report", {"request_id": m["request_id"], "run_id": m["run_id"],
                       "outcome": "succeeded"})
        t = factory.get_task(tid)
        assert t["status"] == "in_review"
        assert t["display_status"] == "评审中"
        # in-review 自动建 review 请求
        rreq = factory.find_request(tid, "review")
        assert rreq is not None

        # 8) review claim → register-review pass → advance done → report
        rv = factory.claim("review", request_id=rreq["request_id"])
        assert rv["claimed"] is True
        report = write_review_report(factory.env.app_support, task_id=tid,
                                     conclusion="pass")
        # §5.24.5 分支评审领取身份：request_id 与 run_id 同源于上面那次真实
        # 的 review 领取（rv），不是任务执行轮次 d/m 的 run。
        reg = cli("register-review", {"task_id": tid,
                                      "request_id": rv["request_id"],
                                      "run_id": rv["run_id"],
                                      "report_path": str(report.path),
                                      "conclusion": "pass"})
        assert reg["ok"] is True
        assert len(reg["report_sha256"]) == 64
        adv2 = cli("advance-task", {"task_id": tid, "target": "done",
                                    "expected_current": "in-review"})
        assert adv2["ok"] is True
        assert adv2["task_status"] == "done"
        assert factory.kaneo.get_task(tid)["status"] == "done"
        cli("report", {"request_id": rv["request_id"], "run_id": rv["run_id"],
                       "outcome": "succeeded"})
        t = factory.get_task(tid)
        assert t["status"] == "done"
        assert t["display_status"] == "已完成"
        assert t["save_state"] == "active"

        # 9) 事件链覆盖关键 kind（state-model §4 枚举）
        kinds = _kinds(cli, tid)
        assert {"task_created", "enqueued", "claimed", "plan_published",
                "status_change", "dispatch_prepared", "kaneo_transition",
                "dispatch_sent", "reported", "heartbeat",
                "review_registered"} <= kinds
        # 时间线倒序
        evs = _events(cli, tid)
        ids = [e["id"] for e in evs]
        assert ids == sorted(ids, reverse=True)


# ---------------------------------------------------------------------------
# changes_requested → create-review-fix（dedupe）→ review_fix 计划流程
# ---------------------------------------------------------------------------


class TestReviewFix:
    def _in_review_with_review_claim(self, factory, cli):
        ir = factory.make_in_review()
        tid = ir.task.task_id
        rreq = factory.find_request(tid, "review")
        rv = factory.claim("review", request_id=rreq["request_id"])
        assert rv["claimed"] is True
        return ir, rv

    def test_create_review_fix_dedupe(self, factory, cli):
        ir, rv = self._in_review_with_review_claim(factory, cli)
        src = ir.task.task_id
        # §5.24.5：create-review-fix 与 register-review 共享
        # core.require_review_identity，分支任务必须带 _in_review_with_review_claim
        # 那次真实 review 领取（rv）的 request_id/run_id
        f1 = cli("create-review-fix", {
            "source_task_id": src, "title": "修复登录态",
            "description": "修复描述", "priority": "high",
            "dedupe_key": "RK-100",
            "request_id": rv["request_id"], "run_id": rv["run_id"]})
        assert f1["ok"] is True
        assert f1["created"] is True
        fix_id = f1["fix_task_id"]
        assert len(f1["source_sha256"]) == 64
        # 远端任务带 dedupe marker
        remote = factory.kaneo.get_task(fix_id)
        assert remote is not None
        assert "<!-- codex-kaneo-review:RK-100 -->" in (remote["description"] or "")
        # 重复调用同 dedupe_key → 返回已存在（命中在守卫之前返回）；仍带同一领取身份
        f2 = cli("create-review-fix", {
            "source_task_id": src, "title": "修复登录态",
            "description": "修复描述", "priority": "high",
            "dedupe_key": "RK-100",
            "request_id": rv["request_id"], "run_id": rv["run_id"]})
        assert f2["created"] is False
        assert f2["fix_task_id"] == fix_id
        # fix task 登记：needs_plan + plan_kind=review_fix + review 字段
        detail = factory.get_task(fix_id)
        assert detail["status"] == "needs_plan"
        assert detail["plan_kind"] == "review_fix"
        assert detail["review_source_task_id"] == src
        assert detail["review_key"] == "RK-100"
        # v2 §5.22.2：修复任务停在 needs_plan，不自动建 plan 请求
        assert factory.find_request(fix_id, "plan") is None
        # register-review changes_requested 需要 fix_task_id + review_key
        report = write_review_report(factory.env.app_support, task_id=src,
                                     conclusion="changes_requested")
        reg = cli("register-review", {
            "task_id": src, "request_id": rv["request_id"],
            "run_id": rv["run_id"],
            "report_path": str(report.path),
            "conclusion": "changes_requested",
            "fix_task_id": fix_id, "review_key": "RK-100"})
        assert reg["ok"] is True
        cli("report", {"request_id": rv["request_id"], "run_id": rv["run_id"],
                       "outcome": "succeeded"})

    def test_review_fix_plan_and_dispatch(self, factory, cli):
        """fix task 走 review_fix 计划 → dispatch 校验标记 → running。"""
        ir, rv = self._in_review_with_review_claim(factory, cli)
        src = ir.task.task_id
        # 领取身份同上：rv 的真实 request_id/run_id（同一守卫消费点）
        f = cli("create-review-fix", {
            "source_task_id": src, "title": "修复问题",
            "description": "修复描述", "priority": "high",
            "dedupe_key": "RK-200",
            "request_id": rv["request_id"], "run_id": rv["run_id"]})
        fix_id = f["fix_task_id"]
        # create-review-fix 已把远端 task_context 落盘为 source 快照
        detail = factory.get_task(fix_id)
        assert detail["source_path"] and detail["source_sha256"]
        # 先写 repair 计划文件：reviewer 在 register-review 登记
        # repair_plan_path/sha，dispatch 校验与 fix 计划全匹配（§4.5-5）
        report = write_review_report(factory.env.app_support, task_id=src,
                                     conclusion="changes_requested")
        plan = write_plan_file(
            factory.env.plan_root, task_id=fix_id,
            project_id=detail["project_id"],
            project_name=detail["project_name"],
            repository=detail["repository"],
            source_sha256=detail["source_sha256"],
            requirements_sha256=detail["requirements_sha256"],
            plan_kind="review_fix", title="修复问题",
            extra_frontmatter={
                "review_report_path": str(report.path),
                "review_report_sha256": report.sha256,
                "review_source_task_id": src,
                "review_key": "RK-200",
            })
        cli("register-review", {
            "task_id": src, "request_id": rv["request_id"],
            "run_id": rv["run_id"],
            "report_path": str(report.path),
            "conclusion": "changes_requested",
            "fix_task_id": fix_id, "review_key": "RK-200",
            "repair_plan_path": str(plan.path)})
        cli("report", {"request_id": rv["request_id"], "run_id": rv["run_id"],
                       "outcome": "succeeded"})
        # fix task → publish 同一 review_fix 计划文件（内含
        # queue_plan+claim；publish-plan 成功即结束请求，不再 report）
        task_view = type("T", (), {
            "task_id": fix_id, "project_id": detail["project_id"],
            "project_name": detail["project_name"],
            "repository": detail["repository"],
            "source_sha256": detail["source_sha256"],
            "requirements_sha256": detail["requirements_sha256"],
            "title": "修复问题"})()
        pub = factory.publish(task_view, plan_kind="review_fix",
                              plan_file=plan)
        assert pub.resp["task_status"] == "ready"
        # dispatch claim：远端描述含标记（create-review-fix 已写入）→ 通过；
        # hold 任务需 confirm_plan enqueue
        factory.enqueue_dispatch(fix_id)
        dreq = factory.find_request(fix_id, "dispatch")
        d = factory.claim("dispatch", request_id=dreq["request_id"])
        assert d["claimed"] is True, f"review_fix 派发应成功：{d}"
        assert d["context"]["plan_kind"] == "review_fix"
        ev = ui_evidence(
            session_marker=d["context"]["session_marker"],
            repository=d["context"]["repository"],
            attachment_name=d["context"]["attachment_name"])
        cli("dispatch-prepare", {"task_id": fix_id, "run_id": d["run_id"],
                                 "evidence": ev})
        cli("dispatch-begin-send", {"task_id": fix_id, "run_id": d["run_id"]})
        ev2 = dict(ev, message_visible=True, session_title="fix",
                   session_id="sess-fix")
        r = cli("dispatch-confirm", {"task_id": fix_id, "run_id": d["run_id"],
                                     "evidence": ev2})
        assert r["status"] == "running"

    def test_register_review_blocked(self, factory, cli):
        ir, rv = self._in_review_with_review_claim(factory, cli)
        report = write_review_report(factory.env.app_support,
                                     task_id=ir.task.task_id,
                                     conclusion="blocked")
        cli("register-review", {"task_id": ir.task.task_id,
                                "request_id": rv["request_id"],
                                "run_id": rv["run_id"],
                                "report_path": str(report.path),
                                "conclusion": "blocked"})
        t = factory.get_task(ir.task.task_id)
        assert t["status"] == "needs_user"


# ---------------------------------------------------------------------------
# §5.4 sync-tasks 转换
# ---------------------------------------------------------------------------


class TestSyncTransitions:
    def _payload(self, task, project_name="StudyTool"):
        return {
            "executor": EXECUTORS["plan"],
            "projects": [{
                "project_id": task.project_id,
                "project_name": project_name,
                "repository": str(task.repository),
                "tasks": [{
                    "task_id": task.task_id,
                    "title": task.title,
                    "kaneo_status": task.kaneo_status,
                    "requirements_sha256": task.requirements_sha256,
                    "source_sha256": task.source_sha256,
                    "source_path": str(task.source_path),
                }],
            }],
        }
    def test_sync_new_task_lands_needs_plan(self, factory, cli):
        task = factory.sync_task()
        assert task.sync["upserted"] == 1
        # v2 §5.22：sync 只落 needs_plan，不自动建 plan 请求
        assert task.sync["created_requests"] == []
        assert factory.find_request(task.task_id, "plan") is None
        assert factory.get_task(task.task_id)["status"] == "needs_plan"
        kinds = _kinds(cli, task.task_id)
        assert "task_created" in kinds

    def test_sync_resync_no_duplicate(self, factory, cli):
        task = factory.sync_task()
        # 再 sync 同样内容 → 不重复建请求（v2：本就无自动请求）
        task.kaneo_status = "to-do"
        out = cli("sync-tasks", self._payload(task))
        assert out["upserted"] >= 1
        reqs = [r for r in cli("list-requests", {})["requests"]
                if r["task_id"] == task.task_id and r["kind"] == "plan"
                and r["status"] in ("queued", "claimed")]
        assert len(reqs) == 0

    def test_sync_requirements_change_replans(self, factory, cli):
        ready = factory.make_ready()
        task = ready.task
        task.kaneo_status = "to-do"
        task.requirements_sha256 = "f" * 64  # 需求指纹变了
        out = cli("sync-tasks", self._payload(task))
        t = factory.get_task(task.task_id)
        assert t["status"] == "needs_plan"
        # v2：需求变化只回 needs_plan，不自动建 plan 请求
        assert factory.find_request(task.task_id, "plan") is None
        assert "requirements_changed" in _kinds(cli, task.task_id)

    def test_sync_kaneo_done_closes_task(self, factory, cli):
        running = factory.make_running()
        task = running.task
        task.kaneo_status = "done"
        cli("sync-tasks", self._payload(task))
        assert factory.get_task(task.task_id)["status"] == "done"

    def test_sync_running_to_in_review(self, factory, cli):
        running = factory.make_running()
        task = running.task
        task.kaneo_status = "in-review"
        cli("sync-tasks", self._payload(task))
        assert factory.get_task(task.task_id)["status"] == "in_review"

    def test_sync_does_not_override_dispatching(self, factory, cli):
        """dispatching/prepared/submitting 不被 sync 覆盖（§5.4）。"""
        ready = factory.make_ready()
        task = ready.task
        factory.enqueue_dispatch(task.task_id)
        c = factory.claim("dispatch")
        assert c["claimed"] is True
        assert factory.get_task(task.task_id)["status"] == "dispatching"
        task.kaneo_status = "in-progress"
        task.requirements_sha256 = "f" * 64  # 需求也变，仍不得覆盖
        cli("sync-tasks", self._payload(task))
        t = factory.get_task(task.task_id)
        assert t["status"] == "dispatching"          # 状态不动
        assert t["kaneo_status"] == "in-progress"    # 仅观察值更新


# ---------------------------------------------------------------------------
# §5.9 advance-task 远端冲突 / §5.11 recheck
# ---------------------------------------------------------------------------


class TestAdvanceAndRecheck:
    def test_advance_remote_conflict(self, factory, cli):
        """远端实际状态与 expected_current 不符 → remote_conflict + needs_user。"""
        running = factory.make_running()
        tid = running.task.task_id
        mreq = factory.find_request(tid, "monitor")
        m = factory.claim("monitor", request_id=mreq["request_id"])
        # §5.9：advance target=in-review 先过 core.require_committed_review
        # 才到远端比较（cmd_remote.cmd_advance_task）——make_running 的任务已被
        # start-plans 登记 workspace_version=1 分支工作区，按本文件 e2e 与
        # conftest.make_in_review 既有模式先完成真实 delivery-commit 再篡改
        # 远端，保留原篡改与 remote_conflict 断言不变。
        delivery = factory.get_task(tid).get("delivery") or {}
        (Path(delivery["worktree_path"]) / "lifecycle-conflict.py").write_text(
            "VALUE = 1\n", encoding="utf-8")
        committed = cli("delivery-commit", {
            "task_id": tid, "request_id": m["request_id"], "run_id": m["run_id"],
            "expected_execution_run_id": running.dispatch_run_id})
        assert committed["outcome"] in ("committed", "already_committed"), committed
        factory.kaneo.set_status(tid, "done")  # 远端被人为改
        out = cli("advance-task", {
            "task_id": tid, "run_id": m["run_id"], "target": "in-review",
            "expected_current": "in-progress"}, expect=1)
        assert out["code"] == "remote_conflict"
        t = factory.get_task(tid)
        assert t["status"] == "needs_user"
        assert t["failure_code"] == "remote_conflict"
        assert t["kaneo_status"] == "done"  # 观察值已记录

    def test_recheck_happy(self, factory, cli):
        ready = factory.make_ready()
        out = cli("recheck", {"task_id": ready.task.task_id})
        assert out["task_id"] == ready.task.task_id
        assert isinstance(out["checks"], list) and out["checks"]
        for c in out["checks"]:
            assert set(c) >= {"name", "ok", "detail"}
        assert out["ok"] is True
        assert out.get("kaneo_status") in ("to-do", "todo", None)
        t = factory.get_task(ready.task.task_id)
        assert t["kaneo_checked_at"]

    def test_recheck_detects_tamper(self, factory, cli):
        ready = factory.make_ready()
        with open(ready.plan.path, "a", encoding="utf-8") as f:
            f.write("tamper")
        out = cli("recheck", {"task_id": ready.task.task_id})
        assert out["ok"] is False
        assert any(c["ok"] is False for c in out["checks"])


# ---------------------------------------------------------------------------
# unittest discover 收集入口（本机无 pytest；见 tests/unittest_compat.py）


def load_tests(loader, standard_tests, pattern):
    import sys

    try:
        import unittest_compat
    except ImportError:
        from tests import unittest_compat
    return unittest_compat.collect_module(sys.modules[__name__])
