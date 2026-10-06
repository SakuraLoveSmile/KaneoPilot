"""v2.1 测试（protocol §5.22.3 / planner-fix-implementation §1）：

``codex_provider.resolve_provider`` 服务商解析全分支——内联 token→env、
显式 env_key、默认认证路径、损坏配置报错、model_catalog 只读解析——以及
脱敏断言（假 bearer token 不出现在 config_args / summary）。
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from helpers import (  # noqa: E402
    FAKE_BEARER_TOKEN,
    KanpilotTestCase,
    make_codex_fixture,
)
from kanpilot import codex_provider, constants, util  # noqa: E402


class TestResolveProvider(KanpilotTestCase):
    """resolve_provider 分支覆盖（setUp 已注入 custom/Sub2API 夹具）。"""

    def codex_home(self) -> Path:
        return constants.codex_home()

    def write_config(self, text: str):
        (self.codex_home() / "config.toml").write_text(
            text, encoding="utf-8"
        )

    def test_custom_provider_inline_token(self):
        """默认夹具：model_provider=custom + experimental_bearer_token。"""
        res = codex_provider.resolve_provider(self.codex_home())
        self.assertEqual(res.provider_id, "custom")
        self.assertEqual(res.name, "Sub2API")
        self.assertEqual(res.base_url, "http://127.0.0.1:9/v1")
        self.assertEqual(res.wire_api, "responses")
        self.assertTrue(res.requires_openai_auth)
        self.assertEqual(
            res.env_key, codex_provider.PROVIDER_ENV_KEY)
        self.assertEqual(
            res.env,
            {codex_provider.PROVIDER_ENV_KEY: FAKE_BEARER_TOKEN})
        self.assertTrue(res.auth_ready)
        self.assertTrue(res.auth_detail)

        args = res.config_args
        self.assertIn('model_provider="custom"', args)
        self.assertIn('model_providers.custom.name="Sub2API"', args)
        self.assertIn(
            'model_providers.custom.base_url="http://127.0.0.1:9/v1"', args)
        self.assertIn('model_providers.custom.wire_api="responses"', args)
        self.assertIn(
            "model_providers.custom.requires_openai_auth=true", args)
        self.assertIn(
            'model_providers.custom.env_key='
            f'"{codex_provider.PROVIDER_ENV_KEY}"', args)
        # 脱敏：token 值不得进入任何 config_args / summary。
        self.assertNotIn(FAKE_BEARER_TOKEN, " ".join(args))
        self.assertNotIn(FAKE_BEARER_TOKEN, json.dumps(res.summary))
        self.assertNotIn(
            "experimental_bearer_token", " ".join(args))
        self.assertEqual(set(res.summary), {
            "provider_id", "name", "base_url", "wire_api",
            "requires_openai_auth", "env_key", "auth_ready",
            "auth_detail", "model_catalog_json",
        })

    def test_env_key_path(self):
        """显式 env_key 的 provider：变量名原样沿用，env={}。"""
        new_env = make_codex_fixture(self.root, inline_token=False)
        os.environ.update(new_env)
        self.addCleanup(os.environ.pop, "KANPILOT_TEST_CODEX_KEY", None)

        res = codex_provider.resolve_provider(self.codex_home())
        self.assertEqual(res.env_key, "KANPILOT_TEST_CODEX_KEY")
        self.assertEqual(res.env, {})
        self.assertTrue(res.auth_ready)
        self.assertIn(
            'model_providers.custom.env_key="KANPILOT_TEST_CODEX_KEY"',
            res.config_args)
        self.assertNotIn(
            codex_provider.PROVIDER_ENV_KEY, " ".join(res.config_args))

        # 变量未设值 → 不就绪（实测 codex 0.157.0 直接报
        # Missing environment variable，不回落 auth.json）。
        os.environ.pop("KANPILOT_TEST_CODEX_KEY", None)
        res = codex_provider.resolve_provider(self.codex_home())
        self.assertEqual(res.env_key, "KANPILOT_TEST_CODEX_KEY")
        self.assertFalse(res.auth_ready)
        self.assertIn("KANPILOT_TEST_CODEX_KEY", res.auth_detail)

        # auth.json 缺失时同样不就绪，说明仍点名变量名。
        (self.codex_home() / "auth.json").unlink()
        res = codex_provider.resolve_provider(self.codex_home())
        self.assertFalse(res.auth_ready)
        self.assertIn("KANPILOT_TEST_CODEX_KEY", res.auth_detail)

    def test_requires_openai_auth_only(self):
        """仅 requires_openai_auth（无 token/env_key）：走 auth.json 探测。"""
        self.write_config(
            'model_provider = "custom"\n'
            "[model_providers.custom]\n"
            'name = "Sub2API"\n'
            'base_url = "http://127.0.0.1:9/v1"\n'
            'wire_api = "responses"\n'
            "requires_openai_auth = true\n"
        )
        res = codex_provider.resolve_provider(self.codex_home())
        self.assertIsNone(res.env_key)
        self.assertEqual(res.env, {})
        self.assertTrue(res.auth_ready)  # 夹具 auth.json 存在
        self.assertNotIn(
            "env_key", " ".join(res.config_args))

        (self.codex_home() / "auth.json").unlink()
        res = codex_provider.resolve_provider(self.codex_home())
        self.assertFalse(res.auth_ready)

    def test_no_provider_default_path(self):
        """无 model_provider：config_args=[]、env={}、默认 auth.json 探测。"""
        make_codex_fixture(self.root, with_provider=False)
        res = codex_provider.resolve_provider(self.codex_home())
        self.assertIsNone(res.provider_id)
        self.assertEqual(res.config_args, [])
        self.assertEqual(res.env, {})
        self.assertIsNone(res.env_key)
        self.assertTrue(res.auth_ready)  # 夹具 auth.json 存在
        self.assertIn("默认认证路径", res.auth_detail)
        self.assertEqual(res.summary["provider_id"], None)

    def test_missing_config_toml(self):
        """config.toml 不存在：默认路径，不就绪与否取决于 auth.json。"""
        (self.codex_home() / "config.toml").unlink()
        res = codex_provider.resolve_provider(self.codex_home())
        self.assertIsNone(res.provider_id)
        self.assertEqual(res.config_args, [])
        self.assertTrue(res.auth_ready)  # auth.json 仍在

    def test_broken_toml(self):
        """config.toml 非法 → KanpilotError(validation_failed)，不回落。"""
        self.write_config("model_provider = = broken\n")
        with self.assertRaises(util.KanpilotError) as ctx:
            codex_provider.resolve_provider(self.codex_home())
        self.assertEqual(ctx.exception.code, "validation_failed")

    def test_provider_reference_missing(self):
        """model_provider 指向不存在的 model_providers 段 → 明确报错。"""
        self.write_config(
            'model_provider = "ghost"\n'
            "[model_providers.custom]\n"
            'name = "Sub2API"\n'
            'base_url = "http://127.0.0.1:9/v1"\n'
            'wire_api = "responses"\n'
            f'experimental_bearer_token = "{FAKE_BEARER_TOKEN}"\n'
        )
        with self.assertRaises(util.KanpilotError) as ctx:
            codex_provider.resolve_provider(self.codex_home())
        self.assertEqual(ctx.exception.code, "validation_failed")
        self.assertIn("ghost", ctx.exception.message)

    def test_missing_model_providers_section(self):
        """有 model_provider 但无 model_providers 段 → 报错。"""
        self.write_config('model_provider = "custom"\n')
        with self.assertRaises(util.KanpilotError) as ctx:
            codex_provider.resolve_provider(self.codex_home())
        self.assertEqual(ctx.exception.code, "validation_failed")

    def test_no_auth_method(self):
        """无 token/env_key 且未声明 requires_openai_auth → 认证缺失报错。"""
        self.write_config(
            'model_provider = "custom"\n'
            "[model_providers.custom]\n"
            'name = "Sub2API"\n'
            'base_url = "http://127.0.0.1:9/v1"\n'
            'wire_api = "responses"\n'
        )
        with self.assertRaises(util.KanpilotError) as ctx:
            codex_provider.resolve_provider(self.codex_home())
        self.assertEqual(ctx.exception.code, "validation_failed")
        self.assertIn("认证", ctx.exception.message)

    def test_model_catalog_json_passthrough(self):
        """顶层 model_catalog_json 存在 → config_args 原样沿用。"""
        self.write_config(
            'model_provider = "custom"\n'
            'model_catalog_json = "~/catalog/models.json"\n'
            "[model_providers.custom]\n"
            'name = "Sub2API"\n'
            'base_url = "http://127.0.0.1:9/v1"\n'
            'wire_api = "responses"\n'
            f'experimental_bearer_token = "{FAKE_BEARER_TOKEN}"\n'
        )
        res = codex_provider.resolve_provider(self.codex_home())
        self.assertEqual(res.model_catalog_json, "~/catalog/models.json")
        self.assertIn(
            'model_catalog_json="~/catalog/models.json"', res.config_args)

    def test_sensitive_fields_not_forwarded(self):
        """query_params/http_headers 中认证类键被剔除，非敏感保留。"""
        self.write_config(
            'model_provider = "custom"\n'
            "[model_providers.custom]\n"
            'name = "Sub2API"\n'
            'base_url = "http://127.0.0.1:9/v1"\n'
            'wire_api = "responses"\n'
            f'experimental_bearer_token = "{FAKE_BEARER_TOKEN}"\n'
            'query_params = { region = "cn", api_key = "leak-me" }\n'
            'http_headers = { "X-Team" = "infra", '
            '"Authorization" = "Bearer leak-me" }\n'
        )
        res = codex_provider.resolve_provider(self.codex_home())
        joined = " ".join(res.config_args)
        self.assertIn('query_params={ region = "cn" }', joined)
        self.assertIn('http_headers={ X-Team = "infra" }', joined)
        self.assertNotIn("leak-me", joined)
        self.assertNotIn("api_key", joined)
        self.assertNotIn("Authorization", joined)


class TestModelCatalog(KanpilotTestCase):
    """model_catalog(path)：slug/effort 提取 + 坏文件容错。"""

    def test_catalog_entries(self):
        path = self.root / "models.json"
        path.write_text(json.dumps({"models": [
            {
                "slug": "gpt-6-astra",
                "display_name": "GPT-6-Astra",
                "supported_reasoning_levels": [
                    {"effort": "low"}, {"effort": "medium"},
                    {"effort": "xhigh"}, {"description": "no effort"},
                ],
            },
            {"slug": "gpt-5.6-sol"},          # 无 display_name/levels
            {"display_name": "no slug"},       # 无 slug → 跳过
            "junk",                            # 非 dict → 跳过
        ]}), encoding="utf-8")
        entries = codex_provider.model_catalog(path)
        self.assertEqual(
            [e["id"] for e in entries], ["gpt-6-astra", "gpt-5.6-sol"])
        self.assertEqual(entries[0]["name"], "GPT-6-Astra")
        self.assertEqual(
            entries[0]["reasoning_efforts"], ["low", "medium", "xhigh"])
        self.assertEqual(entries[1]["name"], "gpt-5.6-sol")
        self.assertEqual(entries[1]["reasoning_efforts"], [])

    def test_catalog_bad_files(self):
        self.assertEqual(
            codex_provider.model_catalog(self.root / "absent.json"), [])
        bad = self.root / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        self.assertEqual(codex_provider.model_catalog(bad), [])
        odd = self.root / "odd.json"
        odd.write_text('{"models": {"not": "a list"}}', encoding="utf-8")
        self.assertEqual(codex_provider.model_catalog(odd), [])

    def test_real_catalog_smoke(self):
        """本机真实 models.json 冒烟（存在时）：结构可解析、字段齐。"""
        real = Path.home() / ".codex" / "models.json"
        if not real.is_file():
            self.skipTest("本机无 ~/.codex/models.json")
        entries = codex_provider.model_catalog(real)
        self.assertTrue(entries)
        for entry in entries:
            self.assertTrue(entry["id"])
            self.assertIsInstance(entry["reasoning_efforts"], list)


if __name__ == "__main__":
    unittest.main()
