"""A14 P1: automation-driven enqueue/resolve/recheck must not be recorded as user.

Sandbox evidence §4.2 (``docs/evidence/sandbox-live-20261010.md``): the event
table of a live run showed ``enqueued``/``resolved``/``rechecked`` with
``actor=user`` even though the engine triggered them, which breaks A14 ("on an
unattended run the only ``actor=user`` events are the task creations").

``enqueue``/``resolve``/``recheck`` now take an explicit ``actor``
(``user`` default, ``automation`` for unattended callers) — the same pair
``prepare-plan-input`` already uses. The reconciler passes ``automation`` on
every enqueue it issues, and the monitor stage passes it on its recheck.

Run: ``cd core && python3 -m pytest tests/test_engine_event_actor.py -q``.
"""
from __future__ import annotations

import unittest

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import core, db, util  # noqa: E402
from kanpilot.engine import decide, loop, steps  # noqa: E402

PROJECT = "ryryo1reph1xfemqtwkjyiiz"


def _task(**kw):
    base = {"task_id": "t1", "plan_sha256": "sha", "send_state": None}
    base.update(kw)
    return base


class TestStepsCarryAutomationActor(unittest.TestCase):
    """Every enqueue the reconciler issues says who asked (A14)."""

    def test_all_enqueue_payloads_declare_actor(self):
        cases = [
            (decide.Decision(decide.CONFIRM_PLAN, params={}), _task()),
            (decide.Decision(decide.ENQUEUE_DISPATCH, params={}), _task()),
            (decide.Decision(decide.RUN_VERDICT, params={}), _task()),
            (decide.Decision(decide.COMMIT_DELIVERY, params={}), _task()),
            (decide.Decision(decide.SYNC_DELIVERY, params={}), _task()),
            (decide.Decision(decide.ENQUEUE_REVIEW, params={}), _task()),
            (decide.Decision(decide.RETRY, params={"kind": "monitor",
                                                   "not_before": "2099-01-01T00:00:00+00:00"}),
             _task()),
        ]
        for decision, task in cases:
            with self.subTest(action=decision.action):
                calls = steps.commands_for(decision, task)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0].command, "enqueue")
                self.assertEqual(calls[0].payload.get("actor"), "automation")


class TestEnqueueActor(KanpilotTestCase):
    def _ready(self, task_id="taskactor000000000001"):
        """Published plan awaiting execution confirmation → (task_id, plan_sha)."""
        info = self.make_ready_task(task_id=task_id)
        tid = info["remote"]["id"]
        conn = db.connect(self.db_path)
        try:
            sha = db.require_task(conn, tid)["plan_sha256"]
        finally:
            conn.close()
        return tid, sha

    def _running(self, task_id):
        tid, sha = self._ready(task_id)
        self.set_task_status(tid, "running")
        return tid, sha

    def _events(self, task_id, kind, request_kind=None):
        """Events of ``kind``; ``request_kind`` narrows ``enqueued`` to one request
        kind so a fixture's pre-existing plan request does not answer for us."""
        out = []
        for event in self.call("list-events", {"task_id": task_id,
                                               "limit": 200})["events"]:
            if event["kind"] != kind:
                continue
            if request_kind is not None and (
                    (event.get("detail") or {}).get("kind") != request_kind):
                continue
            out.append(event)
        return out

    def test_automation_actor_recorded(self):
        tid, sha = self._ready()
        self.call("enqueue", {"task_id": tid, "kind": "dispatch",
                              "confirm_plan": True, "expected_plan_sha256": sha,
                              "actor": "automation"})
        events = self._events(tid, "enqueued", request_kind="dispatch")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["actor"], "automation")
        request = self.call("list-requests", {"limit": 50})["requests"][0]
        self.assertEqual(request["requested_by"], "automation")

    def test_default_stays_user(self):
        tid, sha = self._ready("taskactor000000000002")
        self.call("enqueue", {"task_id": tid, "kind": "dispatch",
                              "confirm_plan": True, "expected_plan_sha256": sha})
        events = self._events(tid, "enqueued", request_kind="dispatch")
        self.assertEqual(events[0]["actor"], "user")
        request = self.call("list-requests", {"limit": 50})["requests"][0]
        self.assertEqual(request["requested_by"], "user")

    def test_legacy_not_before_keeps_engine(self):
        """历史调用方只设 not_before 时 requested_by 仍是 engine（不静默改语义）。"""
        tid, _sha = self._running("taskactor000000000003")
        self.call("enqueue", {"task_id": tid, "kind": "monitor",
                              "not_before": "2099-01-01T00:00:00+00:00"})
        request = self.call("list-requests", {"limit": 50})["requests"][0]
        self.assertEqual(request["requested_by"], "engine")
        self.assertEqual(
            self._events(tid, "enqueued", request_kind="monitor")[0]["actor"],
            "engine")

    def test_invalid_actor_rejected(self):
        tid, sha = self._ready("taskactor000000000004")
        for raw in ("engine", "system", "", "USER", 3):
            with self.subTest(raw=raw):
                exc = self.call_err("enqueue", {"task_id": tid, "kind": "dispatch",
                                                "confirm_plan": True,
                                                "expected_plan_sha256": sha,
                                                "actor": raw})
                self.assertEqual(getattr(exc, "code", None), "bad_request")

    def test_confirm_plan_records_automation(self):
        """引擎的 CONFIRM_PLAN 走 confirm_plan_inline：enqueued + resolved 都记 automation。"""
        tid, sha = self._ready("taskactor000000000005")
        self.call("enqueue", {"task_id": tid, "kind": "dispatch",
                              "confirm_plan": True, "expected_plan_sha256": sha,
                              "actor": "automation"})
        self.assertEqual(
            self._events(tid, "enqueued", request_kind="dispatch")[0]["actor"],
            "automation")
        resolved = self._events(tid, "resolved")
        self.assertTrue(resolved)
        self.assertEqual(resolved[0]["actor"], "automation")
        self.assertEqual(resolved[0]["detail"]["action"], "confirm_plan")


class TestResolveAndRecheckActor(KanpilotTestCase):
    def _needs_user(self, task_id):
        info = self.make_ready_task(task_id=task_id)
        tid = info["remote"]["id"]
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                task = db.require_task(conn, tid)
                core.update_automation(conn, task, util.now_iso(),
                                       adopted_at=util.now_iso())
                core.block_task(conn, db.require_task(conn, tid), util.now_iso(),
                                "测试阻塞", "review_blocked", "test", stage="review")
        finally:
            conn.close()
        return tid

    def _events(self, task_id, kind):
        return [e for e in self.call("list-events", {"task_id": task_id,
                                                     "limit": 300})["events"]
                if e["kind"] == kind]

    def test_resolve_default_user_and_explicit_automation(self):
        tid = self._needs_user("taskactorres000000001")
        self.call("resolve", {"task_id": tid, "action": "dismiss", "note": "x"})
        self.assertEqual(self._events(tid, "resolved")[0]["actor"], "user")

        tid2 = self._needs_user("taskactorres000000002")
        self.call("resolve", {"task_id": tid2, "action": "dismiss", "note": "x",
                              "actor": "automation"})
        events = self._events(tid2, "resolved")
        self.assertEqual(events[0]["actor"], "automation")

    def test_resolve_invalid_actor_rejected(self):
        tid = self._needs_user("taskactorres000000003")
        exc = self.call_err("resolve", {"task_id": tid, "action": "dismiss",
                                        "actor": "engine"})
        self.assertEqual(getattr(exc, "code", None), "bad_request")

    def test_recheck_default_user_and_explicit_automation(self):
        tid = self._needs_user("taskactorrec000000001")
        self.call("recheck", {"task_id": tid, "offline": True})
        self.assertEqual(self._events(tid, "rechecked")[0]["actor"], "user")

        tid2 = self._needs_user("taskactorrec000000002")
        self.call("recheck", {"task_id": tid2, "offline": True,
                              "actor": "automation"})
        self.assertEqual(self._events(tid2, "rechecked")[0]["actor"], "automation")

    def test_recheck_invalid_actor_rejected(self):
        tid = self._needs_user("taskactorrec000000003")
        exc = self.call_err("recheck", {"task_id": tid, "actor": "scheduler"})
        self.assertEqual(getattr(exc, "code", None), "bad_request")


class TestReconcilerLeavesNoUserTrail(KanpilotTestCase):
    def test_live_enqueue_events_are_automation(self):
        info = self.make_ready_task(task_id="taskactoreng000000001",
                                    project_name="Feedback", project_id=PROJECT)
        tid = info["remote"]["id"]
        revision = self.call("get-project-mappings", {}).get("revision", 0)
        self.call("set-project-mapping", {
            "project_id": PROJECT, "repository": str(self.repos_root / "Feedback"),
            "enabled": True, "expected_revision": revision})
        self.call("set-project-engine-mode",
                  {"project_id": PROJECT, "engine_mode": "live"})
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                task = db.require_task(conn, tid)
                core.update_automation(conn, task, util.now_iso(),
                                       adopted_at=util.now_iso())
        finally:
            conn.close()

        loop.Reconciler(self.db_path, mode="live").reconcile_once()

        events = self.call("list-events", {"task_id": tid, "limit": 300})["events"]
        # A14: the engine created no ``actor=user`` trail of its own. Task
        # creation (sync-tasks) is the only permitted user-actor event and it
        # happened before the engine ran, so nothing new may claim "user".
        enqueued = [e for e in events if e["kind"] == "enqueued"
                    and (e.get("detail") or {}).get("kind") == "dispatch"]
        self.assertTrue(enqueued)
        for event in enqueued:
            self.assertEqual(event["actor"], "automation")
        request = self.call("list-requests", {"limit": 50})["requests"][0]
        self.assertEqual(request["requested_by"], "automation")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
