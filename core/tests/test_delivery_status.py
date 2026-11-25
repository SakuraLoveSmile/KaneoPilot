"""Read-once delivery status for the desktop「交付」pane.

A fake PR reader stands in for GitHub: no network, and the SQLite file is compared
before/after to prove the command only reads.
"""
import sqlite3
import subprocess
from unittest.mock import patch

from helpers import KanpilotTestCase
from kanpilot import cli, core, db, delivery_status, gh_client, project_config, util

TASK = "taskdl01"
PROJECT = "fixture-project-feedback"
HEAD = "a" * 40


class DeliveryStatusTest(KanpilotTestCase):
    def setUp(self):
        super().setUp()
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.repo = self.make_repo("Feedback")
        subprocess.run(["git", "-C", str(self.repo), "remote", "add", "origin",
                        "https://github.com/owner/repo"], check=True, capture_output=True)
        self.policy()

    # ----------------------------------------------------------- fixtures

    def policy(self, **automation):
        settings = db.get_setting(self.conn, project_config.SETTINGS_KEY)
        settings["items"][PROJECT]["automation"] = dict(
            {"enabled": False, "auto_merge_pr": True, "policy_revision": 1}, **automation)
        db.set_setting(self.conn, project_config.SETTINGS_KEY, settings)

    def add_task(self, **delivery):
        now = util.now_iso()
        values = {"task_id": TASK, "project_id": PROJECT, "project_name": "Feedback",
                  "title": "交付页读一次状态", "repository": str(self.repo),
                  "stage": "delivery", "status": "in_review", "created_at": now,
                  "updated_at": now, "extra_json": util.jdump({"delivery": delivery})}
        self.conn.execute("INSERT INTO tasks (" + ",".join(values) + ") VALUES ("
                          + ",".join("?" for _ in values) + ")", tuple(values.values()))
        db.add_event(self.conn, now, "delivery_published", TASK,
                     detail={"pr_number": delivery.get("pr_number")})
        return db.require_task(self.conn, TASK)

    def delivery(self, **changes):
        value = {"mode": "pr", "pr_number": 7,
                 "pr_url": "https://github.com/owner/repo/pull/7", "pr_state": "OPEN",
                 "branch": "feat/kp-dl01", "base_branch": "main", "head_sha": HEAD,
                 "merged_at": None, "last_sync_at": "2026-11-23T08:00:00+00:00",
                 "next_sync_at": "2026-11-23T08:05:00+00:00",
                 "auto_merge": {"state": "waiting_checks", "reason": "检查仍在运行",
                                "head_sha": HEAD,
                                "updated_at": "2026-11-23T08:00:00+00:00"}}
        value.update(changes)
        return value

    def pr(self, **changes):
        value = {"state": "OPEN", "url": "https://github.com/owner/repo/pull/7",
                 "number": 7, "headRefOid": HEAD, "baseRefName": "main",
                 "headRefName": "feat/kp-dl01", "isCrossRepository": False,
                 "mergedAt": None, "isDraft": False, "mergeable": "MERGEABLE",
                 "mergeStateStatus": "CLEAN", "reviewDecision": "",
                 "statusCheckRollup": []}
        value.update(changes)
        return value

    def status(self, reader):
        ctx = cli.Context(self.db_path)
        try:
            return delivery_status.cmd_get_delivery_status(
                ctx, {"task_id": TASK}, reader=reader)
        finally:
            ctx.close()

    def unused_reader(self):
        def read(*args, **kwargs):
            raise AssertionError("本次不应读取 GitHub")
        return read

    def snapshot(self):
        """Raw file view: another connection must see the very same rows afterwards."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            return (conn.execute("SELECT extra_json FROM tasks WHERE task_id=?",
                                 (TASK,)).fetchone()[0],
                    conn.execute("SELECT count(*) FROM events").fetchone()[0],
                    conn.execute("SELECT count(*) FROM runtime_operations").fetchone()[0])
        finally:
            conn.close()

    # -------------------------------------------------------------- tests

    def test_live_read_groups_checks_and_quotes_the_merge_gate(self):
        self.add_task(**self.delivery())
        rollup = [
            {"__typename": "CheckRun", "name": "unit", "workflowName": "CI",
             "status": "IN_PROGRESS", "conclusion": ""},
            {"__typename": "CheckRun", "name": "lint", "workflowName": "CI",
             "status": "QUEUED", "conclusion": ""},
            {"__typename": "CheckRun", "name": "contract", "workflowName": "Docs",
             "status": "COMPLETED", "conclusion": "FAILURE",
             "detailsUrl": "https://github.com/owner/repo/actions/runs/42/job/7",
             "startedAt": "2026-11-23T07:59:00Z",
             "completedAt": "2026-11-23T08:00:00Z"},
        ]
        result = self.status(lambda repo, number, **kw: self.pr(statusCheckRollup=rollup))
        self.assertTrue(result["live"])
        self.assertIsNone(result["error"])
        self.assertEqual(result["task_id"], TASK)
        self.assertIsNotNone(util.parse_ts(result["observed_at"]))
        self.assertEqual(result["gate"]["state"], "blocked")
        self.assertFalse(result["gate"]["ready"])
        self.assertTrue(result["gate"]["reason"].startswith("检查未通过："))
        self.assertEqual([item["name"] for item in result["checks"]],
                         ["contract", "lint", "unit"])
        self.assertEqual([item["state"] for item in result["checks"]],
                         ["failed", "running", "running"])
        self.assertEqual(result["checks_summary"],
                         {"total": 3, "passed": 0, "failed": 1, "running": 2, "unknown": 0})
        self.assertEqual(result["checks"][0]["conclusion"], "FAILURE")
        self.assertEqual(result["checks"][0]["url"],
                         "https://github.com/owner/repo/actions/runs/42/job/7")
        self.assertEqual(result["checks"][0]["completed_at"], "2026-11-23T08:00:00Z")
        self.assertIsNone(result["checks"][1]["conclusion"])
        self.assertEqual(result["pr"]["state"], "OPEN")
        self.assertEqual(result["pr"]["head_sha"], HEAD)
        self.assertTrue(result["pr"]["head_matches_delivery"])
        self.assertEqual(result["pr"]["url"], "https://github.com/owner/repo/pull/7")
        self.assertEqual(result["delivery"]["pr_number"], 7)
        self.assertEqual(result["delivery"]["pr_url"],
                         "https://github.com/owner/repo/pull/7")
        self.assertTrue(result["delivery"]["auto_merge_enabled"])
        self.assertTrue(result["delivery"]["notice_matches_head"])
        self.assertEqual(result["delivery"]["notice"]["state"], "waiting_checks")

    def test_same_name_older_round_folds_into_superseded_runs(self):
        self.add_task(**self.delivery())
        rollup = [
            {"__typename": "CheckRun", "name": "test", "workflowName": "CI",
             "status": "COMPLETED", "conclusion": "FAILURE",
             "startedAt": "2026-11-23T07:50:00Z",
             "completedAt": "2026-11-23T07:52:00Z"},
            {"__typename": "CheckRun", "name": "test", "workflowName": "CI",
             "status": "IN_PROGRESS", "conclusion": "",
             "startedAt": "2026-11-23T07:58:00Z"},
        ]
        result = self.status(lambda repo, number, **kw: self.pr(statusCheckRollup=rollup))
        self.assertEqual(len(result["checks"]), 1)
        check = result["checks"][0]
        self.assertEqual(check["state"], "running")
        self.assertEqual(check["superseded_runs"], 1)
        self.assertEqual(check["started_at"], "2026-11-23T07:58:00Z")
        self.assertIsNone(check["completed_at"])
        self.assertEqual(result["checks_summary"],
                         {"total": 1, "passed": 0, "failed": 0, "running": 1, "unknown": 0})
        # gate 与 checks 共用 pr_auto_merge.check_rounds：被覆盖的旧 FAILURE 不再挡住
        # 合并门，页面也不会出现「列表说在跑、门说失败」的矛盾。
        self.assertEqual(result["gate"]["state"], "waiting_checks")
        self.assertEqual(result["gate"]["reason"], "检查仍在运行：CI/test，等待结果")
        self.assertFalse(result["gate"]["ready"])

    def test_running_round_wins_over_a_later_completion_of_the_old_round(self):
        # GitHub 对未完成检查返回 0001-01-01T00:00:00Z；被覆盖那轮的完成时间可能比
        # 当前轮的开始时间更晚，所以「正在跑的那轮」就是当前轮。
        self.add_task(**self.delivery())
        rollup = [
            {"__typename": "CheckRun", "name": "test", "workflowName": "CI",
             "status": "COMPLETED", "conclusion": "SUCCESS",
             "completedAt": "2026-11-23T08:11:28Z"},
            {"__typename": "CheckRun", "name": "test", "workflowName": "CI",
             "status": "IN_PROGRESS", "conclusion": "",
             "startedAt": "2026-11-23T08:08:02Z",
             "completedAt": "0001-01-01T00:00:00Z"},
        ]
        result = self.status(lambda repo, number, **kw: self.pr(statusCheckRollup=rollup))
        self.assertEqual(len(result["checks"]), 1)
        check = result["checks"][0]
        self.assertEqual(check["state"], "running")
        self.assertEqual(check["superseded_runs"], 1)
        self.assertIsNone(check["completed_at"])
        self.assertEqual(check["started_at"], "2026-11-23T08:08:02Z")
        self.assertEqual(result["gate"]["state"], "waiting_checks")

    def test_single_completed_round_keeps_its_completion_time(self):
        self.add_task(**self.delivery())
        rollup = [{"__typename": "CheckRun", "name": "test", "workflowName": "CI",
                   "status": "COMPLETED", "conclusion": "SUCCESS",
                   "startedAt": "2026-11-23T08:00:00Z",
                   "completedAt": "2026-11-23T08:02:00Z"}]
        result = self.status(lambda repo, number, **kw: self.pr(statusCheckRollup=rollup))
        check = result["checks"][0]
        self.assertEqual(check["state"], "passed")
        self.assertEqual(check["completed_at"], "2026-11-23T08:02:00Z")
        self.assertEqual(check["started_at"], "2026-11-23T08:00:00Z")
        self.assertEqual(check["superseded_runs"], 0)

    def test_command_path_uses_no_write_helper(self):
        self.add_task(**self.delivery())
        with patch.object(db, "write_txn",
                          side_effect=AssertionError("不应开启写事务")), \
             patch.object(db, "add_event",
                          side_effect=AssertionError("不应写事件")), \
             patch.object(core, "update_delivery",
                          side_effect=AssertionError("不应改写交付记录")):
            result = self.status(lambda repo, number, **kw: self.pr(
                statusCheckRollup=[{"__typename": "CheckRun", "name": "test",
                                    "workflowName": "CI", "status": "COMPLETED",
                                    "conclusion": "SUCCESS"}]))
        self.assertTrue(result["live"])
        self.assertEqual(result["delivery"]["pr_number"], 7)

    def test_non_pr_delivery_reads_no_pr(self):
        self.add_task(**self.delivery(mode="manual", pr_number=None, pr_url=None,
                                      auto_merge=None))
        result = self.status(self.unused_reader())
        self.assertFalse(result["live"])
        self.assertIsNone(result["error"])
        for key in ("pr", "gate", "checks", "checks_summary"):
            self.assertIsNone(result[key])
        self.assertEqual(result["delivery"]["mode"], "manual")
        self.assertIsNone(result["delivery"]["pr_number"])
        self.assertIsNone(result["delivery"]["pr_url"])
        self.assertEqual(result["delivery"]["branch"], "feat/kp-dl01")
        self.assertIsNone(result["delivery"]["notice"])
        self.assertFalse(result["delivery"]["notice_matches_head"])

    def test_pr_delivery_without_a_number_reads_no_pr(self):
        self.add_task(**self.delivery(pr_number=None, pr_url=None))
        result = self.status(self.unused_reader())
        self.assertFalse(result["live"])
        self.assertIsNone(result["error"])
        self.assertIsNone(result["checks"])
        self.assertEqual(result["delivery"]["mode"], "pr")
        self.assertIsNone(result["delivery"]["pr_number"])

    def test_merged_delivery_reads_no_pr(self):
        self.add_task(**self.delivery(pr_state="MERGED",
                                      merged_at="2026-11-23T08:10:00+00:00"))
        result = self.status(self.unused_reader())
        self.assertFalse(result["live"])
        self.assertIsNone(result["error"])
        self.assertIsNone(result["pr"])
        self.assertEqual(result["delivery"]["merged_at"], "2026-11-23T08:10:00+00:00")
        self.assertEqual(result["delivery"]["pr_state"], "MERGED")

    def test_recorded_merge_time_alone_stops_the_read(self):
        self.add_task(**self.delivery(merged_at="2026-11-23T08:10:00+00:00"))
        result = self.status(self.unused_reader())
        self.assertFalse(result["live"])
        self.assertIsNone(result["error"])
        self.assertIsNone(result["gate"])

    def test_closed_delivery_reads_no_pr(self):
        self.add_task(**self.delivery(pr_state="CLOSED"))
        result = self.status(self.unused_reader())
        self.assertFalse(result["live"])
        self.assertIsNone(result["error"])
        self.assertIsNone(result["checks_summary"])
        self.assertEqual(result["delivery"]["pr_state"], "CLOSED")

    def test_github_read_failure_keeps_the_stored_notice(self):
        delivery = self.delivery()
        self.add_task(**delivery)

        def fail(repo, number, **kwargs):
            raise gh_client.GhError("transient",
                                    "GitHub 读取超时：https://api.github.com/repos/o/r")

        result = self.status(fail)
        self.assertFalse(result["live"])
        self.assertEqual(result["error"]["code"], "transient")
        self.assertTrue(result["error"]["message"])
        self.assertNotIn("https://", result["error"]["message"])
        self.assertIsNone(result["pr"])
        self.assertIsNone(result["gate"])
        self.assertEqual(result["delivery"]["notice"], delivery["auto_merge"])
        self.assertTrue(result["delivery"]["notice_matches_head"])

    def test_unverifiable_origin_is_reported_as_a_git_error(self):
        plain = self.make_repo("Library")  # 本地仓库，没有 origin 远端
        self.add_task(**self.delivery())
        with db.write_txn(self.conn):
            self.conn.execute("UPDATE tasks SET repository=? WHERE task_id=?",
                              (str(plain), TASK))
        result = self.status(self.unused_reader())
        self.assertFalse(result["live"])
        self.assertEqual(result["error"]["code"], "origin_missing")
        self.assertTrue(result["error"]["message"])
        self.assertIsNone(result["checks"])
        self.assertEqual(result["delivery"]["pr_number"], 7)

    def test_read_only_command_leaves_the_database_untouched(self):
        self.add_task(**self.delivery())
        before = self.snapshot()
        passing = [{"__typename": "CheckRun", "name": "test", "workflowName": "CI",
                    "status": "COMPLETED", "conclusion": "SUCCESS"}]
        with patch.object(gh_client, "pr_view",
                          return_value=self.pr(statusCheckRollup=passing)) as view:
            result = cli.run("get-delivery-status", {"task_id": TASK}, self.db_path)
        view.assert_called_once_with("owner/repo", 7, merge_checks=True)
        self.assertTrue(result["live"])
        self.assertEqual(result["gate"],
                         {"state": "ready", "reason": delivery_status.READY_REASON,
                          "ready": True})
        self.assertEqual(result["checks_summary"],
                         {"total": 1, "passed": 1, "failed": 0, "running": 0, "unknown": 0})
        self.assertEqual(self.snapshot(), before)

    def test_command_is_registered_as_a_read_only_task_lookup(self):
        self.assertEqual(cli.COMMANDS["get-delivery-status"][0],
                         {"task_id": (True, "str")})

    def test_check_urls_are_limited_to_this_repository(self):
        self.add_task(**self.delivery())
        rollup = [
            {"__typename": "CheckRun", "name": "run", "workflowName": "CI",
             "status": "COMPLETED", "conclusion": "SUCCESS",
             "detailsUrl": "https://github.com/owner/repo/actions/runs/42"},
            {"__typename": "CheckRun", "name": "job", "workflowName": "CI",
             "status": "COMPLETED", "conclusion": "SUCCESS",
             "detailsUrl": "https://github.com/owner/repo/actions/runs/42/job/9"},
            {"__typename": "CheckRun", "name": "foreign", "workflowName": "CI",
             "status": "COMPLETED", "conclusion": "SUCCESS",
             "detailsUrl": "https://github.com/someone/else/actions/runs/42"},
            {"__typename": "CheckRun", "name": "query", "workflowName": "CI",
             "status": "COMPLETED", "conclusion": "SUCCESS",
             "detailsUrl": "https://github.com/owner/repo/actions/runs/42?foo=1"},
            {"__typename": "StatusContext", "name": "external", "state": "SUCCESS",
             "targetUrl": "https://example.invalid/status"},
        ]
        result = self.status(lambda repo, number, **kw: self.pr(statusCheckRollup=rollup))
        urls = {item["name"]: item["url"] for item in result["checks"]}
        self.assertEqual(urls["run"], "https://github.com/owner/repo/actions/runs/42")
        self.assertEqual(urls["job"], "https://github.com/owner/repo/actions/runs/42/job/9")
        self.assertIsNone(urls["foreign"])
        self.assertIsNone(urls["query"])
        self.assertIsNone(urls["external"])

    def test_status_context_and_unknown_entries_never_break_the_read(self):
        self.add_task(**self.delivery())
        rollup = [
            {"__typename": "StatusContext", "context": "ci/external", "state": "SUCCESS"},
            {"__typename": "StatusContext", "context": "ci/pending", "state": "PENDING"},
            {"__typename": "StatusContext", "context": "ci/failed", "state": "FAILURE"},
            {"__typename": "Mystery", "name": "unknown-kind"},
            "not-a-dict",
            {},
        ]
        result = self.status(lambda repo, number, **kw: self.pr(statusCheckRollup=rollup))
        checks = {item["name"]: item for item in result["checks"]}
        self.assertEqual(checks["ci/external"]["state"], "passed")
        self.assertIsNone(checks["ci/external"]["conclusion"])
        self.assertEqual(checks["ci/pending"]["state"], "running")
        self.assertEqual(checks["ci/failed"]["state"], "failed")
        self.assertEqual(checks["unknown-kind"]["state"], "unknown")
        unnamed = checks["未命名检查"]
        self.assertEqual(unnamed["state"], "unknown")
        self.assertEqual(unnamed["superseded_runs"], 1)
        self.assertEqual([item["state"] for item in result["checks"]],
                         ["failed", "running", "unknown", "unknown", "passed"])
        self.assertEqual(result["checks_summary"],
                         {"total": 5, "passed": 1, "failed": 1, "running": 1, "unknown": 2})

    def test_unreadable_rollup_stays_null_instead_of_empty(self):
        self.add_task(**self.delivery())
        result = self.status(lambda repo, number, **kw: self.pr(statusCheckRollup=None))
        self.assertTrue(result["live"])
        self.assertIsNone(result["checks"])
        self.assertIsNone(result["checks_summary"])
        self.assertEqual(result["gate"]["state"], "waiting_checks")
        self.assertIn("检查结果未知", result["gate"]["reason"])

    def test_check_text_is_redacted_and_bounded(self):
        self.add_task(**self.delivery())
        rollup = [{"__typename": "CheckRun", "name": "n" * 200, "workflowName": "w" * 120,
                   "status": "COMPLETED", "conclusion": "FAILURE",
                   "detailsUrl": "https://github.com/owner/repo/actions/runs/42"},
                  {"__typename": "CheckRun", "name": "see https://secret.invalid/token",
                   "workflowName": "CI", "status": "COMPLETED", "conclusion": "SUCCESS"}]
        result = self.status(lambda repo, number, **kw: self.pr(statusCheckRollup=rollup))
        bounded = next(item for item in result["checks"] if item["state"] == "failed")
        self.assertEqual(len(bounded["name"]), 120)
        self.assertEqual(len(bounded["workflow"]), 80)
        self.assertEqual(bounded["conclusion"], "FAILURE")
        redacted = next(item for item in result["checks"] if item["state"] == "passed")
        self.assertNotIn("https://", redacted["name"])

    def test_auto_merge_flag_follows_the_project_policy(self):
        self.policy(auto_merge_pr=False)
        self.add_task(**self.delivery())
        result = self.status(lambda repo, number, **kw: self.pr())
        self.assertFalse(result["delivery"]["auto_merge_enabled"])
        settings = db.get_setting(self.conn, project_config.SETTINGS_KEY)
        settings["items"][PROJECT].pop("automation")
        db.set_setting(self.conn, project_config.SETTINGS_KEY, settings)
        self.assertFalse(self.status(lambda repo, number, **kw: self.pr())
                         ["delivery"]["auto_merge_enabled"])

    def test_moved_pr_head_is_reported_without_touching_the_delivery(self):
        self.add_task(**self.delivery())
        result = self.status(lambda repo, number, **kw: self.pr(headRefOid="b" * 40))
        self.assertFalse(result["pr"]["head_matches_delivery"])
        self.assertEqual(result["pr"]["head_sha"], "b" * 40)
        self.assertEqual(result["delivery"]["head_sha"], HEAD)
        self.assertEqual(result["delivery"]["notice"]["head_sha"], HEAD)
        self.assertTrue(result["delivery"]["notice_matches_head"])
