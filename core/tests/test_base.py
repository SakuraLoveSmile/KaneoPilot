"""Core mechanics: schema, enqueue dedup, claim race/guards, heartbeat,
report, pause semantics."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import util  # noqa: E402


class TestSchemaAndCli(KanpilotTestCase):
    def test_schema_initialized(self):
        self.call("overview")
        conn = sqlite3.connect(str(self.db_path))
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertTrue(
            {"schema_meta", "tasks", "requests", "attempts", "events",
             "settings", "planner_inputs", "runtime_operations", "worktree_slots"} <= tables
        )
        version = conn.execute("SELECT version FROM schema_meta").fetchone()[0]
        self.assertEqual(version, 6)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode, "wal")
        conn.close()

    def test_cli_json_and_flags(self):
        code, out, _ = self.call_main(
            ["overview", "--db", str(self.db_path), "--json", "{}"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["schema_version"], 6)

    def test_cli_stdin_fallback(self):
        code, out, _ = self.call_main(
            ["overview", "--db", str(self.db_path)], stdin="{}"
        )
        self.assertEqual(code, 0)
        self.assertIn("counts", out)

    def test_cli_unknown_command_and_bad_payload(self):
        code, out, _ = self.call_main(["bogus", "--db", str(self.db_path)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["code"], "bad_request")
        code, out, _ = self.call_main(
            ["get-task", "--db", str(self.db_path), "--json", "{}"]
        )
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["code"], "bad_request")
        code, out, _ = self.call_main(
            ["get-task", "--db", str(self.db_path),
             "--json", '{"task_id": "x", "surprise": 1}']
        )
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["code"], "bad_request")

    def test_doctor_offline(self):
        result = self.call("doctor", {})
        names = {c["name"] for c in result["checks"]}
        self.assertIn("kaneo_ping", names)
        ping = next(c for c in result["checks"] if c["name"] == "kaneo_ping")
        self.assertTrue(ping["skipped"])


class TestEnqueueClaim(KanpilotTestCase):
    def _new_task(self, status="to-do"):
        task = self.fake.add_task(
            "task0000000000000000001", "fixture-project-feedback",
            status=status,
        )
        self.make_repo("Feedback")
        source_sha, req_sha, source_path = self.make_source_snapshot(task)
        self.sync_task(
            task, project_name="Feedback",
            requirements_sha256=req_sha, source_sha256=source_sha,
            source_path=source_path,
        )
        return task

    def test_sync_creates_needs_plan_without_plan_request(self):
        # v2 §5.22.2：sync 只导入 needs_plan，不再自动创建 plan 请求——
        # 规划经规划面板（prepare-plan-input + start-plans）手动提交。
        task = self._new_task()
        view = self.task(task["id"])
        self.assertEqual(view["status"], "needs_plan")
        self.assertEqual(view["display_status"], "待规划")
        plan_reqs = [r for r in self.requests(status="queued")
                     if r["kind"] == "plan"]
        self.assertEqual(plan_reqs, [])
        # enqueue kind=plan 同样被拒绝（bad_request）。
        exc = self.call_err(
            "enqueue", {"task_id": task["id"], "kind": "plan"})
        self.assertEqual(exc.code, "bad_request")

    def test_enqueue_idempotent_dedup(self):
        task = self._new_task()
        # v2：kind=plan 不经 enqueue；用 monitor 验证幂等键去重。
        self.set_task_status(task["id"], "running")
        fresh = self.call("enqueue", {"task_id": task["id"], "kind": "monitor"})
        self.assertFalse(fresh["deduplicated"])
        again = self.call("enqueue", {"task_id": task["id"], "kind": "monitor"})
        self.assertTrue(again["deduplicated"])
        self.assertEqual(
            fresh["request"]["request_id"], again["request"]["request_id"]
        )
        active = [
            r for r in self.requests(status="active")
            if r["task_id"] == task["id"]
        ]
        self.assertEqual(len(active), 1)

    def test_enqueue_kind_inference_and_rejection(self):
        task = self._new_task()
        # needs_plan -> plan (already queued by sync); running -> monitor.
        self.set_task_status(task["id"], "running")
        result = self.call("enqueue", {"task_id": task["id"]})
        self.assertEqual(result["request"]["kind"], "monitor")
        exc = self.call_err("enqueue", {"task_id": task["id"], "kind": "dispatch"})
        self.assertEqual(exc.code, "bad_request")
        self.set_task_status(task["id"], "needs_user")
        exc = self.call_err("enqueue", {"task_id": task["id"]})
        self.assertEqual(exc.code, "bad_request")

    def test_claim_race_single_winner(self):
        task = self._new_task()
        request_id, _ = self.queue_plan(task["id"])
        request = next(
            r for r in self.requests(status="queued")
            if r["request_id"] == request_id
        )
        results = []

        def claim():
            try:
                results.append(
                    self.call(
                        "claim",
                        {
                            "stage": "plan", "executor": "kaneo-plan",
                            "request_id": request["request_id"],
                        },
                    )
                )
            except Exception as exc:
                results.append(exc)

        t1, t2 = threading.Thread(target=claim), threading.Thread(target=claim)
        t1.start(), t2.start()
        t1.join(), t2.join()
        wins = [r for r in results if isinstance(r, dict) and r.get("claimed")]
        self.assertEqual(len(wins), 1)
        self.assertEqual(wins[0]["context"]["kind"], "plan")
        self.assertEqual(wins[0]["context"]["task_id"], task["id"])
        # the loser hit a serialized transaction -> conflict or claimed:false
        losers = [r for r in results if r not in wins]
        self.assertEqual(len(losers), 1)
        loser = losers[0]
        if isinstance(loser, dict):
            self.assertFalse(loser["claimed"])
        else:
            self.assertEqual(loser.code, "conflict")
        view = self.task(task["id"])
        self.assertEqual(view["status"], "planning")

    def test_claim_guard_failure_marks_request_failed(self):
        task = self._new_task()
        # authorized plan request queued, but task moved to ready meanwhile.
        self.queue_plan(task["id"])
        self.set_task_status(task["id"], "ready")
        result = self.call("claim", {"stage": "plan", "executor": "kaneo-plan"})
        self.assertFalse(result["claimed"])
        self.assertEqual(result["reason"], "guard_failed")
        reqs = self.requests(status="failed")
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0]["request_id"], result["request_id"])

    def test_claim_specific_request_and_priority(self):
        task = self._new_task()
        request_id, _ = self.queue_plan(task["id"])
        queued = self.requests(status="queued")
        target = next(r for r in queued if r["request_id"] == request_id)
        result = self.call(
            "claim",
            {
                "stage": "plan", "executor": "kaneo-plan",
                "request_id": target["request_id"],
            },
        )
        self.assertTrue(result["claimed"])
        self.assertEqual(result["request_id"], target["request_id"])
        # targeted claim of an already-claimed request loses the race
        # gracefully (protocol §4.2-4): claimed:false, not an error.
        result = self.call(
            "claim",
            {
                "stage": "plan", "executor": "kaneo-plan",
                "request_id": target["request_id"],
            },
        )
        self.assertFalse(result["claimed"])
        self.assertEqual(result["reason"], "already_claimed")

    def test_heartbeat_extends_lease_and_stale_run(self):
        task = self._new_task()
        self.queue_plan(task["id"])
        claim = self.call("claim", {"stage": "plan", "executor": "kaneo-plan"})
        self.assertTrue(claim["claimed"])
        beat = self.call(
            "heartbeat",
            {
                "request_id": claim["request_id"], "run_id": claim["run_id"],
                "progress": "half done",
            },
        )
        self.assertTrue(beat["ok"])
        exc = self.call_err(
            "heartbeat",
            {"request_id": claim["request_id"], "run_id": "wrong"},
        )
        self.assertEqual(exc.code, "stale_run")

    def test_report_outcomes(self):
        task = self._new_task()
        self.queue_plan(task["id"])
        claim = self.call("claim", {"stage": "plan", "executor": "kaneo-plan"})
        result = self.call(
            "report",
            {
                "request_id": claim["request_id"], "run_id": claim["run_id"],
                "outcome": "needs_user", "note": "missing evidence",
                "failure_code": "plan_blocked",
            },
        )
        self.assertEqual(result["request_status"], "failed")
        self.assertEqual(result["task_status"], "needs_user")
        task_id = claim["context"]["task_id"]
        view = self.task(task_id)
        self.assertEqual(view["status"], "needs_user")
        self.assertEqual(view["display_status"], "需人工")

    def test_pause_blocks_new_claim_only(self):
        task = self._new_task()
        self.queue_plan(task["id"])
        self.call("set-paused", {"stage": None, "paused": True})
        result = self.call("claim", {"stage": "plan", "executor": "kaneo-plan"})
        self.assertEqual(result, {"claimed": False, "reason": "paused"})
        # claimed request still works while paused
        self.call("set-paused", {"stage": None, "paused": False})
        self.call("set-paused", {"stage": "plan", "paused": True})
        result = self.call("claim", {"stage": "plan", "executor": "kaneo-plan"})
        self.assertFalse(result["claimed"])
        overview = self.call("overview")
        self.assertEqual(overview["paused"]["stages"], ["plan"])
        self.call("set-paused", {"stage": "plan", "paused": False})
        result = self.call("claim", {"stage": "plan", "executor": "kaneo-plan"})
        self.assertTrue(result["claimed"])

    def test_lost_derivation_no_auto_release(self):
        task = self._new_task()
        self.queue_plan(task["id"])
        claim = self.call("claim", {"stage": "plan", "executor": "kaneo-plan"})
        # Force the lease into the past.
        conn = sqlite3.connect(str(self.db_path))
        past = (util.now_dt() - timedelta(seconds=60)).isoformat()
        conn.execute(
            "UPDATE requests SET lease_expires_at=? WHERE request_id=?",
            (past, claim["request_id"]),
        )
        conn.commit()
        conn.close()
        lost = self.requests(status="lost")
        self.assertEqual(len(lost), 1)
        self.assertEqual(lost[0]["display_status"], "执行端失联")
        # Still claimed in storage; not auto-requeued or released.
        claimed = self.requests(status="claimed")
        self.assertEqual(len(claimed), 1)
        # No new request was created for the lost task.
        queued_for_task = [
            r for r in self.requests(status="queued")
            if r["task_id"] == claim["context"]["task_id"]
        ]
        self.assertEqual(queued_for_task, [])
        view = self.task(claim["context"]["task_id"])
        self.assertEqual(view["display_status"], "执行端失联")
        self.assertIn("release_lost", view["allowed_actions"])


if __name__ == "__main__":
    unittest.main()
