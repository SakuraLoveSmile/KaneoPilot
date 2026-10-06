"""Goal 报告完成判定与派发命名（protocol.md §5.23.10，v3 T5/T6）。

覆盖契约条款（与 `docs/protocol.md` §5.23.10 / `docs/automation-contract.md`
§6 一一对应）：

- **T5-A 报告读取**：`pi_goal_report_get` 的分态互不混淆
  （`ready`/`pending`/`draft`/`failed`/`not_found`/`corrupt`/`truncated`
  + 核心侧 `tool_missing`/`unreadable`）；失败**绝不**返回伪造正文；
  缺只读报告工具 → 明确失败。
- **T5-B 身份与报告底线**：`task_id`/`run_id`、`job.session_id`、
  `job.goal_approval.proposal_id`、已批准执行 turn 与报告的
  `sessionId`/`proposalId`/`executionId`/`turnId` 全部核对；`turnId` 允许
  沿 `turn_adopted` 链；`plan_sha256` 与 `contractHash` **不比较成同一个
  哈希**；无 `goal_approval` → 不能放行。
- **T5-C AI 判定与核心硬闸**：`record-goal-evaluation` 先核 monitor claim
  与租约，再只读重取报告核 hash；`decision=complete` 时确定性核对报告
  底线（status/integrity/execution/verdict、审查项非空且 ID 唯一且全
  `met`、引用 ID 存在）；`advance-task in-progress -> in-review` 对
  `pi_desktop` 硬闸（绕过 monitor/旧判定/报告变化 → conflict，远端不动）。
  幂等：同签名不重复登记。`execution_run_id` 与 `monitor_run_id` 不可互换。
- **T5-D 没有可用报告就没有完成**：`ready+fallback` 等同没有报告；
  `pending` 宽限与过期；报告不可读/损坏 → 原因区别于「已验证失败」；
  升级时仍 `running` 的旧 Plus 轮次按缺报告规则交人工；历史
  `in_review`/`done`/`needs_user` 不回填。
- **T6 派发命名**：新旧 spec 展示快照；`pi_session_create` 携带
  `projectName`；Plus 返回身份不符 → 不下发 `agent_prompt`；旧 Plus 缺
  `projectName` → 明确阻断「版本缺少工作区命名能力」。
- **采集修补**：`Bash` 真实形状 → 大小写归一 + `exit_code` 保真。

隔离：全程无真实 PI-Desktop、无真实 Plus、无真实模型调用。fake MCP server
是本文件自写的 `http.server` 线程（JSON-RPC + Bearer + Mcp-Session-Id +
turn store + 分页 session_get + Goal 报告只读工具）。
"""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import (  # noqa: E402
    cmd_exec, cmd_read, constants, db, dsh_events, pi_desktop_backend,
    pi_desktop_worker, round_store, util,
)

TASK_ID = "taskgoalreport0000000001"
RUN_ID = "rungoalreport01"
MONITOR_RUN_ID = "monitor-run-01"
SESSION_ID = "pd-goal-sess-1"
TURN_ID = "turn-goal-1"
EXEC_TURN_ID = "turn-goal-exec"
EXECUTION_ID = "exec-goal-1"
PROPOSAL_ID = "prop-goal-1"
PLAN_SHA = "a" * 64
REPORT_SHA = "b" * 64
CONTRACT_HASH = "c" * 64


def goal_report(**overrides) -> dict:
    """Plus Host `GoalReport` 的真实形状（camelCase，§5.23.10 T5-A）。"""
    report = {
        "reportId": "report-1",
        "sessionId": SESSION_ID,
        "executionId": EXECUTION_ID,
        "proposalId": PROPOSAL_ID,
        "turnId": EXEC_TURN_ID,
        "contractHash": CONTRACT_HASH,
        "status": "ready",
        "integrity": {"kind": "structured", "issues": []},
        "execution": {"status": "completed", "endedAt": util.now_iso()},
        "verdict": "met",
        "criteria": [
            {"id": "c1", "text": "计划验收一"},
            {"id": "c2", "text": "计划验收二"},
        ],
        "checks": [{"id": "k1", "text": "测试通过", "status": "passed"}],
        "evidences": [{"id": "e1", "text": "pytest 输出", "kind": "log"}],
        "limitations": [],
        "nextSteps": [],
    }
    report.update(overrides)
    return report


def report_payload(report: dict, report_sha256=REPORT_SHA) -> dict:
    payload = dict(report)
    payload["report_sha256"] = report_sha256
    return payload


class _GoalReportCase(KanpilotTestCase):
    """共享夹具：一轮已批准 Goal 契约的 pi_desktop 轮次 + monitor claim。"""

    def setUp(self):
        super().setUp()
        self.server = _goal_server()
        self.addCleanup(self.server.stop)
        self.runs_root = self.root / "session_runs"
        self.runs_root.mkdir(parents=True, exist_ok=True)
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                db.set_setting(
                    conn, "session_runs_root", str(self.runs_root)
                )
        finally:
            conn.close()

    # --------------------------------------------------------------- 夹具
    def write_connection(self, **overrides) -> Path:
        data = {
            "active": True,
            "url": self.server.url,
            "token": self.server.token,
            "pid": __import__("os").getpid(),
            "startedAt": util.now_iso(),
        }
        data.update(overrides)
        path = self.root / "pi-desktop" / "mcp-control.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        path.chmod(0o600)
        return path

    def seed_round(self, connection_file=None, *, backend="pi_desktop",
                   plan_sha256=PLAN_SHA, status="running",
                   goal_approval=True, run_status="finished",
                   turn_id=None, turn_adopted=None, session_id=SESSION_ID):
        """登记任务 + 轮次 + job.json/events.jsonl（隔离运行记录）。"""
        connection_file = connection_file or self.write_connection()
        run_dir = self.runs_root / TASK_ID / RUN_ID
        run_dir.mkdir(parents=True, exist_ok=True)
        now = util.now_iso()
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                conn.execute(
                    "INSERT OR REPLACE INTO tasks(task_id, project_id, "
                    "project_name, title, repository, stage, status, run_id, "
                    "session_marker, plan_path, plan_sha256, created_at, "
                    "updated_at, save_state) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'active')",
                    (TASK_ID, "example-project-05", "Feedback",
                     "修复计划验收", str(self.repos_root / "Feedback"),
                     "monitor", status, RUN_ID, f"{TASK_ID}/{RUN_ID}",
                     str(self.plan_root / "Feedback" / "p.md"), plan_sha256,
                     now, now),
                )
                round_store.upsert_on_claim(
                    conn, TASK_ID, RUN_ID,
                    {"backend": backend,
                     "connection_file": str(connection_file),
                     "provider_id": "test-provider",
                     "model_id": "test-model", "thinking_level": "low",
                     "permission_mode": "ask"},
                    now, backend=backend,
                )
                conn.execute(
                    "UPDATE execution_rounds SET run_status=?, started_at=? "
                    "WHERE task_id=? AND run_id=?",
                    (run_status, now, TASK_ID, RUN_ID),
                )
        finally:
            conn.close()
        job = {
            "task_id": TASK_ID, "run_id": RUN_ID, "backend": "pi_desktop",
            "session_id": session_id, "session_title": "Goal 会话",
            "turn_id": turn_id or TURN_ID, "send_state": "sent",
            "config": {"connection_file": str(connection_file)},
            "pid": 999999, "status": "finished",
            "started_at": now, "ended_at": now, "exit_code": 0, "detail": None,
        }
        if turn_adopted:
            job["turn_adopted"] = turn_adopted
        if goal_approval:
            job["goal_approval"] = {
                "proposal_id": PROPOSAL_ID, "title": "目标契约",
                "status": "approved", "planning_turn_id": TURN_ID,
                "execution_turn_id": EXEC_TURN_ID,
                "target_permission_mode": "ask",
                "approved_at": now, "approved_by": "kaneopilot",
                "execution_id": EXECUTION_ID,
            }
        (run_dir / dsh_events.JOB_NAME).write_text(
            json.dumps(job, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        events = [
            {"v": 1, "seq": 1, "ts": now, "type": "session",
             "session_id": session_id, "session_title": "Goal 会话",
             "turn_id": job["turn_id"]},
            {"v": 1, "seq": 2, "ts": now, "type": "user", "content": "任务"},
            {"v": 1, "seq": 3, "ts": now, "type": "terminal",
             "outcome": "completed", "reason": "completed"},
        ]
        (run_dir / dsh_events.EVENT_LOG_NAME).write_text(
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events),
            encoding="utf-8",
        )
        return run_dir, connection_file

    def seed_monitor_claim(self, *, executor="kaneo-monitor",
                           lease_expires_at=None, status="claimed"):
        """登记本端 monitor 请求（默认有效 claim + 有效租约）。"""
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                request, _ = db.create_request(
                    conn, util.now_iso(), "monitor", f"monitor:{TASK_ID}",
                    task_id=TASK_ID, requested_by="test",
                )
                conn.execute(
                    "UPDATE requests SET status=?, run_id=?, claimed_by=?, "
                    "claimed_at=?, lease_expires_at=? WHERE request_id=?",
                    (status, MONITOR_RUN_ID, executor, util.now_iso(),
                     lease_expires_at or util.plus_seconds(1200, util.now_dt()),
                     request["request_id"]),
                )
            return request["request_id"]
        finally:
            conn.close()

    def set_report(self, report=None, sha=REPORT_SHA, mode="ok"):
        self.server.goal_report = (
            report_payload(report, sha) if report is not None else None
        )
        self.server.goal_report_mode = mode

    def call_err_code(self, command, payload):
        exc = self.call_err(command, payload)
        return exc.code, str(exc.message)

    # ------------------------------------------------------- record 助手
    def record(self, request_id, *, decision="complete", reviews=None,
               reason="计划验收逐项通过", **overrides):
        payload = {
            "task_id": TASK_ID,
            "execution_run_id": RUN_ID,
            "monitor_request_id": request_id,
            "monitor_run_id": MONITOR_RUN_ID,
            "plan_sha256": PLAN_SHA,
            "report_id": "report-1",
            "report_sha256": REPORT_SHA,
            "decision": decision,
            "criterion_reviews": reviews if reviews is not None else [
                {"id": "c1", "result": "met", "reason": "测试通过",
                 "evidence_refs": ["k1", "e1"]},
                {"id": "c2", "result": "met", "reason": "构建通过",
                 "evidence_refs": []},
            ],
            "reason": reason,
        }
        payload.update(overrides)
        return self.call("record-goal-evaluation", payload)


def _goal_server():
    """复用 ``test_pi_desktop_backend`` 的 fake MCP server（同线程内对象）。"""
    from test_pi_desktop_backend import FakeMCPServer
    server = FakeMCPServer()
    server.session_id = SESSION_ID
    server.protocol_version = "2025-06-18"
    # T5：fake Plus 同时暴露只读 Goal 报告工具（真实 Plus 版本的形状）。
    for name in pi_desktop_backend.GOAL_REPORT_LIST_TOOL, \
            pi_desktop_backend.GOAL_REPORT_GET_TOOL:
        if name not in server.tools:
            server.tools.append(name)
    server.httpd.tools = server.tools
    return server.start()


# ======================================================= T5-A 报告读取分态

class TestGoalReportReadStates(_GoalReportCase):
    def test_ready_structured_report_is_returned_with_identity(self):
        self.seed_round()
        self.set_report(goal_report())
        out = self.call("get-goal-report",
                        {"task_id": TASK_ID, "run_id": RUN_ID})
        self.assertEqual(out["state"], "ready")
        self.assertEqual(out["report"]["sessionId"], SESSION_ID)
        self.assertEqual(out["report_sha256"], REPORT_SHA)
        self.assertEqual(out["proposal_id"], PROPOSAL_ID)
        self.assertEqual(out["execution_id"], EXECUTION_ID)
        self.assertEqual(out["turn_id"], EXEC_TURN_ID)
        self.assertEqual(out["integrity_kind"], "structured")
        self.assertEqual(out["execution_status"], "completed")
        self.assertEqual(out["verdict"], "met")
        self.assertTrue(out["read_only"])
        # plan_sha256 与 contractHash 各管各的文件：不得相同/被比较。
        self.assertEqual(out["plan_sha256"], PLAN_SHA)
        self.assertNotEqual(out["plan_sha256"],
                            out["report"]["contractHash"])

    def test_fallback_report_is_a_ready_report_without_completion(self):
        self.seed_round()
        self.set_report(goal_report(
            integrity={"kind": "fallback", "issues": ["no draft"]},
            verdict="unknown",
        ))
        out = self.call("get-goal-report",
                        {"task_id": TASK_ID, "run_id": RUN_ID})
        self.assertEqual(out["state"], "ready", "fallback 是成功返回的报告")
        self.assertEqual(out["integrity_kind"], "fallback")
        self.assertEqual(out["verdict"], "unknown")

    def test_pending_and_draft_states_carry_no_body(self):
        self.seed_round()
        for state in ("pending", "draft", "failed"):
            with self.subTest(state=state):
                self.set_report(goal_report(status=state))
                out = self.call("get-goal-report",
                                {"task_id": TASK_ID, "run_id": RUN_ID})
                self.assertEqual(out["state"], state)
                self.assertIsNone(out["report"],
                                  "未 ready 的报告不得返回正文")

    def test_not_found_when_plus_has_no_report(self):
        self.seed_round()
        self.set_report(None, mode="error")
        out = self.call("get-goal-report",
                        {"task_id": TASK_ID, "run_id": RUN_ID})
        self.assertEqual(out["state"], "not_found")
        self.assertIsNone(out["report"])

    def test_corrupt_when_schema_or_hash_missing(self):
        self.seed_round()
        self.set_report(goal_report())
        # 缺少可信 report_sha256 → corrupt（不返回正文）。
        self.server.goal_report = dict(self.server.goal_report)
        self.server.goal_report.pop("report_sha256")
        out = self.call("get-goal-report",
                        {"task_id": TASK_ID, "run_id": RUN_ID})
        self.assertEqual(out["state"], "corrupt")
        self.assertIsNone(out["report"])

    def test_truncated_is_explicit(self):
        self.seed_round()
        self.set_report(goal_report())
        self.server.goal_report = {"truncated": True,
                                   "report_sha256": REPORT_SHA}
        out = self.call("get-goal-report",
                        {"task_id": TASK_ID, "run_id": RUN_ID})
        self.assertEqual(out["state"], "truncated")
        self.assertIsNone(out["report"])

    def test_missing_read_only_tool_blocks_with_explicit_reason(self):
        self.seed_round()
        self.server.tools = [
            t for t in self.server.tools
            if t not in pi_desktop_backend.GOAL_REPORT_LIST_TOOL
            and t != pi_desktop_backend.GOAL_REPORT_GET_TOOL
        ]
        from test_pi_desktop_backend import _sync
        _sync(self.server)
        code, message = self.call_err_code(
            "get-goal-report", {"task_id": TASK_ID, "run_id": RUN_ID}
        )
        self.assertEqual(code, "validation_failed")
        self.assertIn("只读报告工具", message)

    def test_read_failure_is_not_reported_as_no_report(self):
        """临时读取失败不得伪称「没有报告」（§5.23.10 T5-D）。"""
        self.seed_round()
        self.set_report(goal_report())
        original = self.server.tool_call

        def broken(name, args):
            if name == pi_desktop_backend.GOAL_REPORT_GET_TOOL:
                return {"isError": True, "structuredContent": {"error": {
                    "code": "INTERNAL",
                    "message": "upstream transport broke"}}}
            return original(name, args)

        self.server.tool_call = broken
        from test_pi_desktop_backend import _sync
        _sync(self.server)
        code, message = self.call_err_code(
            "get-goal-report", {"task_id": TASK_ID, "run_id": RUN_ID}
        )
        self.assertEqual(code, "remote_unavailable")
        self.assertIn("临时读取失败", message)
        self.assertIn("不得当作没有报告", message)

    def test_explicit_not_found_is_a_distinct_state(self):
        """服务端明确 NOT_FOUND → not_found（报告确实不存在）。"""
        self.seed_round()
        self.set_report(None, mode="error")
        out = self.call("get-goal-report",
                        {"task_id": TASK_ID, "run_id": RUN_ID})
        self.assertEqual(out["state"], "not_found")
        self.assertIsNone(out["report"])


# ================================================== T5-B 身份与前置条件

class TestGoalReportIdentity(_GoalReportCase):
    def test_missing_goal_approval_cannot_pass(self):
        self.seed_round(goal_approval=False)
        self.set_report(goal_report())
        code, message = self.call_err_code(
            "get-goal-report", {"task_id": TASK_ID, "run_id": RUN_ID}
        )
        self.assertEqual(code, "conflict")
        self.assertIn(cmd_read.GOAL_REPORT_MISSING_APPROVAL, message)

    def test_non_pi_desktop_round_has_no_goal_report(self):
        self.seed_round(backend="pi")
        self.set_report(goal_report())
        code, message = self.call_err_code(
            "get-goal-report", {"task_id": TASK_ID, "run_id": RUN_ID}
        )
        self.assertEqual(code, "validation_failed")
        self.assertIn(cmd_read.GOAL_REPORT_NOT_PI_DESKTOP, message)

    def test_session_identity_mismatch_is_rejected(self):
        self.seed_round()
        self.set_report(goal_report(sessionId="other-session"))
        code, message = self.call_err_code(
            "get-goal-report", {"task_id": TASK_ID, "run_id": RUN_ID}
        )
        self.assertEqual(code, "conflict")
        self.assertIn(cmd_read.GOAL_REPORT_IDENTITY_MISMATCH, message)
        self.assertIn("sessionId", message)

    def test_proposal_identity_mismatch_is_rejected(self):
        self.seed_round()
        self.set_report(goal_report(proposalId="other-proposal"))
        code, message = self.call_err_code(
            "get-goal-report", {"task_id": TASK_ID, "run_id": RUN_ID}
        )
        self.assertEqual(code, "conflict")
        self.assertIn("proposalId", message)

    def test_execution_identity_mismatch_is_rejected(self):
        self.seed_round()
        self.set_report(goal_report(executionId="other-execution"))
        code, message = self.call_err_code(
            "get-goal-report", {"task_id": TASK_ID, "run_id": RUN_ID}
        )
        self.assertEqual(code, "conflict")
        self.assertIn("executionId", message)

    def test_turn_checked_along_recorded_adoption_chain(self):
        """turnId 允许沿本轮 `turn_adopted` 链核对（§5.23.8）。"""
        adopted = [{"from": EXEC_TURN_ID, "to": "turn-goal-3",
                    "code": "EMPTY_MODEL_RESPONSE", "at": util.now_iso()}]
        self.seed_round(turn_id="turn-goal-3", turn_adopted=adopted)
        self.set_report(goal_report(turnId="turn-goal-3"))
        out = self.call("get-goal-report",
                        {"task_id": TASK_ID, "run_id": RUN_ID})
        self.assertEqual(out["state"], "ready")
        self.assertEqual(out["turn_id"], "turn-goal-3")

    def test_foreign_turn_is_rejected(self):
        self.seed_round()
        self.set_report(goal_report(turnId="turn-from-another-round"))
        code, message = self.call_err_code(
            "get-goal-report", {"task_id": TASK_ID, "run_id": RUN_ID}
        )
        self.assertEqual(code, "conflict")
        self.assertIn("turnId", message)

    def test_cross_round_report_is_rejected_by_execution_id(self):
        """另一轮次（同会话）的报告不得被当成本轮报告。"""
        self.seed_round()
        self.set_report(goal_report(executionId="exec-from-old-round"))
        code, message = self.call_err_code(
            "get-goal-report", {"task_id": TASK_ID, "run_id": RUN_ID}
        )
        self.assertEqual(code, "conflict")


# ==================================================== T5-C 核心硬闸

class TestRecordGoalEvaluation(_GoalReportCase):
    def test_complete_registers_event_with_both_round_identities(self):
        self.seed_round()
        self.set_report(goal_report())
        request_id = self.seed_monitor_claim()
        out = self.record(request_id)
        self.assertTrue(out["ok"])
        self.assertFalse(out["deduplicated"])
        self.assertEqual(out["decision"], "complete")
        self.assertEqual(out["execution_run_id"], RUN_ID)
        self.assertEqual(out["monitor_run_id"], MONITOR_RUN_ID)
        self.assertFalse(out["execution_run_id_is_monitor_run_id"],
                         "两种轮次身份不得互换")
        events = [e for e in self.call("list-events", {
            "task_id": TASK_ID})["events"]
            if e["kind"] == cmd_exec.GOAL_EVALUATION_EVENT]
        self.assertEqual(len(events), 1)
        detail = events[0]["detail"]
        self.assertEqual(detail["task_id"], TASK_ID)
        self.assertEqual(detail["execution_run_id"], RUN_ID)
        self.assertEqual(detail["monitor_run_id"], MONITOR_RUN_ID)
        self.assertEqual(detail["report_sha256"], REPORT_SHA)
        self.assertEqual(detail["plan_sha256"], PLAN_SHA)
        self.assertEqual(len(detail["criterion_reviews"]), 2)
        self.assertIn("k1", detail["evidence_refs"])
        self.assertIn("Goal 报告", events[0]["summary"])

    def test_invalid_monitor_lease_is_rejected(self):
        self.seed_round()
        self.set_report(goal_report())
        request_id = self.seed_monitor_claim(
            lease_expires_at=util.to_iso(
                util.now_dt() - __import__("datetime").timedelta(hours=2)
            )
        )
        code, message = self.call_err_code(
            "record-goal-evaluation",
            {
                "task_id": TASK_ID, "execution_run_id": RUN_ID,
                "monitor_request_id": request_id,
                "monitor_run_id": MONITOR_RUN_ID,
                "plan_sha256": PLAN_SHA, "report_id": "report-1",
                "report_sha256": REPORT_SHA, "decision": "complete",
                "criterion_reviews": [
                    {"id": "c1", "result": "met"}],
                "reason": "x",
            },
        )
        self.assertEqual(code, "stale_run")
        self.assertIn("租约", message)
        events = [e for e in self.call("list-events", {
            "task_id": TASK_ID})["events"]
            if e["kind"] == cmd_exec.GOAL_EVALUATION_EVENT]
        self.assertEqual(events, [])

    def test_non_monitor_executor_cannot_register(self):
        self.seed_round()
        self.set_report(goal_report())
        request_id = self.seed_monitor_claim(executor="kaneo-dispatch")
        code, message = self.call_err_code(
            "record-goal-evaluation",
            {
                "task_id": TASK_ID, "execution_run_id": RUN_ID,
                "monitor_request_id": request_id,
                "monitor_run_id": MONITOR_RUN_ID,
                "plan_sha256": PLAN_SHA, "report_id": "report-1",
                "report_sha256": REPORT_SHA, "decision": "complete",
                "criterion_reviews": [{"id": "c1", "result": "met"}],
                "reason": "x",
            },
        )
        self.assertEqual(code, "stale_run")
        self.assertIn("kaneo-monitor", message)

    def test_plan_hash_mismatch_is_rejected(self):
        self.seed_round()
        self.set_report(goal_report())
        request_id = self.seed_monitor_claim()
        code, message = self.call_err_code(
            "record-goal-evaluation",
            {
                "task_id": TASK_ID, "execution_run_id": RUN_ID,
                "monitor_request_id": request_id,
                "monitor_run_id": MONITOR_RUN_ID,
                "plan_sha256": "f" * 64, "report_id": "report-1",
                "report_sha256": REPORT_SHA, "decision": "complete",
                "criterion_reviews": [{"id": "c1", "result": "met"}],
                "reason": "x",
            },
        )
        self.assertEqual(code, "conflict")
        self.assertIn("plan_sha256", message)

    def test_report_hash_mismatch_is_rejected(self):
        self.seed_round()
        self.set_report(goal_report())
        request_id = self.seed_monitor_claim()
        code, message = self.call_err_code(
            "record-goal-evaluation",
            {
                "task_id": TASK_ID, "execution_run_id": RUN_ID,
                "monitor_request_id": request_id,
                "monitor_run_id": MONITOR_RUN_ID,
                "plan_sha256": PLAN_SHA, "report_id": "report-1",
                "report_sha256": "d" * 64, "decision": "complete",
                "criterion_reviews": [{"id": "c1", "result": "met"}],
                "reason": "x",
            },
        )
        self.assertEqual(code, "conflict")
        self.assertIn("report_sha256", message)

    def test_execution_run_id_must_be_current_round(self):
        self.seed_round()
        self.set_report(goal_report())
        request_id = self.seed_monitor_claim()
        code, message = self.call_err_code(
            "record-goal-evaluation",
            {
                "task_id": TASK_ID, "execution_run_id": "other-run",
                "monitor_request_id": request_id,
                "monitor_run_id": MONITOR_RUN_ID,
                "plan_sha256": PLAN_SHA, "report_id": "report-1",
                "report_sha256": REPORT_SHA, "decision": "complete",
                "criterion_reviews": [{"id": "c1", "result": "met"}],
                "reason": "x",
            },
        )
        self.assertEqual(code, "conflict")
        self.assertIn("当前执行轮次", message)


class TestGoalEvaluationBottomLine(_GoalReportCase):
    """T5-C 确定性报告底线：AI 不可能自行升格的项。"""

    def _complete_with(self, report, reviews):
        self.set_report(report)
        request_id = self.seed_monitor_claim()
        return self.call_err_code(
            "record-goal-evaluation",
            {
                "task_id": TASK_ID, "execution_run_id": RUN_ID,
                "monitor_request_id": request_id,
                "monitor_run_id": MONITOR_RUN_ID,
                "plan_sha256": PLAN_SHA, "report_id": "report-1",
                "report_sha256": REPORT_SHA, "decision": "complete",
                "criterion_reviews": reviews, "reason": "AI 判完成",
            },
        )

    def test_ready_but_fallback_cannot_be_complete(self):
        self.seed_round()
        code, message = self._complete_with(
            goal_report(integrity={"kind": "fallback"}),
            [{"id": "c1", "result": "met"}],
        )
        self.assertEqual(code, "conflict")
        self.assertIn("structured", message)

    def test_execution_not_completed_cannot_be_complete(self):
        self.seed_round()
        code, message = self._complete_with(
            goal_report(execution={"status": "running"}),
            [{"id": "c1", "result": "met"}],
        )
        self.assertEqual(code, "conflict")
        self.assertIn("execution.status", message)

    def test_verdict_not_met_cannot_be_complete(self):
        self.seed_round()
        code, message = self._complete_with(
            goal_report(verdict="partial"),
            [{"id": "c1", "result": "met"}],
        )
        self.assertEqual(code, "conflict")
        self.assertIn("verdict", message)

    def test_empty_criterion_reviews_rejected(self):
        self.seed_round()
        code, message = self._complete_with(goal_report(), [])
        self.assertEqual(code, "conflict")
        self.assertIn("审查项为空", message)

    def test_duplicate_criterion_ids_rejected(self):
        self.seed_round()
        code, message = self._complete_with(
            goal_report(),
            [{"id": "c1", "result": "met"}, {"id": "c1", "result": "met"}],
        )
        self.assertEqual(code, "conflict")
        self.assertIn("重复", message)

    def test_non_met_result_rejected(self):
        self.seed_round()
        code, message = self._complete_with(
            goal_report(),
            [
                {"id": "c1", "result": "met"},
                {"id": "c2", "result": "missing", "reason": "未验证"},
            ],
        )
        self.assertEqual(code, "conflict")
        self.assertIn("met", message)
        self.assertIn("c2", message)

    def test_fabricated_criterion_reference_rejected(self):
        self.seed_round()
        code, message = self._complete_with(
            goal_report(),
            [{"id": "c-not-in-report", "result": "met"}],
        )
        self.assertEqual(code, "conflict")
        self.assertIn("不存在的验收项", message)

    def test_fabricated_check_or_evidence_reference_rejected(self):
        self.seed_round()
        code, message = self._complete_with(
            goal_report(),
            [{"id": "c1", "result": "met",
              "evidence_refs": ["check-does-not-exist"]}],
        )
        self.assertEqual(code, "conflict")
        self.assertIn("check/evidence", message)

    def test_ai_claimed_complete_but_report_absent_rejected(self):
        self.seed_round()
        self.set_report(None, mode="error")
        request_id = self.seed_monitor_claim()
        code, message = self.call_err_code(
            "record-goal-evaluation",
            {
                "task_id": TASK_ID, "execution_run_id": RUN_ID,
                "monitor_request_id": request_id,
                "monitor_run_id": MONITOR_RUN_ID,
                "plan_sha256": PLAN_SHA, "report_id": "report-1",
                "report_sha256": REPORT_SHA, "decision": "complete",
                "criterion_reviews": [{"id": "c1", "result": "met"}],
                "reason": "AI 判完成",
            },
        )
        self.assertEqual(code, "conflict")
        self.assertIn("不可用", message)

    def test_incomplete_decision_registers_without_bottom_line(self):
        """AI 判 incomplete 可登记（交人工），但不做完成底线核对。"""
        self.seed_round()
        self.set_report(goal_report(verdict="partial"))
        request_id = self.seed_monitor_claim()
        out = self.record(request_id, decision="incomplete",
                          reviews=[{"id": "c2", "result": "missing"}],
                          reason="计划验收 c2 未验证")
        self.assertTrue(out["ok"])
        self.assertEqual(out["decision"], "incomplete")


class TestGoalEvaluationIdempotency(_GoalReportCase):
    def test_same_signature_is_idempotent(self):
        self.seed_round()
        self.set_report(goal_report())
        first = self.record(self.seed_monitor_claim())
        self.assertFalse(first["deduplicated"])
        # 新 monitor 请求（新 claim）引用同一报告 → 幂等复用原事件。
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                conn.execute(
                    "UPDATE requests SET status='succeeded' "
                    "WHERE run_id=?", (MONITOR_RUN_ID,))
        finally:
            conn.close()
        second_request = self.seed_monitor_claim()
        second = self.record(second_request)
        self.assertTrue(second["deduplicated"])
        self.assertEqual(second["event_id"], first["event_id"])
        events = [e for e in self.call("list-events", {
            "task_id": TASK_ID})["events"]
            if e["kind"] == cmd_exec.GOAL_EVALUATION_EVENT]
        self.assertEqual(len(events), 1, "同一签名不得重复登记")

    def test_new_report_hash_is_a_new_evaluation(self):
        """报告真的变化（新 hash）→ 是新的判定，不幂等复用旧事件。"""
        self.seed_round()
        self.set_report(goal_report())
        first = self.record(self.seed_monitor_claim())
        # Host 发布了一份新报告（hash 变化）。
        self.set_report(goal_report(reportId="report-2"), sha="e" * 64)
        second = self.record(self.seed_monitor_claim(),
                             report_sha256="e" * 64, report_id="report-2")
        self.assertFalse(second["deduplicated"])
        self.assertNotEqual(second["event_id"], first["event_id"])

    def test_new_monitor_request_must_still_hold_a_valid_claim(self):
        """新 monitor 请求可引用既有判定，但必须自己仍是有效 claim。"""
        self.seed_round()
        self.set_report(goal_report())
        self.record(self.seed_monitor_claim())
        # 新请求自己已收尾（非 claimed）→ 即使判定已存在也必须拒绝。
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                conn.execute(
                    "UPDATE requests SET status='succeeded' "
                    "WHERE run_id=?", (MONITOR_RUN_ID,))
        finally:
            conn.close()
        code, message = self.call_err_code(
            "record-goal-evaluation",
            {
                "task_id": TASK_ID, "execution_run_id": RUN_ID,
                "monitor_request_id": self.seed_monitor_claim(
                    status="succeeded"),
                "monitor_run_id": MONITOR_RUN_ID,
                "plan_sha256": PLAN_SHA, "report_id": "report-1",
                "report_sha256": REPORT_SHA, "decision": "complete",
                "criterion_reviews": [{"id": "c1", "result": "met"}],
                "reason": "x",
            },
        )
        self.assertEqual(code, "stale_run")


# ==================================================== 推进硬闸（T5-C）

class TestAdvanceTaskGoalGate(_GoalReportCase):
    """`advance-task in-progress -> in-review` 对 pi_desktop 的硬闸。"""

    def _advance(self, event_id, request_id, **overrides):
        payload = {
            "task_id": TASK_ID, "target": "in-review",
            "expected_current": "in-progress",
            "monitor_request_id": request_id,
            "monitor_run_id": MONITOR_RUN_ID,
            "evaluation_event_id": event_id,
        }
        payload.update(overrides)
        return self.call_err_code("advance-task", payload)

    def _register(self):
        self.seed_round()
        self.set_report(goal_report())
        request_id = self.seed_monitor_claim()
        out = self.record(request_id)
        return request_id, out["event_id"]

    def test_direct_call_without_monitor_material_is_rejected(self):
        self.seed_round()
        self.set_report(goal_report())
        code, message = self.call_err_code(
            "advance-task",
            {"task_id": TASK_ID, "target": "in-review",
             "expected_current": "in-progress"},
        )
        self.assertEqual(code, "conflict")
        self.assertIn("绕过 monitor", message)
        # 远端未动。
        self.assertEqual(
            self.fake.tasks.get(TASK_ID)["status"]
            if TASK_ID in self.fake.tasks else None, None
        )

    def test_valid_material_passes_and_touches_remote_once(self):
        self.seed_round()
        self.set_report(goal_report())
        request_id = self.seed_monitor_claim()
        out = self.record(request_id)
        # advance-task 需要远端任务存在（in-progress）。
        self.fake.add_task(
            TASK_ID, "example-project-05", status="in-progress"
        )
        result = self.call("advance-task", {
            "task_id": TASK_ID, "target": "in-review",
            "expected_current": "in-progress",
            "monitor_request_id": request_id,
            "monitor_run_id": MONITOR_RUN_ID,
            "evaluation_event_id": out["event_id"],
        })
        self.assertEqual(result["kaneo_status"], "in-review")
        self.assertEqual(self.fake.tasks[TASK_ID]["status"], "in-review")

    def test_new_monitor_claim_reuses_completed_claim_evaluation_and_advances(self):
        """M1 收尾后，M2 复用同签名的可信判定仍可进入 Review。"""
        self.seed_round()
        self.set_report(goal_report())
        first_request = self.call("enqueue", {
            "task_id": TASK_ID, "kind": "monitor",
        })["request"]["request_id"]
        first_claim = self.call("claim", {
            "stage": "monitor", "executor": "kaneo-monitor",
            "request_id": first_request,
        })
        self.assertTrue(first_claim["claimed"])
        first = self.record(first_request,
                            monitor_run_id=first_claim["run_id"])
        self.call("report", {
            "request_id": first_request, "run_id": first_claim["run_id"],
            "outcome": "succeeded",
        })
        second_request = self.call("enqueue", {
            "task_id": TASK_ID, "kind": "monitor",
        })["request"]["request_id"]
        second_claim = self.call("claim", {
            "stage": "monitor", "executor": "kaneo-monitor",
            "request_id": second_request,
        })
        self.assertTrue(second_claim["claimed"])
        self.assertNotEqual(second_request, first_request)
        self.assertNotEqual(second_claim["run_id"], first_claim["run_id"])
        second = self.record(second_request,
                             monitor_run_id=second_claim["run_id"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(second["event_id"], first["event_id"])
        self.fake.add_task(
            TASK_ID, "example-project-05", status="in-progress"
        )
        result = self.call("advance-task", {
            "task_id": TASK_ID, "target": "in-review",
            "expected_current": "in-progress",
            "monitor_request_id": second_request,
            "monitor_run_id": second_claim["run_id"],
            "evaluation_event_id": second["event_id"],
        })
        self.assertEqual(result["task_status"], "in_review")
        self.assertEqual(self.fake.tasks[TASK_ID]["status"], "in-review")
        events = self.call("list-events", {"task_id": TASK_ID})["events"]
        evaluations = [e for e in events
                       if e["kind"] == cmd_exec.GOAL_EVALUATION_EVENT]
        self.assertEqual(len(evaluations), 1)
        self.assertEqual(evaluations[0]["detail"]["monitor_request_id"],
                         first_request)
        adoption = [e["detail"] for e in events
                    if e["detail"].get("evaluation_event_id") == first["event_id"]]
        self.assertEqual(len(adoption), 1)
        self.assertEqual(adoption[0]["monitor_request_id"], second_request)
        self.assertEqual(adoption[0]["monitor_run_id"], second_claim["run_id"])

    def test_event_without_monitor_registration_provenance_is_rejected(self):
        request_id, event_id = self._register()
        self.fake.add_task(
            TASK_ID, "example-project-05", status="in-progress"
        )
        for field, value in (("actor", "executor"), ("request_id", None)):
            with self.subTest(field=field):
                conn = db.connect(self.db_path)
                try:
                    with db.write_txn(conn):
                        conn.execute(
                            f"UPDATE events SET {field}=? WHERE id=?",
                            (value, event_id),
                        )
                    code, message = self._advance(event_id, request_id)
                    self.assertEqual(code, "conflict")
                    self.assertIn("登记归属", message)
                    self.assertEqual(self.fake.requests_log, [])
                    self.assertEqual(self.fake.tasks[TASK_ID]["status"],
                                     "in-progress")
                finally:
                    with db.write_txn(conn):
                        conn.execute(
                            "UPDATE events SET actor='kaneo-monitor', "
                            "request_id=? WHERE id=?", (request_id, event_id),
                        )
                    conn.close()

    def test_stale_monitor_claim_is_rejected_before_remote(self):
        request_id, event_id = self._register()
        self.fake.add_task(
            TASK_ID, "example-project-05", status="in-progress"
        )
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                conn.execute(
                    "UPDATE requests SET lease_expires_at=? WHERE "
                    "request_id=?",
                    (util.to_iso(util.now_dt()
                                 - __import__("datetime").timedelta(
                                     hours=3)),
                     request_id))
        finally:
            conn.close()
        code, message = self._advance(event_id, request_id)
        self.assertEqual(code, "stale_run")
        self.assertEqual(self.fake.tasks[TASK_ID]["status"], "in-progress")

    def test_old_evaluation_from_another_round_is_rejected(self):
        request_id, event_id = self._register()
        self.fake.add_task(
            TASK_ID, "example-project-05", status="in-progress"
        )
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET run_id='new-round' WHERE task_id=?",
                    (TASK_ID,))
                round_store.upsert_on_claim(
                    conn, TASK_ID, "new-round",
                    {"backend": "pi_desktop"}, util.now_iso(),
                    backend="pi_desktop",
                )
        finally:
            conn.close()
        code, message = self._advance(event_id, request_id)
        self.assertEqual(code, "conflict")
        self.assertEqual(self.fake.tasks[TASK_ID]["status"], "in-progress")

    def test_changed_report_is_rejected(self):
        request_id, event_id = self._register()
        self.fake.add_task(
            TASK_ID, "example-project-05", status="in-progress"
        )
        # 报告在上次判定后被改写（hash 变化）。
        self.set_report(goal_report(), sha="9" * 64)
        code, message = self._advance(event_id, request_id)
        self.assertEqual(code, "conflict")
        self.assertIn("report_sha256", message)
        self.assertEqual(self.fake.tasks[TASK_ID]["status"], "in-progress")

    def test_non_completed_execution_terminal_is_rejected(self):
        request_id, event_id = self._register()
        self.fake.add_task(
            TASK_ID, "example-project-05", status="in-progress"
        )
        # 执行终态被改成 failed（job 与 terminal 都改）。
        run_dir = self.runs_root / TASK_ID / RUN_ID
        job = json.loads(
            (run_dir / dsh_events.JOB_NAME).read_text(encoding="utf-8")
        )
        job.update({"status": "failed", "exit_code": 1})
        (run_dir / dsh_events.JOB_NAME).write_text(
            json.dumps(job, ensure_ascii=False), encoding="utf-8"
        )
        events = [
            json.loads(line)
            for line in (run_dir / dsh_events.EVENT_LOG_NAME).read_text(
                encoding="utf-8").splitlines() if line.strip()
        ]
        events[-1] = {"v": 1, "seq": events[-1]["seq"],
                      "ts": events[-1]["ts"], "type": "terminal",
                      "outcome": "failed", "reason": "boom"}
        (run_dir / dsh_events.EVENT_LOG_NAME).write_text(
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events),
            encoding="utf-8",
        )
        code, message = self._advance(event_id, request_id)
        self.assertEqual(code, "conflict")
        self.assertIn("终态", message)
        self.assertEqual(self.fake.tasks[TASK_ID]["status"], "in-progress")

    def test_event_from_another_monitor_claim_is_rejected(self):
        """判定中的原 monitor_run_id 必须匹配历史登记请求。"""
        request_id, event_id = self._register()
        self.fake.add_task(
            TASK_ID, "example-project-05", status="in-progress"
        )
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                row = conn.execute(
                    "SELECT detail_json FROM events WHERE kind=? ORDER BY id "
                    "DESC LIMIT 1", (cmd_exec.GOAL_EVALUATION_EVENT,)
                ).fetchone()
                detail = util.jload(row["detail_json"])
                detail["monitor_run_id"] = "other-monitor-run"
                conn.execute(
                    "UPDATE events SET detail_json=? WHERE kind=? AND id="
                    "(SELECT MAX(id) FROM events WHERE kind=?)",
                    (util.jdump(detail), cmd_exec.GOAL_EVALUATION_EVENT,
                     cmd_exec.GOAL_EVALUATION_EVENT),
                )
        finally:
            conn.close()
        code, message = self._advance(event_id, request_id)
        self.assertEqual(code, "conflict")
        self.assertIn("旧判定", message)
        self.assertEqual(self.fake.tasks[TASK_ID]["status"], "in-progress")


# ==================================================== 旧后端不变（pi）

class TestPiBackendUnchanged(_GoalReportCase):
    def test_pi_round_advance_keeps_old_interface(self):
        """pi 后端轮次不要求 monitor claim/evaluation event（接口不变）。"""
        self.seed_round(backend="pi")
        self.fake.add_task(
            TASK_ID, "example-project-05", status="in-progress"
        )
        result = self.call("advance-task", {
            "task_id": TASK_ID, "target": "in-review",
            "expected_current": "in-progress",
        })
        self.assertEqual(result["kaneo_status"], "in-review")
        self.assertEqual(self.fake.tasks[TASK_ID]["status"], "in-review")

    def test_review_precondition_preserved_for_pi(self):
        self.seed_round(backend="pi")
        self.fake.add_task(
            TASK_ID, "example-project-05", status="in-progress"
        )
        code, message = self.call_err_code("advance-task", {
            "task_id": TASK_ID, "target": "done",
            "expected_current": "in-progress",
        })
        self.assertEqual(code, "conflict")
        self.assertIn("review", message.lower())

    def _assert_history_untouched(self, status):
        """历史任务保持原状态：只读报告命令不改写、不降级（T5-D）。"""
        self.seed_round(backend="pi", status=status)
        task = self.call("get-task", {"task_id": TASK_ID})["task"]
        self.assertEqual(task["status"], status)
        # 只读报告命令对历史 pi 轮次明确拒绝，不改写状态。
        self.call_err("get-goal-report",
                      {"task_id": TASK_ID, "run_id": RUN_ID})
        after = self.call("get-task", {"task_id": TASK_ID})["task"]
        self.assertEqual(after["status"], status)

    def test_historical_in_review_round_is_not_rewritten(self):
        self._assert_history_untouched("in_review")

    def test_historical_done_round_is_not_rewritten(self):
        self._assert_history_untouched("done")

    def test_historical_needs_user_round_is_not_rewritten(self):
        self._assert_history_untouched("needs_user")


# ================================================== 采集修补（Bash）

class TestBashExitCodeFidelity(KanpilotTestCase):
    def test_uppercase_bash_is_normalized_and_exit_code_preserved(self):
        """真实形状 `Bash` → command_output + exit_code 保真（§5.23.10）。"""
        log = pi_desktop_worker._EventLog(self.root / "events.jsonl")
        collector = pi_desktop_worker._Collector(object(), "s", log, "")
        collector.gate_open = True
        collector._emit({
            "id": "msg-bash-1", "role": "toolResult", "toolName": "Bash",
            "toolStatus": "success", "toolCallId": "call-1",
            "toolArgs": {"command": "pytest -q"},
            "toolResult": {"content": [{"type": "text", "text": "ok"}],
                           "details": {"exitCode": 0}},
            "toolCompletedAt": util.now_iso(), "isError": False,
        })
        events = read_events(log)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["type"], "command_output")
        self.assertEqual(event["exit_code"], 0)
        self.assertEqual(event["name"], "Bash")

    def test_uppercase_bash_exit_code_from_raw_output(self):
        log = pi_desktop_worker._EventLog(self.root / "events2.jsonl")
        collector = pi_desktop_worker._Collector(object(), "s", log, "")
        collector.gate_open = True
        collector._emit({
            "id": "msg-bash-2", "role": "toolResult", "toolName": "Bash",
            "toolStatus": "error", "toolCallId": "call-2",
            "toolArgs": {"command": "false"},
            "toolResult": {"exitCode": 3},
            "toolCompletedAt": util.now_iso(), "isError": True,
        })
        events = read_events(log)
        self.assertEqual(events[0]["type"], "command_output")
        self.assertEqual(events[0]["exit_code"], 3)

    def test_is_shell_tool_is_case_insensitive(self):
        for name in ("Bash", "bash", "SHELL", "Run_Command", "terminal"):
            self.assertTrue(pi_desktop_worker._is_shell_tool(name), name)
        for name in ("read", "Read", "edit", ""):
            self.assertFalse(pi_desktop_worker._is_shell_tool(name), name)


def read_events(log) -> list:
    events, _detail = dsh_events.iter_events(log.path)
    return events


class TestGoalDecisionVariants(_GoalReportCase):
    """AI 三种判定与报告自称 partial/blocked/unknown 的边界。"""

    def _decide(self, report, decision, reviews=None):
        self.set_report(report)
        request_id = self.seed_monitor_claim()
        return self.record(
            request_id, decision=decision,
            reviews=reviews if reviews is not None else
            [{"id": "c1", "result": "met"}],
            reason=f"AI 判 {decision}",
        )

    def test_report_claims_partial_and_ai_says_incomplete(self):
        self.seed_round()
        out = self._decide(goal_report(verdict="partial"), "incomplete")
        self.assertTrue(out["ok"])
        self.assertEqual(out["decision"], "incomplete")

    def test_report_claims_blocked_and_ai_says_incomplete(self):
        self.seed_round()
        out = self._decide(goal_report(verdict="blocked"), "incomplete",
                           reviews=[{"id": "c1", "result": "failed"}])
        self.assertTrue(out["ok"])
        self.assertEqual(out["decision"], "incomplete")

    def test_report_claims_unknown_and_ai_says_uncertain(self):
        self.seed_round()
        out = self._decide(goal_report(verdict="unknown"), "uncertain")
        self.assertTrue(out["ok"])
        self.assertEqual(out["decision"], "uncertain")

    def test_ai_uncertain_is_never_a_completion(self):
        """AI uncertain 不得被硬闸当完成放行（进入 in-review 必须 complete）。"""
        self.seed_round()
        self.set_report(goal_report())
        request_id = self.seed_monitor_claim()
        out = self.record(request_id, decision="uncertain",
                          reviews=[{"id": "c1", "result": "unknown"}],
                          reason="报告无法解释的证据缺口")
        self.assertEqual(out["decision"], "uncertain")
        self.fake.add_task(
            TASK_ID, "example-project-05", status="in-progress"
        )
        code, message = self.call_err_code("advance-task", {
            "task_id": TASK_ID, "target": "in-review",
            "expected_current": "in-progress",
            "monitor_request_id": request_id,
            "monitor_run_id": MONITOR_RUN_ID,
            "evaluation_event_id": out["event_id"],
        })
        self.assertEqual(code, "conflict")
        self.assertIn("decision", message)
        self.assertEqual(self.fake.tasks[TASK_ID]["status"], "in-progress")

    def test_display_names_never_used_for_identity_or_completion(self):
        """展示名只用于展示：同名不同轮次仍是不同身份/路径（T6）。"""
        self.seed_round()
        self.set_report(goal_report())
        out = self.call("get-goal-report",
                        {"task_id": TASK_ID, "run_id": RUN_ID})
        # 身份全部来自机器字段：session/proposal/execution/turn/plan hash。
        self.assertEqual(out["session_id"], SESSION_ID)
        self.assertEqual(out["proposal_id"], PROPOSAL_ID)
        self.assertEqual(out["execution_id"], EXECUTION_ID)
        self.assertEqual(out["plan_sha256"], PLAN_SHA)
        # 报告里即使带着与任务同名的会话标题，也不参与身份判定。
        self.set_report(goal_report(sessionTitle="修复计划验收"))
        again = self.call("get-goal-report",
                          {"task_id": TASK_ID, "run_id": RUN_ID})
        self.assertEqual(again["state"], "ready")
        self.assertEqual(again["session_id"], SESSION_ID)


# ==================================================== T5-D 无报告处理

class TestWorkerGoalReportSettlement(_GoalReportCase):
    """worker 终态落地后的只读报告宽限（不重发 prompt）。"""

    def setUp(self):
        super().setUp()
        self.connection = self.write_connection()

    def _job(self, **overrides):
        job = {
            "task_id": TASK_ID, "run_id": RUN_ID, "backend": "pi_desktop",
            "session_id": SESSION_ID, "turn_id": EXEC_TURN_ID,
            "send_state": "sent",
            "goal_approval": {
                "proposal_id": PROPOSAL_ID, "status": "approved",
                "planning_turn_id": TURN_ID,
                "execution_turn_id": EXEC_TURN_ID,
                "execution_id": EXECUTION_ID,
            },
        }
        job.update(overrides)
        return job

    def _client(self):
        record = pi_desktop_backend.load_connection(self.connection)
        client = pi_desktop_backend.MCPClient(record["url"], record["token"])
        client.connect()
        return client

    def _settle(self, job):
        return pi_desktop_worker._goal_report_settlement(self._client(), job)

    def test_structured_report_settles_with_identity(self):
        self.set_report(goal_report())
        job = self._job()
        detail, reason = self._settle(job)
        self.assertIsNone(detail)
        self.assertIsNone(reason)
        self.assertEqual(job["goal_report"]["report_sha256"], REPORT_SHA)
        self.assertEqual(job["goal_report"]["integrity_kind"], "structured")
        self.assertEqual(job["goal_report"]["execution_status"], "completed")

    def test_fallback_report_is_a_concrete_gap(self):
        self.set_report(goal_report(integrity={"kind": "fallback"}))
        detail, reason = self._settle(self._job())
        self.assertIn("fallback", detail)
        self.assertEqual(reason, "execution_failed")

    def test_no_report_after_grace_is_a_concrete_gap(self):
        self.server.goal_report = None
        self.server.goal_report_mode = "error"
        original = pi_desktop_worker._GOAL_REPORT_GRACE_SECONDS
        pi_desktop_worker._GOAL_REPORT_GRACE_SECONDS = 0.0
        try:
            detail, reason = self._settle(self._job())
        finally:
            pi_desktop_worker._GOAL_REPORT_GRACE_SECONDS = original
        self.assertIn("goal_report_missing", detail)
        self.assertIn("没有结构化完成报告", detail)
        self.assertEqual(reason, "execution_failed")

    def test_pending_within_grace_then_ready(self):
        """`pending` 在宽限内继续只读等待；发布后即收敛（不重发 prompt）。"""
        self.set_report(goal_report(status="pending"))
        job = self._job()
        original = pi_desktop_worker._GOAL_REPORT_GRACE_SECONDS

        def publish_soon():
            import time as _t
            _t.sleep(0.2)
            self.set_report(goal_report())

        import threading
        pi_desktop_worker._GOAL_REPORT_GRACE_SECONDS = 3.0
        thread = threading.Thread(target=publish_soon, daemon=True)
        thread.start()
        try:
            detail, reason = self._settle(job)
        finally:
            pi_desktop_worker._GOAL_REPORT_GRACE_SECONDS = original
            thread.join(timeout=5)
        self.assertIsNone(detail)
        self.assertIsNone(reason)
        self.assertEqual(job["goal_report"]["report_sha256"], REPORT_SHA)

    def test_pending_past_grace_is_a_concrete_gap(self):
        self.set_report(goal_report(status="pending"))
        original = pi_desktop_worker._GOAL_REPORT_GRACE_SECONDS
        pi_desktop_worker._GOAL_REPORT_GRACE_SECONDS = 0.0
        try:
            detail, reason = self._settle(self._job())
        finally:
            pi_desktop_worker._GOAL_REPORT_GRACE_SECONDS = original
        self.assertIn("pending", detail)
        self.assertEqual(reason, "execution_failed")

    def test_corrupt_report_reason_differs_from_verified_failure(self):
        self.set_report(goal_report())
        # report_sha256 非 64 位十六进制 → corrupt（schema/哈希不符）。
        self.server.goal_report = dict(self.server.goal_report)
        self.server.goal_report["report_sha256"] = "not-a-sha"
        original = pi_desktop_worker._GOAL_REPORT_GRACE_SECONDS
        pi_desktop_worker._GOAL_REPORT_GRACE_SECONDS = 0.0
        try:
            detail, reason = self._settle(self._job())
        finally:
            pi_desktop_worker._GOAL_REPORT_GRACE_SECONDS = original
        self.assertIn("corrupt", detail)
        self.assertEqual(reason, "report_unreadable")
        self.assertNotEqual(reason, "execution_failed")

    def test_upgrade_time_running_old_round_without_report(self):
        """升级时仍 running 的旧 Plus 轮次（无 executionId 记录）→ 缺报告。"""
        self.server.goal_reports = []
        original = pi_desktop_worker._GOAL_REPORT_GRACE_SECONDS
        pi_desktop_worker._GOAL_REPORT_GRACE_SECONDS = 0.0
        job = self._job(goal_approval={
            "proposal_id": PROPOSAL_ID, "status": "approved",
            "planning_turn_id": TURN_ID, "execution_turn_id": EXEC_TURN_ID,
        })
        try:
            detail, reason = self._settle(job)
        finally:
            pi_desktop_worker._GOAL_REPORT_GRACE_SECONDS = original
        self.assertIn("goal_report_missing", detail)
        self.assertEqual(reason, "execution_failed")

    def test_old_round_with_listing_but_report_published_is_accepted(self):
        """旧轮次无 executionId：只读 list 按 proposalId+turn 收敛到唯一报告。"""
        self.server.goal_reports = [{
            "reportId": "report-1", "proposalId": PROPOSAL_ID,
            "turnId": EXEC_TURN_ID, "executionId": EXECUTION_ID,
        }]
        self.set_report(goal_report())
        job = self._job(goal_approval={
            "proposal_id": PROPOSAL_ID, "status": "approved",
            "planning_turn_id": TURN_ID, "execution_turn_id": EXEC_TURN_ID,
        })
        detail, reason = self._settle(job)
        self.assertIsNone(detail)
        self.assertIsNone(reason)

    def test_no_goal_approval_means_no_report_gap(self):
        """无已批准 Goal 契约的轮次不是 Canvas 报告场景（历史轮次语义）。"""
        detail, reason = self._settle(
            self._job(goal_approval=None)
        )
        self.assertIsNone(detail)
        self.assertIsNone(reason)


# ==================================================== T6（独立基类）

class T6Case(KanpilotTestCase):
    """T6 夹具：真实 dispatch 链路 + fake MCP（无真实 Plus）。"""

    TASK_ID = "tasknaming00000000000001"

    def call_err_code(self, command, payload):
        exc = self.call_err(command, payload)
        return exc.code, str(exc.message)

    def setUp(self):
        super().setUp()
        from test_pi_desktop_backend import FakeMCPServer
        self.server = FakeMCPServer().start()
        self.addCleanup(self.server.stop)
        self.runs_root = self.root / "session_runs"
        self.runs_root.mkdir(parents=True, exist_ok=True)
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                db.set_setting(conn, "session_runs_root", str(self.runs_root))
        finally:
            conn.close()

    def write_connection(self) -> Path:
        import os
        data = {
            "active": True, "url": self.server.url, "token": self.server.token,
            "pid": os.getpid(), "startedAt": util.now_iso(),
        }
        path = self.root / "pi-desktop" / "mcp-control.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        path.chmod(0o600)
        return path

    def seed_claimed_dispatch(self, run_id=RUN_ID):
        """登记一条 claimed dispatch 请求（worker heartbeat 目标）。"""
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                request, _ = db.create_request(
                    conn, util.now_iso(), "dispatch",
                    f"dispatch:{run_id}", task_id=None, requested_by="test",
                )
                conn.execute(
                    "UPDATE requests SET status='claimed', run_id=?, "
                    "claimed_by='kaneo-dispatch', lease_expires_at=? "
                    "WHERE request_id=?",
                    (run_id, util.plus_seconds(1200, util.now_dt()),
                     request["request_id"]),
                )
            return request["request_id"]
        finally:
            conn.close()

    def worker_spec(self, run_dir, plan, cwd, conn_file, **overrides):
        """worker spec（真实分支；含 request_id/db_path 供 heartbeat）。"""
        spec = {
            "v": 1, "task_id": TASK_ID, "run_id": RUN_ID,
            "session_marker": f"{TASK_ID}/{RUN_ID}",
            "backend": "pi_desktop",
            "config": {
                "connection_file": str(conn_file),
                "provider_id": "test-provider", "model_id": "test-model",
                "thinking_level": "low", "permission_mode": "ask",
            },
            "cwd": str(cwd), "repository": str(self.repos_root / "Feedback"),
            "plan_path": str(plan),
            "plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
            "instruction": "任务标识：测试。",
            "runs_dir": str(run_dir), "timeout_seconds": 30.0,
            "request_id": self.seed_claimed_dispatch(),
            "db_path": str(self.db_path),
        }
        spec.update(overrides)
        return spec

    def seed_ready(self, *, title="修复计划验收", project_name="Feedback"):
        from test_pi_desktop_backend import _sync
        _sync(self.server)
        conn_file = self.write_connection()
        self.call("set-exec-config", {
            "backend": "pi_desktop",
            "pi_desktop": {
                "connection_file": str(conn_file),
                "provider_id": "test-provider", "model_id": "test-model",
                "thinking_level": "low", "permission_mode": "ask",
            },
        })
        self.make_ready_task(task_id=self.TASK_ID,
                             project_name=project_name)
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET title=? WHERE task_id=?",
                    (title, self.TASK_ID))
        finally:
            conn.close()
        return conn_file


class TestT6WorkspaceNaming(T6Case):
    def test_spec_freezes_display_snapshot_for_new_round(self):
        """新轮次 spec.json 固化 workspace/session 展示名（§5.23.10 T6）。"""
        conn_file = self.seed_ready(title="随机歌曲右侧按钮应随机新歌")
        self.enqueue_dispatch(self.TASK_ID)
        claim = self.call("claim", {
            "stage": "dispatch", "executor": "kaneo-dispatch"})
        self.assertTrue(claim["claimed"], claim)
        run_id = claim["run_id"]
        ctx = claim["context"]
        self.call("dispatch-prepare", {
            "task_id": self.TASK_ID, "run_id": run_id,
            "evidence": {
                "session_marker": ctx["session_marker"],
                "repository": ctx["repository"],
                "backend": "pi_desktop",
                "config": ctx["exec_config"],
            },
        })
        self.call("dispatch-begin-send", {
            "task_id": self.TASK_ID, "run_id": run_id})
        from test_pi_desktop_backend import _sync
        self.server.turn_status = "running"
        _sync(self.server)
        with self.subTest("dispatch-start"):
            # worktree 需要 git 仓库；非 git → require_isolated 失败。
            # 本用例只验证「记录展示快照」，用 git 夹具确保 start 走到写
            # spec.json 之前不失败。
            self._init_git_fixture()
        with self.subTest("start"):
            import os as _os
            started = self.call("dispatch-start", {
                "task_id": self.TASK_ID, "run_id": run_id})
            spec = json.loads(
                (self.runs_root / self.TASK_ID / run_id
                 / "spec.json").read_text(encoding="utf-8")
            )
            self.assertEqual(spec["workspace_display_name"], "Feedback")
            self.assertEqual(spec["session_display_name"],
                             "随机歌曲右侧按钮应随机新歌")
            self.addCleanup(self._kill_group, started["pid"])

    def _init_git_fixture(self):
        import subprocess
        repo = self.repos_root / "Feedback"
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email",
                        "t@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"],
                       check=True)
        (repo / ".fixture").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", ".fixture"],
                       check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "f"],
                       check=True)

    def _kill_group(self, pid):
        import os as _os
        import signal
        if not util.pid_alive(pid):
            return
        try:
            _os.killpg(pid, signal.SIGKILL)
        except OSError:
            try:
                _os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def test_old_plus_without_project_name_field_is_blocked(self):
        """旧 Plus 缺 projectName → 明确阻断「版本缺少工作区命名能力」。"""
        self.server.session_naming = False
        self.seed_ready()
        self.enqueue_dispatch(self.TASK_ID)
        claim = self.call("claim", {
            "stage": "dispatch", "executor": "kaneo-dispatch"})
        self.assertTrue(claim["claimed"], claim)
        run_id = claim["run_id"]
        ctx = claim["context"]
        self.call("dispatch-prepare", {
            "task_id": self.TASK_ID, "run_id": run_id,
            "evidence": {
                "session_marker": ctx["session_marker"],
                "repository": ctx["repository"],
                "backend": "pi_desktop",
                "config": ctx["exec_config"],
            },
        })
        self.call("dispatch-begin-send", {
            "task_id": self.TASK_ID, "run_id": run_id})
        self._init_git_fixture()
        code, message = self.call_err_code("dispatch-start", {
            "task_id": self.TASK_ID, "run_id": run_id})
        self.assertEqual(code, "validation_failed")
        self.assertIn("版本缺少工作区命名能力", message)
        # 不静默派发：未创建会话、未发送 prompt。
        self.assertEqual(self.server.prompt_count, 0)
        self.assertIsNone(self.server.created)

    def test_worker_passes_project_name_and_title_exactly(self):
        """worker 在 pi_session_create 精确透传 title/projectName。"""
        from test_pi_desktop_backend import _sync
        _sync(self.server)
        self.server.turn_statuses = ["running", "completed"]  # 证明/规划轮
        self.server.turn_status = "completed"                  # 执行轮终态
        self.server.messages = [
            {"id": "m1", "role": "user", "content": "任务标识：测试。",
             "timestamp": util.now_iso()},
            {"id": "m2", "role": "assistant",
             "content": [{"type": "text", "text": "执行完成。"}],
             "timestamp": util.now_iso()},
        ]
        self.server.plans = [{
            "id": PROPOSAL_ID, "sessionId": self.server.session_id,
            "turnId": "turn-1", "toolCallId": "t1", "kind": "goal",
            "title": "目标契约", "status": "pending", "version": 1,
        }]
        self.server.planning_state = "awaiting_approval"
        self.server.execution_turn = "turn-exec"
        conn_file = self.write_connection()
        run_dir = self.root / "runs" / RUN_ID
        run_dir.mkdir(parents=True, exist_ok=True)
        plan = self.plan_root / "Feedback" / "plan-naming.md"
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_text("---\ntask_id: t\n---\n# 计划正文\n", encoding="utf-8")
        cwd = self.root / "worktree"
        cwd.mkdir(parents=True, exist_ok=True)
        spec = self.worker_spec(
            run_dir, plan, cwd, conn_file,
            workspace_display_name="Feedback",
            session_display_name="修复计划验收",
        )
        with _fast_clock():
            rc = pi_desktop_worker.run_real(spec)
        self.assertEqual(rc, 0, self.server.calls)
        self.assertEqual(self.server.created["title"], "修复计划验收")
        self.assertEqual(self.server.created["projectName"], "Feedback")
        self.assertEqual(self.server.created["mode"], "goal")
        self.assertEqual(self.server.prompt_count, 1)

    def test_plus_returned_name_mismatch_blocks_agent_prompt(self):
        """Plus 返回的显示名与请求不符 → 不下发 agent_prompt。"""
        from test_pi_desktop_backend import _sync
        _sync(self.server)
        self.server.turn_status = "completed"
        self.server.created_response_names = {"title": "别的会话名"}
        conn_file = self.write_connection()
        run_dir = self.root / "runs2" / RUN_ID
        run_dir.mkdir(parents=True, exist_ok=True)
        plan = self.plan_root / "Feedback" / "plan-naming2.md"
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_text("---\ntask_id: t\n---\n# 计划正文\n", encoding="utf-8")
        cwd = self.root / "worktree2"
        cwd.mkdir(parents=True, exist_ok=True)
        spec = self.worker_spec(
            run_dir, plan, cwd, conn_file,
            workspace_display_name="Feedback",
            session_display_name="修复计划验收",
        )
        with _fast_clock():
            rc = pi_desktop_worker.run_real(spec)
        self.assertEqual(rc, 1)
        self.assertEqual(self.server.prompt_count, 0,
                         "显示名不符绝不下发 agent_prompt")
        job = json.loads(
            (run_dir / dsh_events.JOB_NAME).read_text(encoding="utf-8"))
        self.assertIn("session_naming_mismatch", job["detail"])
        self.assertEqual(job["session_id"], self.server.session_id,
                         "已创建会话 ID 保留供人工检查")
        # 绝不自动再建第二个会话。
        self.assertEqual(
            len([n for n, _a in self.server.calls
                 if n == "pi_session_create"]), 1)

    def test_old_spec_without_display_names_keeps_old_semantics(self):
        """旧 spec 缺展示快照字段 → 仍按旧语义（title=session_marker）。"""
        from test_pi_desktop_backend import _sync
        _sync(self.server)
        self.server.turn_status = "completed"
        self.server.messages = [
            {"id": "m1", "role": "user", "content": "任务标识：测试。",
             "timestamp": util.now_iso()},
            {"id": "m2", "role": "assistant",
             "content": [{"type": "text", "text": "执行完成。"}],
             "timestamp": util.now_iso()},
        ]
        conn_file = self.write_connection()
        run_dir = self.root / "runs3" / RUN_ID
        run_dir.mkdir(parents=True, exist_ok=True)
        plan = self.plan_root / "Feedback" / "plan-naming3.md"
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_text("---\ntask_id: t\n---\n# 计划正文\n", encoding="utf-8")
        cwd = self.root / "worktree3"
        cwd.mkdir(parents=True, exist_ok=True)
        marker = f"{TASK_ID}/{RUN_ID}"
        # 旧 spec：缺展示快照字段（不得推断、不得重发）。
        spec = self.worker_spec(run_dir, plan, cwd, conn_file)
        spec["session_marker"] = marker
        with _fast_clock():
            rc = pi_desktop_worker.run_real(spec)
        self.assertEqual(rc, 0, self.server.calls)
        self.assertEqual(self.server.created["title"], marker)
        self.assertNotIn("projectName", self.server.created)


def _fast_clock():
    import contextlib

    @contextlib.contextmanager
    def _cm():
        saved = {}
        for name, value in (
            ("_POLL_SECONDS", 0.05), ("_HEARTBEAT_SECONDS", 0.05),
            ("_PROOF_WAIT_SECONDS", 0.8), ("_GOAL_CONTRACT_WAIT_SECONDS", 0.5),
            ("_GOAL_EXECUTION_WAIT_SECONDS", 0.5), ("_GOAL_POLL_SECONDS", 0.05),
        ):
            saved[name] = getattr(pi_desktop_worker, name)
            setattr(pi_desktop_worker, name, value)
        try:
            yield
        finally:
            for name, value in saved.items():
                setattr(pi_desktop_worker, name, value)

    return _cm()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
