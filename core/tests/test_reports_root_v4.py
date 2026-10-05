"""评审报告根：v4 默认与覆盖（protocol §5.24.5 / automation-contract v4 §2）。

v4 起自动 Review 的报告固定在 ``<APP_SUPPORT>/reports/<task_id>/<run_id>.md``。
``constants.review_reports_root()`` 同时被写入方（review runner）与校验方
（``cmd_register_review``/``dispatch_validate`` 的 ``util.inside``）使用，因此
根一旦不一致，报告就会被拒收或落到规格之外的目录。

本测试锁定：

- 无 ``KANPILOT_REPORTS_ROOT`` 时默认 = ``app_support()/reports``（旧
  ``<LEGACY_ROOT>/KaneoReviewer/reports`` 不再是默认）；
- 显式覆盖仍然生效（隔离夹具/保留旧目录的部署）。
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from kanpilot import constants  # noqa: E402


class ReviewReportsRoot(unittest.TestCase):
    def test_default_is_app_support_reports(self):
        env = {k: v for k, v in os.environ.items()
               if k != "KANPILOT_REPORTS_ROOT"}
        with mock.patch.dict(os.environ, env, clear=True):
            root = constants.review_reports_root()
        self.assertEqual(root, constants.app_support() / "reports")
        # 明确不等于 v3 的历史默认。
        self.assertNotEqual(
            root, constants.legacy_state_root() / "KaneoReviewer" / "reports"
        )

    def test_env_override_wins(self):
        target = Path("/tmp/kaneopilot-t6-reports-root")
        with mock.patch.dict(
            os.environ, {"KANPILOT_REPORTS_ROOT": str(target)}
        ):
            self.assertEqual(constants.review_reports_root(), target)


if __name__ == "__main__":
    unittest.main()
