"""Codex execution configuration, isolated DB/CLI/provider fixtures only."""
import json
import os
from unittest.mock import patch

from helpers import KanpilotTestCase
from kanpilot import codex_execution, constants, db, exec_config


class CodexExecutionConfigTests(KanpilotTestCase):
    def setUp(self):
        super().setUp()
        self.block = dict(codex_execution.DEFAULT_CONFIG)
        home = constants.codex_home()
        catalog = home / "models.json"
        catalog.write_text(json.dumps({"models": [
            {"slug": self.block["model"], "supported_reasoning_levels": [
                {"effort": "high"}, {"effort": "low"}]}]}))
        config = home / "config.toml"
        config.write_text('model_catalog_json = ' + json.dumps(str(catalog)) + '\n' + config.read_text())

    def connection(self):
        self.call("get-exec-config")
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        return conn

    def test_default_display_does_not_claim_codex_available(self):
        result = self.call("get-exec-config")
        self.assertIsNone(result["config"])
        self.assertEqual(result["defaults"]["codex"], self.block)
        self.assertFalse(self.call("exec-capabilities")["codex"]["available"])
        self.call("set-exec-config", {"reasoning_effort": "low"})
        conn = self.connection()
        self.assertNotIn("codex", db.get_setting(conn, "exec_config"))
        self.assertFalse(self.call("exec-capabilities")["codex"]["available"])

    def test_codex_save_and_availability_do_not_require_pi_or_plus(self):
        with patch.dict(os.environ, {"KANPILOT_PI_BIN": str(self.root / "missing-pi")}):
            self.call("set-exec-config", {"backend": "codex", "codex": self.block})
            caps = self.call("exec-capabilities")
            self.assertTrue(caps["ok"])
            self.assertTrue(caps["codex"]["available"])
            self.assertFalse(caps["checks"][0]["ok"])
            with patch.object(exec_config, "_probe_version", side_effect=AssertionError("pi probe forbidden")):
                self.assertEqual(exec_config.check_available(self.connection()), (True, None))

    def test_snapshot_is_flat_and_changes_only_for_new_round(self):
        self.call("set-exec-config", {"backend": "codex", "codex": self.block,
                                      "execution_question_max_wait_seconds": 42})
        conn = self.connection()
        original = exec_config.snapshot(conn)
        self.assertEqual(original, {"backend": "codex", **self.block,
                                   "sandbox": "workspace-write", "execution_question_max_wait_seconds": 42})
        self.call("set-exec-config", {"codex": {"reasoning_effort": "low"}})
        self.assertEqual(original["reasoning_effort"], "high")
        self.assertEqual(exec_config.snapshot(conn)["reasoning_effort"], "low")
        self.assertEqual(exec_config.check_available(conn, config_snapshot=original), (True, None))

    def test_switching_preserves_all_backend_groups(self):
        plus = {"model_source": "custom", "connection_file": "/tmp/fixture-plus.json",
                "provider_id": "p", "model_id": "m", "thinking_level": "high", "permission_mode": "ask"}
        self.call("set-exec-config", {"backend": "pi_desktop", "pi_desktop": plus})
        self.call("set-exec-config", {"backend": "codex", "codex": self.block})
        result = self.call("set-exec-config", {"backend": "pi"})["config"]
        self.assertEqual(result["codex"], self.block)
        self.assertEqual(result["pi_desktop"], plus)
        self.assertEqual(result["model"], "deepseek-flash")
        self.assertEqual(self.call("set-exec-config", {"backend": "codex"})["config"]["codex"], self.block)

    def test_invalid_blocks_are_rejected_atomically(self):
        for block in (None, [], "bad", {**self.block, "sandbox": "danger-full-access"},
                      {**self.block, "unexpected": True}, {"model": "missing", "reasoning_effort": "high"},
                      {**self.block, "reasoning_effort": "ultra"}):
            with self.subTest(block=block):
                error = self.call_err("set-exec-config", {"backend": "codex", "codex": block})
                self.assertEqual(error.code, "bad_request")
                self.assertIsNone(self.call("get-exec-config")["config"])

    def test_switch_to_unsaved_codex_is_rejected(self):
        error = self.call_err("set-exec-config", {"backend": "codex"})
        self.assertIn("未保存", str(error))
        self.assertIsNone(self.call("get-exec-config")["config"])

    def test_saved_config_is_not_overwritten_by_flat_snapshot(self):
        self.call("set-exec-config", {"backend": "codex", "codex": self.block})
        conn = self.connection()
        snapshot = exec_config.snapshot(conn)
        self.call("set-exec-config", {"codex": {"reasoning_effort": "low"}})
        with patch.object(codex_execution, "capabilities", return_value={"available": True}) as probe:
            self.assertEqual(exec_config.check_available(conn, config_snapshot=snapshot), (True, None))
        self.assertEqual(probe.call_args.args[0]["reasoning_effort"], "high")

    def test_snapshot_cannot_widen_sandbox(self):
        conn = self.connection()
        snapshot = {"backend": "codex", **self.block, "sandbox": "danger-full-access"}
        available, reason = exec_config.check_available(conn, config_snapshot=snapshot)
        self.assertFalse(available)
        self.assertIn("workspace-write", reason)
