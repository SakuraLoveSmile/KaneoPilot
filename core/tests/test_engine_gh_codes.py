"""P1: delivery-layer gh failure codes are ladder-2 environmental, not the coarse
gh_unavailable (ladder 4). gh_client distinguishes missing binary / timeout /
auth; cmd_delivery_pr maps them to gh_missing / gh_timeout / gh_not_logged_in.

Run: ``cd core && python3 -m pytest tests/test_engine_gh_codes.py -q``.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from test_delivery_pr import DeliveryPR  # noqa: E402
from kanpilot import constants, gh_client  # noqa: E402
from kanpilot.engine import attention, ladder  # noqa: E402


class TestDeliveryGhCodes(DeliveryPR):
    def test_missing_binary_maps_gh_missing(self):
        with patch.object(gh_client, "pr_list",
                          side_effect=gh_client.GhError("missing", "no gh")):
            self.assertEqual(self.publish_delivery()["failure_code"], "gh_missing")

    def test_timeout_maps_gh_timeout(self):
        with patch.object(gh_client, "pr_list",
                          side_effect=gh_client.GhError("timeout", "slow")):
            self.assertEqual(self.publish_delivery()["failure_code"], "gh_timeout")

    def test_auth_maps_gh_not_logged_in(self):
        with patch.object(gh_client, "pr_list",
                          side_effect=gh_client.GhError("auth", "no auth")):
            self.assertEqual(self.publish_delivery()["failure_code"], "gh_not_logged_in")

    def test_other_non_transient_stays_gh_unavailable(self):
        with patch.object(gh_client, "pr_list",
                          side_effect=gh_client.GhError("rejected", "bad")):
            self.assertEqual(self.publish_delivery()["failure_code"], "gh_unavailable")

    def test_gh_codes_are_ladder2_and_resumable(self):
        for code in ("gh_missing", "gh_timeout", "gh_not_logged_in"):
            with self.subTest(code=code):
                self.assertEqual(ladder.classify(code), ladder.LADDER_ENVIRONMENTAL)
                self.assertEqual(attention.reason_code_for(code), attention.GH_ENVIRONMENT)
                self.assertIn(code, constants.AUTOMATION_FAILURE_CODES)


class TestGhClientKinds(unittest.TestCase):
    def test_missing_binary_is_kind_missing(self):
        with patch.dict(os.environ, {"KANPILOT_GH_BIN": "/nonexistent/gh-xyz"}):
            with self.assertRaises(gh_client.GhError) as ctx:
                gh_client._run(["--version"], json_output=False)
        self.assertEqual(ctx.exception.kind, "missing")


if __name__ == "__main__":
    unittest.main()
