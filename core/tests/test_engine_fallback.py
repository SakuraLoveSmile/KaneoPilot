"""P2: fallback execution model for bounded quality remediation (scenarios.md #5).

An empty-diff verdict gives the task ≤2 same-plan remediation rounds. A project
that configures ``fallback_model`` gets the retry on that model — but only after
the model is probed in the Plus cache catalog; a failed probe is a hard
``validation_failed`` (ladder 2) and **never** a silent switch. Without a
configured fallback the round stays on the primary model and a hint event
recommends configuring one.

Covers: config resolution (entry level wins), probe outcomes, snapshot shape,
decision params, the reconciler's bookkeeping/events (live vs shadow, armed
override not re-burned, budget spent), the dispatch-claim seam, and the
``set-project-fallback-model`` command.

Run: ``cd core && python3 -m pytest tests/test_engine_fallback.py -q``.
"""
from __future__ import annotations

import unittest
from unittest import mock

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import core, db, exec_config, util  # noqa: E402
from kanpilot.engine import decide, fallback, ladder, loop  # noqa: E402

REPO = "Feedback"
PROJECT_ID = "projfallback00000000a"
TASK_ID = "taskfallback0000000001"

_PRIMARY = {
    "backend": "pi_desktop",
    "connection_file": "/tmp/fake-connection.json",
    "provider_id": "707a4c67-4ee0-484d-b635-730372501eaf",
    "model_id": "deepseek/deepseek-v4.1-flash",
    "thinking_level": "high",
    "permission_mode": "auto",
}
_FALLBACK = {"provider_id": "707a4c67-4ee0-484d-b635-730372501eaf",
             "model_id": "google/gemini-3.7-flash"}


class TestFallbackConfig(unittest.TestCase):
    def test_recommended_default_is_spike_conclusion(self):
        self.assertEqual(
            fallback.recommended_default(),
            {"provider_id": "707a4c67-4ee0-484d-b635-730372501eaf",
             "model_id": "google/gemini-3.7-flash"})

    def test_normalize_rejects_incomplete(self):
        for raw in (None, {}, {"provider_id": "p"}, {"model_id": "m"},
                    {"provider_id": "  ", "model_id": "m"}, "x", 3, ["p", "m"]):
            with self.subTest(raw=raw):
                self.assertIsNone(fallback.normalize(raw))

    def test_normalize_trims_values(self):
        self.assertEqual(
            fallback.normalize({"provider_id": " p ", "model_id": " m "}),
            {"provider_id": "p", "model_id": "m"})

    def test_entry_level_wins_over_automation(self):
        entry = {"fallback_model": _FALLBACK,
                 "automation": {"fallback_model": {"provider_id": "x",
                                                   "model_id": "y"}}}
        self.assertEqual(fallback.configured_model(entry), _FALLBACK)

    def test_automation_level_is_accepted_as_fallback(self):
        self.assertEqual(
            fallback.configured_model({"automation": {"fallback_model": _FALLBACK}}),
            _FALLBACK)

    def test_unconfigured_is_none(self):
        self.assertIsNone(fallback.configured_model({}))
        self.assertIsNone(fallback.configured_model({"automation": {}}))
        self.assertIsNone(fallback.configured_model(None))
        self.assertIsNone(fallback.configured_model({"fallback_model": {"provider_id": "p"}}))


class TestFallbackPlan(unittest.TestCase):
    def test_unconfigured_retries_primary_with_hint(self):
        plan = fallback.plan_retry(None, primary=_PRIMARY)
        self.assertEqual(plan["model"], fallback.MODEL_PRIMARY)
        self.assertIsNone(plan["snapshot"])
        self.assertFalse(plan["validation_failed"])
        self.assertIsNone(plan["probe_ok"])
        self.assertEqual([e["kind"] for e in plan["events"]],
                         [fallback.EVENT_MODEL, fallback.EVENT_MODEL_HINT])
        self.assertEqual(plan["events"][0]["detail"]["model"], "primary")
        self.assertEqual(plan["events"][0]["detail"]["reason"],
                         "fallback_model_not_configured")
        self.assertEqual(plan["events"][1]["detail"]["hint"],
                         "configure_fallback_model")
        self.assertEqual(plan["events"][1]["detail"]["recommended"],
                         fallback.recommended_default())

    def test_probe_ok_uses_fallback_snapshot(self):
        plan = fallback.plan_retry(_FALLBACK, primary=_PRIMARY,
                                   probe=lambda p, m: (True, "cache 命中"))
        self.assertEqual(plan["model"], fallback.MODEL_FALLBACK)
        self.assertTrue(plan["probe_ok"])
        self.assertFalse(plan["validation_failed"])
        snap = plan["snapshot"]
        self.assertEqual(snap["model_source"], "custom")
        self.assertEqual(snap["provider_id"], _FALLBACK["provider_id"])
        self.assertEqual(snap["model_id"], _FALLBACK["model_id"])
        # 主模型的连接文件/思考等级/权限沿用——不新增第二套凭据。
        self.assertEqual(snap["connection_file"], _PRIMARY["connection_file"])
        self.assertEqual(snap["thinking_level"], _PRIMARY["thinking_level"])
        self.assertEqual(snap["permission_mode"], _PRIMARY["permission_mode"])
        self.assertEqual([e["kind"] for e in plan["events"]], [fallback.EVENT_MODEL])
        self.assertEqual(plan["events"][0]["detail"]["model"], "fallback")

    def test_probe_failure_is_validation_failed_and_never_switches(self):
        plan = fallback.plan_retry(
            _FALLBACK, primary=_PRIMARY,
            probe=lambda p, m: (False, "PI-Desktop model 不存在：x"))
        self.assertTrue(plan["validation_failed"])
        self.assertFalse(plan["probe_ok"])
        self.assertIsNone(plan["snapshot"])       # 不静默换模型
        self.assertEqual(plan["model"], fallback.MODEL_PRIMARY)
        self.assertEqual(plan["events"][0]["detail"]["reason"],
                         "fallback_probe_failed")
        self.assertNotIn(fallback.EVENT_MODEL_HINT,
                         [e["kind"] for e in plan["events"]])

    def test_non_plus_backend_never_switches(self):
        plan = fallback.plan_retry(_FALLBACK, primary={"backend": "pi"},
                                   probe=lambda p, m: (True, "cache 命中"))
        self.assertEqual(plan["model"], fallback.MODEL_PRIMARY)
        self.assertIsNone(plan["snapshot"])
        self.assertFalse(plan["validation_failed"])
        self.assertEqual(plan["events"][0]["detail"]["reason"],
                         "unsupported_backend")

    def test_build_snapshot_requires_plus_backend(self):
        self.assertIsNone(fallback.build_snapshot({"backend": "pi"}, _FALLBACK))
        self.assertIsNone(fallback.build_snapshot(None, _FALLBACK))


class TestOverrideIdentity(unittest.TestCase):
    def test_override_matches_plan_identity(self):
        task = {"plan_sha256": "sha1", "plan_generated_at": "t1"}
        self.assertTrue(fallback.override_matches(
            {"plan_sha256": "sha1", "plan_generated_at": "t1"}, task))
        self.assertFalse(fallback.override_matches(
            {"plan_sha256": "other", "plan_generated_at": "t1"}, task))
        self.assertFalse(fallback.override_matches(
            {"plan_sha256": "sha1", "plan_generated_at": "t0"}, task))
        # 缺 plan_generated_at 的旧覆盖只按 sha 判定。
        self.assertTrue(fallback.override_matches({"plan_sha256": "sha1"}, task))

    def test_read_override(self):
        self.assertIsNone(fallback.read_override({}))
        self.assertIsNone(fallback.read_override({"engine_model_override": None}))
        self.assertIsNone(fallback.read_override({"engine_model_override": {}}))
        self.assertIsNone(fallback.read_override(None))
        self.assertEqual(
            fallback.read_override({"engine_model_override": {"a": 1}}), {"a": 1})


class TestDecisionCarriesModelChoice(unittest.TestCase):
    def test_quality_round_records_model_choice(self):
        with_fb = decide.failure_decision("empty_diff", fix_round=0,
                                          fallback_model=_FALLBACK)
        self.assertEqual(with_fb.action, decide.START_FIX_ROUND)
        self.assertEqual(with_fb.params["model"], "fallback")
        self.assertEqual(with_fb.params["fallback_model"], _FALLBACK)
        without = decide.failure_decision("empty_diff", fix_round=0)
        self.assertEqual(without.params["model"], "primary")
        self.assertIsNone(without.params["fallback_model"])

    def test_other_ladders_unchanged(self):
        transient = decide.failure_decision("remote_unavailable", attempt_no=0)
        self.assertEqual(transient.action, decide.RETRY)
        self.assertNotIn("model", transient.params)
        capped = decide.failure_decision("empty_diff",
                                         fix_round=ladder.MAX_FIX_ROUNDS)
        self.assertTrue(capped.is_block)


class TestQualityRetryLoop(KanpilotTestCase):
    def _adopt(self):
        info = self.make_ready_task(task_id=TASK_ID, project_name=REPO,
                                    project_id=PROJECT_ID)
        tid = info["remote"]["id"]
        revision = self.call("get-project-mappings", {}).get("revision", 0)
        self.call("set-project-mapping", {
            "project_id": PROJECT_ID, "repository": str(self.repos_root / REPO),
            "enabled": True, "expected_revision": revision})
        self.call("set-project-engine-mode",
                  {"project_id": PROJECT_ID, "engine_mode": "live"})
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                task = db.require_task(conn, tid)
                core.update_automation(conn, task, util.now_iso(),
                                       adopted_at=util.now_iso())
        finally:
            conn.close()
        return tid

    def _empty_verdict(self, tid, run_id="run-empty-1"):
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                task = db.require_task(conn, tid)
                db.set_extra(conn, task, util.now_iso(), engine_verdict={
                    "execution_run_id": run_id,
                    "plan_sha256": task["plan_sha256"],
                    "empty_diff": True, "verification_ok": False})
        finally:
            conn.close()

    def _extra(self, tid):
        conn = db.connect(self.db_path)
        try:
            return db.task_extra(db.require_task(conn, tid))
        finally:
            conn.close()

    def _events(self, tid):
        conn = db.connect(self.db_path)
        try:
            return [(row["kind"], util.jload(row["detail_json"]))
                    for row in conn.execute(
                        "SELECT kind, detail_json FROM events WHERE task_id=? "
                        "ORDER BY id", (tid,))]
        finally:
            conn.close()

    def _actions(self, tid):
        summary = loop.Reconciler(self.db_path, mode="live").reconcile_once()
        return [a for a in summary["actions"] if a.get("task_id") == tid]

    def test_unconfigured_records_primary_and_hint(self):
        tid = self._adopt()
        self._empty_verdict(tid)
        actions = self._actions(tid)
        self.assertTrue(actions)
        self.assertEqual(actions[0]["action"], "start_fix_round")
        kinds = [kind for kind, _ in self._events(tid)]
        self.assertIn("model", kinds)
        self.assertIn("model_hint", kinds)
        model_events = [d for k, d in self._events(tid) if k == "model"]
        self.assertEqual(model_events[0]["model"], "primary")
        rounds = self._extra(tid)["engine_fix_rounds"]
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0]["model"], "primary")
        # 未配 fallback：不武装任何模型覆盖。
        self.assertIsNone(fallback.read_override(self._extra(tid)))

    def test_configured_and_probe_ok_arms_fallback_override(self):
        tid = self._adopt()
        self.call("set-project-fallback-model",
                  {"project_id": PROJECT_ID, "fallback_model": _FALLBACK})
        self._empty_verdict(tid)
        with mock.patch.object(fallback, "probe_model",
                               return_value=(True, "cache 命中")), \
                mock.patch.object(fallback, "primary_snapshot",
                                  return_value=(_PRIMARY, None)):
            actions = self._actions(tid)
        self.assertEqual(actions[0]["action"], "start_fix_round")
        self.assertEqual(actions[0]["reason"], "empty_diff")
        model_events = [d for k, d in self._events(tid) if k == "model"]
        self.assertEqual(model_events[-1]["model"], "fallback")
        self.assertEqual(model_events[-1]["model_id"], _FALLBACK["model_id"])
        override = fallback.read_override(self._extra(tid))
        self.assertIsNotNone(override)
        self.assertEqual(override["model_id"], _FALLBACK["model_id"])
        self.assertEqual(override["provider_id"], _FALLBACK["provider_id"])
        self.assertEqual(self._extra(tid)["engine_fix_rounds"][0]["model"],
                         "fallback")

    def test_probe_failure_blocks_with_validation_failed(self):
        tid = self._adopt()
        self.call("set-project-fallback-model",
                  {"project_id": PROJECT_ID, "fallback_model": _FALLBACK})
        self._empty_verdict(tid)
        with mock.patch.object(fallback, "probe_model",
                               return_value=(False, "PI-Desktop model 不存在")), \
                mock.patch.object(fallback, "primary_snapshot",
                                  return_value=(_PRIMARY, None)):
            actions = self._actions(tid)
        self.assertEqual(actions[0]["action"], "block")
        self.assertEqual(actions[0]["reason"], "validation_failed")
        self.assertEqual(actions[0]["ladder"], ladder.LADDER_ENVIRONMENTAL)
        # 探针失败：既不换模型，也不记录补救轮。
        self.assertIsNone(fallback.read_override(self._extra(tid)))
        self.assertNotIn("engine_fix_rounds", self._extra(tid))

    def test_shadow_computes_without_writing(self):
        tid = self._adopt()
        self.call("set-project-engine-mode",
                  {"project_id": PROJECT_ID, "engine_mode": "shadow"})
        self._empty_verdict(tid)
        before = self._extra(tid)
        with mock.patch.object(fallback, "probe_model",
                               return_value=(True, "cache 命中")), \
                mock.patch.object(fallback, "primary_snapshot",
                                  return_value=(_PRIMARY, None)):
            actions = self._actions(tid)
        self.assertTrue(actions)
        self.assertTrue(all(a["engine_mode"] == "shadow" and not a.get("executed")
                            for a in actions))
        self.assertEqual(self._extra(tid), before)
        model_kinds = [kind for kind, _ in self._events(tid)
                       if kind in (fallback.EVENT_MODEL, fallback.EVENT_MODEL_HINT)]
        self.assertEqual(model_kinds, [])

    def test_armed_override_is_not_reburned_next_tick(self):
        tid = self._adopt()
        self._empty_verdict(tid)
        self._actions(tid)
        self.assertEqual(len(self._extra(tid)["engine_fix_rounds"]), 1)
        # 同一失败轮次只武装一次：再 tick 不重复计入补救轮。
        self._actions(tid)
        self.assertEqual(len(self._extra(tid)["engine_fix_rounds"]), 1)

    def test_budget_spent_stops_remediating(self):
        tid = self._adopt()
        self._empty_verdict(tid)
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                task = db.require_task(conn, tid)
                db.set_extra(conn, task, util.now_iso(), engine_fix_rounds=[
                    {"plan_sha256": task["plan_sha256"], "model": "primary"},
                    {"plan_sha256": task["plan_sha256"], "model": "fallback"}])
        finally:
            conn.close()
        # 同计划补救轮用尽 → 不再有界补救，回到稳态决策（不下补救命令）。
        actions = self._actions(tid)
        self.assertTrue(actions)
        self.assertNotEqual(actions[0]["action"], "start_fix_round")
        self.assertEqual(len(self._extra(tid)["engine_fix_rounds"]), 2)

    def test_consumed_override_is_cleared_for_a_new_round(self):
        tid = self._adopt()
        self._empty_verdict(tid, run_id="run-1")
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                task = db.require_task(conn, tid)
                db.set_extra(conn, task, util.now_iso(), engine_model_override={
                    "plan_sha256": task["plan_sha256"],
                    "provider_id": "p", "model_id": "m"})
        finally:
            conn.close()
        # 新轮次开始（run_id 与 receipt 不同）→ 覆盖被清除，不静默套用。
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                conn.execute("UPDATE tasks SET run_id=? WHERE task_id=?",
                             ("run-2", tid))
        finally:
            conn.close()
        self._actions(tid)
        self.assertIsNone(fallback.read_override(self._extra(tid)))


class TestDispatchClaimSeam(KanpilotTestCase):
    def _task_with_override(self, override):
        info = self.make_ready_task(task_id=TASK_ID, project_name=REPO,
                                    project_id=PROJECT_ID)
        tid = info["remote"]["id"]
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                task = db.require_task(conn, tid)
                db.set_extra(conn, task, util.now_iso(),
                             engine_model_override=override)
        finally:
            conn.close()
        return tid

    def test_no_override_matches_plain_snapshot(self):
        self.make_ready_task(task_id=TASK_ID, project_name=REPO,
                             project_id=PROJECT_ID)
        conn = db.connect(self.db_path)
        try:
            task = db.require_task(conn, TASK_ID)
            self.assertEqual(exec_config.snapshot_with_model_override(conn, task),
                             exec_config.snapshot(conn))
        finally:
            conn.close()

    def test_override_freezes_into_the_round_snapshot(self):
        tid = self._task_with_override({"provider": "CloseAI",
                                        "model": "google/gemini-3.7-flash",
                                        "plan_sha256": "PLAIN"})
        conn = db.connect(self.db_path)
        try:
            task = db.require_task(conn, tid)
            with db.write_txn(conn):
                conn.execute("UPDATE tasks SET plan_sha256='PLAIN' WHERE task_id=?",
                             (tid,))
            task = db.require_task(conn, tid)
            snapshot = exec_config.snapshot_with_model_override(conn, task)
            self.assertEqual(snapshot["backend"], "pi")
            self.assertEqual(snapshot["model"], "google/gemini-3.7-flash")
            # 覆盖不写回设置。
            self.assertNotEqual(exec_config.load(conn).get("model"),
                                "google/gemini-3.7-flash")
        finally:
            conn.close()

    def test_stale_override_for_another_plan_is_ignored(self):
        tid = self._task_with_override({"provider": "CloseAI",
                                        "model": "other-model",
                                        "plan_sha256": "STALE"})
        conn = db.connect(self.db_path)
        try:
            task = db.require_task(conn, tid)
            self.assertEqual(exec_config.snapshot_with_model_override(conn, task),
                             exec_config.snapshot(conn))
        finally:
            conn.close()


class TestSetProjectFallbackModel(KanpilotTestCase):
    def _mapped(self):
        self.make_ready_task(task_id=TASK_ID, project_name=REPO,
                             project_id=PROJECT_ID)
        revision = self.call("get-project-mappings", {}).get("revision", 0)
        self.call("set-project-mapping", {
            "project_id": PROJECT_ID, "repository": str(self.repos_root / REPO),
            "enabled": True, "expected_revision": revision})

    def _raw_entry(self):
        """Raw mapping entry (the read view does not project entry-level keys)."""
        from kanpilot import project_config
        conn = db.connect(self.db_path)
        try:
            _, _, items, _ = project_config.load_raw_mappings(conn)
            return items.get(PROJECT_ID) or {}
        finally:
            conn.close()

    def test_set_read_and_clear(self):
        self._mapped()
        result = self.call("set-project-fallback-model",
                           {"project_id": PROJECT_ID, "fallback_model": _FALLBACK})
        self.assertEqual(result["fallback_model"], _FALLBACK)
        self.assertEqual(self._raw_entry()["fallback_model"], _FALLBACK)
        # 清理后回到「未配置 = 不换模型」。
        cleared = self.call("set-project-fallback-model",
                            {"project_id": PROJECT_ID})
        self.assertIsNone(cleared["fallback_model"])
        self.assertNotIn("fallback_model", self._raw_entry())

    def test_invalid_shapes_rejected(self):
        self._mapped()
        for raw in ({}, {"provider_id": "p"}, "x", {"provider_id": "p", "model_id": ""}):
            with self.subTest(raw=raw):
                exc = self.call_err("set-project-fallback-model",
                                    {"project_id": PROJECT_ID,
                                     "fallback_model": raw})
                self.assertEqual(getattr(exc, "code", None), "bad_request")

    def test_unknown_project_is_not_found(self):
        exc = self.call_err("set-project-fallback-model",
                            {"project_id": "nope", "fallback_model": _FALLBACK})
        self.assertEqual(getattr(exc, "code", None), "not_found")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
