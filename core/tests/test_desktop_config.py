"""Desktop configuration boundaries with isolated settings and frozen requests."""
from copy import deepcopy
from contextlib import closing
from unittest.mock import patch

from helpers import KanpilotTestCase
from kanpilot import (automation_policy, db, planfile, planner_config, review_config)


class DesktopConfigTests(KanpilotTestCase):
    def test_desktop_save_preserves_cli_groups_without_provider_resolution(self):
        original = {"planner_model": "cli-only-model", "reasoning_effort": "high"}
        self.call("set-planner-config", {"standard": original})
        with patch("kanpilot.cmd_planner._catalog", side_effect=AssertionError("CLI provider read")):
            saved = self.call("set-planner-config", {
                "backend": "codex_desktop", "default_method": "spec_plan"})["config"]
        self.assertEqual(saved["standard"], original)
        self.assertEqual(saved["default_method"], "spec_plan")
        with closing(db.connect(self.db_path)) as conn:
            self.assertEqual(planner_config.snapshot(conn, "standard"), planner_config.DESKTOP_MODEL)
        restored = self.call("set-planner-config", {"backend": "codex"})["config"]
        self.assertEqual(restored["standard"], original)

    def test_plan_request_uses_desktop_source_and_native_skill_without_cli_probe(self):
        remote = self.fake.add_task("desktopconfigplan00001", "ryryo1reph1xfemqtwkjyiiz", status="to-do")
        self.make_repo("Feedback")
        self.sync_task(remote, project_name="Feedback")
        info = self.make_planner_input(remote["id"], manifest_extra={
            "task": {"task_id": remote["id"], "project_name": "Feedback"}})
        self.call("set-planner-config", {"backend": "codex_desktop", "default_method": "spec_plan"})
        with patch("kanpilot.codex_planner.probe_spec_plugin", side_effect=AssertionError("CLI plugin probe")), \
             patch("kanpilot.planner_config.resolve_runtime_config", side_effect=AssertionError("Plus runtime")), \
             patch("kanpilot.cmd_planner._provider_module", side_effect=AssertionError("CLI provider")):
            entry = self.call("start-plans", {"items": [{
                "input_id": info["input_id"], "input_sha256": info["input_sha256"],
                "planner_backend": "codex_desktop", "planner_method": "spec_plan"}]})["results"][0]
        request_id = entry["request_id"]
        frozen = self.call("get-request", {"request_id": request_id})["payload"]
        self.assertEqual(frozen["model_config"], planner_config.DESKTOP_MODEL)
        self.assertEqual(frozen["runtime_config"], planner_config.DESKTOP_RUNTIME)
        self.assertEqual(frozen["planner_skill"], "spec-planner:spec-plan")
        self.assertNotIn("plugin_id", frozen)
        self.assertEqual(planfile.expected_model_snapshot(frozen), frozen["model_config"])
        self.call("set-planner-config", {"backend": "codex", "standard": {
            "planner_model": "different-cli-model", "reasoning_effort": "low"}})
        self.assertEqual(self.call("get-request", {"request_id": request_id})["payload"], frozen)

    def test_first_desktop_switch_saves_legacy_without_plus_runtime(self):
        plus = {"model_source": "inherit", "connection_file": "/tmp/plus/control.json",
                "permission_mode": "auto"}
        with closing(db.connect(self.db_path)) as conn:
            db.set_setting(conn, "review_config", {
                "backend": "pi_desktop", "mode": "inherit", "pi_desktop": plus,
                "updated_at": "before"})
            original = review_config.queue_snapshot(conn)
            with patch("kanpilot.exec_config.resolve_runtime_config", side_effect=AssertionError("Plus connection")), \
                 patch("kanpilot.exec_config.capabilities", side_effect=AssertionError("provider probe")):
                review_config.save(conn, {"backend": "codex_desktop"}, "switch")
            saved = db.get_setting(conn, "review_config")
            self.assertEqual(saved["legacy_config"], original)
            self.assertEqual(review_config.for_request(conn, {}), original)
            self.assertEqual(review_config.backend_for_request(conn, {}), "pi_desktop")
            fresh = review_config.queue_snapshot(conn)
            self.assertEqual(fresh["backend"], "codex_desktop")
            self.assertNotIn("legacy_config", fresh)
            self.assertNotIn("updated_at", fresh)
            review_config.save(conn, {"backend": "codex"}, "next")
            review_config.save(conn, {"backend": "codex_desktop"}, "again")
            self.assertEqual(db.get_setting(conn, "review_config")["legacy_config"], original)

    def test_review_frozen_configuration_survives_setting_changes_and_is_copied(self):
        with closing(db.connect(self.db_path)) as conn:
            review_config.save(conn, {"backend": "codex", "codex": {
                "model": "review-frozen", "reasoning_effort": "high"}}, "first")
            frozen = review_config.queue_snapshot(conn)
            payload = {"review_backend": "codex", "review_config": deepcopy(frozen)}
            review_config.save(conn, {"backend": "codex_desktop"}, "switch")
            selected = review_config.for_request(conn, payload)
            self.assertEqual(selected, frozen)
            self.assertEqual(review_config.backend_for_request(conn, payload), "codex")
            self.assertEqual(review_config.snapshot(conn, selected), {
                "backend": "codex", "model": "review-frozen", "reasoning_effort": "high"})
            selected["codex"]["model"] = "mutated"
            self.assertEqual(payload["review_config"], frozen)

    def test_legacy_review_keeps_original_backend_after_leaving_desktop(self):
        with closing(db.connect(self.db_path)) as conn:
            review_config.save(conn, {"backend": "codex", "codex": {
                "model": "legacy-model", "reasoning_effort": "high"}}, "before")
            original = review_config.queue_snapshot(conn)
            review_config.save(conn, {"backend": "codex_desktop"}, "desktop")
            desktop_payload = {"review_backend": "codex_desktop",
                               "review_config": review_config.queue_snapshot(conn)}
            for backend in ("claude", "codex", "codex_desktop"):
                with self.subTest(backend=backend):
                    fields = {"backend": backend}
                    if backend == "codex":
                        fields["codex"] = {"model": "new-model", "reasoning_effort": "low"}
                    review_config.save(conn, fields, "next")
                    self.assertEqual(review_config.for_request(conn, {}), original)
                    self.assertEqual(review_config.backend_for_request(conn, {}), "codex")
                    self.assertEqual(review_config.snapshot(conn, review_config.for_request(conn, {})), {
                        "backend": "codex", "model": "legacy-model", "reasoning_effort": "high"})
                    self.assertEqual(review_config.for_request(conn, desktop_payload), desktop_payload["review_config"])
                    self.assertEqual(review_config.queue_snapshot(conn)["backend"], backend)

    def test_plus_frozen_override_resolves_its_block_not_current_settings(self):
        block = {"model_source": "custom", "connection_file": "/tmp/frozen-plus.json",
                 "provider_id": "frozen", "model_id": "frozen-model",
                 "thinking_level": "high", "permission_mode": "auto"}
        with closing(db.connect(self.db_path)) as conn:
            review_config.save(conn, {"backend": "codex_desktop"}, "current")
            with patch("kanpilot.exec_config.resolve_runtime_config", return_value=(block, None)) as resolve:
                snapshot = review_config.snapshot(conn, {"backend": "pi_desktop", "pi_desktop": block})
            self.assertEqual(snapshot, {"backend": "pi_desktop", **block})
            self.assertEqual(resolve.call_args.args[1], block)

    def test_automation_readiness_is_configured_state_not_cli_or_live_probe(self):
        with closing(db.connect(self.db_path)) as conn:
            planner_config.save(conn, {"backend": "codex_desktop"}, "now")
            with patch("kanpilot.codex_planner.check_available", side_effect=AssertionError("CLI check")):
                self.assertFalse(automation_policy._planner_ready(conn))
                db.set_setting(conn, "desktop_queue_config", {"enabled": True, "auto_configured": False})
                self.assertFalse(automation_policy._planner_ready(conn))
                db.set_setting(conn, "desktop_queue_config", {"enabled": True, "auto_configured": True})
                self.assertTrue(automation_policy._planner_ready(conn))
            self.assertIn("在线状态未验证", planner_config.desktop_capabilities(conn)["detail"])

    def test_default_legacy_review_never_inherits_desktop(self):
        with closing(db.connect(self.db_path)) as conn:
            review_config.save(conn, {"backend": "codex_desktop"}, "first")
            self.assertEqual(review_config.backend_for_request(conn, {}), "pi")
            self.assertEqual(review_config.for_request(conn, {}), {"backend": "pi", "mode": "inherit"})
