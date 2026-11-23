"""kp-mag06：Plus inherit 只跟随当前默认配置（忽略并清空历史钉住字段）。"""
from contextlib import closing
from unittest.mock import patch

from helpers import KanpilotTestCase
from kanpilot import constants, db, exec_config

PINNED_INHERIT = {
    "model_source": "inherit",
    "connection_file": "/tmp/kp-mag06/control.json",
    "permission_mode": "auto",
    # Magpie 接管前钉住、如今已被 Plus 停用的服务商，必须被忽略。
    "provider_id": "c43106c3-791e-498a-bafd-58b7e790d528",
    "model_id": "step-5-preview",
    "thinking_level": "high",
}

RESOLVED = {
    "connection_file": "/tmp/kp-mag06/control.json",
    "provider_id": "plugin:local.magpie-piplus:magpie",
    "model_id": "piplus-selected",
    "thinking_level": "high",
}


class PlusInheritTests(KanpilotTestCase):
    def test_merge_drops_pinned_fields_for_inherit(self):
        merged = exec_config.merge_pi_desktop_block(dict(PINNED_INHERIT))
        self.assertEqual(merged["model_source"], "inherit")
        for key in ("provider_id", "model_id", "thinking_level"):
            self.assertNotIn(key, merged)
        self.assertEqual(merged["connection_file"], PINNED_INHERIT["connection_file"])
        self.assertEqual(merged["permission_mode"], "auto")

    def test_merge_keeps_pinned_fields_for_custom(self):
        merged = exec_config.merge_pi_desktop_block(
            dict(PINNED_INHERIT, model_source="custom"))
        self.assertEqual(merged["provider_id"], PINNED_INHERIT["provider_id"])
        self.assertEqual(merged["model_id"], PINNED_INHERIT["model_id"])
        self.assertEqual(merged["thinking_level"], "high")

    def test_probe_inherit_uses_plus_default_not_stale_pin(self):
        seen = {}

        def fake_probe(conn_file, **kwargs):
            seen["conn_file"] = conn_file
            seen.update(kwargs)
            return {"ok": True, "detail": "PI-Desktop 控制面就绪",
                    "tools": set(), "version": "2025-06-18"}

        with patch("kanpilot.pi_desktop_backend.resolve_plus_default",
                   return_value=(dict(RESOLVED), None)), \
             patch("kanpilot.pi_desktop_backend.probe_connection",
                   side_effect=fake_probe):
            result = exec_config._probe_pi_desktop(dict(PINNED_INHERIT))
        self.assertTrue(result["available"], result)
        self.assertEqual(seen["provider_id"], RESOLVED["provider_id"])
        self.assertEqual(seen["model_id"], RESOLVED["model_id"])

    def test_probe_custom_still_reports_disabled_provider(self):
        def fake_probe(conn_file, **kwargs):
            self.assertEqual(kwargs["provider_id"], PINNED_INHERIT["provider_id"])
            return {"ok": False,
                    "detail": "PI-Desktop provider 已禁用："
                              + PINNED_INHERIT["provider_id"],
                    "tools": set(), "version": None}

        with patch("kanpilot.pi_desktop_backend.resolve_plus_default",
                   side_effect=AssertionError("custom 不应解析默认")), \
             patch("kanpilot.pi_desktop_backend.probe_connection",
                   side_effect=fake_probe):
            result = exec_config._probe_pi_desktop(
                dict(PINNED_INHERIT, model_source="custom"))
        self.assertFalse(result["available"])
        self.assertIn("已禁用", result["detail"])

    def test_saving_inherit_clears_stored_pins(self):
        saved = self.call("set-planner-config", {
            "backend": "pi_desktop",
            "pi_desktop": dict(PINNED_INHERIT),
        })["config"]
        self.assertEqual(saved["pi_desktop"]["model_source"], "inherit")
        for key in ("provider_id", "model_id", "thinking_level"):
            self.assertNotIn(key, saved["pi_desktop"])
        with closing(db.connect(self.db_path)) as conn:
            stored = db.get_setting(conn, "planner_config")["pi_desktop"]
        for key in ("provider_id", "model_id", "thinking_level"):
            self.assertNotIn(key, stored)
        self.assertEqual(stored["connection_file"], PINNED_INHERIT["connection_file"])

    def test_review_saving_inherit_clears_stored_pins(self):
        saved = self.call("set-review-config", {
            "backend": "pi_desktop",
            "pi_desktop": dict(PINNED_INHERIT),
        })["config"]
        self.assertEqual(saved["pi_desktop"]["model_source"], "inherit")
        for key in ("provider_id", "model_id", "thinking_level"):
            self.assertNotIn(key, saved["pi_desktop"])

    def test_saved_legacy_inherit_pins_probe_ready_via_capabilities(self):
        with closing(db.connect(self.db_path)) as conn:
            db.set_setting(conn, "planner_config", {
                "standard": {"planner_model": "sub/gpt-6.2-sol",
                             "reasoning_effort": "max"},
                "review_fix": {"planner_model": "sub/gpt-6.2-sol",
                               "reasoning_effort": "max"},
                "default_method": "builtin",
                "backend": "pi_desktop",
                "pi_desktop": dict(PINNED_INHERIT),
                "updated_at": "fixture",
            })
            with patch("kanpilot.pi_desktop_backend.resolve_plus_default",
                       return_value=(dict(RESOLVED), None)), \
                 patch("kanpilot.pi_desktop_backend.probe_connection",
                       return_value={"ok": True, "detail": "PI-Desktop 控制面就绪",
                                     "tools": set(constants.PI_DESKTOP_PLAN_TOOLS),
                                     "version": "2025-06-18"}):
                caps = self.call("planner-capabilities", {})["backends"]["pi_desktop"]
        self.assertTrue(caps["available"], caps)


if __name__ == "__main__":
    import unittest
    unittest.main()
