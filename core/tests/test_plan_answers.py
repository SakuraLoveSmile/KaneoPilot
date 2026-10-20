"""Isolated software clarification persistence/continuation tests."""
import concurrent.futures
import json
import os
from pathlib import Path
from unittest import mock

from helpers import KanpilotTestCase
from kanpilot import (cli, codex_planner, core, db, desktop_queue, plan_answers,
                      plan_questions, planner_inputs, planner_runner, util)

QUESTION = {"id": "button_color", "title": "播放全部采用哪种配色？", "description": "只改按钮配色",
            "options": [{"id": "theme", "label": "跟随主题色", "description": "随主题变化"},
                        {"id": "gray", "label": "固定深灰", "description": "#353743"}],
            "recommended_option_id": "theme", "allow_custom": True, "required": True}


class PlanAnswersTest(KanpilotTestCase):
    def test_workspace_repair_blocks_continuation_but_keeps_answer_draft(self):
        task_id, item, _ = self.blocked()
        with db.connect(self.db_path) as conn:
            db.set_setting(conn, "review_fix_workspace_repair:" + task_id, "repair:pending")
        payload = self.payload(task_id, item)
        self.error("submit-plan-answers", payload, "conflict")
        task = self.call("get-task", {"task_id": task_id})["task"]
        self.assertEqual(task["plan_clarification"]["saved_answers"], payload["answers"])
        self.assertFalse(any(r["status"] in ("queued", "claimed") for r in self.requests()))

    def test_updated_workspace_requires_fresh_input_instead_of_old_snapshot(self):
        task_id, item, _ = self.blocked()
        with db.connect(self.db_path) as conn:
            original = db.request_payload(db.get_request(conn, item["request_id"]))
            original.update(delivery_head_sha="a" * 40, plan_cwd="/old/worktree")
            db.update_request(conn, item["request_id"], util.now_iso(), payload_json=original)
            core.update_delivery(conn, db.require_task(conn, task_id), util.now_iso(),
                                 head_sha="b" * 40, worktree_path="/old/worktree")
        with self.assertRaisesRegex(util.KanpilotError, "重新准备规划材料"):
            self.call("submit-plan-answers", self.payload(task_id, item))
        self.assertFalse(any(r["status"] in ("queued", "claimed") for r in self.requests()))

    def blocked(self, questions=None, legacy=False, backend="codex"):
        task_id = "clarification-task"
        project_id = "ryryo1reph1xfemqtwkjyiiz"
        self.make_repo("Feedback")
        self.fake.add_task(task_id, project_id, status="to-do", title="问题作答", description="改变按钮配色")
        original = self.root / "user-note.md"
        original.write_text("原始附件材料\n")
        prepared = self.call("prepare-plan-input", {"task_id": task_id, "local_paths": [str(original)], "supplement": "原始补充"})
        started = self.call("start-plans", {"items": [{"input_id": prepared["input_id"],
                                                       "input_sha256": prepared["input_sha256"]}]})["results"][0]
        request_id = started["request_id"]
        claim = self.call("claim", {"stage": "plan", "request_id": request_id, "executor": "fixture"})
        self.assertTrue(claim.get("claimed"), claim)
        qs = [QUESTION] if questions is None else questions
        evidence = {"questions": qs}
        if not legacy:
            evidence["plan_markdown"] = "# 原规划草稿\n按钮布局与功能保留"
        else:
            directory = self.runs_root / task_id / claim["run_id"]
            directory.mkdir(parents=True)
            draft = directory / "plan-draft.md"
            draft.write_text("# 历史草稿\n保留既有布局")
            evidence["draft_path"] = str(draft)
        self.call("report", {"request_id": request_id, "run_id": claim["run_id"], "outcome": "needs_user",
                              "failure_code": "reported", "note": "规划需要补充材料", "evidence": evidence})
        if backend != "codex":
            conn = db.connect(self.db_path)
            try:
                request = db.get_request(conn, request_id)
                payload = db.request_payload(request)
                payload["planner_backend"] = backend
                payload["runtime_config"] = {"backend": backend}
                db.update_request(conn, request_id, util.now_iso(), payload_json=payload)
                conn.commit()
            finally:
                conn.close()
        if legacy:
            conn = db.connect(self.db_path)
            try:
                with db.write_txn(conn):
                    task = db.require_task(conn, task_id)
                    extra = db.task_extra(task)
                    extra.pop("plan_clarification", None)
                    db.update_task(conn, task_id, util.now_iso(), extra_json=extra)
            finally:
                conn.close()
        view = self.call("get-task", {"task_id": task_id})["task"]
        return task_id, view["plan_clarification"], prepared

    def payload(self, task_id, clarification, **extra):
        value = {"task_id": task_id, "clarification_id": clarification["id"],
                 "answers": [{"question_id": clarification["questions"][0]["id"], "option_id": "theme"}],
                 "supplement": "仅修改按钮配色", "operation_id": "submit-1"}
        value.update(extra)
        return value

    def error(self, command, payload, code):
        with self.assertRaises(util.KanpilotError) as raised:
            self.call(command, payload)
        self.assertEqual(raised.exception.code, code)

    def test_draft_survives_context_restart_no_default_answer(self):
        task_id, item, _ = self.blocked()
        self.assertEqual(item["saved_answers"], [])
        task = self.call("get-task", {"task_id": task_id})["task"]
        self.assertIn("answer_plan_questions", task["allowed_actions"])
        self.assertNotIn("retry", task["allowed_actions"])
        self.error("resolve", {"task_id": task_id, "action": "retry"}, "conflict")
        payload = self.payload(task_id, item)
        payload.pop("operation_id")
        self.call("save-plan-answers", payload)
        fresh = self.call("get-task", {"task_id": task_id})["task"]
        self.assertEqual(fresh["status"], "needs_user")
        self.assertEqual(fresh["plan_clarification"]["saved_answers"], payload["answers"])
        self.assertEqual(fresh["plan_clarification"]["supplement"], payload["supplement"])
        self.assertIsNotNone(fresh["plan_clarification"]["saved_at"])

    def test_submit_copies_original_material_and_draft_then_ready_keeps_hold(self):
        task_id, item, prepared = self.blocked()
        payload = self.payload(task_id, item)
        result = self.call("submit-plan-answers", payload)
        self.assertFalse(result["deduplicated"])
        self.assertNotEqual(result["request_id"], item["request_id"])
        with db.connect(self.db_path) as conn:
            next_payload = db.request_payload(db.get_request(conn, result["request_id"]))
            old_payload = db.request_payload(db.get_request(conn, item["request_id"]))
        self.assertEqual(next_payload["model_config"], old_payload["model_config"])
        self.assertEqual(next_payload["manifest"]["supplement"], "原始补充")
        details = next_payload["manifest"]["plan_clarification"]
        self.assertIn("原规划草稿", details["plan_markdown"])
        self.assertEqual(details["answers"], payload["answers"])
        self.assertEqual(next_payload["manifest"]["files"], old_payload["manifest"]["files"])
        copy_root = Path(prepared["input_dir"]).parent / next_payload["input_id"]
        for entry in next_payload["manifest"]["files"]:
            self.assertEqual((copy_root / entry["path"]).read_bytes(), (Path(prepared["input_dir"]) / entry["path"]).read_bytes())
        prompt = codex_planner.build_prompt({}, next_payload["manifest"], copy_root)
        self.assertIn("用户已回答的规划问题", prompt)
        self.assertIn("theme", prompt)
        self.assertIn("原规划草稿", prompt)
        with mock.patch.dict(os.environ, {"KANPILOT_CODEX_FAKE": "ready"}):
            self.assertTrue(planner_runner.run_one(self.db_path, result["request_id"]))
        task = self.call("get-task", {"task_id": task_id})["task"]
        self.assertEqual(task["status"], "ready")
        self.assertTrue(task["extra"]["plan_execution_hold"])
        self.assertIn("confirm_dispatch", task["allowed_actions"])
        self.assertIsNone(task["plan_clarification"])
        events = self.call("list-events", {"task_id": task_id})["events"]
        event = next(e for e in events if e["kind"] == "plan_answers_submitted")
        self.assertEqual((event["stage"], event["result"]), ("plan", "succeeded"))
        self.assertIn("跟随主题色", event["summary"])
        self.assertIn("跟随主题色", json.dumps(event, ensure_ascii=False))
        self.assertIn("播放全部", json.dumps(event, ensure_ascii=False))

    def test_second_question_round_preserves_first_decision_and_draft(self):
        task_id, item, _ = self.blocked()
        first = self.call("submit-plan-answers", self.payload(task_id, item))
        claim = self.call("claim", {"stage": "plan", "request_id": first["request_id"], "executor": "fixture"})
        size = {"id": "size", "title": "采用哪个尺寸？", "options": [], "required": True}
        self.call("report", {"request_id": first["request_id"], "run_id": claim["run_id"], "outcome": "needs_user",
                              "failure_code": "reported", "evidence": {"questions": [size], "plan_markdown": ""}})
        second_item = self.call("get-task", {"task_id": task_id})["task"]["plan_clarification"]
        second = self.call("submit-plan-answers", self.payload(task_id, second_item, operation_id="submit-2",
            answers=[{"question_id": "size", "text": "保持原尺寸"}]))
        with db.connect(self.db_path) as conn:
            payload = db.request_payload(db.get_request(conn, second["request_id"]))
        manifest = payload["manifest"]
        self.assertEqual(manifest["plan_clarification_history"][0]["answers"][0]["option_id"], "theme")
        self.assertIn("原规划草稿", manifest["plan_clarification_history"][0]["plan_markdown"])
        prompt = codex_planner.build_prompt({}, manifest, None)
        self.assertIn("theme", prompt)
        self.assertIn("保持原尺寸", prompt)
        self.assertIn("原规划草稿", prompt)
        self.error("save-plan-answers", {"task_id": task_id, "clarification_id": item["id"], "answers": []}, "conflict")

    def test_idempotent_receipt_after_next_plan_completed_and_conflict(self):
        task_id, item, _ = self.blocked()
        payload = self.payload(task_id, item)
        first = self.call("submit-plan-answers", payload)
        with mock.patch.dict(os.environ, {"KANPILOT_CODEX_FAKE": "ready"}):
            planner_runner.run_one(self.db_path, first["request_id"])
        second = self.call("submit-plan-answers", payload)
        self.assertEqual(first["request_id"], second["request_id"])
        self.assertTrue(second["deduplicated"])
        changed = dict(payload, supplement="另一个决定")
        self.error("submit-plan-answers", changed, "conflict")
        self.error("submit-plan-answers", dict(payload, operation_id="new"), "conflict")

    def test_missing_custom_and_required_saved_without_queue(self):
        task_id, item, _ = self.blocked()
        for answers in ([], [{"question_id": "button_color", "option_id": "custom"}]):
            payload = self.payload(task_id, item, answers=answers)
            self.error("submit-plan-answers", payload, "bad_request")
            task = self.call("get-task", {"task_id": task_id})["task"]
            self.assertEqual(task["status"], "needs_user")
            self.assertEqual(task["plan_clarification"]["saved_answers"], answers)
        result = self.call("submit-plan-answers", self.payload(task_id, item,
            answers=[{"question_id": "button_color", "option_id": "custom", "text": "#304040"}]))
        self.assertTrue(result["ok"])

    def test_invalid_option_duplicate_unknown_question_rejected(self):
        task_id, item, _ = self.blocked()
        for answers in ([{"question_id": "button_color", "option_id": "missing"}],
                        [{"question_id": "unknown", "text": "value"}],
                        [{"question_id": "button_color"}, {"question_id": "button_color"}]):
            self.error("submit-plan-answers", self.payload(task_id, item, answers=answers), "bad_request")
        self.assertEqual(self.call("get-task", {"task_id": task_id})["task"]["plan_clarification"]["saved_answers"], [])

    def test_remote_drift_and_unavailable_preserve_answers_and_block(self):
        task_id, item, _ = self.blocked()
        payload = self.payload(task_id, item)
        with mock.patch.object(plan_answers.remote, "get_task", side_effect=RuntimeError("offline")):
            self.error("submit-plan-answers", payload, "remote_unavailable")
        self.fake.tasks[task_id]["description"] = "需求已改变"
        self.error("submit-plan-answers", payload, "conflict")
        task = self.call("get-task", {"task_id": task_id})["task"]
        self.assertEqual(task["status"], "needs_user")
        self.assertEqual(task["plan_clarification"]["saved_answers"], payload["answers"])

    def test_material_tampering_rejected_preserving_original_and_answers(self):
        task_id, item, prepared = self.blocked()
        manifest = json.loads((Path(prepared["input_dir"]) / "manifest.json").read_text())
        entry = next(e for e in manifest["files"] if e["source"] == "local")
        (Path(prepared["input_dir"]) / entry["path"]).write_text("被修改的材料")
        self.error("submit-plan-answers", self.payload(task_id, item), "conflict")
        task = self.call("get-task", {"task_id": task_id})["task"]
        self.assertEqual(task["status"], "needs_user")
        self.assertEqual(task["plan_clarification"]["saved_answers"][0]["option_id"], "theme")

    def test_legacy_attempt_text_question_stable_and_resumes_with_draft(self):
        task_id, item, _ = self.blocked(questions=["A主题色或B灰色，请选择"], legacy=True)
        question = item["questions"][0]
        self.assertEqual(question["options"], [])
        self.assertTrue(question["required"])
        self.assertEqual(item["id"], self.call("get-task", {"task_id": task_id})["task"]["plan_clarification"]["id"])
        result = self.call("submit-plan-answers", self.payload(task_id, item,
            answers=[{"question_id": question["id"], "text": "A，采用主题色"}]))
        with db.connect(self.db_path) as conn:
            payload = db.request_payload(db.get_request(conn, result["request_id"]))
        self.assertIn("历史草稿", payload["manifest"]["plan_clarification"]["plan_markdown"])
        self.assertIn("A，采用主题色", json.dumps(payload, ensure_ascii=False))

    def test_legacy_draft_uses_registered_report_path_after_runs_root_change(self):
        task_id, item, _ = self.blocked(questions=["确认配色"], legacy=True)
        with db.connect(self.db_path) as conn:
            db.set_setting(conn, "session_runs_root", str(self.root / "new-runs-root"))
            conn.commit()
        result = self.call("submit-plan-answers", self.payload(task_id, item,
            answers=[{"question_id": item["questions"][0]["id"], "text": "主题色"}]))
        with db.connect(self.db_path) as conn:
            payload = db.request_payload(db.get_request(conn, result["request_id"]))
        self.assertIn("历史草稿", payload["manifest"]["plan_clarification"]["plan_markdown"])

    def test_local_material_pointer_drift_invalidates_submission(self):
        task_id, item, _ = self.blocked()
        with db.connect(self.db_path) as conn:
            db.update_task(conn, task_id, util.now_iso(), source_sha256="changed-local-material")
            conn.commit()
        self.error("submit-plan-answers", self.payload(task_id, item), "conflict")
        self.assertEqual(self.call("get-task", {"task_id": task_id})["task"]["status"], "needs_user")

    def test_backend_snapshot_retained_desktop(self):
        # Same immutable material and continuation semantics independent of harness.
        task_id, item, _ = self.blocked(backend="codex_desktop")
        result = self.call("submit-plan-answers", self.payload(task_id, item))
        with db.connect(self.db_path) as conn:
            payload = db.request_payload(db.get_request(conn, result["request_id"]))
        self.assertEqual(payload["planner_backend"], "codex_desktop")
        self.assertEqual(payload["runtime_config"], {"backend": "codex_desktop"})
        self.assertNotIn("desktop_claim", payload)

    def test_concurrent_duplicate_only_one_new_request(self):
        task_id, item, _ = self.blocked()
        payload = self.payload(task_id, item)
        # Identical concurrent operations recover the same committed receipt.
        def submit(_):
            try:
                return cli.run("submit-plan-answers", payload, self.db_path)
            except util.KanpilotError as exc:
                return {"code": exc.code}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, range(2)))
        successful = [r for r in results if r.get("ok")]
        self.assertEqual(len(successful), 2, results)
        recovered = self.call("submit-plan-answers", payload)
        self.assertTrue(recovered["deduplicated"])
        self.assertTrue(all(r["request_id"] == recovered["request_id"] for r in successful))
        with db.connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM requests WHERE task_id=? AND kind='plan'", (task_id,)).fetchone()[0], 2)


    def test_receipt_rechecked_after_stale_initial_read(self):
        task_id, item, _ = self.blocked()
        payload = self.payload(task_id, item)
        original = plan_answers._receipt
        count = [0]
        committed = []
        def race(conn, task_id, operation_id, fingerprint):
            count[0] += 1
            if count[0] == 1:
                committed.append(self.call("submit-plan-answers", payload))
                return None  # A stale first receipt read in the competing call.
            return original(conn, task_id, operation_id, fingerprint)
        with mock.patch.object(plan_answers, "_receipt", side_effect=race):
            result = self.call("submit-plan-answers", payload)
        self.assertTrue(result["deduplicated"])
        self.assertEqual(result["request_id"], committed[0]["request_id"])


class QuestionContractTest(KanpilotTestCase):
    def test_codex_parser_and_desktop_accept_structured_and_legacy_questions(self):
        output = {"status": "needs_input", "plan_markdown": "# 草稿", "questions": [QUESTION, "版本范围？"], "evidence": []}
        path = self.root / "result.json"
        path.write_text(json.dumps(output))
        parsed = planner_runner.parse_output(path)
        desktop = desktop_queue._plan_result(output)
        self.assertEqual(parsed, desktop)
        self.assertEqual(parsed["questions"][0], QUESTION)
        self.assertEqual(parsed["questions"][1]["options"], [])
        self.assertNotIn("{'id'", plan_questions.summary(parsed["questions"]))
        self.assertEqual(codex_planner.output_schema()["properties"]["questions"]["items"], plan_questions.QUESTION_SCHEMA)

    def test_invalid_question_container_is_not_silently_dropped(self):
        path = self.root / "bad-result.json"
        for value in (None, QUESTION, "choose a color"):
            path.write_text(json.dumps({"status": "ready", "plan_markdown": "# plan", "questions": value, "evidence": []}))
            with self.assertRaises(util.KanpilotError):
                planner_runner.parse_output(path)

    def test_invalid_structured_questions_never_turn_into_prose(self):
        for question in ({**QUESTION, "recommended_option_id": "missing"},
                         {**QUESTION, "options": [QUESTION["options"][0], QUESTION["options"][0]]},
                         {**QUESTION, "options": [{"id": "custom", "label": "bad"}]},
                         {**QUESTION, "required": "false"}):
            with self.assertRaises(util.KanpilotError):
                plan_questions.normalize([question])
        with self.assertRaises(util.KanpilotError):
            plan_questions.normalize([QUESTION, QUESTION])
