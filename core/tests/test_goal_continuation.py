"""Goal continuation uses isolated Plus/MCP and SQLite fixtures only."""
import json
from unittest import mock

from kanpilot import cli, cmd_dispatch, db, mcp_tools, pi_desktop_worker, runtime_client, util
import test_pi_desktop_backend as backend_fixture
from test_pi_desktop_backend import (
    PiDesktopCase, TASK_ID, RUN_ID, SESSION_ID, TURN_ID,
)


class TestGoalContinuation(PiDesktopCase):
    def scene(self):
        spec, run_dir = backend_fixture.TestRunReal._base(self)
        config = spec["config"]
        client = runtime_client.RuntimeClient(
            role="execution", connection_file=config["connection_file"],
            project_path=spec["cwd"], runtime_config=config, task_id=TASK_ID,
            run_id=RUN_ID, timeout=10, lease_seconds=30)
        client.connect()
        client.session_create(spec["cwd"], mode="goal")
        job = self.write_lost_observer_scene(spec, run_dir)
        job["scope_id"] = client.scope_id
        job["completion_policy"] = "command_verification"
        (run_dir / "job.json").write_text(json.dumps(job), encoding="utf-8")
        self.server.turn_status = "completed"
        self.server.planning_state = "awaiting_approval"
        self.server.current_turn = TURN_ID
        self.server.plans = [backend_fixture.TestRunReal._goal_contract(self)]
        self.server.execution_turn = "exec-turn"
        self.server.messages = [{"id": "initial", "role": "user",
                                 "content": self.expected_prompt(spec)}]
        original_tool = self.server.tool_call
        def tool_with_history(name, args):
            result = original_tool(name, args)
            if name == "pi_plans_resolve" and args.get("action") == "approve":
                for item in self.server.plans:
                    if item.get("id") == args.get("proposalId") and item.get("status") == "approved":
                        item["version"] += 1
            if name == "pi_plans_pending":
                result["structuredContent"]["history"] = [dict(item) for item in self.server.plans if item.get("status") == "approved"]
            return result
        self.server.tool_call = tool_with_history
        conn = db.connect(self.db_path)
        with db.write_txn(conn):
            conn.execute("UPDATE tasks SET run_id=?,status='running',extra_json=?,plan_path=?,plan_sha256=? WHERE task_id=?",
                         (RUN_ID, util.jdump({"automation": {"enabled": True}}),
                          spec["plan_path"], spec["plan_sha256"], TASK_ID))
        conn.close()
        reader = runtime_client.RuntimeClient(
            role="execution", connection_file=config["connection_file"],
            project_path=spec["cwd"], runtime_config=config, task_id=TASK_ID,
            run_id=RUN_ID, timeout=10, lease_seconds=30, read_only=True)
        reader.connect()
        return spec, run_dir, job, reader

    def test_authorized_resume_approves_and_settles_without_new_prompt(self):
        spec, run_dir, job, reader = self.scene()
        spec["continue_goal_authorization"] = pi_desktop_worker.prepare_goal_continuation(spec, job, reader)
        with self.env(KANPILOT_PI_DESKTOP_FAKE=None), self.fast_clock():
            self.assertEqual(pi_desktop_worker.run_real(spec, resume=True), 0)
        result = self.read_job(run_dir)
        self.assertEqual(result["status"], "finished")
        self.assertEqual(result["goal_approval"]["execution_turn_id"], "exec-turn")
        self.assertEqual(result["resume_previous"]["detail"], job["detail"])
        self.assertEqual(len(self.server.plans_resolve_calls), 1)
        self.assertEqual(self.server.plans_resolve_calls[0]["version"], 3)
        self.assertEqual(self.server.plans[0]["version"], 4)
        self.assertEqual(self.server.prompt_count, 0)
        self.assertIsNone(self.server.configured)

    def test_ordinary_resume_remains_read_only(self):
        spec, run_dir, _, _ = self.scene()
        with self.env(KANPILOT_PI_DESKTOP_FAKE=None), self.fast_clock():
            self.assertEqual(pi_desktop_worker.run_real(spec, resume=True), 3)
        self.assertEqual(self.server.plans_resolve_calls, [])
        self.assertIn("只读续接", self.read_job(run_dir)["resume_detail"])

    def test_later_completed_turn_adopted_after_persistent_network_error(self):
        spec, run_dir, job, reader = self.scene()
        self.server.current_turn = "continued-goal"
        self.server.plans[0]["turnId"] = "continued-goal"
        original = runtime_client.RuntimeClient.turn_get
        def records(client, session, turn, **kwargs):
            record = original(client, session, turn, **kwargs)
            if turn == TURN_ID:
                record.update(status="error", errorCode="NETWORK_ERROR", endedAt="2026-10-09T03:04:32Z")
            else:
                record["endedAt"] = "2026-10-09T03:43:05Z"
            return record
        with mock.patch.object(runtime_client.RuntimeClient, "turn_get", records):
            spec["continue_goal_authorization"] = pi_desktop_worker.prepare_goal_continuation(spec, job, reader)
            with self.env(KANPILOT_PI_DESKTOP_FAKE=None), self.fast_clock():
                self.assertEqual(pi_desktop_worker.run_real(spec, resume=True), 0)
        result = self.read_job(run_dir)
        self.assertEqual(result["turn_adopted"][0]["from"], TURN_ID)
        self.assertEqual(result["turn_adopted"][0]["to"], "continued-goal")
        self.assertEqual(result["goal_continuation_previous_turn"]["errorCode"], "NETWORK_ERROR")
        self.assertEqual(result["status"], "finished")
        self.assertEqual(self.server.plans_resolve_calls[0]["turnId"], "continued-goal")
        self.assertEqual(self.server.plans[0]["version"], 4)
        self.assertEqual(self.server.prompt_count, 0)

    def test_pending_contract_requires_unique_complete_current_identity(self):
        valid = backend_fixture.TestRunReal._goal_contract(self)
        self.assertEqual(pi_desktop_worker._pending_goal_contract({"plans": [valid]}, SESSION_ID, TURN_ID), valid)
        changes = [{"sessionId": None}, {"turnId": None}, {"turnId": "other"},
                   {"toolCallId": ""}, {"id": ""}, {"version": True},
                   {"version": None}, {"status": "expired"}, {"status": "rejected"},
                   {"kind": "plan"}, {"expiresAt": "2000-01-01T00:00:00Z"}]
        for change in changes:
            with self.subTest(change=change):
                self.assertIsNone(pi_desktop_worker._pending_goal_contract(
                    {"plans": [{**valid, **change}]}, SESSION_ID, TURN_ID))
        self.assertIsNone(pi_desktop_worker._pending_goal_contract({"plans": [valid, dict(valid)]}, SESSION_ID, TURN_ID))

    def test_approved_execution_missing_identity_defers(self):
        spec, run_dir, job, reader = self.scene()
        spec["continue_goal_authorization"] = pi_desktop_worker.prepare_goal_continuation(spec, job, reader)
        original = self.server.tool_call
        def missing_identity(name, args):
            result = original(name, args)
            if name == "pi_turn_get" and args.get("turnId") == "exec-turn":
                result["structuredContent"].pop("turnId", None)
            return result
        self.server.tool_call = missing_identity
        with self.env(KANPILOT_PI_DESKTOP_FAKE=None), self.fast_clock():
            self.assertEqual(pi_desktop_worker.run_real(spec, resume=True), 3)
        self.assertIn("goal_execution_unconfirmed", self.read_job(run_dir)["resume_detail"])
        self.assertEqual(self.server.prompt_count, 0)

    def test_approved_execution_added_user_defers(self):
        spec, run_dir, job, reader = self.scene()
        spec["continue_goal_authorization"] = pi_desktop_worker.prepare_goal_continuation(spec, job, reader)
        original = self.server.tool_call
        def added_user(name, args):
            result = original(name, args)
            if name == "pi_plans_resolve":
                self.server.messages.append({"id": "changed", "role": "user", "content": "different task"})
            return result
        self.server.tool_call = added_user
        with self.env(KANPILOT_PI_DESKTOP_FAKE=None), self.fast_clock():
            self.assertEqual(pi_desktop_worker.run_real(spec, resume=True), 3)
        self.assertIn("goal_execution_unconfirmed", self.read_job(run_dir)["resume_detail"])

    def test_preflight_rejects_added_user_or_missing_anchor(self):
        spec, _, job, reader = self.scene()
        self.server.messages.append({"id": "manual", "role": "user", "content": "continue"})
        with self.assertRaisesRegex(util.KanpilotError, "追加用户指令"):
            pi_desktop_worker.prepare_goal_continuation(spec, job, reader)
        self.server.messages = [{"id": "other", "role": "user", "content": "unrelated"}]
        with self.assertRaisesRegex(util.KanpilotError, "原始授权不符"):
            pi_desktop_worker.prepare_goal_continuation(spec, job, reader)
        self.assertEqual(self.server.plans_resolve_calls, [])

    def test_bound_client_forbids_every_other_write_and_replays_receipt(self):
        spec, _, job, reader = self.scene()
        bound = pi_desktop_worker.prepare_goal_continuation(spec, job, reader)
        reader.goal_continuation = bound
        with self.assertRaises(util.KanpilotError):
            reader.agent_prompt(SESSION_ID, "repeat")
        with self.assertRaises(util.KanpilotError):
            reader.plans_resolve("other", SESSION_ID, TURN_ID, bound["tool_call_id"], "approve", target_permission_mode="auto", version=3)
        for _ in range(2):
            reader.plans_resolve(bound["proposal_id"], SESSION_ID, TURN_ID, bound["tool_call_id"], "approve", target_permission_mode="auto", version=3)
        self.assertEqual(len(self.server.plans_resolve_calls), 1)

    def test_pause_changed_during_remote_precheck_blocks_write(self):
        spec, _, job, reader = self.scene()
        bound = pi_desktop_worker.prepare_goal_continuation(spec, job, reader)
        reader.goal_continuation = bound
        original = mcp_tools.harness_plus_adapter.plus_proposals
        def pause(*args, **kwargs):
            result = original(*args, **kwargs)
            conn = db.connect(self.db_path)
            with db.write_txn(conn):
                db.set_setting(conn, "paused_global", True)
            conn.close()
            return result
        with mock.patch.object(mcp_tools.harness_plus_adapter, "plus_proposals", pause):
            with self.assertRaises(util.KanpilotError):
                reader.plans_resolve(bound["proposal_id"], SESSION_ID, TURN_ID, bound["tool_call_id"], "approve", target_permission_mode="auto", version=3)
        self.assertEqual(self.server.plans_resolve_calls, [])

    def test_continuation_only_expired_goal_does_not_launch_observer(self):
        spec, old_dir, job, _ = self.scene()
        run_dir = self.runs_root / TASK_ID / RUN_ID
        run_dir.mkdir(parents=True)
        spec["runs_dir"] = str(run_dir)
        self.write_spec(spec)
        for name in ("job.json", "events.jsonl"):
            (run_dir / name).write_bytes((old_dir / name).read_bytes())
        conn = db.connect(self.db_path)
        with db.write_txn(conn):
            conn.execute("UPDATE execution_rounds SET config_json=? WHERE task_id=? AND run_id=?",
                         (util.jdump(spec["config"]), TASK_ID, RUN_ID))
        conn.close()
        self.server.plans[0]["status"] = "expired"
        ctx = cli.Context(self.db_path)
        try:
            with mock.patch.object(cmd_dispatch.subprocess, "Popen") as launch:
                result = cmd_dispatch.cmd_round_observe(ctx, {"task_id": TASK_ID, "run_id": RUN_ID,
                                                             "continue_goal": True, "continue_goal_only": True})
            self.assertEqual(result["status"], "deferred")
            launch.assert_not_called()
            self.assertEqual(self.server.plans_resolve_calls, [])
        finally:
            ctx.close()

    def test_task_stop_and_ownership_boundaries_block_bound_write(self):
        spec, _, job, reader = self.scene()
        reader.goal_continuation = pi_desktop_worker.prepare_goal_continuation(spec, job, reader)
        bound = reader.goal_continuation
        changes = ["paused_global", "paused_monitor", "paused_dispatch", "auto_paused",
                   "archived", "manual_takeover", "other_run", "review"]
        for change in changes:
            with self.subTest(change=change):
                conn = db.connect(self.db_path)
                with db.write_txn(conn):
                    db.set_setting(conn, "paused_global", change == "paused_global")
                    db.set_setting(conn, "paused_stages", [change.removeprefix("paused_")] if change in ("paused_monitor", "paused_dispatch") else [])
                    conn.execute("UPDATE tasks SET run_id=?,status=?,save_state=?,auto_advance_paused=?,extra_json=? WHERE task_id=?",
                                 ("other" if change == "other_run" else RUN_ID,
                                  "in_review" if change == "review" else "running",
                                  "archived" if change == "archived" else "active", int(change == "auto_paused"),
                                  util.jdump({"automation": {"manual_takeover": change == "manual_takeover"}}), TASK_ID))
                conn.close()
                with self.assertRaises(util.KanpilotError):
                    reader.plans_resolve(bound["proposal_id"], SESSION_ID, TURN_ID, bound["tool_call_id"], "approve", target_permission_mode="auto", version=3)
                self.assertEqual(self.server.plans_resolve_calls, [])
