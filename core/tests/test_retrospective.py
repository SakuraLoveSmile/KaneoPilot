"""观测复盘投影（protocol §5.32.9 验收清单）。

直接在临时 SQLite 上写入任务、事件与执行轮次，调用 `retrospective.build`；
时间全部使用固定基准 T0 的偏移秒，结果可精确断言。
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from helpers import CORE  # noqa: F401 - installs isolated core import path
from kanpilot import db, mobile_access, retrospective, task_workspace, util

T0 = datetime(2026, 10, 22, 7, 40, 0, tzinfo=timezone.utc)


def at(seconds):
    return util.to_iso(T0 + timedelta(seconds=seconds))


def dt(seconds):
    return T0 + timedelta(seconds=seconds)


class RetroCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self._tmp.name) / "kanpilot.db")

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def add_task(self, status="needs_plan", *, extra=None, save_state="active",
                 plan_sha="plan-sha", run_id="run-1", updated=0):
        with db.write_txn(self.conn):
            self.conn.execute(
                "INSERT INTO tasks(task_id, project_id, project_name, title, repository, stage,"
                " status, plan_sha256, run_id, created_at, updated_at, extra_json, save_state)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("T-1", "P1", "Demo", "演示", "demo", "plan", status, plan_sha, run_id,
                 at(0), at(updated), util.jdump(extra or {}), save_state),
            )
        return db.get_task(self.conn, "T-1")

    def event(self, seconds, event_kind, *, request_id=None, task_id="T-1", **detail):
        with db.write_txn(self.conn):
            db.add_event(self.conn, at(seconds), event_kind, task_id=task_id, request_id=request_id,
                         actor="test", detail=detail or None)

    def status(self, seconds, to, frm=None):
        self.event(seconds, "status_change", **{"from": frm, "to": to})

    def build(self, as_of_seconds):
        return retrospective.build(self.conn, db.get_task(self.conn, "T-1"), dt(as_of_seconds))

    def lanes(self, result):
        return [(item["lane"], item["seconds"]) for item in result["segments"]["items"]]

    def distribution(self, result):
        return {row["lane"]: row["seconds"] for row in result["distribution"]}


class CycleAndLaneTests(RetroCase):
    def test_none_without_enqueued_request(self):
        self.add_task("needs_plan")
        self.assertIsNone(self.build(100))

    def test_basic_lanes_sum_to_elapsed(self):
        self.add_task("running")
        self.event(0, "enqueued", request_id="p1", kind="plan")
        self.status(0, "planning", frm="needs_plan")
        self.status(60, "ready", frm="planning")
        self.event(120, "enqueued", request_id="d1", kind="dispatch")
        self.event(150, "dispatch_sent", request_id="d1")
        self.status(160, "dispatching", frm="ready")
        self.status(200, "running", frm="dispatching")
        result = self.build(260)
        self.assertEqual(result["elapsed_seconds"], 260)
        self.assertEqual(self.lanes(result), [
            ("plan", 60), ("other", 60), ("dispatch", 30), ("other", 10),
            ("dispatch", 40), ("execute", 60),
        ])
        seconds = self.distribution(result)
        self.assertEqual(seconds["plan"], 60)
        self.assertEqual(seconds["dispatch"], 70)
        self.assertEqual(seconds["execute"], 60)
        self.assertEqual(seconds["other"], 70)
        self.assertIsNone(seconds["waiting"])
        self.assertEqual(sum(v for k, v in seconds.items() if v is not None), 260)
        self.assertEqual(sum(row["percent"] for row in result["distribution"] if row["percent"] is not None), 100)
        self.assertEqual(result["state"], "running")
        self.assertIsNone(result["ended_at"])
        self.assertIsNone(result["exec_seconds"])
        self.assertIsNone(result["waiting"])
        self.assertIsNone(result["verify"])
        self.assertIsNone(result["verify_runs"])
        self.assertEqual(result["highlights"], [])

    def test_pause_moves_dispatch_queue_to_other(self):
        self.add_task("ready")
        self.event(0, "enqueued", request_id="d1", kind="dispatch")
        self.event(30, "paused", task_id=None, stage=None)
        self.event(90, "unpaused", task_id=None, stage=None)
        self.event(120, "dispatch_sent", request_id="d1")
        self.status(120, "running", frm="ready")
        result = self.build(180)
        self.assertEqual(self.lanes(result), [
            ("dispatch", 30), ("other", 60), ("dispatch", 30), ("execute", 60),
        ])

    def test_stage_scoped_pause_outside_dispatch_is_ignored(self):
        self.add_task("ready")
        self.event(0, "enqueued", request_id="d1", kind="dispatch")
        self.event(30, "paused", task_id=None, stage="plan")
        self.event(90, "unpaused", task_id=None, stage="plan")
        result = self.build(120)
        self.assertEqual(self.lanes(result), [("dispatch", 120)])

    def test_pause_before_cycle_still_applies(self):
        self.add_task("ready")
        self.event(0, "paused", task_id=None, stage=None)
        self.event(10, "enqueued", request_id="d1", kind="dispatch")
        result = self.build(60)
        self.assertEqual(self.lanes(result), [("other", 50)])

    def test_done_cycle_is_finished_and_frozen(self):
        self.add_task("done")
        self.event(0, "enqueued", request_id="p1", kind="plan")
        self.status(50, "running", frm="needs_plan")
        self.status(200, "done", frm="running")
        first = self.build(500)
        later = self.build(800)
        for result in (first, later):
            self.assertEqual(result["state"], "finished")
            self.assertEqual(result["ended_at"], at(200))
            self.assertEqual(result["elapsed_seconds"], 200)
            self.assertEqual(self.distribution(result)["execute"], 150)
        self.assertEqual(first["distribution"], later["distribution"])

    def test_done_without_registered_transition_uses_clamped_updated_at(self):
        self.add_task("done", updated=150)
        self.event(0, "enqueued", request_id="p1", kind="plan")
        result = self.build(400)
        self.assertEqual(result["state"], "finished")
        self.assertEqual(result["ended_at"], at(150))
        self.assertEqual(result["elapsed_seconds"], 150)

    def test_idle_flag_for_long_other_and_dispatch_segments(self):
        self.add_task("needs_plan")
        self.event(0, "enqueued", request_id="p1", kind="plan")
        self.status(200, "planning", frm="needs_plan")
        result = self.build(260)
        items = result["segments"]["items"]
        self.assertEqual([(i["lane"], i["seconds"], i["idle"]) for i in items],
                         [("other", 200, True), ("plan", 60, False)])
        self.assertIsNone(items[-1]["ended_at"])
        self.assertEqual(items[0]["ended_at"], at(200))

    def test_requirements_change_anchors_the_cycle(self):
        self.add_task("running")
        self.event(0, "enqueued", request_id="p1", kind="plan")
        self.status(10, "running", frm="needs_plan")
        self.event(100, "requirements_changed")
        self.event(120, "enqueued", request_id="p2", kind="plan")
        result = self.build(200)
        self.assertEqual(result["cycle_started_at"], at(120))
        self.assertEqual(self.lanes(result), [("execute", 80)])

    def test_review_repair_window_until_running(self):
        self.add_task("running")
        self.event(0, "enqueued", request_id="p1", kind="plan")
        self.status(100, "in_review", frm="running")
        self.event(150, "review_registered", conclusion="changes_requested")
        self.status(160, "ready", frm="in_review")
        self.status(300, "running", frm="ready")
        result = self.build(320)
        self.assertEqual(self.lanes(result), [
            ("execute", 100), ("review", 50), ("repair", 150), ("execute", 20),
        ])
        self.assertEqual([h["kind"] for h in result["highlights"]], ["review_not_passed"])
        self.assertEqual(result["highlights"][0]["at"], at(150))
        self.assertEqual(result["highlights"][0]["label"], "评审未通过")

    def test_round_index_follows_started_at(self):
        self.add_task("running")
        self.event(0, "enqueued", request_id="p1", kind="plan")
        self.status(60, "needs_user", frm="running")
        self.status(130, "running", frm="needs_user")
        for run_id, started in (("run-a", 10), ("run-b", 120)):
            with db.write_txn(self.conn):
                self.conn.execute(
                    "INSERT INTO execution_rounds(task_id, run_id, started_at, created_at, updated_at)"
                    " VALUES (?,?,?,?,?)", ("T-1", run_id, at(started), at(0), at(0)))
        result = self.build(200)
        self.assertEqual([item["round"] for item in result["segments"]["items"]], [None, 1, 2])

    def test_segments_truncate_to_latest_200_without_changing_totals(self):
        self.add_task("running")
        self.event(0, "enqueued", request_id="p1", kind="plan")
        for index in range(250):
            self.status(index * 10 + 10, "needs_user" if index % 2 == 0 else "running")
        result = self.build(2600)
        self.assertEqual(len(result["segments"]["items"]), 200)
        self.assertTrue(result["segments"]["truncated"])
        self.assertEqual(sum(row["seconds"] for row in result["distribution"] if row["seconds"] is not None),
                         2600)

    def test_purged_task_has_no_retrospective(self):
        task = self.add_task("running", save_state="purged")
        self.event(0, "enqueued", request_id="p1", kind="plan")
        self.assertIsNone(retrospective.build(self.conn, task, dt(60)))


class WaitingGateTests(RetroCase):
    def setUp(self):
        super().setUp()
        self.add_task("running", extra={"execution_input_wait_intervals": [
            {"run_id": "run-1", "start": at(40), "end": at(100)},
            {"run_id": "run-1", "start": at(150), "end": None},
        ]})
        self.event(0, "enqueued", request_id="p1", kind="plan")

    def test_gate_closed_hides_waiting_and_exec(self):
        result = self.build(200)
        self.assertFalse(retrospective.WAIT_SOURCE_READY)
        self.assertIsNone(result["waiting"])
        self.assertIsNone(result["exec_seconds"])
        self.assertEqual(self.lanes(result), [("execute", 200)])
        self.assertEqual(result["state"], "running")
        self.assertIsNone(self.distribution(result)["waiting"])

    def test_gate_open_splits_waiting_intervals(self):
        with mock.patch.object(retrospective, "WAIT_SOURCE_READY", True):
            result = self.build(200)
        self.assertEqual(self.lanes(result), [
            ("execute", 40), ("waiting", 60), ("execute", 50), ("waiting", 50),
        ])
        self.assertEqual(result["waiting"], {
            "count": 2, "total_seconds": 110, "longest_seconds": 60, "open_since": at(150),
        })
        self.assertEqual(result["exec_seconds"], 90)
        self.assertEqual(result["state"], "waiting")
        self.assertEqual(sum(row["seconds"] for row in result["distribution"]), 200)

    def test_gate_open_long_wait_highlight(self):
        with db.write_txn(self.conn):
            self.conn.execute("UPDATE tasks SET extra_json=? WHERE task_id='T-1'", (util.jdump({
                "execution_input_wait_intervals": [{"run_id": "run-1", "start": at(10), "end": at(700)}]}),))
        with mock.patch.object(retrospective, "WAIT_SOURCE_READY", True):
            result = self.build(800)
        self.assertEqual([h["kind"] for h in result["highlights"]], ["long_wait"])
        self.assertEqual(result["highlights"][0]["seconds"], 690)


class ProjectionRuleTests(RetroCase):
    def test_repair_exhausted_then_long_execute(self):
        self.add_task("running", extra={"automation": {"fix_round": 2}})
        self.event(0, "enqueued", request_id="p1", kind="plan")
        with mock.patch.object(retrospective.automation_policy, "policy_for",
                               return_value={"max_fix_rounds": 2}):
            result = self.build(3700)
        self.assertEqual([h["kind"] for h in result["highlights"]], ["repair_exhausted", "long_execute"])
        self.assertEqual(result["highlights"][0]["label"], "修复轮已用完")
        self.assertEqual(result["highlights"][1]["seconds"], 3700)

    def test_claim_rollback_registers_status_change(self):
        base = util.now_dt().replace(microsecond=0)
        self.add_task("planning")
        with db.write_txn(self.conn):
            db.add_event(self.conn, util.to_iso(base - timedelta(seconds=120)), "enqueued",
                         task_id="T-1", request_id="req-1", actor="test", detail={"kind": "plan"})
            self.conn.execute(
                "INSERT INTO requests(request_id, idempotency_key, task_id, stage, kind, status,"
                " payload_json, requested_by, run_id, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("req-1", "key-1", "T-1", "plan", "plan", "claimed",
                 util.jdump({"task_status_at_claim": "needs_plan"}), "test", "run-1",
                 util.to_iso(base), util.to_iso(base)),
            )
        with mock.patch.object(util, "now_iso", return_value=util.to_iso(base)):
            task_workspace.pool_wait(self.conn, "req-1", "run-1", {"repo_key": "r1", "reason": "pool_full"})
        rows = self.conn.execute(
            "SELECT detail_json, actor FROM events WHERE task_id='T-1' AND kind='status_change'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(util.jload(rows[0]["detail_json"]), {
            "from": "planning", "to": "needs_plan", "actor": "worktree-pool"})
        result = retrospective.build(self.conn, db.get_task(self.conn, "T-1"), base + timedelta(seconds=30))
        self.assertEqual(self.lanes(result), [("plan", 120), ("other", 30)])


class VerifyTests(RetroCase):
    COMMANDS = ["make a", "make b"]

    def verdict(self, source, checks, coverage=None, commands_sha=None, run_id="run-1"):
        digest = commands_sha or util.sha256_text(util.jdump(self.COMMANDS))
        receipt = {"evidence_source": source, "checks": checks,
                   "binding": {"plan_sha256": "plan-sha", "commands_sha256": digest,
                               "execution_run_id": run_id}}
        if coverage is not None:
            receipt["goal_check_coverage"] = {"matched": coverage}
        return receipt

    def add_verify(self, receipt):
        self.add_task("running", extra={
            "plan_verify": {"plan_sha256": "plan-sha", "commands": self.COMMANDS},
            "engine_verdict": receipt})
        self.event(0, "enqueued", request_id="p1", kind="plan")

    def test_goal_report_counts_matched_and_failed(self):
        self.add_verify(self.verdict("goal_report", [
            {"command": "make a", "exit_code": 0, "timed_out": False, "command_not_found": False},
            {"command": "make b", "exit_code": 1, "timed_out": False, "command_not_found": False},
        ], coverage=["make a"]))
        result = self.build(60)
        self.assertEqual(result["verify"], {"total": 2, "passed": 1, "failed": 1, "unconfirmed": 0,
                                            "current": True, "evidence_source": "goal_report"})
        self.assertEqual(result["verify_runs"], {"runs": 2, "failed": 1, "timed_out": 0, "command_not_found": 0})

    def test_stale_receipt_counts_as_unconfirmed(self):
        self.add_verify(self.verdict("goal_report", [
            {"command": "make a", "exit_code": 0}], coverage=["make a", "make b"], commands_sha="stale"))
        result = self.build(60)
        self.assertEqual(result["verify"]["current"], False)
        self.assertEqual((result["verify"]["passed"], result["verify"]["failed"],
                          result["verify"]["unconfirmed"]), (0, 0, 2))
        self.assertIsNone(result["verify_runs"])

    def test_codex_counts_only_harness_observed_checks(self):
        self.add_verify(self.verdict("codex_report", [
            {"command": "make a", "exit_code": 0, "provenance": "model_declared"},
            {"command": "make a", "exit_code": 1, "provenance": "harness_observed"},
            {"command": "make b", "exit_code": 0, "provenance": "harness_observed"},
        ]))
        result = self.build(60)
        self.assertEqual((result["verify"]["passed"], result["verify"]["failed"],
                          result["verify"]["unconfirmed"]), (1, 1, 0))

    def test_missing_or_errored_declaration_is_null(self):
        self.add_task("running", extra={"plan_verify": {"error": "bad block"}})
        self.event(0, "enqueued", request_id="p1", kind="plan")
        result = self.build(60)
        self.assertIsNone(result["verify"])
        self.assertIsNone(result["verify_runs"])


class HeartbeatInvarianceTests(RetroCase):
    def test_heartbeat_flood_does_not_change_result(self):
        self.add_task("running")
        self.event(0, "enqueued", request_id="p1", kind="plan")
        self.status(60, "needs_user", frm="running")
        baseline = self.build(200)
        with db.write_txn(self.conn):
            self.conn.executemany(
                "INSERT INTO events(at, task_id, request_id, kind, actor, detail_json) VALUES (?,?,?,?,?,?)",
                [(at(i % 200), "T-1", None, "heartbeat", "engine", "{}") for i in range(5000)])
        self.assertEqual(self.build(200), baseline)

    def test_global_pause_query_uses_task_index(self):
        plan = self.conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM events WHERE task_id IS NULL AND kind IN ('paused','unpaused')"
        ).fetchall()
        self.assertIn("events_task", " ".join(str(row[-1]) for row in plan))


class ContractTests(RetroCase):
    def test_mobile_projection_keeps_retrospective(self):
        self.add_task("running")
        self.event(0, "enqueued", request_id="p1", kind="plan")
        self.status(60, "needs_user", frm="running")
        payload = {"task": {"task_id": "T-1", "title": "演示",
                            "retrospective": self.build(200)}}
        projected = mobile_access.project(payload)
        kept = projected["task"]["retrospective"]
        self.assertEqual(kept["distribution"], payload["task"]["retrospective"]["distribution"])
        self.assertEqual(kept["segments"], payload["task"]["retrospective"]["segments"])


if __name__ == "__main__":
    unittest.main()
