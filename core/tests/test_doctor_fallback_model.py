"""doctor --live 的 fallback 模型核对（spike-fallback-model-id-20261010.md）。

The Plus catalog is a local cache and can go stale after a Plus upgrade or an
account change, so the spike requires ``doctor --live`` to re-run the model probe
and report a missing fallback id with an actionable fix — never a silent model
switch. This module pins that behaviour: read-only, no token in the output, and
the same ``validation_failed`` invariant the engine enforces at retry time.

Run: ``cd core && python3 -m pytest tests/test_doctor_fallback_model.py -q``.
"""
from __future__ import annotations

import unittest
from unittest import mock

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import cli, core, db, util  # noqa: E402
from kanpilot.engine import fallback  # noqa: E402

PROJECT = "ryryo1reph1xfemqtwkjyiiz"
_FALLBACK = {"provider_id": "707a4c67-4ee0-484d-b635-730372501eaf",
             "model_id": "google/gemini-3.7-flash"}


def _check(checks, name):
    return next((c for c in checks if c["name"] == name), None)


class TestConfiguredModels(unittest.TestCase):
    def test_collects_dedupes_and_sorts(self):
        items = {
            "p1": {"fallback_model": dict(_FALLBACK)},
            "p2": {"fallback_model": dict(_FALLBACK)},          # 重复配置只算一次
            "p3": {"fallback_model": {"provider_id": "aaa", "model_id": "zzz"}},
            "p4": {},                                            # 未配置
            "p5": {"fallback_model": {"provider_id": "x"}},      # 残块 → 忽略
            "p6": {"automation": {"fallback_model": {"provider_id": "bbb",
                                                     "model_id": "yyy"}}},
        }
        self.assertEqual(
            [(m["provider_id"], m["model_id"]) for m in fallback.configured_models(items)],
            [("707a4c67-4ee0-484d-b635-730372501eaf", "google/gemini-3.7-flash"),
             ("aaa", "zzz"), ("bbb", "yyy")])

    def test_empty_when_nothing_configured(self):
        self.assertEqual(fallback.configured_models({}), [])
        self.assertEqual(fallback.configured_models(None), [])
        self.assertEqual(fallback.configured_models({"p": {"automation": {}}}), [])

    def test_recommended_alternates_are_documented(self):
        # spike 结论值 + 同 provider 备选；只作修复建议，不参与切换。
        self.assertEqual(fallback.recommended_default(), _FALLBACK)
        self.assertIn("google/gemini-3.7-flash", fallback.RECOMMENDED_ALTERNATES)
        self.assertGreater(len(fallback.RECOMMENDED_ALTERNATES), 1)


class TestDoctorFallbackCheck(KanpilotTestCase):
    def _map_project(self, model=_FALLBACK):
        self.make_repo("Feedback")   # set-project-mapping 校验仓库路径存在
        revision = self.call("get-project-mappings", {}).get("revision", 0)
        self.call("set-project-mapping", {
            "project_id": PROJECT, "repository": str(self.repos_root / "Feedback"),
            "enabled": True, "expected_revision": revision})
        if model is not None:
            self.call("set-project-fallback-model",
                      {"project_id": PROJECT, "fallback_model": model})

    def _doctor(self, live=True):
        return self.call("doctor", {"online": live})

    def test_unconfigured_is_informational_skip(self):
        self._map_project(model=None)
        result = self._doctor()
        check = _check(result["checks"], "plus_fallback_model")
        self.assertIsNotNone(check)
        self.assertTrue(check["ok"])
        self.assertTrue(check["skipped"])
        self.assertIn("未配置", check["detail"])

    def test_offline_doctor_has_no_fallback_probe(self):
        """非 live 只做本地检查，不该发起 Plus 目录探测。"""
        self._map_project()
        result = self._doctor(live=False)
        names = [c["name"] for c in result["checks"]]
        self.assertNotIn("plus_fallback_model", names)
        self.assertIn("plus", names)

    def test_configured_and_present_is_ok(self):
        self._map_project()
        with mock.patch("kanpilot.pi_desktop_backend.probe_connection",
                        return_value={"ok": True, "detail": "cache 命中"}) as probe:
            result = self._doctor()
        check = _check(result["checks"], "plus_fallback_model")
        self.assertTrue(check["ok"])
        self.assertFalse(check["skipped"])
        self.assertIn("已核对存在", check["detail"])
        self.assertIn("google/gemini-3.7-flash", check["detail"])
        # 只读探测：provider + model 精确匹配，未发起任何写操作。
        # 只数带 provider_id 的调用——_doctor_plus 也会调 probe_connection（不带 model）。
        model_calls = [c for c in probe.call_args_list if c.kwargs.get("provider_id")]
        self.assertEqual(len(model_calls), 1)
        self.assertEqual(model_calls[0].kwargs["provider_id"], _FALLBACK["provider_id"])
        self.assertEqual(model_calls[0].kwargs["model_id"], _FALLBACK["model_id"])

    def test_configured_but_missing_fails_with_actionable_fix(self):
        self._map_project()
        with mock.patch("kanpilot.pi_desktop_backend.probe_connection",
                        return_value={"ok": False,
                                      "detail": "PI-Desktop model 不存在：x"}):
            result = self._doctor()
        check = _check(result["checks"], "plus_fallback_model")
        self.assertFalse(check["ok"])
        self.assertFalse(check["skipped"])
        # 明确可执行的修复项（spike「失效与刷新」要求），且声明不静默换模型。
        self.assertIn("不存在", check["detail"])
        self.assertIn("修复", check["detail"])
        for alternate in fallback.RECOMMENDED_ALTERNATES:
            self.assertIn(alternate, check["detail"])
        self.assertIn("不会静默换模型", check["detail"])
        # 顶层 ok 随该检查项失败（配置了却探不到是真缺陷）。
        self.assertFalse(result["ok"])

    def test_probe_exception_is_reported_not_raised(self):
        self._map_project()
        with mock.patch("kanpilot.pi_desktop_backend.probe_connection",
                        side_effect=RuntimeError("boom")):
            result = self._doctor()
        check = _check(result["checks"], "plus_fallback_model")
        self.assertFalse(check["ok"])
        self.assertIn("探测异常", check["detail"])

    def test_no_credential_in_any_check_output(self):
        """§5.27.6：doctor 的**全部** check detail 都不得外泄凭据。

        范围是整份输出而不只是 plus_fallback_model：既有的 ``plus`` 检查会原样
        透传 ``probe["detail"]``，cmd_doctor 现在对所有 detail 统一过 util.redact
        （protocol §5.22.3），把「靠探针自己构造得正确」变成可执行保证。
        """
        self._map_project()
        # 凭据值一律不得出现；连接文件**路径**是诊断信息（缺文件时 plus 检查
        # 会报它），不算凭据——凭据是文件内容里的 url/token。
        leaky = ("cache 命中 token=ghp_secret "
                 "Authorization: Bearer ghp_another_secret "
                 "api_key=sk-1234567890abcdef accessToken=at-9876543210 "
                 'connection body {"url":"http://127.0.0.1:37123/mcp",'
                 '"token":"ghp_connsecretvalue"}')
        with mock.patch("kanpilot.pi_desktop_backend.probe_connection",
                        return_value={"ok": True, "detail": leaky}):
            result = self._doctor()
        blob = repr(result)
        for secret in ("ghp_secret", "ghp_another_secret", "sk-1234567890abcdef",
                       "at-9876543210", "Bearer ghp_", "ghp_connsecretvalue"):
            self.assertNotIn(secret, blob, secret)
        # 脱敏标记确实出现（不是靠碰巧没有而通过）。
        self.assertIn("«redacted»", blob)
        # plus 与 plus_fallback_model 两项都还在，且 ok 未被脱敏改变。
        for name in ("plus", "plus_fallback_model"):
            check = _check(result["checks"], name)
            self.assertIsNotNone(check)
            self.assertTrue(check["ok"])

    def test_redaction_keeps_useful_diagnostics(self):
        """脱敏不得吃掉排障所需的信息：状态码/模型名/错误原因要保留。"""
        self._map_project()
        with mock.patch("kanpilot.pi_desktop_backend.probe_connection",
                        return_value={"ok": False,
                                      "detail": "PI-Desktop model 不存在："
                                                "google/gemini-3.7-flash"}):
            result = self._doctor()
        detail = _check(result["checks"], "plus_fallback_model")["detail"]
        self.assertIn("google/gemini-3.7-flash", detail)
        self.assertIn("不存在", detail)
        self.assertIn("修复", detail)

    def test_missing_connection_file_is_informational_skip(self):
        """无连接文件时无法核对目录：skip 而不是失败（与 plus 检查同口径）。"""
        self._map_project()
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                db.set_setting(conn, "exec_config",
                               {"backend": "pi_desktop",
                                "pi_desktop": {"connection_file": "/nope/missing.json",
                                               "provider_id": "p", "model_id": "m",
                                               "thinking_level": "high",
                                               "permission_mode": "auto"}})
        finally:
            conn.close()
        with mock.patch("kanpilot.pi_desktop_backend.probe_connection",
                        return_value={"ok": True}) as probe:
            result = self._doctor()
        check = _check(result["checks"], "plus_fallback_model")
        self.assertTrue(check["ok"])
        self.assertTrue(check["skipped"])
        self.assertIn("连接文件", check["detail"])
        self.assertEqual(probe.call_count, 0)   # 没文件就不探针

    def test_multiple_projects_deduped_into_one_probe_each(self):
        self._map_project()
        other = "projfallbackdoc00000b"
        revision = self.call("get-project-mappings", {}).get("revision", 0)
        self.call("set-project-mapping", {
            "project_id": other, "repository": str(self.repos_root / "Feedback"),
            "enabled": True, "expected_revision": revision})
        self.call("set-project-fallback-model",
                  {"project_id": other, "fallback_model": dict(_FALLBACK)})
        with mock.patch("kanpilot.pi_desktop_backend.probe_connection",
                        return_value={"ok": True, "detail": "cache"}) as probe:
            self._doctor()
        # 两个项目配了同一个 fallback：去重后只探一次（不计 plus 检查那次）。
        model_calls = [c for c in probe.call_args_list if c.kwargs.get("provider_id")]
        self.assertEqual(len(model_calls), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
