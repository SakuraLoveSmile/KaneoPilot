"""Magpie 接管（§5.33，kp-mag01）的隔离测试。

全部用临时 agent.json 与假 codex home；不连 Magpie 网关、不调用任何模型。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kanpilot import claude_cli, codex_provider, magpie_agent, planner_config


class MagpieAgentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "agent.json"

    def write(self, payload):
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        return self.path

    def activate(self, payload=None):
        self.write(payload or {
            "baseURL": "http://127.0.0.1:3425/v1",
            "apiKey": "magpie-test-key",
            "model": "opencode-go/deepseek-v4.1-flash",
        })
        patch = mock.patch.dict(os.environ, {magpie_agent.ENV_PATH: str(self.path)})
        patch.start()
        self.addCleanup(patch.stop)

    def test_no_file_means_no_takeover(self):
        with mock.patch.dict(os.environ, {magpie_agent.ENV_PATH: str(self.path)}):
            state = magpie_agent.status()
            self.assertFalse(state["active"])
            self.assertFalse(state["present"])
            self.assertIsNone(magpie_agent.load())
            self.assertIsNone(magpie_agent.model_override())

    def test_active_normalizes_and_never_exposes_key(self):
        self.activate()
        state = magpie_agent.status()
        self.assertTrue(state["active"])
        self.assertEqual(state["model"], "opencode-go/deepseek-v4.1-flash")
        self.assertTrue(state["has_key"])
        self.assertNotIn("magpie-test-key", json.dumps(state))

    def test_api_key_optional(self):
        self.activate({"baseURL": "http://127.0.0.1:3425/v1",
                       "model": "auto/deepseek-v4.1-flash"})
        state = magpie_agent.status()
        self.assertTrue(state["active"])
        self.assertFalse(state["has_key"])

    def test_non_loopback_base_url_is_not_taken_over(self):
        self.activate({"baseURL": "https://example.com/v1", "model": "m"})
        state = magpie_agent.status()
        self.assertFalse(state["active"])
        self.assertIn("回环", state["reason"])
        self.assertIsNone(magpie_agent.load())

    def test_missing_model_is_not_taken_over(self):
        self.activate({"baseURL": "http://127.0.0.1:3425/v1"})
        state = magpie_agent.status()
        self.assertFalse(state["active"])
        self.assertIn("model", state["reason"])

    def test_corrupt_json_is_not_taken_over(self):
        self.path.write_text("{not json", encoding="utf-8")
        with mock.patch.dict(os.environ, {magpie_agent.ENV_PATH: str(self.path)}):
            state = magpie_agent.status()
        self.assertFalse(state["active"])
        self.assertIn("JSON", state["reason"])

    def test_codex_provider_prefers_magpie(self):
        self.activate()
        resolution = codex_provider.resolve_provider(self.root / "codex-home")
        self.assertEqual(resolution.provider_id, "magpie")
        self.assertTrue(resolution.summary.get("magpie"))
        self.assertIn('model_provider="magpie"', resolution.config_args)
        self.assertIn(
            'model_providers.magpie.base_url="http://127.0.0.1:3425/v1"',
            resolution.config_args)
        self.assertIn('model_providers.magpie.wire_api="responses"',
                      resolution.config_args)
        self.assertTrue(resolution.auth_ready)
        # key 只在 env，不进 argv/config_args
        self.assertNotIn("magpie-test-key", " ".join(resolution.config_args))
        self.assertEqual(resolution.env[codex_provider.PROVIDER_ENV_KEY],
                         "magpie-test-key")

    def test_codex_provider_without_key_skips_env_key(self):
        self.activate({"baseURL": "http://127.0.0.1:3425/v1", "model": "m"})
        resolution = codex_provider.resolve_provider(self.root / "codex-home")
        self.assertFalse([a for a in resolution.config_args if "env_key" in a])
        self.assertEqual(resolution.env, {})

    def test_claude_child_env_follows_gateway(self):
        self.activate()
        env = claude_cli.child_env()
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:3425")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "magpie-test-key")

    def test_planner_model_override_scope(self):
        self.activate({"baseURL": "http://127.0.0.1:3425/v1",
                       "model": "auto/deepseek-v4.1-flash"})
        codex = planner_config._apply_magpie(
            {"planner_model": "gpt-6.1-sol", "reasoning_effort": "high"}, "codex")
        self.assertEqual(codex["planner_model"], "auto/deepseek-v4.1-flash")
        self.assertEqual(codex["model_source"], "magpie")
        claude = planner_config._apply_magpie(
            {"planner_model": "sonnet", "reasoning_effort": "high"}, "claude")
        self.assertEqual(claude["planner_model"], "auto/deepseek-v4.1-flash")
        qoder = planner_config._apply_magpie(
            {"planner_model": "auto", "reasoning_effort": "inherit"}, "qoder")
        self.assertEqual(qoder, {"planner_model": "auto", "reasoning_effort": "inherit"})

    def test_review_model_override_scope(self):
        from kanpilot import review_config

        self.activate({"baseURL": "http://127.0.0.1:3425/v1",
                       "model": "auto/deepseek-v4.1-flash"})
        codex = review_config._apply_magpie_cli(
            {"model": "gpt-6.1-sol", "reasoning_effort": "high"}, "codex")
        self.assertEqual(codex["model"], "auto/deepseek-v4.1-flash")
        self.assertEqual(codex["model_source"], "magpie")
        qoder = review_config._apply_magpie_cli(
            {"model": "auto", "reasoning_effort": "inherit"}, "qoder")
        self.assertEqual(qoder, {"model": "auto", "reasoning_effort": "inherit"})

    def test_execution_snapshot_model_override(self):
        from kanpilot import exec_config

        self.activate({"baseURL": "http://127.0.0.1:3425/v1",
                       "model": "auto/deepseek-v4.1-flash"})
        snapshot = exec_config._apply_magpie_model(
            {"backend": "codex", "model": "gpt-6.1-sol",
             "reasoning_effort": "high", "sandbox": "workspace-write"})
        self.assertEqual(snapshot["model"], "auto/deepseek-v4.1-flash")
        self.assertEqual(snapshot["model_source"], "magpie")
        self.assertEqual(snapshot["reasoning_effort"], "high")
        self.assertEqual(snapshot["sandbox"], "workspace-write")

    def test_execution_snapshot_untouched_without_takeover(self):
        from kanpilot import exec_config

        with mock.patch.dict(os.environ, {magpie_agent.ENV_PATH: str(self.path)}):
            snapshot = exec_config._apply_magpie_model(
                {"backend": "codex", "model": "gpt-6.1-sol"})
        self.assertEqual(snapshot, {"backend": "codex", "model": "gpt-6.1-sol"})

    def test_planner_model_untouched_without_takeover(self):
        with mock.patch.dict(os.environ, {magpie_agent.ENV_PATH: str(self.path)}):
            snapshot = planner_config._apply_magpie(
                {"planner_model": "gpt-6.1-sol", "reasoning_effort": "high"}, "codex")
        self.assertEqual(snapshot, {"planner_model": "gpt-6.1-sol",
                                    "reasoning_effort": "high"})


if __name__ == "__main__":
    unittest.main()
