"""New local task branches against disposable Git repositories and FakeKaneo."""
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

from helpers import KanpilotTestCase
from kanpilot import cmd_dispatch, core, db, git_delivery, pi_desktop_planner, planner_inputs, task_workspace, util, worktree_pool, planner_runner


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True, env=git_delivery.git_env()).stdout.strip()


class TaskWorkspaceTest(KanpilotTestCase):
    def repository(self):
        repo = self.make_repo("Feedback")
        git(repo, "init", "--initial-branch=main")
        git(repo, "config", "user.name", "Workspace Fixture")
        git(repo, "config", "user.email", "fixture@example.invalid")
        (repo / "tracked.txt").write_text("committed baseline\n")
        git(repo, "add", "tracked.txt")
        git(repo, "commit", "-m", "baseline")
        (repo / "tracked.txt").write_text("primary dirty\n")
        (repo / "user-note.txt").write_text("preserve primary untracked\n")
        return repo

    def start(self, task_id="newlocaltask0001", backend="codex"):
        self.fake.add_task(task_id, "example-project-05")
        prepared = self.call("prepare-plan-input", {"task_id": task_id})
        return self.call("start-plans", {"items": [{"input_id": prepared["input_id"],
            "input_sha256": prepared["input_sha256"], "planner_backend": backend}]})["results"][0]

    def stored(self, result):
        conn = db.connect(self.db_path)
        task = db.require_task(conn, result["task_id"])
        payload = db.request_payload(db.get_request(conn, result["request_id"]))
        return conn, task, payload

    def test_new_local_task_freezes_head_and_preserves_primary(self):
        repo = self.repository()
        before = (git(repo, "rev-parse", "HEAD"), git(repo, "status", "--porcelain"))
        result = self.start()
        conn, task, payload = self.stored(result)
        try:
            delivery = core.delivery_of(task)
            self.assertEqual(delivery["workspace_version"], 1)
            self.assertEqual(delivery["mode"], "manual")
            self.assertEqual(delivery["base_sha"], before[0])
            worktree = task_workspace.verify_plan_workspace(task, payload)
            self.assertEqual((worktree / "tracked.txt").read_text(), "committed baseline\n")
            self.assertFalse((worktree / "user-note.txt").exists())
            repeated = self.call("start-plans", {"items": [{"input_id": payload["input_id"],
                                                             "input_sha256": payload["planner_input_sha256"]}]})["results"][0]
            self.assertTrue(repeated["deduplicated"])
            self.assertEqual(git(repo, "remote"), "")
            self.assertEqual(cmd_dispatch._ensure_worktree(task, task["task_id"], "new-run", conn),
                             (worktree, "branch"))
            self.assertIn("不要自行提交", cmd_dispatch._execution_instruction(task, worktree, desktop=True))
        finally:
            conn.close()
        self.assertEqual(before, (git(repo, "rev-parse", "HEAD"), git(repo, "status", "--porcelain")))

    def test_all_new_planner_backend_requests_bind_workspace(self):
        self.repository()
        for backend in ("codex", "claude", "codex_desktop"):
            with self.subTest(backend=backend):
                result = self.start("local" + backend.replace("_", "") + "0001", backend)
                conn, task, payload = self.stored(result)
                try:
                    self.assertEqual(payload["workspace_version"], 1)
                    self.assertEqual(task_workspace.verify_plan_workspace(task, payload), Path(payload["plan_cwd"]))
                finally:
                    conn.close()

    def test_replan_reuses_branch_including_frozen_dirty_changes(self):
        self.repository()
        result = self.start()
        conn, task, payload = self.stored(result)
        delivery = core.delivery_of(task)
        path = Path(delivery["worktree_path"])
        (path / "tracked.txt").write_text("task changes retained\n")
        db.update_request(conn, result["request_id"], util.now_iso(), status="succeeded")
        conn.commit()
        conn.close()
        again = self.start()
        conn, task, current = self.stored(again)
        try:
            self.assertEqual(core.delivery_of(task), delivery)
            self.assertEqual(task_workspace.verify_plan_workspace(task, current), path)
            self.assertNotEqual(current["worktree_state_sha256"], payload["worktree_state_sha256"])
            copied = current["manifest"]["execution_worktree"]
            self.assertEqual(copied["state_sha256"], current["worktree_state_sha256"])
            self.assertEqual(copied["changes"][0]["path"], "tracked.txt")
            with self.assertRaises(util.KanpilotError):
                task_workspace.verify_plan_workspace(task, payload)
            (path / "tracked.txt").write_text("changed after queue\n")
            with self.assertRaises(util.KanpilotError):
                task_workspace.verify_plan_workspace(task, current)
        finally:
            conn.close()

    def test_non_git_and_creation_collision_block_without_plain_fallback(self):
        (self.repos_root / "Feedback").mkdir()
        rejected = self.start()
        self.assertEqual(rejected["code"], "worktree_invalid")
        repo = self.repository()
        git(repo, "branch", "kaneo/newlocaltask0002")
        rejected = self.start("newlocaltask0002")
        self.assertEqual(rejected["code"], "worktree_invalid")
        conn = db.connect(self.db_path)
        try:
            self.assertIsNone(db.active_request_for_key(conn, "plan:newlocaltask0002"))
        finally:
            conn.close()

    def test_wrong_identity_and_missing_fields_cannot_execute_or_plan(self):
        self.repository()
        result = self.start()
        conn, task, payload = self.stored(result)
        try:
            for change in ({"plan_cwd": task["repository"]}, {"delivery_head_sha": "b" * 40},
                           {"delivery_branch": "main"}, {"workspace_version": 2}):
                with self.subTest(change=change), self.assertRaises(util.KanpilotError):
                    task_workspace.verify_plan_workspace(task, dict(payload, **change))
            core.update_delivery(conn, task, util.now_iso(), worktree_path=None)
            task = db.require_task(conn, task["task_id"])
            with self.assertRaises(util.KanpilotError):
                cmd_dispatch._ensure_worktree(task, task["task_id"], "run", conn)
        finally:
            conn.close()

    def test_historical_authorized_plan_is_not_migrated(self):
        repo = self.make_repo("Feedback")
        self.fake.add_task("legacytask0001", "example-project-05")
        prepared = self.call("prepare-plan-input", {"task_id": "legacytask0001"})
        with mock.patch.object(task_workspace, "prepare_for_submission", return_value=None):
            result = self.call("start-plans", {"items": [{"input_id": prepared["input_id"]}]})["results"][0]
        conn, task, payload = self.stored(result)
        try:
            self.assertNotIn("workspace_version", payload)
            self.assertIsNone(task_workspace.verify_plan_workspace(task, payload))
            self.assertIsNone(task_workspace.prepare_for_submission(conn, task["task_id"], str(repo), task))
        finally:
            conn.close()

    def test_created_workspace_survives_rejected_registration_and_dirty_retry(self):
        self.repository()
        with mock.patch.object(planner_inputs, "_submit_one", return_value={
                "task_id": "newlocaltask0001", "code": "conflict", "error": "config changed"}):
            rejected = self.start()
        self.assertEqual(rejected["code"], "conflict")
        conn = db.connect(self.db_path)
        try:
            intent = db.get_setting(conn, "task_workspace_preparing:newlocaltask0001")
            path = Path(intent["delivery"]["worktree_path"])
            (path / "partial.txt").write_text("retain between attempts\n")
            self.assertIsNone(db.get_task(conn, "newlocaltask0001"))
        finally:
            conn.close()
        result = self.start()
        conn, task, payload = self.stored(result)
        try:
            self.assertEqual(task_workspace.verify_plan_workspace(task, payload), path)
            self.assertEqual((path / "partial.txt").read_text(), "retain between attempts\n")
            self.assertIsNone(db.get_setting(conn, "task_workspace_preparing:newlocaltask0001"))
        finally:
            conn.close()

    def test_creation_intent_recovers_interrupted_submission(self):
        self.repository()
        with mock.patch.object(planner_inputs, "_submit_one", side_effect=RuntimeError("interrupted")):
            interrupted = self.start("interruptedtask0001")
        self.assertEqual(interrupted["code"], "internal")
        conn = db.connect(self.db_path)
        try:
            intent = db.get_setting(conn, "task_workspace_preparing:interruptedtask0001")
            path = Path(intent["delivery"]["worktree_path"])
            (path / "retained.txt").write_text("recover me\n")
        finally:
            conn.close()
        result = self.start("interruptedtask0001")
        conn, task, payload = self.stored(result)
        try:
            self.assertEqual(task_workspace.verify_plan_workspace(task, payload), path)
            self.assertEqual((path / "retained.txt").read_text(), "recover me\n")
        finally:
            conn.close()

    def test_independent_review_fix_starts_at_reviewed_feature_commit(self):
        repo = self.repository()
        result = self.start("sourcefeature0001")
        conn, source, _ = self.stored(result)
        try:
            source_delivery = core.delivery_of(source)
            worktree = Path(source_delivery["worktree_path"])
            (worktree / "feature.txt").write_text("feature being repaired\n")
            committed = git_delivery.commit_worktree(dict(source_delivery, repository=str(repo)),
                task_id=source["task_id"], title="feature", execution_run_id="source-run", fix_round=0)
            core.update_delivery(conn, source, util.now_iso(), head_sha=committed["sha"])
            db.set_extra(conn, db.require_task(conn, source["task_id"]), util.now_iso(),
                         review_history=[{"review_key": "fixed-review", "head_sha": committed["sha"]}])
            conn.commit()
        finally:
            conn.close()
        fix = self.fake.add_task("independentfix0001", "example-project-05")
        self.sync_task(fix)
        conn = db.connect(self.db_path)
        try:
            db.update_task(conn, fix["id"], util.now_iso(), plan_kind="review_fix",
                           review_source_task_id=source["task_id"], review_key="fixed-review")
            task = db.require_task(conn, fix["id"])
            prepared = task_workspace.prepare_for_submission(conn, fix["id"], str(repo), task)
            self.assertEqual(prepared["delivery"]["base_sha"], committed["sha"])
            self.assertEqual((Path(prepared["delivery"]["worktree_path"]) / "feature.txt").read_text(),
                             "feature being repaired\n")
            self.assertFalse((repo / "feature.txt").exists())
        finally:
            conn.close()

    def test_plus_materials_preserve_frozen_dirty_task_state_in_snapshot(self):
        repo = self.repository()
        (repo / "deleted.py").write_text("old file\n")
        git(repo, "add", "deleted.py")
        git(repo, "commit", "-m", "baseline deletion target")
        result = self.start()
        conn, task, _ = self.stored(result)
        branch = Path(core.delivery_of(task)["worktree_path"])
        (branch / "tracked.txt").write_bytes(b"dirty tracked\x00binary\n")
        (branch / "new.py").write_text("print('untracked')\n")
        (branch / "new.py").chmod(0o755)
        (branch / "deleted.py").unlink()
        db.update_request(conn, result["request_id"], util.now_iso(), status="succeeded")
        conn.commit()
        conn.close()
        result = self.start()
        conn, task, payload = self.stored(result)
        try:
            run_dir = self.runs_root / "frozen-plan"
            run_dir.mkdir()
            claim = self.call("claim", {"stage": "plan", "executor": "fixture", "request_id": result["request_id"]})
            round_ = pi_desktop_planner._Round(self.db_path, result["request_id"], claim["run_id"],
                task_id=task["task_id"], context={"payload": payload,
                    "input_dir": str(self.app_support / "planner_inputs" / payload["input_id"])},
                task={"repository": str(repo)}, run_dir=run_dir, timeout=2, heartbeat_every=1)
            snapshot, mode, materials = round_._prepare_materials()
            self.assertNotEqual(snapshot, branch)
            self.assertEqual(git(snapshot, "rev-parse", "--abbrev-ref", "HEAD"), "HEAD")
            self.assertEqual((snapshot / "tracked.txt").read_bytes(), (branch / "tracked.txt").read_bytes())
            self.assertEqual((snapshot / "new.py").read_bytes(), (branch / "new.py").read_bytes())
            self.assertTrue((snapshot / "new.py").stat().st_mode & 0o111)
            self.assertFalse((snapshot / "deleted.py").exists())
            self.assertFalse((branch / ".kaneopilot-plan-material").exists())
            self.assertTrue(materials.manifest_path.is_file())
            self.assertEqual(task_workspace.verify_plan_workspace(task, payload), branch)
            self.assertEqual((repo / "tracked.txt").read_text(), "primary dirty\n")
        finally:
            conn.close()

    def test_acquire_binding_race_returns_unexposed_slot_without_changing_new_owner(self):
        repo = self.repository()
        result = self.start()
        claim = self.call("claim", {"stage": "plan", "executor": "fixture", "request_id": result["request_id"]})
        conn, task, payload = self.stored(result)
        original = worktree_pool.acquire
        def taken_during_git(*args, **kwargs):
            lease = original(*args, **kwargs)
            with db.write_txn(conn):
                db.update_request(conn, result["request_id"], util.now_iso(), run_id="replacement-run")
            return lease
        try:
            with mock.patch.object(worktree_pool, "acquire", taken_during_git), self.assertRaises(util.KanpilotError):
                task_workspace.acquire_pool(conn, repo, stage="plan", run_id=claim["run_id"],
                    request_id=result["request_id"], sha=payload["delivery_head_sha"])
            self.assertEqual(conn.execute("SELECT state FROM worktree_slots").fetchone()["state"], "free")
            self.assertEqual(db.get_request(conn, result["request_id"])["run_id"], "replacement-run")
        finally:
            conn.close()

    def test_acquire_binding_failure_preserves_existing_running_or_unknown_owner(self):
        repo = self.repository()
        for state in ("running", "unknown"):
            with self.subTest(state=state):
                result = self.start("binding" + state)
                claim = self.call("claim", {"stage": "plan", "executor": "fixture", "request_id": result["request_id"]})
                conn, task, payload = self.stored(result)
                try:
                    lease = worktree_pool.acquire(conn, repo, stage="plan", holder_id=claim["run_id"], sha=payload["delivery_head_sha"])
                    worktree_pool.mark_owner(conn, **task_workspace.pool_identity(lease), state=state)
                    with db.write_txn(conn):
                        db.update_request(conn, result["request_id"], util.now_iso(), run_id="replacement-run")
                    with self.assertRaises(util.KanpilotError):
                        task_workspace.acquire_pool(conn, repo, stage="plan", run_id=claim["run_id"],
                            request_id=result["request_id"], sha=payload["delivery_head_sha"])
                    current = worktree_pool.validate_lease(conn, **task_workspace.pool_identity(lease))
                    self.assertEqual(current["state"], "leased")
                    self.assertEqual(current["owner_state"], state)
                    self.assertEqual(db.get_request(conn, result["request_id"])["run_id"], "replacement-run")
                    task_workspace.release_pool(self.db_path,
                        {"pool_lease": lease, "request_id": result["request_id"], "send_state": "not_sent"},
                        self.root, unexposed=True)
                    current = worktree_pool.validate_lease(conn, **task_workspace.pool_identity(lease))
                    self.assertEqual(current["owner_state"], state)
                    with db.write_txn(conn):
                        db.update_request(conn, result["request_id"], util.now_iso(), status="failed")
                finally:
                    conn.close()

    def test_plan_pool_uses_frozen_task_head_after_primary_advances(self):
        repo = self.repository()
        result = self.start()
        claim = self.call("claim", {"stage": "plan", "executor": "fixture", "request_id": result["request_id"]})
        git(repo, "add", "tracked.txt")
        git(repo, "commit", "-m", "advance primary")
        conn, task, payload = self.stored(result)
        job = {"request_id": result["request_id"], "run_id": claim["run_id"]}
        try:
            root = task_workspace.prepare_plan_pool(conn, self.db_path, task, payload, job, self.root / "job.json")
            self.assertEqual(root.name, "slot-1")
            self.assertEqual(git(root, "rev-parse", "HEAD"), payload["delivery_head_sha"])
            self.assertEqual((root / "tracked.txt").read_text(), "committed baseline\n")
            self.assertNotEqual(git(repo, "rev-parse", "HEAD"), payload["delivery_head_sha"])
            self.assertEqual(db.request_payload(db.get_request(conn, result["request_id"]))["plan_cwd"], payload["plan_cwd"])
        finally:
            conn.close()

    def test_codex_real_subprocess_uses_slot_and_releases_after_wait(self):
        self.repository()
        result = self.start()
        conn, task, payload = self.stored(result)
        branch = Path(payload["plan_cwd"])
        conn.close()
        observed = self.root / "observed.json"
        output = {"status": "ready", "plan_markdown": "# 计划\n\n修改入口\n\n```verify\npython3 -m unittest\n```\n", "questions": [], "evidence": []}
        stub = self.root / "codex-observer"
        stub.write_text("#!" + sys.executable + "\nimport json,os,sys\nfrom pathlib import Path\n"
            "prompt=sys.stdin.read()\n"
            "Path(" + repr(str(observed)) + ").write_text(json.dumps({'cwd':os.getcwd(),'argv':sys.argv,'prompt':prompt}))\n"
            "Path(sys.argv[sys.argv.index('-o')+1]).write_text(" + repr(json.dumps(output)) + ")\n"
            "print(json.dumps({'type':'turn.completed'}),flush=True)\n")
        stub.chmod(0o755)
        with mock.patch.dict(os.environ, {"KANPILOT_CODEX_BIN": str(stub), "KANPILOT_CODEX_FAKE": ""}):
            self.assertTrue(planner_runner.run_one(self.db_path, result["request_id"]))
        seen = json.loads(observed.read_text())
        root = Path(seen["cwd"])
        self.assertEqual(root.name, "slot-1")
        self.assertEqual(seen["argv"][seen["argv"].index("-C") + 1], str(root))
        self.assertNotEqual(root, branch.resolve())
        self.assertIn(str(root), seen["prompt"])
        with db.connect(self.db_path) as conn:
            request = db.get_request(conn, result["request_id"])
            self.assertEqual(request["status"], "succeeded")
            self.assertEqual(db.request_payload(request)["plan_cwd"], str(branch))
            self.assertEqual(conn.execute("SELECT state FROM worktree_slots").fetchone()["state"], "free")
        self.assertEqual((branch / "tracked.txt").read_text(), "committed baseline\n")

    def test_plan_pool_detects_clean_source_change_during_acquire(self):
        repo = self.repository()
        result = self.start()
        claim = self.call("claim", {"stage": "plan", "executor": "fixture", "request_id": result["request_id"]})
        conn, task, payload = self.stored(result)
        branch = Path(payload["plan_cwd"])
        original = worktree_pool.acquire
        def change_source(*args, **kwargs):
            lease = original(*args, **kwargs)
            (branch / "tracked.txt").write_text("changed during preparation\n")
            return lease
        job = {"request_id": result["request_id"], "run_id": claim["run_id"]}
        try:
            with mock.patch.object(worktree_pool, "acquire", change_source), self.assertRaises(util.KanpilotError):
                task_workspace.prepare_plan_pool(conn, self.db_path, task, payload, job, self.root / "job.json")
            self.assertEqual(job["send_state"], "not_sent")
            self.assertIn("pool_lease", job)
        finally:
            conn.close()

    def test_plan_pool_preserves_frozen_dirty_baseline_and_returns_clean_slot(self):
        self.repository()
        result = self.start()
        conn, task, payload = self.stored(result)
        branch = Path(payload["plan_cwd"])
        (branch / "tracked.txt").write_text("frozen task changes\n")
        (branch / "new.txt").write_text("frozen untracked\n")
        with db.write_txn(conn):
            db.update_request(conn, result["request_id"], util.now_iso(), status="succeeded")
        conn.close()
        result = self.start()
        claim = self.call("claim", {"stage": "plan", "executor": "fixture", "request_id": result["request_id"]})
        conn, task, payload = self.stored(result)
        job = {"request_id": result["request_id"], "run_id": claim["run_id"]}
        try:
            root = task_workspace.prepare_plan_pool(conn, self.db_path, task, payload, job, self.root / "job.json")
            self.assertEqual((root / "tracked.txt").read_text(), "frozen task changes\n")
            self.assertEqual((root / "new.txt").read_text(), "frozen untracked\n")
            with db.write_txn(conn):
                db.update_request(conn, result["request_id"], util.now_iso(), status="failed")
            task_workspace.release_pool(self.db_path, job, self.root / "evidence")
            self.assertTrue(job["pool_released"], job)
            self.assertEqual((root / "tracked.txt").read_text(), "committed baseline\n")
            self.assertFalse((root / "new.txt").exists())
            self.assertEqual((branch / "tracked.txt").read_text(), "frozen task changes\n")
            self.assertEqual((branch / "new.txt").read_text(), "frozen untracked\n")
        finally:
            conn.close()

    def test_full_pool_keeps_codex_and_claude_requests_queued_without_launch(self):
        repo = self.repository()
        with db.connect(self.db_path) as conn:
            with db.write_txn(conn):
                db.set_setting(conn, "worktree_pool_size", 1)
            worktree_pool.acquire(conn, repo, stage="plan", holder_id="occupied",
                                  sha=git(repo, "rev-parse", "HEAD"))
        launched = self.root / "unexpected-launch"
        stub = self.root / "never-launch-planner"
        stub.write_text("#!" + sys.executable + "\nfrom pathlib import Path\nPath(" + repr(str(launched)) + ").touch()\n")
        stub.chmod(0o755)
        for backend in ("codex", "claude"):
            with self.subTest(backend=backend):
                result = self.start("fullpool" + backend, backend)
                with mock.patch.dict(os.environ, {"KANPILOT_CODEX_FAKE": "", "KANPILOT_CODEX_BIN": str(stub), "KANPILOT_CLAUDE_BIN": str(stub)}):
                    self.assertTrue(planner_runner.run_one(self.db_path, result["request_id"]))
                with db.connect(self.db_path) as conn:
                    request = db.get_request(conn, result["request_id"])
                    self.assertEqual(request["status"], "queued")
                    self.assertIsNone(request["run_id"])
                    self.assertEqual(db.require_task(conn, result["task_id"])["status"], "needs_plan")
                    self.assertIsNotNone(db.get_setting(conn, "worktree_pool_waiting:" + result["request_id"]))
                self.assertFalse(launched.exists())

    def test_unexposed_cleanup_rechecks_owner_inside_release_transaction(self):
        repo = self.repository()
        result = self.start()
        claim = self.call("claim", {"stage": "plan", "executor": "fixture", "request_id": result["request_id"]})
        conn, task, payload = self.stored(result)
        job = {"request_id": result["request_id"], "run_id": claim["run_id"]}
        try:
            task_workspace.prepare_plan_pool(conn, self.db_path, task, payload, job, self.root / "job.json")
            release = worktree_pool.release
            def starts_before_release(*args, **kwargs):
                worktree_pool.mark_owner(conn, **task_workspace.pool_identity(job["pool_lease"]), state="running")
                return release(*args, **kwargs)
            with mock.patch.object(worktree_pool, "release", starts_before_release):
                task_workspace.release_pool(self.db_path, job, self.root, unexposed=True)
            current = worktree_pool.validate_lease(conn, **task_workspace.pool_identity(job["pool_lease"]))
            self.assertEqual(current["owner_state"], "running")
            self.assertEqual(current["state"], "leased")
            self.assertFalse(job.get("pool_released"))
        finally:
            conn.close()
