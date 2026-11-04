"""Tests for project and repository mappings (T1 protocol §5.26)."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from helpers import PROJECTS, WORKSPACE_ID, KanpilotTestCase
from kanpilot import auto_monitor, bridge, constants, db, project_config, util


class TestProjectMappings(KanpilotTestCase):

    def test_unset_mappings_have_no_implicit_projects(self):
        conn = self._open_db()
        conn.execute("DELETE FROM settings WHERE key='project_mappings'")
        conn.commit()
        conn.close()
        res = self.call("get-project-mappings")
        self.assertEqual(res["revision"], 0)
        self.assertEqual(res["workspace_id"], WORKSPACE_ID)
        self.assertEqual(res["items"], [])
        conn = self._open_db()
        self.assertIsNone(db.get_setting(conn, "project_mappings"))
        conn.close()

    def test_refresh_remote_includes_unmapped_current_projects(self):
        """refresh_remote=True 时拉取远端项目列表，未映射项目以 remote_only / unmapped 展现。"""
        self.fake.add_project("startool0000000000000001", "StarTool")
        res = self.call("get-project-mappings", {"refresh_remote": True})
        self.assertEqual(res["remote"]["status"], "ok")
        self.assertTrue(res["remote"]["fetched_at"])

        items_by_id = {item["project_id"]: item for item in res["items"]}
        self.assertIn("startool0000000000000001", items_by_id)
        star = items_by_id["startool0000000000000001"]
        self.assertEqual(star["project_name"], "StarTool")
        self.assertEqual(star["origin"], "remote_only")
        self.assertEqual(star["mapping_state"], "unmapped")
        self.assertIsNone(star["repository"])
        self.assertFalse(star["enabled"])
        self.assertEqual(star["blocked_reason"], "尚未选择仓库")

    def test_deleted_project_stays_hidden_without_deleting_local_state(self):
        pid = PROJECTS["Feedback"]
        self.make_repo("Feedback")
        task = self.fake.add_task("deleted-project-history", pid)
        self.sync_task(task, project_name="Feedback")
        conn = self._open_db()
        saved = db.get_setting(conn, "project_mappings")
        saved["items"][pid].update(engine_mode="shadow", automation={"enabled": False})
        db.set_setting(conn, "project_mappings", saved)
        conn.commit()
        effective = project_config.get_effective_mapping(conn, pid)
        history = {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]
                   for table in ("tasks", "requests", "events")}
        conn.close()

        self.current_mappings(refresh_remote=True)
        self.fake.projects = [p for p in self.fake.projects if p["id"] != pid]
        fresh = self.current_mappings(refresh_remote=True)
        local = self.current_mappings()
        self.assertNotIn(pid, [p["project_id"] for p in fresh["items"]])
        self.assertEqual(local["items"], fresh["items"])
        self.assertEqual(local["remote"]["fetched_at"], fresh["remote"]["fetched_at"])
        conn = self._open_db()
        self.assertEqual(db.get_setting(conn, "project_mappings"), saved)
        self.assertEqual(project_config.get_effective_mapping(conn, pid), effective)
        for table, rows in history.items():
            self.assertEqual([tuple(row) for row in conn.execute(f"SELECT * FROM {table}")], rows)
        conn.close()

        self.fake.add_project(pid, "Feedback Restored")
        restored = self.current_mappings(refresh_remote=True)
        item = next(p for p in restored["items"] if p["project_id"] == pid)
        self.assertEqual(item["project_name"], "Feedback Restored")
        self.assertEqual(item["repository"], effective["repository"])
        self.assertEqual(item["engine_mode"], "live")

    def test_unmapping_and_local_reload_do_not_restore_deleted_projects(self):
        deleted = PROJECTS["Feedback"]
        current = PROJECTS["Music"]
        self.fake.projects = [p for p in self.fake.projects if p["id"] != deleted]
        self.current_mappings(refresh_remote=True)
        self.call("set-project-mapping", {
            "project_id": current, "repository": None, "enabled": False, "expected_revision": 0,
        })
        local = self.current_mappings()
        items = {p["project_id"]: p for p in local["items"]}
        self.assertNotIn(deleted, items)
        self.assertEqual(items[current]["mapping_state"], "unmapped")
        self.assertEqual(local["revision"], 1)

    def test_empty_success_is_authoritative_even_after_failure(self):
        self.fake.projects = []
        fresh = self.current_mappings(refresh_remote=True)
        self.assertEqual(fresh["remote"]["status"], "ok")
        self.assertEqual(fresh["items"], [])
        self.fake.fail_next = bridge.BridgeError("fixture offline")
        failed = self.current_mappings(refresh_remote=True)
        self.assertEqual(failed["remote"]["status"], "error")
        self.assertEqual(failed["remote"]["fetched_at"], fresh["remote"]["fetched_at"])
        self.assertEqual(failed["items"], [])
        self.assertEqual(self.current_mappings()["items"], [])

    def test_first_failed_refresh_preserves_saved_list(self):
        before = self.current_mappings()
        self.fake.fail_next = bridge.BridgeError("fixture unauthorized")
        failed = self.current_mappings(refresh_remote=True)
        self.assertEqual(failed["remote"]["status"], "error")
        self.assertIn("fixture unauthorized", failed["remote"]["error"])
        self.assertEqual(failed["items"], before["items"])
        self.assertIsNone(failed["remote"]["fetched_at"])
        conn = self._open_db()
        self.assertIsNone(db.get_setting(conn, "project_catalog"))
        conn.close()

    def test_invalid_or_partial_response_keeps_last_successful_catalog(self):
        self.fake.projects = [{"id": PROJECTS["Music"], "name": "Renamed Music"}]
        fresh = self.current_mappings(refresh_remote=True)
        conn = self._open_db()
        catalog = db.get_setting(conn, "project_catalog")
        conn.close()
        row = {"id": PROJECTS["Music"], "name": "Music"}
        for response in (None, {}, {"error": "forbidden"}, {"data": {}}, [None],
                         [{"name": "missing id"}], [{"id": 5, "name": "bad id"}],
                         [{"id": "", "name": "empty id"}], [{"id": "p"}],
                         [row, row], [row, {"id": "p", "name": None}],
                         {"data": [row], "pagination": {"totalPages": 2}},
                         {"projects": [row], "hasMore": True}):
            with self.subTest(response=response), patch.object(self.fake, "request", return_value=response):
                failed = self.current_mappings(refresh_remote=True)
                self.assertEqual(failed["remote"]["status"], "error")
                self.assertEqual(failed["items"], fresh["items"])
                self.assertEqual(failed["remote"]["fetched_at"], fresh["remote"]["fetched_at"])
                conn = self._open_db()
                self.assertEqual(db.get_setting(conn, "project_catalog"), catalog)
                conn.close()

    def test_complete_list_shapes_and_archived_filter(self):
        rows = [{"id": "current", "name": "Current"},
                {"id": "archived", "name": "Archived", "archivedAt": "2026-11-04"}]
        for response in (rows, {"data": rows}, {"projects": rows}):
            with self.subTest(response=response), patch.object(self.fake, "request", return_value=response):
                result = self.current_mappings(refresh_remote=True)
                self.assertEqual(result["remote"]["status"], "ok")
                self.assertEqual([p["project_id"] for p in result["items"]], ["current"])
                self.assertEqual(result["items"][0]["origin"], "remote_only")

    def test_catalog_is_not_reused_for_another_workspace(self):
        self.current_mappings(refresh_remote=True)
        conn = self._open_db()
        conn.execute("DELETE FROM settings WHERE key='project_mappings'")
        conn.commit()
        conn.close()
        self.call("set-config", {"workspace_id": "other-workspace"})
        local = self.current_mappings()
        self.assertEqual(local["workspace_id"], "other-workspace")
        self.assertEqual(local["items"], [])
        self.assertIsNone(local["remote"]["fetched_at"])
        self.fake.projects = [{"id": "other-project", "name": "Other"}]
        refreshed = self.current_mappings(refresh_remote=True)
        self.assertEqual([p["project_id"] for p in refreshed["items"]], ["other-project"])

    def test_workspace_change_during_fetch_does_not_overwrite_catalog(self):
        self.current_mappings(refresh_remote=True)
        conn = self._open_db()
        catalog = db.get_setting(conn, "project_catalog")
        conn.execute("DELETE FROM settings WHERE key='project_mappings'")
        conn.commit()
        conn.close()

        def delayed_response(*args, **kwargs):
            self.call("set-config", {"workspace_id": "changed-during-fetch"})
            return []

        with patch.object(self.fake, "request", side_effect=delayed_response):
            failed = self.current_mappings(refresh_remote=True)
        self.assertEqual(failed["remote"]["status"], "error")
        self.assertIn("工作区已变更", failed["remote"]["error"])
        conn = self._open_db()
        self.assertEqual(db.get_setting(conn, "project_catalog"), catalog)
        conn.close()
        self.assertEqual(self.current_mappings()["items"], [])

    def test_hidden_project_normalizes_live_and_keeps_verification_gate(self):
        pid = PROJECTS["Feedback"]
        self.fake.projects = []
        self.current_mappings(refresh_remote=True)
        for mode in ("legacy", "shadow", "live"):
            conn = self._open_db()
            saved = db.get_setting(conn, "project_mappings")
            saved["items"][pid]["engine_mode"] = mode
            db.set_setting(conn, "project_mappings", saved)
            conn.commit()
            conn.close()
            self.assertEqual(self.current_mappings()["items"], [])
            item = next(p for p in self.call("get-project-mappings")["items"] if p["project_id"] == pid)
            self.assertEqual(item["engine_mode"], "live")
            self.assertEqual(item["effective_engine_mode"], "live")
            self.assertEqual(auto_monitor._engine_mode(SimpleNamespace(call=self.call), {"project_id": pid}), "live")
            conn = self._open_db()
            self.assertEqual(db.get_setting(conn, "project_mappings"), saved)
            conn.close()

        calls = []
        def monitor_call(command, payload):
            calls.append((command, payload))
            if command == "get-project-mappings":
                return self.call(command, payload)
            self.assertEqual(command, "engine-verdict")
            return {"verification_ok": False}

        controller = SimpleNamespace(call=monitor_call, claim={"context": {}, "request_id": "fixture-monitor"},
                                     deadline=60, clock=lambda: 0, identity=lambda: {},
                                     heartbeat=Mock(), report=Mock())
        task = {"task_id": "fixture-task", "project_id": pid, "run_id": "fixture-run", "plan_sha256": "fixture-plan"}
        with patch.object(auto_monitor, "_verification") as legacy_verification, \
                patch.object(auto_monitor, "_finish_complete") as finish:
            auto_monitor._complete(controller, task, {"backend": "pi_desktop"}, {}, [])
        self.assertEqual([command for command, _ in calls], ["get-project-mappings", "engine-verdict"])
        legacy_verification.assert_not_called()
        finish.assert_not_called()
        self.assertEqual(controller.report.call_args.args, ("needs_user",))
        self.assertEqual(controller.report.call_args.kwargs["failure_code"], "verification_unconfirmed")

    def test_set_project_mapping_and_idempotency(self):
        """配置有效仓库路径，首次保存持久化完整映射，重复保存幂等返回。"""
        star_repo = self.root / "StarToolRepo"
        star_repo.mkdir(parents=True, exist_ok=True)
        self.fake.add_project("startool0000000000000001", "StarTool")

        # 首次保存 expected_revision=0
        res1 = self.call("set-project-mapping", {
            "project_id": "startool0000000000000001",
            "repository": str(star_repo),
            "enabled": True,
            "expected_revision": 0,
        })
        self.assertEqual(res1["revision"], 1)
        self.assertFalse(res1["deduplicated"])
        self.assertEqual(res1["item"]["repository"], str(star_repo.resolve()))
        self.assertEqual(res1["item"]["mapping_state"], "available")
        self.assertEqual(res1["item"]["repository_kind"], "directory")

        # 重复保存同一配置：幂等返回 deduplicated: True, revision 仍为 1
        res2 = self.call("set-project-mapping", {
            "project_id": "startool0000000000000001",
            "repository": str(star_repo),
            "enabled": True,
            "expected_revision": 1,
        })
        self.assertEqual(res2["revision"], 1)
        self.assertTrue(res2["deduplicated"])

        # 检查 settings.project_mappings 确实写入
        conn = self._open_db()
        stored = db.get_setting(conn, "project_mappings")
        conn.close()
        self.assertIsNotNone(stored)
        self.assertEqual(stored["revision"], 1)
        self.assertIn("startool0000000000000001", stored["items"])

    def test_conflict_on_stale_revision(self):
        """修改配置时如果 expected_revision 不符且非幂等，返回 conflict 错误。"""
        star_repo = self.root / "StarToolRepo"
        star_repo.mkdir(parents=True, exist_ok=True)
        self.fake.add_project("startool0000000000000001", "StarTool")

        self.call("set-project-mapping", {
            "project_id": "startool0000000000000001",
            "repository": str(star_repo),
            "enabled": True,
            "expected_revision": 0,
        })

        other_repo = self.root / "OtherRepo"
        other_repo.mkdir(parents=True, exist_ok=True)

        err = self.call_err("set-project-mapping", {
            "project_id": "startool0000000000000001",
            "repository": str(other_repo),
            "enabled": True,
            "expected_revision": 0,  # 已经变成 1 了
        })
        self.assertEqual(err.code, "conflict")

    def test_git_subdirectory_normalization(self):
        """选择 Git 仓库的子目录时，自动规范化至 Git 仓库根。"""
        git_repo = self.root / "MyGitProject"
        git_repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", str(git_repo)], capture_output=True, check=True)
        sub_dir = git_repo / "sub" / "pkg"
        sub_dir.mkdir(parents=True, exist_ok=True)

        res = self.call("set-project-mapping", {
            "project_id": "startool0000000000000001",
            "repository": str(sub_dir),
            "enabled": True,
            "expected_revision": 0,
        })
        self.assertEqual(res["item"]["repository"], str(git_repo.resolve()))
        self.assertEqual(res["item"]["repository_kind"], "git")
        self.assertEqual(res["item"]["mapping_state"], "available")

    def test_disable_and_unmap_lifecycle(self):
        """停用和解除映射明确持久化，绝不回退至旧硬编码常量。"""
        feedback_pid = PROJECTS["Feedback"]
        feedback_repo = self.make_repo("Feedback")

        # 停用 Feedback
        res_dis = self.call("set-project-mapping", {
            "project_id": feedback_pid,
            "repository": str(feedback_repo),
            "enabled": False,
            "expected_revision": 0,
        })
        self.assertEqual(res_dis["item"]["mapping_state"], "disabled")
        self.assertEqual(res_dis["item"]["blocked_reason"], "已停用新规划")

        # 解除映射 (repository: null, enabled: false)
        res_unmap = self.call("set-project-mapping", {
            "project_id": feedback_pid,
            "repository": None,
            "enabled": False,
            "expected_revision": 1,
        })
        self.assertEqual(res_unmap["item"]["mapping_state"], "unmapped")
        self.assertIsNone(res_unmap["item"]["repository"])

        # 再次获取 mappings：Feedback 仍然保持 unmapped，不会从 PROJECTS 恢复
        mappings = self.call("get-project-mappings")
        item = next(it for it in mappings["items"] if it["project_id"] == feedback_pid)
        self.assertEqual(item["mapping_state"], "unmapped")
        self.assertIsNone(item["repository"])

    def test_startool_planning_end_to_end(self):
        """StarTool 完整规划链：从未映射阻塞 -> 配置映射 -> fetch-todos 可选 -> prepare -> start-plans -> fake runner -> publish -> dispatch guard 通行。"""
        pid = "startool0000000000000001"
        self.fake.add_project(pid, "StarTool")
        task = self.fake.add_task("taskstar0000000000001", pid, status="to-do", title="StarTool feature")

        # 1. 未配置时 fetch-todos：不可选，标明未映射
        todos = self.call("fetch-todos")
        todo_item = next(it for it in todos["items"] if it["task_id"] == task["id"])
        self.assertFalse(todo_item["selectable"])
        self.assertEqual(todo_item["mapping_state"], "unmapped")
        self.assertEqual(todo_item["blocked_reason"], "尚未选择仓库")

        # 2. 配置仓库
        star_repo = self.root / "StarToolWorkspace"
        star_repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", str(star_repo)], capture_output=True, check=True)
        for args in (("config", "user.name", "Mapping Fixture"),
                     ("config", "user.email", "fixture@example.invalid"),
                     ("commit", "--allow-empty", "-m", "isolated mapping baseline")):
            subprocess.run(["git", "-C", str(star_repo), *args], capture_output=True, check=True)

        self.call("set-project-mapping", {
            "project_id": pid,
            "repository": str(star_repo),
            "enabled": True,
            "expected_revision": 0,
        })

        # 3. 再次 fetch-todos：变为可选
        todos2 = self.call("fetch-todos")
        todo_item2 = next(it for it in todos2["items"] if it["task_id"] == task["id"])
        self.assertTrue(todo_item2["selectable"])
        self.assertEqual(todo_item2["mapping_state"], "available")
        self.assertEqual(todo_item2["repository"], str(star_repo.resolve()))

        # 4. prepare-plan-input
        prepared = self.call("prepare-plan-input", {"task_id": task["id"]})
        self.assertTrue(prepared["can_start"])
        self.assertTrue(prepared["input_sha256"])

        # 5. start-plans
        started = self.call("start-plans", {
            "items": [{
                "input_id": prepared["input_id"],
                "input_sha256": prepared["input_sha256"],
            }]
        })
        self.assertEqual(len(started["results"]), 1)
        self.assertFalse(started["results"][0]["deduplicated"])

        # 6. Fake runner 运行
        os.environ["KANPILOT_CODEX_FAKE"] = "ready"
        self.addCleanup(os.environ.pop, "KANPILOT_CODEX_FAKE", None)
        from kanpilot import planner_runner
        rc = planner_runner.run_loop(self.db_path, None, once=True)
        self.assertEqual(rc, 0)

        # 7. 检查任务状态与计划文件路径
        view = self.task(task["id"])
        self.assertEqual(view["status"], "ready")
        self.assertTrue(view["plan_path"])
        # 验证落在 plan_root/projects/<project_id>/ 目录下
        plan_p = Path(view["plan_path"]).resolve()
        expected_dir = (self.plan_root / "projects" / pid).resolve()
        self.assertTrue(str(plan_p).startswith(str(expected_dir)))

        enq = self.call("enqueue", {
            "task_id": task["id"],
            "confirm_plan": True,
            "expected_plan_sha256": view["plan_sha256"],
        })
        self.assertIn("request", enq)
        self.assertEqual(enq["request"]["kind"], "dispatch")
    def test_mapping_change_after_prepare_triggers_conflict(self):
        """准备材料后修改目标项目仓库，start-plans 事务内检测冲突并拒绝。"""
        pid = "startool0000000000000001"
        self.fake.add_project(pid, "StarTool")
        task = self.fake.add_task("taskstar0000000000002", pid, status="to-do")

        repo1 = self.root / "Repo1"
        repo1.mkdir(parents=True, exist_ok=True)
        self.call("set-project-mapping", {
            "project_id": pid,
            "repository": str(repo1),
            "enabled": True,
            "expected_revision": 0,
        })

        prepared = self.call("prepare-plan-input", {"task_id": task["id"]})

        # 修改为 Repo2
        repo2 = self.root / "Repo2"
        repo2.mkdir(parents=True, exist_ok=True)
        self.call("set-project-mapping", {
            "project_id": pid,
            "repository": str(repo2),
            "enabled": True,
            "expected_revision": 1,
        })

        # start-plans 应报错材料变更冲突
        started = self.call("start-plans", {
            "items": [{
                "input_id": prepared["input_id"],
                "input_sha256": prepared["input_sha256"],
            }]
        })
        self.assertEqual(started["results"][0]["code"], "conflict")

    def current_mappings(self, *, refresh_remote=False):
        return self.call("get-project-mappings", {"current_only": True, "refresh_remote": refresh_remote})

    def _open_db(self):
        import sqlite3
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def task(self, task_id):
        return self.call("get-task", {"task_id": task_id})["task"]


if __name__ == "__main__":
    unittest.main()
