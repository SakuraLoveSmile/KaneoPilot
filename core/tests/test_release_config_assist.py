"""Release configuration assistance uses isolated DB/repos and a fake Codex CLI."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from helpers import KanpilotTestCase, PROJECTS
from kanpilot import db, release_config_assist as assist, runtime_operations, util


WORKFLOW = """name: Release
run-name: pilot-release-${{ inputs.release_batch_id }}
on:
  workflow_dispatch:
    inputs:
      target_sha: {type: string}
      release_batch_id: {type: string}
      task_manifest_json: {type: string}
      task_manifest_sha256: {type: string}
      tag_name: {type: string}
jobs:
  android:
    name: Android
    runs-on: ubuntu-latest
    steps:
      - run: bash scripts/release.sh
"""
SCRIPT = """#!/bin/sh
# App-android.apk is the exact release asset.
# kaneopilot-provenance uploads kaneopilot-release.json
echo 'fixture, never executed'
"""


class ReleaseConfigAssistTests(KanpilotTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.repos_root / "Music"
        self.repo.mkdir()
        (self.repo / ".github/workflows").mkdir(parents=True)
        (self.repo / "scripts").mkdir()
        self.workflow = self.repo / ".github/workflows/release.yml"
        self.workflow.write_text(WORKFLOW, encoding="utf-8")
        (self.repo / "scripts/release.sh").write_text(SCRIPT, encoding="utf-8")
        (self.repo / "package.json").write_text('{"version":"1.2.3"}', encoding="utf-8")
        self.git("init", "-b", "main")
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-m", "fixture")
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.ctx = SimpleNamespace(conn=self.conn)
        self.payload = {"project_id": PROJECTS["Music"]}
        self.answer = dict(workflow_path=".github/workflows/release.yml", workflow_ref="main",
                           version_base="1.2.3", required_assets=["App-android.apk"],
                           required_jobs=["Android"], summary="根据现有发布工作流填写。",
                           evidence=[".github/workflows/release.yml", "scripts/release.sh", "package.json"],
                           warnings=[])
        self.result_path = self.root / "model-result.json"
        self.invocations = self.root / "invocations.json"
        self.set_answer(self.answer)
        binary = Path(os.environ["KANPILOT_CODEX_BIN"])
        binary.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "prompt = sys.stdin.read()\n"
            f"record = pathlib.Path({str(self.invocations)!r})\n"
            "records = json.loads(record.read_text()) if record.exists() else []\n"
            "records.append({'args':args, 'prompt':prompt, 'cwd':str(pathlib.Path.cwd())})\n"
            "record.write_text(json.dumps(records))\n"
            f"answer = pathlib.Path({str(self.result_path)!r}).read_text()\n"
            "pathlib.Path(args[args.index('-o')+1]).write_text(answer)\n",
            encoding="utf-8")
        binary.chmod(0o755)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def set_answer(self, answer):
        self.result_path.write_text(json.dumps(answer), encoding="utf-8")

    def prepare(self, **extra):
        return assist.cmd_prepare_project_release_assist(self.ctx, dict(self.payload, **extra))

    def analyze(self, operation="assist-1", prepared=None):
        prepared = prepared or self.prepare()
        return assist.cmd_analyze_project_release_assist(self.ctx, dict(
            self.payload, expected_context_sha256=prepared["context_sha256"],
            operation_id=operation, confirm=True))

    def test_prepare_is_read_only_and_analysis_returns_grounded_draft(self):
        before = self.conn.execute("SELECT count(*) FROM runtime_operations").fetchone()[0]
        original_policy = db.get_setting(self.conn, "project_mappings")
        prepared = self.prepare()
        self.assertFalse(self.invocations.exists())
        self.assertEqual(self.conn.execute("SELECT count(*) FROM runtime_operations").fetchone()[0], before)
        self.assertIn("scripts/release.sh", prepared["source_paths"])
        result = self.analyze(prepared=prepared)
        self.assertEqual(result["suggestion"], self.answer)
        self.assertEqual(db.get_setting(self.conn, "project_mappings"), original_policy)
        self.assertEqual(self.git("status", "--porcelain"), "")
        invocation = json.loads(self.invocations.read_text())[0]
        self.assertIn("--ignore-user-config", invocation["args"])
        self.assertIn("--ephemeral", invocation["args"])
        self.assertEqual(invocation["args"][invocation["args"].index("-s") + 1], "read-only")
        self.assertNotEqual(invocation["cwd"], str(self.repo))
        self.assertFalse(Path(invocation["cwd"]).exists())

    def test_real_cli_json_prepare_and_analyze_commands(self):
        code, output, stderr = self.call_main([
            "prepare-project-release-assist", "--db", str(self.db_path),
            "--json", json.dumps(self.payload)])
        self.assertEqual((code, stderr), (0, ""))
        prepared = json.loads(output)
        self.assertEqual(prepared["project_id"], self.payload["project_id"])
        request = dict(self.payload, expected_context_sha256=prepared["context_sha256"],
                       operation_id="cli-json", confirm=True)
        code, output, stderr = self.call_main([
            "analyze-project-release-assist", "--db", str(self.db_path),
            "--json", json.dumps(request)])
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(output)["suggestion"], self.answer)

    def test_accepted_replay_does_not_call_model_even_after_source_changes(self):
        prepared = self.prepare()
        first = self.analyze(prepared=prepared)
        self.workflow.write_text(WORKFLOW + "# changed\n", encoding="utf-8")
        self.assertEqual(self.analyze(prepared=prepared), first)
        self.assertEqual(len(json.loads(self.invocations.read_text())), 1)

    def test_unknown_and_rejected_operations_do_not_resend(self):
        prepared = self.prepare()
        scope = "release-config-assist:" + self.payload["project_id"]
        request = dict(project_id=self.payload["project_id"], repository=None,
                       context_sha256=prepared["context_sha256"])
        runtime_operations.reserve_downstream(self.conn, scope, "assist-1", "core", assist.TOOL, request)
        with self.assertRaisesRegex(util.KanpilotError, "不会重复调用"):
            self.analyze(prepared=prepared)
        self.assertFalse(self.invocations.exists())
        self.set_answer({"wrong": "schema"})
        with self.assertRaisesRegex(util.KanpilotError, "字段不符合约定"):
            self.analyze("assist-2", prepared=prepared)
        self.set_answer(self.answer)
        with self.assertRaisesRegex(util.KanpilotError, "字段不符合约定"):
            self.analyze("assist-2", prepared=prepared)
        self.assertEqual(len(json.loads(self.invocations.read_text())), 1)

    def test_material_and_model_drift_reject_old_confirmation(self):
        prepared = self.prepare()
        self.workflow.write_text(WORKFLOW + "# changed\n", encoding="utf-8")
        with self.assertRaisesRegex(util.KanpilotError, "已变化"):
            self.analyze(prepared=prepared)
        prepared = self.prepare()
        db.set_setting(self.conn, "planner_config", {"standard": {
            "planner_model": "different-model", "reasoning_effort": "low"}})
        self.conn.commit()
        with self.assertRaisesRegex(util.KanpilotError, "已变化"):
            self.analyze(prepared=prepared)
        self.assertFalse(self.invocations.exists())

    def test_source_change_during_analysis_discards_the_result(self):
        prepared = self.prepare()

        def change_source(_snapshot):
            self.workflow.write_text(WORKFLOW + "# edited during analysis\n", encoding="utf-8")
            return self.answer

        with mock.patch.object(assist, "_run_analysis", side_effect=change_source):
            with self.assertRaisesRegex(util.KanpilotError, "分析期间"):
                self.analyze(prepared=prepared)
        receipt = runtime_operations.get_operation(
            self.conn, "release-config-assist:" + self.payload["project_id"], "assist-1")
        self.assertEqual(receipt["status"], "rejected")

    def test_draft_repository_is_supported_and_unknown_project_rejected(self):
        prepared = self.prepare(repository=str(self.repo / "scripts"))
        self.assertEqual(prepared["repository"], str(self.repo))
        with self.assertRaisesRegex(util.KanpilotError, "有效映射"):
            assist.cmd_prepare_project_release_assist(self.ctx, {"project_id": "unknown", "repository": str(self.repo)})

    def test_missing_workflow_and_symlink_are_reported_without_reading_target(self):
        self.workflow.unlink()
        outside = self.root / "credentials-secret.yml"
        outside.write_text("PRIVATE_VALUE_MUST_NOT_BE_COLLECTED", encoding="utf-8")
        self.workflow.symlink_to(outside)
        prepared = self.prepare()
        self.assertNotIn(".github/workflows/release.yml", prepared["source_paths"])
        self.assertTrue(any("符号链接" in text for text in prepared["warnings"]))
        self.assertTrue(any("未找到" in text for text in prepared["warnings"]))

    def test_dynamic_assets_and_jobs_are_not_invented_and_compatibility_is_explained(self):
        self.workflow.write_text("""on: {push: {}}
jobs:
  build:
    name: Build ${{ matrix.os }}
    strategy: {matrix: {os: [linux, macos]}}
    steps:
      - run: echo App-${VERSION}.dmg
""", encoding="utf-8")
        self.set_answer(dict(self.answer, required_assets=["App-1.2.3.dmg"], required_jobs=["Build linux"],
                             evidence=[".github/workflows/release.yml", "package.json"]))
        result = self.analyze()["suggestion"]
        self.assertEqual(result["required_assets"], [])
        self.assertEqual(result["required_jobs"], [])
        for expected in ("workflow_dispatch", "run marker", "provenance", "matrix", "精确安装包"):
            self.assertTrue(any(expected in text for text in result["warnings"]), expected)

    def test_unknown_workflow_ref_and_evidence_are_rejected(self):
        for changed in ({"workflow_path": ".github/workflows/invented.yml"},
                        {"workflow_ref": "not-a-local-ref"},
                        {"evidence": ["../secrets"]}):
            with self.subTest(changed=changed):
                self.set_answer(dict(self.answer, **changed))
                with self.assertRaises(util.KanpilotError):
                    self.analyze("assist-" + next(iter(changed)))

    def test_failure_output_is_not_exposed_and_timeout_stops_child(self):
        binary = Path(os.environ["KANPILOT_CODEX_BIN"])
        binary.write_text("#!/bin/sh\necho 'private-credential-fixture' >&2\nexit 7\n", encoding="utf-8")
        with self.assertRaisesRegex(util.KanpilotError, "退出码 7") as error:
            self.analyze("failed")
        self.assertNotIn("private-credential", error.exception.message)
        pidfile = self.root / "model.pid"
        binary.write_text("#!/usr/bin/env python3\nimport os,time,pathlib\n"
                          f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
                          "time.sleep(30)\n", encoding="utf-8")
        with mock.patch.object(assist, "ANALYSIS_TIMEOUT", .2):
            with self.assertRaisesRegex(util.KanpilotError, "超时"):
                self.analyze("timeout")
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)

    def test_timeout_kills_descendant_even_when_parent_exits_on_term(self):
        binary = Path(os.environ["KANPILOT_CODEX_BIN"])
        child_pid = self.root / "descendant.pid"
        child_code = (
            "import os,pathlib,signal,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid())); "
            "time.sleep(30)")
        binary.write_text(
            "#!/usr/bin/env python3\nimport subprocess,sys,time\n"
            f"subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
            "time.sleep(30)\n", encoding="utf-8")
        with mock.patch.object(assist, "ANALYSIS_TIMEOUT", .5):
            with self.assertRaisesRegex(util.KanpilotError, "超时"):
                self.analyze("descendant-timeout")
        self.assertTrue(child_pid.is_file(), "fake CLI descendant must have started")
        pid = int(child_pid.read_text())
        try:
            for _ in range(20):
                state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                                       capture_output=True, text=True).stdout.strip()
                if not state or state.startswith("Z"):
                    break
                time.sleep(.02)
            self.assertTrue(not state or state.startswith("Z"), "descendant remained running: " + state)
        finally:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_explicit_confirmation_required(self):
        with self.assertRaisesRegex(util.KanpilotError, "confirm=true"):
            assist.cmd_analyze_project_release_assist(self.ctx, self.payload)
        self.assertFalse(self.invocations.exists())
