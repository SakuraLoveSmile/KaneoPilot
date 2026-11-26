"""kp-rt01：评审阶段单轮超时可配置（settings.review_timeout_seconds）。

覆盖：未设置回退 executor_timeout_seconds；显式值优先且只影响 review；
0/负数/布尔/字符串等非法值回退而非报错；``set-config`` 接受 300–14400 秒与
null（恢复跟随执行端），越界与非整数被拒；``overview.settings`` 暴露该键；
工作树槽位租约覆盖最长评审（键值 +300s 余量）。
"""

from __future__ import annotations

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import db, executor_runner, worktree_pool  # noqa: E402


class ReviewTimeoutSettingTest(KanpilotTestCase):
    def _conn(self):
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        return conn

    def test_unset_follows_executor_timeout(self):
        conn = self._conn()
        self.assertEqual(executor_runner._review_timeout_seconds(conn),
                         executor_runner.DEFAULT_TIMEOUT_SECONDS)
        db.set_setting(conn, "executor_timeout_seconds", 1200)
        conn.commit()
        self.assertEqual(executor_runner._review_timeout_seconds(conn), 1200)

    def test_explicit_value_wins(self):
        conn = self._conn()
        db.set_setting(conn, "executor_timeout_seconds", 1200)
        db.set_setting(conn, "review_timeout_seconds", 5400)
        conn.commit()
        self.assertEqual(executor_runner._review_timeout_seconds(conn), 5400)
        # 执行端单轮上限本身不因评审键改变。
        self.assertEqual(executor_runner._timeout_seconds(conn), 1200)

    def test_invalid_values_fall_back(self):
        conn = self._conn()
        db.set_setting(conn, "executor_timeout_seconds", 900)
        for bad in (0, -5, True, "600", []):
            db.set_setting(conn, "review_timeout_seconds", bad)
            conn.commit()
            self.assertEqual(executor_runner._review_timeout_seconds(conn), 900, bad)

    def test_set_config_bounds_and_clear(self):
        self.call("set-config", {"review_timeout_seconds": 300})
        self.assertEqual(self.call("overview", {})["settings"]["review_timeout_seconds"], 300)
        self.call("set-config", {"review_timeout_seconds": 14400})
        self.assertEqual(self.call("overview", {})["settings"]["review_timeout_seconds"], 14400)
        self.call("set-config", {"review_timeout_seconds": None})
        self.assertIsNone(self.call("overview", {})["settings"]["review_timeout_seconds"])

    def test_set_config_rejects_out_of_range(self):
        for bad in (299, 14401):
            exc = self.call_err("set-config", {"review_timeout_seconds": bad})
            self.assertEqual(exc.code, "bad_request")
            self.assertIn("review_timeout_seconds", exc.message)
        # 非整数（布尔/字符串）在 schema 层被拒，同样不能落库。
        for bad in (True, "600"):
            exc = self.call_err("set-config", {"review_timeout_seconds": bad})
            self.assertEqual(exc.code, "bad_request")
        self.assertIsNone(self.call("overview", {})["settings"]["review_timeout_seconds"])

    def test_pool_lease_covers_long_review(self):
        conn = self._conn()
        db.set_setting(conn, "review_timeout_seconds", 7200)
        conn.commit()
        self.assertGreaterEqual(worktree_pool._lease_seconds(conn), 7500)

    def test_overview_settings_expose_key_by_default(self):
        settings = self.call("overview", {})["settings"]
        self.assertIn("review_timeout_seconds", settings)
        self.assertIsNone(settings["review_timeout_seconds"])


if __name__ == "__main__":
    import unittest
    unittest.main()