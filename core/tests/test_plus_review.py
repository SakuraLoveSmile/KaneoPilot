"""Plus Review 控制端测试（protocol.md §5.24.5，v4 T6.3）。

覆盖契约条款：
- claim 后身份核对：请求 run_id、任务本轮执行 run_id 与 worktree、计划哈希、
  关联来源；本周期已有登记报告 → 明确受阻（不覆盖新周期）。
- 专属 Review worktree（独立于执行会话）+ 只读 git status/diff 材料 + 计划/
  （修复周期）原报告/执行证据复制；材料缺失必须显式记原因，不静默省略。
- ``mode="plan"`` + ``permissionMode="auto"`` 新会话；指令只要求输出冻结 JSON。
- 三结论登记：pass → register→advance→report succeeded；changes_requested →
  **先** create-review-fix **再** register-review（带 review_key/fix_task_id/
  fix_task_source_sha256）→ report succeeded；blocked → register→report needs_user。
- 登记身份：register-review/create-review-fix/advance-task 都带 request_id +
  run_id + expected_execution_run_id。
- 意外 proposal：只 reject 关闭并记 review_blocked，绝不按其 markdown 登记 pass。
- 格式错误/转录不完整/身份不符/权限等待/超时 → 绝不写 pass、不推进 done。
- 部分成功恢复：登记一致跳过 register、同 key fix 不重复创建、report 响应丢失
  只读核对不重放；零 ``pi -p`` 子进程（Popen spy）。

隔离：fake MCP server 复用 test_plus_controller 的实现；仓库与专属 worktree 都
建在 tmp 隔离根下的真实 git 仓库（不触碰用户仓库）。
"""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from helpers import KanpilotTestCase  # noqa: E402,F401
from kanpilot import (  # noqa: E402
    constants, db, pi_desktop_controller, pi_desktop_review, round_store, util,
)
from test_plus_controller import (  # noqa: E402
    SESSION_ID, TASK_ID, TURN_ID, PlusCase, assistant_json,
)

PROJECT_ID = "example-project-05"
EXECUTION_RUN_ID = "runexec0000000000001"


class ReviewCase(PlusCase):
    """Review 场景：真实 git 仓库 + 执行 worktree + in_review 任务与 review 请求。"""

    def setUp(self):
        super().setUp()
        self.reports_root = self.root / "reports"
        os.environ["KANPILOT_REPORTS_ROOT"] = str(self.reports_root)
        self.addCleanup(self._restore_reports_env)
        # 执行轮次运行记录根也钉在隔离根下（缺省是用户真实目录）。
        self.runs_root = self.root / "session_runs"
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self.set_setting("session_runs_root", str(self.runs_root))
        self.server.turn_status = "completed"

    def _restore_reports_env(self):
        os.environ.pop("KANPILOT_REPORTS_ROOT", None)

    def run_stage(self, stage, resume=False):
        # PlusCase isolates the historical dispatch/monitor controller; Review
        # has its own runner and must keep testing that path directly.
        from kanpilot import executor_runner
        conn = db.connect(self.db_path)
        try:
            return pi_desktop_review.run_stage(
                stage, conn=conn, db_path=self.db_path,
                prompt=executor_runner.render_prompt(stage),
                timeout=executor_runner._timeout_seconds(conn),
                log_path=executor_runner._log_path(stage),
                record_run=lambda **fields: executor_runner._record_run(conn, stage, **fields),
                resume=resume,
            )
        finally:
            conn.close()

    # ------------------------------------------------------------ 夹具
    def git(self, *args, cwd=None):
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        return proc.stdout

    def make_repo_and_worktree(self, name="Feedback"):
        repo = self.repos_root / name
        repo.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", cwd=repo)
        self.git("config", "user.email", "test@example.com", cwd=repo)
        self.git("config", "user.name", "test", cwd=repo)
        (repo / "app.py").write_text("def main():\n    return 0\n",
                                     encoding="utf-8")
        self.git("add", "-A", cwd=repo)
        self.git("commit", "-q", "-m", "base", cwd=repo)
        worktree = self.root / "exec-worktree"
        self.git("worktree", "add", "--detach", str(worktree), "HEAD", cwd=repo)
        # 本轮执行的未提交改动（Review 材料必须能只读看到它）。
        (worktree / "app.py").write_text("def main():\n    return 1\n",
                                        encoding="utf-8")
        return repo, worktree

    def make_scene(self, *, execution_run_id=EXECUTION_RUN_ID,
                   register_report=False):
        """任务进入评审周期 + 排队 review 请求（runner 自己 claim）。"""
        self.make_ready_task(task_id=TASK_ID, project_name="Feedback")
        repo, worktree = self.make_repo_and_worktree("Feedback")
        # 执行轮次运行记录（events.jsonl/job.json）——Review 材料要只读复制它。
        run_dir = self.runs_root / TASK_ID / execution_run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "events.jsonl").write_text(
            json.dumps({
                "v": 1, "seq": 1, "ts": util.now_iso(), "type": "terminal",
                "outcome": "completed",
            }, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (run_dir / "job.json").write_text(
            json.dumps({
                "task_id": TASK_ID, "run_id": execution_run_id,
                "backend": "pi_desktop", "status": "finished", "exit_code": 0,
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                round_store.upsert_on_claim(
                    conn, TASK_ID, execution_run_id,
                    {"backend": "pi_desktop"}, util.now_iso(),
                    backend="pi_desktop",
                )
                round_store.mark_started(
                    conn, TASK_ID, execution_run_id, util.now_iso(),
                    pid=os.getpid(), worktree_path=str(worktree),
                )
                conn.execute(
                    "UPDATE tasks SET status='in_review', run_id=? "
                    "WHERE task_id=?",
                    (execution_run_id, TASK_ID),
                )
                if register_report:
                    db.update_task(
                        conn, TASK_ID, util.now_iso(),
                        review_report_path=str(self.reports_root / "old.md"),
                        review_report_sha256="0" * 64,
                    )
                db.create_request(
                    conn, util.now_iso(), "review", f"review:{TASK_ID}",
                    task_id=TASK_ID, requested_by="scheduler",
                )
        finally:
            conn.close()
        self.fake.tasks[TASK_ID]["status"] = "in-review"
        return repo, worktree

    # ------------------------------------------------------------ 读取断言
    def review_run(self):
        return self.executor_run("review")

    def review_job(self):
        return self.read_job(
            pi_desktop_controller.invocation_dir(
                "review", self.review_run()["invocation_id"]
            )
        )

    def request_by_id(self, request_id):
        return self.call("get-request", {"request_id": request_id})["request"]

    def record_core_calls(self):
        real = pi_desktop_review.core_call
        calls: list = []

        def wrapper(db_path, command, payload):
            calls.append((command, payload))
            return real(db_path, command, payload)

        patcher = mock.patch.object(pi_desktop_review, "core_call", wrapper)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.core_calls = calls
        return calls

    def commands(self):
        return [command for command, _payload in self.core_calls]

    def payload_of(self, command, index=-1):
        rows = [payload for name, payload in self.core_calls if name == command]
        self.assertTrue(rows, f"未调用 {command}")
        return rows[index]

    def fix_tasks(self):
        conn = db.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT * FROM tasks WHERE plan_kind='review_fix'"
            ).fetchall()
        finally:
            conn.close()

    def review_result(self, conclusion, markdown="# 评审\n\n结论与证据",
                      fix=None):
        self.server.messages = [
            {"id": "m1", "role": "user", "content": "审查指令",
             "timestamp": "2026-09-20T00:00:00Z"},
            assistant_json({
                "conclusion": conclusion,
                "report_markdown": markdown,
                "fix": fix,
            }),
        ]

    def run_review(self, resume=False):
        with self.no_subprocess_spy():
            return self.run_stage("review", resume=resume)


    def test_missing_execution_evidence_records_reason(self):
        """执行证据缺失 → 材料里显式记原因，不静默省略。"""
        repo, worktree = self.make_scene()
        materials = self.root / "materials-direct"
        conn = db.connect(self.db_path)
        try:
            task = self.task(TASK_ID)
            result = pi_desktop_review.collect_materials(
                db_path=self.db_path, conn=conn, materials=materials,
                task=task, execution_worktree=worktree,
                execution_run_id="run-missing-evidence",
            )
        finally:
            conn.close()
        note = materials / "execution-evidence.note"
        self.assertIn("execution-evidence.note", result["files"])
        self.assertTrue(note.is_file())
        self.assertIn("证据", note.read_text(encoding="utf-8"))


class TestReviewIdentityAndMaterials(ReviewCase):
    def test_materials_and_plan_session_are_isolated(self):
        repo, worktree = self.make_scene()
        self.review_result("pass")
        self.record_core_calls()
        self.assertEqual(self.run_review(), 0)

        job = self.review_job()
        self.assertEqual(job["execution_run_id"], EXECUTION_RUN_ID)
        self.assertEqual(job["execution_worktree"], str(worktree))
        review_root = job["review_root"]
        self.assertTrue(review_root.startswith(str(constants.worktrees_root(None))))
        self.assertNotEqual(review_root, str(worktree),
                            "专属 Review worktree 不复用执行目录")
        materials = str(pi_desktop_controller.invocation_dir(
            "review", self.review_run()["invocation_id"]) / "materials")
        self.assertFalse((Path(review_root) / "materials").exists(),
                         "审查材料不能污染产品快照")
        self.assertEqual((Path(review_root) / "app.py").read_bytes(),
                         (worktree / "app.py").read_bytes())
        self.assertTrue(os.path.isfile(os.path.join(materials, "changes.diff")))
        self.assertIn(
            "return 1",
            open(os.path.join(materials, "changes.diff"),
                 encoding="utf-8").read(),
            "改动材料必须来自执行 worktree 的实际改动",
        )
        self.assertTrue(os.path.isfile(os.path.join(materials, "plan.md")))
        self.assertTrue(
            os.path.isfile(os.path.join(materials, "context.json"))
        )
        # 执行证据按只读副本进材料目录（本周期执行轮次的 events/job）。
        evidence = os.path.join(materials, "execution-events.jsonl")
        self.assertTrue(os.path.isfile(evidence))
        self.assertIn("completed", open(evidence, encoding="utf-8").read())
        self.assertTrue(
            os.path.isfile(os.path.join(materials, "execution-job.json"))
        )
        # 本地只读 git 是 runner 自身行为；模型 CLI 子进程一律不得出现。
        self.assertTrue(any(call[0] == "git" for call in self.subprocess_calls))
        self.assertFalse(any(
            os.path.basename(call[0]) in ("pi", "codex")
            for call in self.subprocess_calls
        ))

        # 会话：mode=plan + permissionMode=auto，项目根为专属 Review worktree。
        self.assertEqual(self.server.created["mode"], "plan")
        self.assertEqual(self.server.created["projectPath"], review_root)
        self.assertEqual(self.server.created["providerId"], "test-provider")
        self.assertEqual(self.server.configured["mode"], "plan")
        self.assertEqual(self.server.configured["permissionMode"], "auto")
        self.assertEqual(job["config"]["permission_mode"], "auto")
        self.assertEqual(self.review_run()["config"]["permission_mode"], "auto")
        self.assertTrue(self.server.configured["confirm"])

        prompt = self.server.prompt_content
        self.assertIn("\"conclusion\":\"pass|changes_requested|blocked\"", prompt)
        # §5.24.5：无人值守，asktool 只会挂到超时；需用户决定时走 blocked。
        self.assertIn("本轮**无人值守**：禁止调用 `asktool`", prompt)
        self.assertIn("给出 `blocked` 结论", prompt)
        self.assertIn(os.path.join(materials, "changes.diff"), prompt)
        self.assertIn(EXECUTION_RUN_ID, prompt)
        self.assertNotIn("Bearer", prompt)
        self.assertNotIn(self.server.token, prompt)
        self.assertNotIn(self.server.token, json.dumps(job, ensure_ascii=False))

    def test_existing_report_in_cycle_blocks_without_overwrite(self):
        self.make_scene(register_report=True)
        self.review_result("pass")
        calls = self.record_core_calls()
        self.assertEqual(self.run_review(), 1)
        self.assertNotIn("register-review", self.commands())
        self.assertNotIn("advance-task", self.commands())
        task = self.task(TASK_ID)
        self.assertEqual(task["review_report_path"],
                         str(self.reports_root / "old.md"))
        self.assertEqual(task["review_report_sha256"], "0" * 64)
        request = self.request_by_id(self.review_job()["request_id"])
        self.assertEqual(request["status"], "failed")
        self.assertEqual(request["result"]["outcome"], "needs_user")
        self.assertEqual(self.task(TASK_ID)["failure_code"], "review_blocked")
        self.assertEqual(self.server.created_count, 0)
        self.assertEqual(self.server.prompt_count, 0)


# ------------------------------------------------------------ 三结论


class TestReviewConclusions(ReviewCase):
    def test_pass_registers_advances_and_reports(self):
        self.make_scene()
        self.review_result("pass", markdown="# 评审\n\n通过：证据齐全")
        self.record_core_calls()
        self.assertEqual(self.run_review(), 0)

        self.assertEqual(
            [name for name in self.commands()
             if name in ("register-review", "advance-task", "report")],
            ["register-review", "advance-task", "report"],
        )
        job = self.review_job()
        task = self.task(TASK_ID)
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["review_summary"]["conclusion"], "pass")
        self.assertEqual(task["review_report_path"], job["report_path"])
        self.assertTrue(os.path.isfile(job["report_path"]))
        self.assertEqual(job["report_path"],
                         str(self.reports_root / TASK_ID / f"{job['run_id']}.md"))
        self.assertEqual(
            task["review_report_sha256"], util.sha256_file(
                Path(job["report_path"])
            ),
        )
        # 登记身份（§5.24.5）：三处调用都带 request_id + run_id +
        # expected_execution_run_id。
        for command in ("register-review", "advance-task"):
            payload = self.payload_of(command)
            self.assertEqual(payload["request_id"], job["request_id"])
            self.assertEqual(payload["run_id"], job["run_id"])
            self.assertEqual(payload["expected_execution_run_id"],
                             EXECUTION_RUN_ID)
        request = self.request_by_id(job["request_id"])
        self.assertEqual(request["status"], "succeeded")
        self.assertEqual(request["result"]["outcome"], "succeeded")
        run = self.review_run()
        self.assertEqual(run["backend"], "pi_desktop")
        self.assertEqual(run["session_id"], SESSION_ID)
        self.assertEqual(run["turn_id"], TURN_ID)
        self.assertEqual(run["send_state"], "sent")
        self.assertIn("invocation_id", run)

    def test_changes_requested_creates_fix_before_register(self):
        self.make_scene()
        self.review_result("changes_requested", fix={
            "title": "修复返回值", "description": "app.py 返回 1，应为 0",
            "priority": "high",
        })
        self.record_core_calls()
        self.assertEqual(self.run_review(), 0)

        names = [name for name in self.commands()
                 if name in ("create-review-fix", "register-review", "report")]
        self.assertEqual(names, ["create-review-fix", "register-review", "report"],
                         "changes_requested 必须先 create 后 register")
        job = self.review_job()
        register = self.payload_of("register-review")
        self.assertEqual(register["conclusion"], "changes_requested")
        self.assertEqual(register["review_key"], job["review_key"])
        self.assertEqual(register["fix_task_id"], job["fix_task_id"])
        self.assertEqual(register["fix_task_source_sha256"],
                         job["fix_task_source_sha256"])
        expected_key = pi_desktop_review.review_key(
            TASK_ID, EXECUTION_RUN_ID, job["report_sha256"]
        )
        self.assertEqual(job["review_key"], expected_key)
        create = self.payload_of("create-review-fix")
        self.assertEqual(create["dedupe_key"], expected_key)
        self.assertEqual(create["priority"], "high")
        self.assertEqual(create["expected_execution_run_id"], EXECUTION_RUN_ID)

        fix_rows = self.fix_tasks()
        self.assertEqual(len(fix_rows), 1)
        self.assertEqual(fix_rows[0]["task_id"], job["fix_task_id"])
        self.assertEqual(fix_rows[0]["status"], "needs_plan",
                         "修复任务等用户手动发起 review_fix 计划")
        task = self.task(TASK_ID)
        self.assertEqual(task["fix_task_id"], job["fix_task_id"])
        self.assertEqual(task["review_summary"]["conclusion"],
                         "changes_requested")
        self.assertEqual(task["status"], "in_review")
        request = self.request_by_id(job["request_id"])
        self.assertEqual(request["status"], "succeeded")

    def test_blocked_registers_and_reports_needs_user(self):
        self.make_scene()
        self.review_result("blocked", markdown="# 评审\n\n需求不清，阻塞")
        self.record_core_calls()
        self.assertEqual(self.run_review(), 0)
        names = [name for name in self.commands()
                 if name in ("register-review", "advance-task", "report")]
        self.assertEqual(names, ["register-review", "report"])
        register = self.payload_of("register-review")
        self.assertEqual(register["conclusion"], "blocked")
        task = self.task(TASK_ID)
        self.assertEqual(task["status"], "needs_user")
        self.assertEqual(task["failure_code"], "review_blocked")
        self.assertEqual(task["review_summary"]["conclusion"], "blocked")
        request = self.request_by_id(self.review_job()["request_id"])
        self.assertEqual(request["status"], "failed")
        self.assertEqual(request["result"]["outcome"], "needs_user")


# ------------------------------------------------------------ 绝不成 pass


class TestReviewNeverWritesPass(ReviewCase):
    def test_transcript_failure_has_one_error_prefix_and_never_registers(self):
        self.make_scene()
        self.review_result("pass")
        self.record_core_calls()
        with mock.patch.object(pi_desktop_controller, "read_transcript", return_value=(
                [], False, "transcript_incomplete: 单页超过控制面结果上限")):
            self.assertEqual(self.run_review(), 1)
        self.assertNotIn("register-review", self.commands())
        self.assertEqual(self.review_job()["detail"],
                         "transcript_incomplete: 单页超过控制面结果上限")

    def test_unexpected_proposal_is_rejected_and_blocked(self):
        self.make_scene()
        self.review_result("pass", markdown="# 评审\n\n看起来通过")
        self.server.plans = [{
            "id": "prop-1", "sessionId": SESSION_ID, "turnId": "turn-1",
            "toolCallId": "tool-1", "kind": "plan", "status": "pending",
            "version": 4, "markdown": "# 计划（意外生成）",
        }]
        self.record_core_calls()
        self.assertEqual(self.run_review(), 0)

        resolves = [args for name, args in self.server.calls
                    if name == "pi_plans_resolve"]
        self.assertEqual(len(resolves), 1)
        self.assertEqual(resolves[0]["action"], "reject")
        self.assertIs(resolves[0]["confirm"], True)
        self.assertEqual(self.server.plans[0]["status"], "rejected")
        self.assertNotIn("advance-task", self.commands())
        register = self.payload_of("register-review")
        self.assertEqual(register["conclusion"], "blocked")
        task = self.task(TASK_ID)
        self.assertEqual(task["status"], "needs_user")
        self.assertEqual(task["failure_code"], "review_blocked")
        # 报告正文是事故说明，不是模型那篇「看起来通过」。
        report = open(self.review_job()["report_path"], encoding="utf-8").read()
        self.assertIn("proposal", report)
        self.assertNotIn("看起来通过", report)

    def test_invalid_final_json_never_registers(self):
        self.make_scene()
        self.server.messages = [
            {"id": "m1", "role": "assistant", "content": "看起来没问题，可以合并。",
             "timestamp": "2026-09-20T00:00:00Z"},
        ]
        calls = self.record_core_calls()
        self.assertEqual(self.run_review(), 1)
        self.assertNotIn("register-review", self.commands())
        self.assertNotIn("advance-task", self.commands())
        task = self.task(TASK_ID)
        self.assertIsNone(task["review_summary"])
        self.assertNotEqual(task["status"], "done")
        job = self.review_job()
        self.assertIn("review_result_invalid", job["detail"])
        request = self.request_by_id(job["request_id"])
        self.assertEqual(request["result"]["outcome"], "needs_user")
        self.assertEqual(request["status"], "failed")

    def test_incomplete_conclusion_json_never_registers(self):
        """fix 字段不合冻结枚举 → 不合规（不写 pass）。"""
        self.make_scene()
        self.review_result("changes_requested", fix={
            "title": "t", "description": "d", "priority": "P0-紧急",
        })
        self.record_core_calls()
        self.assertEqual(self.run_review(), 1)
        self.assertNotIn("register-review", self.commands())
        self.assertNotIn("create-review-fix", self.commands())
        self.assertEqual(self.fix_tasks(), [])


    def test_stale_review_cycle_stops_without_registration(self):
        """登记时评审周期已被新执行轮次接管 → 核心拒登记（stale_run）：
        停止写入，不 report、不写 pass、不覆盖新周期。"""
        self.make_scene()
        self.review_result("pass")
        real = pi_desktop_review.core_call
        state = {"mutated": False}

        def hijack(db_path, command, payload):
            if command == "register-review" and not state["mutated"]:
                state["mutated"] = True
                # 模拟同一任务被新的执行轮次接管（评审周期已变）。
                conn = db.connect(self.db_path)
                try:
                    with db.write_txn(conn):
                        conn.execute(
                            "UPDATE tasks SET run_id='run-new-cycle' "
                            "WHERE task_id=?",
                            (TASK_ID,),
                        )
                finally:
                    conn.close()
            return real(db_path, command, payload)

        with mock.patch.object(pi_desktop_review, "core_call", hijack):
            with self.no_subprocess_spy():
                rc = self.run_stage("review")
        self.assertTrue(state["mutated"], "必须真的走到 register-review")
        self.assertEqual(rc, 1)
        job = self.review_job()
        self.assertIn("stale_run", job["detail"])
        self.assertIn("停止写入", job["detail"])
        task = self.task(TASK_ID)
        self.assertIsNone(task["review_summary"], "不得登记任何结论")
        self.assertNotEqual(task["status"], "done")
        self.assertEqual(
            self.request_by_id(job["request_id"])["status"], "claimed",
            "身份/周期失配不得汇报、不得覆盖",
        )
    def test_permission_waiting_timeout_never_registers(self):
        self.make_scene()
        self.server.turn_status = "running"
        self.server.pending_tool_confirmations = [
            {"toolCallId": "c1", "toolName": "bash"}
        ]
        self.set_setting("executor_timeout_seconds", 1)
        calls = self.record_core_calls()
        self.assertEqual(self.run_review(), 1)
        self.assertNotIn("register-review", self.commands())
        self.assertNotIn("advance-task", self.commands())
        self.assertGreaterEqual(self.server.stop_calls, 1)
        job = self.review_job()
        self.assertIn("permission_waiting", job["detail"])
        self.assertIsNone(self.task(TASK_ID)["review_summary"])
        request = self.request_by_id(job["request_id"])
        self.assertEqual(request["result"]["outcome"], "needs_user")
        self.assertEqual(self.task(TASK_ID)["failure_code"], "review_blocked")


# ------------------------------------------------------------ 部分成功恢复


class TestReviewRecovery(ReviewCase):
    def test_session_configuration_keeps_historical_ask_snapshot(self):
        client = mock.Mock()
        client.session_create.return_value = {"sessionId": "historical-review"}
        snapshot = {"provider_id": "old-provider", "model_id": "old-model",
                    "thinking_level": "high", "permission_mode": "ask"}
        session, error = pi_desktop_review._create_plan_session(client, snapshot, self.root)
        self.assertIsNone(error)
        self.assertEqual(session, "historical-review")
        client.session_configure.assert_called_once_with(
            session, mode="plan", permission_mode="ask")
        self.assertEqual(snapshot["permission_mode"], "ask")

    def _run_with_failing_report(self, failures=2):
        real = pi_desktop_review._report
        state = {"n": 0}

        def flaky(*args, **kwargs):
            state["n"] += 1
            if state["n"] <= failures:
                return False, "simulated report transport loss"
            return real(*args, **kwargs)

        with mock.patch.object(pi_desktop_review, "_report", flaky):
            rc = self.run_stage("review")
        return rc

    def test_recovery_completes_without_duplicate_fix_or_replay(self):
        self.make_scene()
        self.review_result("changes_requested", fix={
            "title": "修复返回值", "description": "应为 0", "priority": "medium",
        })
        self.record_core_calls()
        with self.no_subprocess_spy():
            self.assertEqual(self._run_with_failing_report(), 1)
        job = self.review_job()
        self.assertEqual(job["status"], "failed")
        self.assertTrue(job["checkpoints"]["fix_created"])
        self.assertTrue(job["checkpoints"]["registered"])
        self.assertFalse(job["checkpoints"].get("reported"))
        self.assertEqual(len(self.fix_tasks()), 1)
        create_calls = self.commands().count("create-review-fix")
        register_calls = self.commands().count("register-review")
        request = self.request_by_id(job["request_id"])
        self.assertEqual(request["status"], "claimed",
                         "汇报未完成，租约仍在有效期内")

        # 恢复：登记一致 → 跳过 register、不重复建 fix，只完成 report。
        self.assertEqual(self.run_review(resume=True), 0)
        self.assertEqual(self.commands().count("create-review-fix"),
                         create_calls)
        self.assertEqual(self.commands().count("register-review"),
                         register_calls)
        self.assertEqual(len(self.fix_tasks()), 1)
        request = self.request_by_id(job["request_id"])
        self.assertEqual(request["status"], "succeeded")
        self.assertEqual(request["result"]["outcome"], "succeeded")
        resumed = self.review_job()
        self.assertEqual(resumed["status"], "finished")
        self.assertTrue(resumed["checkpoints"]["reported"])
        self.assertEqual(self.server.prompt_count, 1, "恢复绝不重发指令")

    def test_report_response_loss_is_not_replayed(self):
        """report 响应丢失但已生效：读 get-request 核对后认定完成，不重放。"""
        self.make_scene()
        self.review_result("pass")
        self.record_core_calls()
        real = pi_desktop_review.core_call
        state = {"raised": False}

        def flaky(db_path, command, payload):
            result = real(db_path, command, payload)
            if command == "report" and not state["raised"]:
                state["raised"] = True
                raise util.KanpilotError("remote_unavailable",
                                         "report response lost")
            return result

        with mock.patch.object(pi_desktop_review, "core_call", flaky):
            with self.no_subprocess_spy():
                rc = self.run_stage("review")
        self.assertEqual(rc, 0, "已生效的 report 不得重放，也不得判失败")
        self.assertEqual(self.commands().count("report"), 1)
        job = self.review_job()
        self.assertEqual(job["status"], "finished")
        self.assertTrue(job["checkpoints"]["reported"])
        self.assertEqual(self.task(TASK_ID)["status"], "done")

    def test_recovery_without_known_session_keeps_block(self):
        """无已知 session/turn 的现场：不自动重发，保留人工阻塞。"""
        self.make_scene()
        self.set_setting("executor_runs", {"review": {
            "backend": "pi_desktop", "pid": 999999,
            "invocation_id": "inv-review-stuck", "send_state": "unknown",
            "session_id": None, "turn_id": None,
            "started_at": util.now_iso(), "finished_at": None,
        }})
        directory = pi_desktop_controller.invocation_dir(
            "review", "inv-review-stuck"
        )
        pi_desktop_controller.write_json(directory / "job.json", {
            "stage": "review", "invocation_id": "inv-review-stuck",
            "backend": "pi_desktop", "status": "running",
            "send_state": "unknown", "session_id": None, "turn_id": None,
            "task_id": TASK_ID, "request_id": "req-x", "run_id": "run-x",
        })
        self.assertEqual(self.run_review(resume=True), 1)
        self.assertEqual(self.server.prompt_count, 0)
        self.assertEqual(self.server.created_count, 0)
        job = self.read_job(directory)
        self.assertEqual(job["status"], "failed",
                         "只读恢复无法接续 → 明确收尾为人工阻塞，不改写现场身份")
        self.assertIn("保留人工阻塞", job["detail"])
        self.assertEqual(job["send_state"], "unknown")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
