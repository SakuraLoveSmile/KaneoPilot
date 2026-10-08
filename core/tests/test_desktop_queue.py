"""Formal Desktop MCP with isolated roots, in-memory Kaneo and local Git only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
import io
import json
import os
from pathlib import Path
import subprocess
import threading
from unittest.mock import patch

from helpers import KanpilotTestCase
from kanpilot import (cli, db, desktop_queue as desktop, headless_adapter,
                      pi_desktop_review, remote, round_store, util)


class DesktopQueueTests(KanpilotTestCase):
    def setUp(self):
        super().setUp()
        self.repos_root = self.repos_root.resolve()
        self.call("set-planner-config", {"backend": "codex_desktop"})
        self.call("set-review-config", {"backend": "codex_desktop"})
        self.configure(enabled=True)

    def configure(self, **payload):
        ctx = cli.Context(self.db_path)
        try:
            return desktop.cmd_set_desktop_queue_config(ctx, payload)
        finally:
            ctx.close()

    def native(self, name, payload=None, error=False):
        result = desktop._tool_result("kp_desktop_" + name, payload or {}, self.db_path)
        self.assertEqual(result["isError"], error, result)
        return json.loads(result["content"][0]["text"])

    def identity(self, claim):
        return {key: claim[key] for key in desktop._IDENTITY}

    def plan(self, suffix="1", method="builtin"):
        task = self.fake.add_task("desktopformalplan" + suffix, "ryryo1reph1xfemqtwkjyiiz",
                                  status="to-do", description="需求：限制整数输入并显示平方")
        self.make_repo("Feedback")
        self.sync_task(task, project_name="Feedback")
        prepared = self.call("prepare-plan-input", {"task_id": task["id"], "local_paths": [],
                                                    "supplement": "只读规划", "excluded_remote_ids": []})
        result = self.call("start-plans", {"items": [{"input_id": prepared["input_id"],
                                                     "input_sha256": prepared["input_sha256"],
                                                     "planner_backend": "codex_desktop", "planner_method": method}]})["results"][0]
        self.assertIn("request_id", result, result)
        return task, result["request_id"]

    def claim(self, role="plan", op="op1"):
        return self.native("claim", {"role": role, "operation_id": op, "thread_id": "native-thread"})

    def submit_plan(self, claim, **override):
        result = {"status": "ready", "plan_markdown": "# 规划\n\n1. 校验整数。\n2. 运行验证。", "questions": [], "evidence": []}
        result.update(override)
        return self.native("submit_plan", {**self.identity(claim), "result": result})

    def mutate(self, sql, args=()):
        with closing(db.connect(self.db_path)) as conn:
            conn.execute(sql, args)
            conn.commit()

    def read_job(self, claim):
        return json.loads(Path(claim["context"]["job_path"]).read_text())

    def make_review(self, suffix="1", automated=False):
        task, _request = self.plan(suffix)
        claimed = self.claim(op="plan-" + suffix)
        self.submit_plan(claimed)
        repository = self.repos_root / "Feedback"
        def git(*args):
            subprocess.run(["git", "-C", str(repository), *args], check=True, capture_output=True)
        if not (repository / ".git").is_dir():
            git("init")
            git("config", "user.name", "Isolated Fixture")
            git("config", "user.email", "fixture@example.invalid")
            (repository / "sample.py").write_text("def square(n):\n    return n*n\n")
            git("add", "sample.py")
            git("commit", "-m", "isolated initial")
        self.fake.tasks[task["id"]]["status"] = "in-review"
        execution_run = "exec-" + suffix
        if automated:
            self.call("set-project-automation", {"project_id": task["projectId"],
                                                 "automation": {"enabled": True, "max_fix_rounds": 2},
                                                 "expected_policy_revision": 0})
        with closing(db.connect(self.db_path)) as conn:
            db.update_task(conn, task["id"], util.now_iso(), status="in_review", stage="review", run_id=execution_run)
            row = db.require_task(conn, task["id"])
            if automated:
                db.set_extra(conn, row, util.now_iso(), automation={"manual_takeover": False, "fix_round": 0,
                                                                  "max_fix_rounds": 3})
            round_store.upsert_on_claim(conn, task["id"], execution_run, {"backend": "pi"}, util.now_iso(), backend="pi")
            conn.execute("UPDATE execution_rounds SET worktree_path=?,run_status='finished' WHERE task_id=? AND run_id=?",
                         (str(repository), task["id"], execution_run))
            evidence = headless_adapter.resolve_source(conn).run_dir(task["id"], execution_run)
            evidence.mkdir(parents=True)
            (evidence / "job.json").write_text(json.dumps({"task_id": task["id"], "run_id": execution_run,
                                                         "backend": "pi", "status": "finished", "exit_code": 0}))
            (evidence / "events.jsonl").write_text('{"type":"terminal","outcome":"completed"}\n')
            request, _ = db.create_request(conn, util.now_iso(), "review", "review:" + task["id"], task_id=task["id"])
            conn.commit()
        return task, request["request_id"]

    def review_result(self, conclusion):
        return {"conclusion": conclusion, "report_markdown": "# Review\n证据、结论与待补事项。",
                "fix": {"title": "补充校验", "description": "覆盖小数与非数字输入", "priority": "high"}
                if conclusion == "changes_requested" else None}

    def test_formal_plan_publishes_hold_and_terminal_retry_is_read_only(self):
        task, request_id = self.plan()
        claim = self.claim()
        self.assertEqual(claim["request_id"], request_id)
        self.assertLessEqual(claim["heartbeat_seconds"], 60)
        self.assertEqual(self.native("heartbeat", {**self.identity(claim), "progress": "读取冻结材料"})["ok"], True)
        result = self.submit_plan(claim)
        self.assertEqual(result["task_status"], "ready")
        self.assertEqual(result["request_status"], "succeeded")
        current = self.call("get-task", {"task_id": task["id"]})["task"]
        self.assertTrue(current["extra"]["plan_execution_hold"])
        with closing(db.connect(self.db_path)) as conn:
            row = db.require_task(conn, task["id"])
            self.assertEqual(row["planner_model"], "codex-desktop")
            self.assertEqual(row["reasoning_effort"], "inherit")
            self.assertEqual(round_store.get_round(conn, task["id"], claim["run_id"])["backend"], "codex_desktop")
        published = Path(result["plan_path"])
        before = published.read_bytes(), published.stat().st_mtime_ns
        with patch.object(desktop.cmd_exec, "cmd_publish_plan", side_effect=AssertionError("terminal retry wrote")), \
             patch.object(desktop.planner_runner, "write_plan_file", side_effect=AssertionError("terminal retry generated")):
            retry = self.submit_plan(claim)
        self.assertTrue(retry["deduplicated"])
        self.assertEqual(before, (published.read_bytes(), published.stat().st_mtime_ns))
        self.assertEqual(self.native("submit_plan", {**self.identity(claim), "result": {"status": "ready", "plan_markdown": "不同"}}, error=True)["code"], "conflict")

    def test_needs_input_draft_and_disabled_inflight_settles(self):
        self.plan()
        claim = self.claim()
        self.configure(enabled=False)
        self.assertEqual(self.claim(op="new")["reason"], "disabled")
        recovered = self.claim()
        self.assertEqual(recovered["claim_token"], claim["claim_token"])
        result = self.submit_plan(claim, status="needs_input", plan_markdown="# 草稿", questions=["整数范围？"])
        self.assertEqual((result["request_status"], result["task_status"]), ("failed", "needs_user"))
        self.assertEqual((Path(claim["context"]["job_path"]).parent / "plan-draft.md").read_text(), "# 草稿")

    def test_native_skill_and_process_evidence_without_cli_credentials(self):
        self.plan(method="spec_plan")
        with patch("kanpilot.codex_planner.resolve_provider", create=True, side_effect=AssertionError("CLI provider")):
            claim = self.claim()
        self.assertIn("$spec-planner:spec-plan", claim["instructions"])
        job = self.read_job(claim)
        self.assertEqual((job["pid"], job["ppid"]), (os.getpid(), os.getppid()))
        self.assertEqual(job["session_id"], "native-thread")
        self.assertEqual(job["config"], desktop.MODEL_SOURCE)
        self.assertNotIn(claim["claim_token"], json.dumps(job))

    def test_claim_response_loss_and_concurrent_operation_do_not_take_next_request(self):
        _task, expected = self.plan()
        self.plan("2")
        # Repeated same operation recovers the original receipt; checkpoint
        # preparation can return busy while another process is preparing it.
        first = self.claim()
        with ThreadPoolExecutor(max_workers=4) as pool:
            values = list(pool.map(lambda _: desktop._tool_result("kp_desktop_claim", {"role": "plan", "operation_id": "op1"}, self.db_path), range(4)))
        successful = [value["structuredContent"] for value in values if not value["isError"]]
        self.assertTrue(successful)
        self.assertTrue(all(value["request_id"] == expected and value["claim_token"] == first["claim_token"] for value in successful))
        self.assertEqual(self.native("claim", {"role": "review", "operation_id": "op1"}, error=True)["code"], "conflict")
        self.assertEqual(self.claim(op="fresh")["reason"], "stage_busy")
        key = next((self.db_path.parent / ".desktop-queue").glob("*/claim.key"))
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(key.parent.stat().st_mode & 0o777, 0o700)

    def test_wrong_token_run_role_and_body_token_never_write_or_leak(self):
        self.plan()
        claim = self.claim()
        result = {"status": "ready", "plan_markdown": "# 规划"}
        for override in ({"claim_token": "foreign-token"}, {"run_id": "different-run"}):
            response = self.native("submit_plan", {**self.identity(claim), **override, "result": result}, error=True)
            self.assertNotIn(override.get("claim_token", "<not-token>"), json.dumps(response))
        self.assertEqual(self.native("submit_review", {**self.identity(claim), "result": self.review_result("pass")}, error=True)["code"], "forbidden")
        self.assertEqual(self.native("submit_plan", {**self.identity(claim), "result": {**result, "plan_markdown": claim["claim_token"]}}, error=True)["code"], "bad_request")
        self.assertEqual(self.native("heartbeat", {**self.identity(claim), "progress": claim["claim_token"]}, error=True)["code"], "bad_request")
        self.assertEqual(self.call("get-request", {"request_id": claim["request_id"]})["request"]["status"], "claimed")

    def test_expired_archive_cancel_and_changed_source_deny_submit(self):
        for suffix, sql, value in (
            ("expired", "UPDATE requests SET lease_expires_at=? WHERE request_id=?", util.to_iso(util.now_dt() - timedelta(seconds=1))),
            ("archive", "UPDATE tasks SET save_state=? WHERE task_id=?", "archived"),
            ("cancel", "UPDATE requests SET status=? WHERE request_id=?", "cancelled"),
            ("source", "UPDATE tasks SET source_sha256=? WHERE task_id=?", "changed-source"),
        ):
            task, request_id = self.plan(suffix)
            claim = self.claim(op=suffix)
            self.mutate(sql, (value, request_id if "requests" in sql else task["id"]))
            result = self.native("submit_plan", {**self.identity(claim), "result": {"status": "ready", "plan_markdown": "# 规划"}}, error=True)
            self.assertIn(result["code"], ("conflict", "stale_run"))
            self.mutate("UPDATE requests SET status='cancelled' WHERE request_id=?", (request_id,))

    def test_frozen_plan_input_tampering_denied(self):
        self.plan()
        claim = self.claim()
        (Path(claim["context"]["input_dir"]) / "supplement.md").write_text("变更后的材料")
        self.assertEqual(self.native("submit_plan", {**self.identity(claim), "result": {"status": "ready", "plan_markdown": "# 规划"}}, error=True)["code"], "conflict")

    def test_plan_publish_commits_then_loses_response_retry_reads_same_result(self):
        self.plan()
        claim = self.claim()
        publish = desktop.cmd_exec.cmd_publish_plan
        def lost(ctx, payload):
            publish(ctx, payload)
            raise util.KanpilotError("response_lost", "fixture response lost")
        with patch.object(desktop.cmd_exec, "cmd_publish_plan", lost), \
             patch.dict(cli.COMMANDS, {"publish-plan": (cli.COMMANDS["publish-plan"][0], lost)}):
            self.native("submit_plan", {**self.identity(claim), "result": {"status": "ready", "plan_markdown": "# 规划"}}, error=True)
        result = self.native("submit_plan", {**self.identity(claim), "result": {"status": "ready", "plan_markdown": "# 规划"}})
        self.assertTrue(result["deduplicated"])
        self.assertEqual(result["request_status"], "succeeded")

    def test_status_configuration_is_local_and_does_not_disclose_token(self):
        self.plan()
        claim = self.claim()
        with patch.object(remote, "get_client", side_effect=AssertionError("status used external service")):
            value = self.configure(poll_minutes=15, auto_configured=True, planner_thread_id="plan-thread", review_thread_id="review-thread", automation_ids=["a", "b"])
            self.assertEqual(value["queue"], {"plan": 0, "review": 0})
            self.assertEqual(value["queue_details"]["plan"]["claimed"], 1)
            self.assertTrue(value["auto_configured"])
            self.assertNotIn(claim["claim_token"], json.dumps(value))
            self.assertNotIn("token_hash", json.dumps(value))

    def test_all_review_conclusions_reuse_core_and_exact_retry(self):
        for suffix, conclusion in enumerate(("pass", "changes_requested", "blocked")):
            task, request_id = self.make_review(str(suffix))
            claim = self.claim("review", "review-" + str(suffix))
            self.assertEqual(claim["request_id"], request_id)
            self.assertTrue(Path(claim["context"]["review_root"]).is_dir())
            result = self.review_result(conclusion)
            reply = self.native("submit_review", {**self.identity(claim), "result": result})
            self.assertEqual(reply["request_status"], "failed" if conclusion == "blocked" else "succeeded")
            self.assertTrue(Path(reply["report_path"]).is_file())
            before = len(self.fake.requests_log)
            self.assertTrue(self.native("submit_review", {**self.identity(claim), "result": result})["deduplicated"])
            self.assertEqual(before, len(self.fake.requests_log))
            if conclusion == "pass":
                self.assertEqual(self.fake.tasks[task["id"]]["status"], "done")
            elif conclusion == "changes_requested":
                current = self.call("get-task", {"task_id": task["id"]})["task"]
                self.assertTrue(current["review_summary"]["fix_task_id"])
            else:
                self.assertEqual(reply["task_status"], "needs_user")

    def test_automated_changes_requested_keeps_same_task(self):
        task, _request = self.make_review(automated=True)
        claim = self.claim("review")
        reply = self.native("submit_review", {**self.identity(claim), "result": self.review_result("changes_requested")})
        self.assertEqual(reply["request_status"], "succeeded")
        current = self.call("get-task", {"task_id": task["id"]})["task"]
        self.assertEqual(current["status"], "needs_plan")
        self.assertEqual(current["extra"]["fix_request"]["state"], "open")

    def test_review_new_execution_plan_or_material_changes_stop_before_remote(self):
        for suffix, mutation in enumerate(("run", "plan", "material", "snapshot", "execution")):
            task, request_id = self.make_review(str(suffix))
            claim = self.claim("review", "review-" + str(suffix))
            if mutation == "run":
                self.mutate("UPDATE tasks SET run_id=? WHERE task_id=?", ("new-exec", task["id"]))
            elif mutation == "plan":
                with closing(db.connect(self.db_path)) as conn:
                    Path(db.require_task(conn, task["id"])["plan_path"]).write_text("changed")
            elif mutation == "material":
                Path(claim["context"]["materials"]["changes.diff"]).write_text("changed")
            elif mutation == "snapshot":
                (Path(claim["context"]["review_root"]) / "sample.py").write_text("changed snapshot")
            else:
                (self.repos_root / "Feedback" / "sample.py").write_text("changed execution")
            before = len(self.fake.requests_log)
            failure = self.native("submit_review", {**self.identity(claim), "result": self.review_result("pass")}, error=True)
            self.assertIn(failure["code"], ("conflict", "stale_run"))
            self.assertEqual(before, len(self.fake.requests_log))
            self.mutate("UPDATE requests SET status='cancelled' WHERE request_id=?", (request_id,))
            self.mutate("UPDATE tasks SET save_state='archived' WHERE task_id=?", (task["id"],))

    def test_review_missing_execution_evidence_blocks_instead_of_reviewing_main(self):
        task, request_id = self.make_review()
        with closing(db.connect(self.db_path)) as conn:
            directory = headless_adapter.resolve_source(conn).run_dir(task["id"], "exec-1")
        for path in directory.iterdir():
            path.unlink()
        failure = self.native("claim", {"role": "review", "operation_id": "review"}, error=True)
        self.assertEqual(failure["code"], "review_blocked")
        self.assertEqual(self.call("get-request", {"request_id": request_id})["request"]["status"], "failed")

    def test_concurrent_submit_is_busy_and_never_reports_other_submit_failed(self):
        self.plan()
        claim = self.claim()
        identity = self.identity(claim)
        result = {"status": "ready", "plan_markdown": "# 正式规划"}
        entered, release = threading.Event(), threading.Event()
        entry = cli.COMMANDS["publish-plan"]
        def slow(ctx, payload):
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return entry[1](ctx, payload)
        with patch.dict(cli.COMMANDS, {"publish-plan": (entry[0], slow)}):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(desktop._tool_result, "kp_desktop_submit_plan", {**identity, "result": result}, self.db_path)
                self.assertTrue(entered.wait(timeout=5))
                second = desktop._tool_result("kp_desktop_submit_plan", {**identity, "result": result}, self.db_path)
                self.assertTrue(second["isError"])
                self.assertEqual(json.loads(second["content"][0]["text"])["code"], "busy")
                self.assertEqual(self.call("get-request", {"request_id": claim["request_id"]})["request"]["status"], "claimed")
                release.set()
                self.assertFalse(first.result(timeout=5)["isError"])

    def test_review_response_loss_after_each_committed_core_stage_recovers(self):
        for suffix, command, conclusion in (("register", "register-review", "pass"),
                                            ("fix", "create-review-fix", "changes_requested"),
                                            ("advance", "advance-task", "pass"),
                                            ("report", "report", "pass")):
            self.make_review(suffix)
            claim = self.claim("review", suffix)
            entry = cli.COMMANDS[command]
            def lost(ctx, payload, original=entry[1]):
                original(ctx, payload)
                raise util.KanpilotError("response_lost", "isolated committed response lost")
            with patch.dict(cli.COMMANDS, {command: (entry[0], lost)}):
                reply = self.native("submit_review", {**self.identity(claim), "result": self.review_result(conclusion)})
            self.assertEqual(reply["request_status"], "succeeded")
            writes_before = len([log for log in self.fake.requests_log if log[0] in ("POST", "PUT", "PATCH")])
            self.assertTrue(self.native("submit_review", {**self.identity(claim), "result": self.review_result(conclusion)})["deduplicated"])
            self.assertEqual(writes_before, len([log for log in self.fake.requests_log if log[0] in ("POST", "PUT", "PATCH")]))

    def test_review_crash_before_unknown_remote_phase_never_replays_write(self):
        self.make_review()
        claim = self.claim("review")
        result = self.review_result("pass")
        entry = cli.COMMANDS["advance-task"]
        def crashed(_ctx, _payload):
            raise RuntimeError("isolated process crash before remote outcome")
        with patch.dict(cli.COMMANDS, {"advance-task": (entry[0], crashed)}):
            with self.assertRaises(RuntimeError):
                desktop._tool_result("kp_desktop_submit_review", {**self.identity(claim), "result": result}, self.db_path)
        before = len(self.fake.requests_log)
        with patch.dict(cli.COMMANDS, {"advance-task": (entry[0], lambda *_: self.fail("unknown phase replayed"))}):
            reply = self.native("submit_review", {**self.identity(claim), "result": result})
        self.assertEqual((reply["request_status"], reply["task_status"]), ("failed", "needs_user"))
        self.assertEqual(before, len(self.fake.requests_log))
        self.assertIn("结果未知", self.read_job(claim)["detail"])

    def test_review_crash_after_advance_reconciles_locally_without_second_remote_write(self):
        self.make_review()
        claim = self.claim("review")
        result = self.review_result("pass")
        entry = cli.COMMANDS["advance-task"]
        def crashed(ctx, payload):
            entry[1](ctx, payload)
            raise RuntimeError("isolated process crash after commit")
        with patch.dict(cli.COMMANDS, {"advance-task": (entry[0], crashed)}):
            with self.assertRaises(RuntimeError):
                desktop._tool_result("kp_desktop_submit_review", {**self.identity(claim), "result": result}, self.db_path)
        before = len(self.fake.requests_log)
        with patch.dict(cli.COMMANDS, {"advance-task": (entry[0], lambda *_: self.fail("committed advance replayed"))}):
            reply = self.native("submit_review", {**self.identity(claim), "result": result})
        self.assertEqual((reply["request_status"], reply["task_status"]), ("succeeded", "done"))
        self.assertEqual(before, len(self.fake.requests_log))

    def test_valid_fail_only_ends_own_claim_and_expired_heartbeat_is_not_renewed(self):
        _task, request_id = self.plan()
        _other, next_request = self.plan("other")
        claim = self.claim()
        response = self.native("fail", {**self.identity(claim), "reason": "缺少只读权限"})
        self.assertEqual(response["request_status"], "failed")
        self.assertEqual(self.call("get-request", {"request_id": next_request})["request"]["status"], "queued")
        next_claim = self.claim(op="other")
        expired = util.to_iso(util.now_dt() - timedelta(seconds=1))
        self.mutate("UPDATE requests SET lease_expires_at=? WHERE request_id=?", (expired, next_request))
        self.assertEqual(self.native("heartbeat", self.identity(next_claim), error=True)["code"], "stale_run")
        with closing(db.connect(self.db_path)) as conn:
            self.assertEqual(db.get_request(conn, next_request)["lease_expires_at"], expired)

    def test_review_untracked_body_is_frozen_and_same_name_content_change_denied(self):
        self.make_review()
        untracked = self.repos_root / "Feedback" / "new.py"
        untracked.write_text("def added():\n    return 42\n")
        claim = self.claim("review")
        manifest = json.loads(Path(claim["context"]["materials"]["untracked-manifest.json"]).read_text())
        self.assertEqual(manifest["files"][0]["repository_path"], "new.py")
        target = Path(manifest["files"][0]["material_path"])
        self.assertEqual(target.read_bytes(), untracked.read_bytes())
        self.assertNotEqual(target, untracked)
        untracked.write_text("def added():\n    return -1\n")
        before = len(self.fake.requests_log)
        reply = self.native("submit_review", {**self.identity(claim), "result": self.review_result("pass")}, error=True)
        self.assertEqual(reply["code"], "conflict")
        self.assertEqual(before, len(self.fake.requests_log))

    def test_review_untracked_symlink_is_blocked_without_copying_target(self):
        task, request_id = self.make_review()
        outside = self.root / "outside-secret.txt"
        outside.write_text("not a review input")
        (self.repos_root / "Feedback" / "linked.txt").symlink_to(outside)
        reply = self.native("claim", {"role": "review", "operation_id": "review"}, error=True)
        self.assertEqual(reply["code"], "review_blocked")
        self.assertIn("symlink", reply["error"])
        self.assertNotIn(outside.read_text(), json.dumps(reply))
        self.assertEqual(self.call("get-request", {"request_id": request_id})["request"]["status"], "failed")

    def test_tracked_binary_bytes_change_invalidates_review_identity(self):
        self.make_review()
        repository = self.repos_root / "Feedback"
        binary = repository / "data.bin"
        binary.write_bytes(b"\x00initial\x01")
        subprocess.run(["git", "-C", str(repository), "add", "data.bin"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repository), "commit", "-m", "isolated binary baseline"], check=True, capture_output=True)
        binary.write_bytes(b"\x00first-change\x01")
        claim = self.claim("review")
        self.assertIn("GIT binary patch", Path(claim["context"]["materials"]["changes.diff"]).read_text())
        binary.write_bytes(b"\x00second-change\x01")
        reply = self.native("submit_review", {**self.identity(claim), "result": self.review_result("pass")}, error=True)
        self.assertEqual(reply["code"], "conflict")

    def test_crash_after_plan_file_write_recovers_fixed_file_without_regenerating(self):
        self.plan()
        claim = self.claim()
        result = {"status": "ready", "plan_markdown": "# 固定计划文件"}
        write = desktop.planner_runner.write_plan_file
        def crashed(*args, **kwargs):
            write(*args, **kwargs)
            raise RuntimeError("isolated process crash after file write")
        with patch.object(desktop.planner_runner, "write_plan_file", crashed):
            with self.assertRaises(RuntimeError):
                desktop._tool_result("kp_desktop_submit_plan", {**self.identity(claim), "result": result}, self.db_path)
        job = self.read_job(claim)
        target = Path(job["plan_path"])
        self.assertTrue(target.is_file())
        before = target.read_bytes(), target.stat().st_mtime_ns
        with patch.object(desktop.planner_runner, "write_plan_file", side_effect=AssertionError("fixed file regenerated")):
            reply = self.native("submit_plan", {**self.identity(claim), "result": result})
        self.assertEqual(reply["request_status"], "succeeded")
        self.assertEqual(before, (target.read_bytes(), target.stat().st_mtime_ns))
        self.assertEqual(len(list(target.parent.glob("*.md"))), 1)

    def test_stdio_namespace_schema_boundary_and_stdout(self):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "kp_desktop_claim", "arguments": {"role": "plan", "operation_id": "notification"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kp_dispatch", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "kp_desktop_claim", "arguments": {"role": "plan", "operation_id": "op", "repository": "/arbitrary"}}},
        ]
        stdin = io.StringIO("\n".join(json.dumps(message) for message in messages) + "\n" + "x" * (desktop.MAX_MESSAGE_BYTES + 10) + "\n")
        output = io.StringIO()
        with patch.object(desktop.sys, "stdin", stdin), patch.object(desktop.sys, "stdout", output):
            self.assertEqual(desktop.run_stdio(self.db_path), 0)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(responses), 5)
        names = {tool["name"] for tool in responses[1]["result"]["tools"]}
        self.assertEqual(names, {"kp_desktop_" + name for name in ("status", "claim", "heartbeat", "submit_plan", "submit_review", "fail")})
        self.assertTrue(responses[2]["result"]["isError"])
        self.assertTrue(responses[3]["result"]["isError"])
        self.assertEqual(responses[4]["error"]["message"], "Message too large")
