"""一次性 AI 处理可单独指定模型（protocol §5.27）。"""
from unittest.mock import patch

import pytest

from helpers import KanpilotTestCase
from kanpilot import cli, db, exec_config, util

PI = {"model_source": "custom", "connection_file": "/tmp/pi-desktop/mcp-control.json",
      "provider_id": "closeai", "model_id": "deepseek-v4.1-flash",
      "thinking_level": "max", "permission_mode": "auto"}


class OneShotModelTest(KanpilotTestCase):
    def setUp(self):
        super().setUp()
        self.ctx = cli.Context(self.db_path)
        self.addCleanup(self.ctx.close)
        self.conn = self.ctx.conn
        db.set_setting(self.conn, "exec_config",
                       {"backend": "pi_desktop", "pi_desktop": dict(PI)})
        probe = patch.object(exec_config, "_probe_pi_desktop",
                             return_value={"available": True})
        probe.start()
        self.addCleanup(probe.stop)

    def save(self, fields):
        return exec_config.save(self.conn, fields, self.ctx.now_iso)

    def test_save_freezes_one_shot_model_and_keeps_the_general_config(self):
        saved = self.save({"one_shot": {"model_id": "cheap-model", "thinking_level": "low"}})
        assert saved["one_shot"] == {"model_id": "cheap-model", "thinking_level": "low"}
        assert saved["pi_desktop"]["model_id"] == "deepseek-v4.1-flash"
        action = exec_config.snapshot_for_action(self.conn)
        assert action["backend"] == "pi_desktop"
        assert action["model_id"] == "cheap-model"
        assert action["thinking_level"] == "low"
        assert action["provider_id"] == "closeai"
        assert action["permission_mode"] == "auto"
        general = exec_config.snapshot(self.conn)
        assert general["model_id"] == "deepseek-v4.1-flash"
        assert general["thinking_level"] == "max"

    def test_clearing_the_override_falls_back_to_the_general_config(self):
        self.save({"one_shot": {"model_id": "cheap-model"}})
        saved = self.save({"one_shot": {}})
        assert not saved.get("one_shot")
        assert exec_config.snapshot_for_action(self.conn) == exec_config.snapshot(self.conn)

    def test_override_keeps_the_remaining_fields_inherited(self):
        self.save({"one_shot": {"thinking_level": "low"}})
        action = exec_config.snapshot_for_action(self.conn)
        assert action["model_id"] == "deepseek-v4.1-flash"
        assert action["thinking_level"] == "low"

    def test_override_is_ignored_off_the_plus_branch(self):
        self.save({"one_shot": {"model_id": "cheap-model", "thinking_level": "low"}})
        stored = db.get_setting(self.conn, "exec_config")
        stored["backend"] = "pi"
        db.set_setting(self.conn, "exec_config", stored)
        assert exec_config.snapshot_for_action(self.conn) == exec_config.snapshot(self.conn)

    def test_invalid_or_unknown_override_fields_are_rejected(self):
        with pytest.raises(util.KanpilotError):
            self.save({"one_shot": {"thinking_level": "bogus"}})
        with pytest.raises(util.KanpilotError):
            self.save({"one_shot": {"model": "x"}})
        with pytest.raises(util.KanpilotError):
            self.save({"one_shot": {"model_id": "  "}})
        assert not (db.get_setting(self.conn, "exec_config") or {}).get("one_shot")

    def test_ready_gate_sees_the_override_snapshot(self):
        self.save({"one_shot": {"model_id": "cheap-model", "thinking_level": "low"}})
        seen = {}
        def available(conn, config_snapshot=None):
            seen.update(config_snapshot or {})
            return True, None
        with patch.object(exec_config, "check_available", side_effect=available):
            snapshot = exec_config.snapshot_for_action(self.conn)
            exec_config.check_available(self.conn, config_snapshot=snapshot)
        assert seen["model_id"] == "cheap-model" and seen["thinking_level"] == "low"