"""Local Claude availability probe; isolated command tests never call a model."""
import subprocess
from types import SimpleNamespace
from unittest import mock

import pytest

from helpers import KanpilotTestCase
from kanpilot import cmd_read


@pytest.mark.parametrize("returncode", [0, 9])
def test_claude_version_probe_only(returncode):
    result = SimpleNamespace(returncode=returncode, stdout="2.0.1 (Claude Code)\n", stderr="secret")
    with mock.patch("kanpilot.constants.claude_bin", return_value="/isolated/bin/claude"), \
            mock.patch.object(cmd_read.subprocess, "run", return_value=result) as run:
        check = cmd_read._doctor_claude(1.25)
    run.assert_called_once_with(["/isolated/bin/claude", "--version"],
                                capture_output=True, text=True, timeout=1.25)
    assert check["name"] == "claude"
    assert check["ok"] is (returncode == 0)
    assert not check["timed_out"]
    assert "secret" not in check["detail"]


@pytest.mark.parametrize("error,timed_out", [
    (FileNotFoundError(), False), (PermissionError(), False),
    (subprocess.TimeoutExpired(["claude", "--version"], 1), True)])
def test_claude_missing_and_timeout(error, timed_out):
    with mock.patch.object(cmd_read.subprocess, "run", side_effect=error):
        check = cmd_read._doctor_claude(1)
    assert not check["ok"]
    assert check["timed_out"] is timed_out


class DoctorClaudeCommand(KanpilotTestCase):
    def test_live_includes_claude_and_redacts_output_offline_skips_probe(self):
        safe = cmd_read._probe_check("fixture", ok=True, detail="isolated")
        with mock.patch("kanpilot.bridge.load_credentials", return_value=("http://fixture.invalid", "fixture")), \
                mock.patch("kanpilot.codex_planner.check_available", return_value=(True, "isolated")), \
                mock.patch.object(cmd_read, "_doctor_kaneo_ping", return_value=safe), \
                mock.patch.object(cmd_read, "_doctor_gh", return_value=safe), \
                mock.patch.object(cmd_read, "_doctor_plus", return_value=safe), \
                mock.patch.object(cmd_read, "_doctor_plus_fallback_model", return_value=safe), \
                mock.patch.object(cmd_read, "_doctor_claude", return_value={
                    "name": "claude", "ok": True, "detail": "Claude token=secret_probe_token",
                    "skipped": False, "timed_out": False}) as probe:
            offline = self.call("doctor", {})
            probe.assert_not_called()
            self.assertNotIn("claude", [c["name"] for c in offline["checks"]])
            live = self.call("doctor", {"live": True})
            probe.assert_called_once()
        check = next(c for c in live["checks"] if c["name"] == "claude")
        self.assertTrue(check["ok"])
        self.assertNotIn("secret_probe_token", repr(live))
        self.assertIn("«redacted»", check["detail"])
