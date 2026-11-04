"""Plus diagnostics follow selected and frozen runtimes, using isolated fixtures."""
import json
from types import SimpleNamespace
from unittest import mock

from helpers import KanpilotTestCase
from kanpilot import cmd_read, db, round_store, util


class DoctorPlusConnections(KanpilotTestCase):
    def setUp(self):
        super().setUp()
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.connection = self.root / "selected-plus.json"
        self.connection.write_text(json.dumps({"active": False}), encoding="utf-8")
        self.default_connection = self.root / "unused-plus.json"
        self.default_connection.write_text(json.dumps({"active": False}), encoding="utf-8")
        patcher = mock.patch("kanpilot.constants.DEFAULT_PI_DESKTOP_CONNECTION_FILE",
                             str(self.default_connection))
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("exec_config", "planner_config", "review_config"):
            db.set_setting(self.conn, key, {"backend": "codex"})

    def _select(self, key, path=None):
        db.set_setting(self.conn, key, {"backend": "pi_desktop", "pi_desktop": {
            "connection_file": str(path or self.connection),
            "model_source": "custom", "provider_id": "fixture-provider",
            "model_id": "fixture-model", "thinking_level": "medium",
            "permission_mode": "auto"}})

    def _run(self):
        return cmd_read._doctor_plus(SimpleNamespace(conn=self.conn), 1.25)

    def test_unused_inactive_saved_connection_and_retired_controller_are_skipped(self):
        db.set_setting(self.conn, "exec_config", {
            "backend": "codex", "controller_backend": "pi_desktop",
            "pi_desktop": {"connection_file": str(self.connection)}})
        with mock.patch("kanpilot.pi_desktop_backend.probe_connection") as probe:
            check = self._run()
        probe.assert_not_called()
        self.assertTrue(check["ok"])
        self.assertTrue(check["skipped"])
        self.assertIn("未使用 Plus", check["detail"])

    def test_each_selected_role_probes_its_own_path_and_reports_inactive(self):
        for key in ("exec_config", "planner_config", "review_config"):
            with self.subTest(role=key):
                self._select(key)
                with mock.patch("kanpilot.pi_desktop_backend.probe_connection", return_value={
                        "ok": False, "detail": "PI-Desktop 控制面未启用（连接文件 active 不是 true）"}) as probe:
                    check = self._run()
                probe.assert_called_once_with(self.connection, timeout=1.25)
                self.assertFalse(check["ok"])
                self.assertFalse(check["skipped"])
                self.assertIn("active", check["detail"])
                db.set_setting(self.conn, key, {"backend": "codex"})

    def test_selected_inactive_connection_fails_real_file_validation_without_network(self):
        self.connection.chmod(0o600)
        self._select("review_config")
        check = self._run()
        self.assertFalse(check["ok"])
        self.assertFalse(check["skipped"])
        self.assertIn("active 不是 true", check["detail"])

    def test_selected_missing_connection_is_failure_even_if_unused_default_exists(self):
        self._select("planner_config", self.root / "missing.json")
        with mock.patch("kanpilot.pi_desktop_backend.probe_connection") as probe:
            check = self._run()
        probe.assert_not_called()
        self.assertFalse(check["ok"])
        self.assertFalse(check["skipped"])
        self.assertIn("连接文件不存在", check["detail"])

    def test_shared_selected_path_is_probed_once(self):
        for key in ("exec_config", "planner_config", "review_config"):
            self._select(key)
        with mock.patch("kanpilot.pi_desktop_backend.probe_connection", return_value={
                "ok": True, "detail": "Plus 控制面就绪"}) as probe:
            check = self._run()
        probe.assert_called_once_with(self.connection, timeout=1.25)
        self.assertTrue(check["ok"])
        self.assertFalse(check["skipped"])
        for role in ("执行", "规划", "评审"):
            self.assertIn(role, check["detail"])

    def test_one_failed_path_does_not_hide_other_paths_or_timeout(self):
        other = self.root / "review-plus.json"
        other.write_text("{}", encoding="utf-8")
        self._select("planner_config")
        self._select("review_config", other)
        with mock.patch("kanpilot.pi_desktop_backend.probe_connection", side_effect=[
                {"ok": False, "detail": "Plus 探测超时"},
                {"ok": True, "detail": "Plus 控制面就绪"}]) as probe:
            check = self._run()
        self.assertEqual(probe.call_count, 2)
        self.assertEqual([call.args[0] for call in probe.call_args_list],
                         [self.connection, other])
        self.assertFalse(check["ok"])
        self.assertTrue(check["timed_out"])
        self.assertIn("超时", check["detail"])
        self.assertIn("就绪", check["detail"])

    def test_frozen_plan_request_keeps_its_path_after_global_backend_changes(self):
        db.create_request(self.conn, util.now_iso(), "plan", "frozen-plan", payload={
            "planner_backend": "pi_desktop",
            "runtime_config": {"connection_file": str(self.connection)}})
        with mock.patch("kanpilot.pi_desktop_backend.probe_connection", return_value={
                "ok": True, "detail": "Plus 控制面就绪"}) as probe:
            self.assertTrue(self._run()["ok"])
        probe.assert_called_once_with(self.connection, timeout=1.25)

    def test_frozen_review_request_keeps_nested_config_after_global_backend_changes(self):
        self._select("review_config")
        db.create_request(self.conn, util.now_iso(), "review", "frozen-review")
        db.set_setting(self.conn, "review_config", {"backend": "codex"})
        with mock.patch("kanpilot.pi_desktop_backend.probe_connection", return_value={
                "ok": True, "detail": "Plus 控制面就绪"}) as probe:
            self.assertTrue(self._run()["ok"])
        probe.assert_called_once_with(self.connection, timeout=1.25)

    def test_unfinished_execution_round_keeps_frozen_path(self):
        round_store.upsert_on_claim(self.conn, "fixture-task", "fixture-execution", {
            "backend": "pi_desktop", "connection_file": str(self.connection)},
            util.now_iso(), backend="pi_desktop")
        with mock.patch("kanpilot.pi_desktop_backend.probe_connection", return_value={
                "ok": False, "detail": "Plus 不可达"}) as probe:
            check = self._run()
        probe.assert_called_once_with(self.connection, timeout=1.25)
        self.assertFalse(check["ok"])
        self.assertFalse(check["skipped"])

    def test_terminal_execution_rounds_do_not_require_plus(self):
        round_store.upsert_on_claim(self.conn, "fixture-task", "fixture-history", {
            "backend": "pi_desktop", "connection_file": str(self.connection)},
            util.now_iso(), backend="pi_desktop")
        for status in ("finished", "failed", "cancelled"):
            with self.subTest(status=status):
                self.conn.execute("UPDATE execution_rounds SET run_status=?", (status,))
                with mock.patch("kanpilot.pi_desktop_backend.probe_connection") as probe:
                    self.assertTrue(self._run()["skipped"])
                probe.assert_not_called()

    def test_terminal_plan_or_review_request_excludes_stale_nested_and_flat_rounds(self):
        task_id = "fixture-planning-task"
        self.make_ready_task(task_id=task_id, publish=False)
        request, _ = db.create_request(self.conn, util.now_iso(), "plan", "past-plan",
                                       task_id=task_id, payload={
                                           "planner_backend": "pi_desktop",
                                           "runtime_config": {"connection_file": str(self.connection)}})
        self.conn.execute("UPDATE requests SET status='failed',run_id='past-run' WHERE request_id=?",
                          (request["request_id"],))
        for stage in ("plan", "review"):
            self.conn.execute("UPDATE requests SET stage=? WHERE request_id=?",
                              (stage, request["request_id"]))
            for config in ({"backend": "pi_desktop", "connection_file": str(self.connection)},
                           {"backend": "pi_desktop", "planner": {"connection_file": str(self.connection)}},
                           {"backend": "pi_desktop", "review": {"connection_file": str(self.connection)}}):
                for status in ("succeeded", "failed", "cancelled"):
                    with self.subTest(stage=stage, config=config, status=status):
                        self.conn.execute("UPDATE requests SET status=? WHERE request_id=?",
                                          (status, request["request_id"]))
                        round_store.upsert_on_claim(self.conn, task_id, "past-run", config,
                                                    util.now_iso(), backend="pi_desktop")
                        self.conn.execute("UPDATE execution_rounds SET run_status='running'")
                        with mock.patch("kanpilot.pi_desktop_backend.probe_connection") as probe:
                            self.assertTrue(self._run()["skipped"])
                        probe.assert_not_called()

    def test_terminal_monitor_request_does_not_hide_unfinished_execution(self):
        task_id = "fixture-execution-task"
        self.make_ready_task(task_id=task_id, publish=False)
        request, _ = db.create_request(self.conn, util.now_iso(), "monitor", "past-monitor",
                                       task_id=task_id)
        self.conn.execute("UPDATE requests SET status='succeeded',run_id='active-execution' WHERE request_id=?",
                          (request["request_id"],))
        round_store.upsert_on_claim(self.conn, task_id, "active-execution", {
            "backend": "pi_desktop", "connection_file": str(self.connection)},
            util.now_iso(), backend="pi_desktop")
        with mock.patch("kanpilot.pi_desktop_backend.probe_connection", return_value={
                "ok": False, "detail": "Plus 不可达"}) as probe:
            self.assertFalse(self._run()["ok"])
        probe.assert_called_once_with(self.connection, timeout=1.25)

    def test_doctor_command_redacts_probe_errors(self):
        self._select("review_config")
        safe = cmd_read._probe_check("fixture", ok=True, detail="isolated")
        with mock.patch("kanpilot.bridge.load_credentials", return_value=("http://fixture.invalid", "fixture")), \
                mock.patch("kanpilot.codex_planner.check_available", return_value=(True, "isolated")), \
                mock.patch.object(cmd_read, "_doctor_kaneo_ping", return_value=safe), \
                mock.patch.object(cmd_read, "_doctor_gh", return_value=safe), \
                mock.patch.object(cmd_read, "_doctor_claude", return_value=safe), \
                mock.patch.object(cmd_read, "_doctor_plus_fallback_model", return_value=safe), \
                mock.patch("kanpilot.pi_desktop_backend.probe_connection", return_value={
                    "ok": False, "detail": "probe failed token=secret_probe_token"}):
            result = self.call("doctor", {"live": True})
        check = next(item for item in result["checks"] if item["name"] == "plus")
        self.assertFalse(check["ok"])
        self.assertNotIn("secret_probe_token", json.dumps(result))
        self.assertIn("«redacted»", check["detail"])
