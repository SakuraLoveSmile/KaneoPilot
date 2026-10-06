from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from helpers import KanpilotTestCase
from kanpilot import bridge, cli, cmd_delivery_pr as commands, constants, core, db, gh_client, git_delivery, remote, util
from test_gh_client import pr as gh_pr
from test_git_delivery import make_repo, branch, commit, git


class DeliveryPR(KanpilotTestCase):
    def setUp(self):
        super().setUp()
        self.repo = make_repo(self.root / "repo")
        self.make_ready_task(task_id="task1234")
        self.ctx = cli.Context(self.db_path)
        self.addCleanup(self.ctx.close)
        self.clock = self.ctx.now
        clock_patch = patch.object(util, "now_dt", side_effect=lambda: self.clock)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        self.delivery = branch(self.repo, self.root / "trees")
        (Path(self.delivery["worktree_path"]) / "fix.txt").write_text("fix")
        result = commit(self.delivery, "exec1")
        self.delivery.update(head_sha=result["sha"], commits=[{"sha": result["sha"], "execution_run_id": "exec1"}],
                             mode="pr", publish_failures=0)
        self.delivery.pop("repository")
        report = constants.review_reports_root() / "task1234" / "review1.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("Review pass")
        report_sha = util.sha256_file(report)
        with db.write_txn(self.ctx.conn):
            db.update_task(self.ctx.conn, "task1234", self.ctx.now_iso, repository=str(self.repo),
                           status="in_review", stage="delivery", run_id="exec1")
            task = self.task()
            core.update_delivery(self.ctx.conn, task, self.ctx.now_iso, **self.delivery)
            core.update_automation(self.ctx.conn, task, self.ctx.now_iso, fix_round=2)
            db.set_extra(self.ctx.conn, self.task(), self.ctx.now_iso,
                         review_registered_at=self.ctx.now_iso, review_conclusion="pass",
                         review_history=[{"review_key": "review1", "conclusion": "pass",
                                          "execution_run_id": "exec1", "head_sha": result["sha"],
                                          "report_path": str(report), "report_sha256": report_sha, "fix_round": 2}],
                         plan_verify={"commands": ["python -m pytest tests -q"]})
            db.update_task(self.ctx.conn, "task1234", self.ctx.now_iso, review_key="review1",
                           review_report_path=str(report), review_report_sha256=report_sha)
            request, _ = db.create_request(self.ctx.conn, self.ctx.now_iso, "delivery", "delivery:test", task_id="task1234")
            db.update_request(self.ctx.conn, request["request_id"], self.ctx.now_iso, status="claimed",
                              run_id="delivery1", lease_expires_at=util.plus_seconds(3600, self.ctx.now))
        self.payload = {"task_id": "task1234", "request_id": request["request_id"], "run_id": "delivery1"}
        self.posts, self.comments, self.order = [], [], []
        self.primary = (git(self.repo, "rev-parse", "HEAD"), git(self.repo, "status", "--porcelain"))
        self.addCleanup(lambda: self.assertEqual(self.primary, (git(self.repo, "rev-parse", "HEAD"), git(self.repo, "status", "--porcelain"))))
        def outside(label, result):
            def call(*args, **kw):
                self.assertFalse(self.ctx.conn.in_transaction, label + " IO inside transaction")
                self.order.append(label)
                return result(*args, **kw) if callable(result) else result
            return call
        self.outside = outside
        real_parser = git_delivery.github_repository
        parser_patch = patch.object(git_delivery, "github_repository", side_effect=lambda url:
                                   "owner/repo" if url == str(self.repo.parent / "origin.git") else real_parser(url))
        parser_patch.start()
        self.addCleanup(parser_patch.stop)
        real_git = git_delivery._git
        for module, name, result in ((git_delivery, "_git", real_git),
            (git_delivery, "origin_github_repo", "owner/repo"), (gh_client, "pr_list", [self.pr()]),
            (gh_client, "pr_create", self.pr()), (gh_client, "pr_view", self.pr()),
            (remote, "get_client", object()), (remote, "get_task", {"task": {"status": "in-review"}}),
            (remote, "find_marker_comment", lambda *_: self.comments[0] if self.comments else None),
            (remote, "post_comment", self.post)):
            patcher = patch.object(module, name, side_effect=outside(name, result))
            patcher.start()
            self.addCleanup(patcher.stop)

    def pr(self, state="OPEN", number=7, **changes):
        return dict(gh_pr(state, number), **dict({"headRefOid": self.delivery["head_sha"]}, **changes))

    def post(self, client, task_id, content):
        self.posts.append(content)
        self.comments.append({"id": "comment1", "content": content})
        return self.comments[-1]

    def task(self):
        return db.require_task(self.ctx.conn, "task1234")

    def update(self, **changes):
        with db.write_txn(self.ctx.conn):
            core.update_delivery(self.ctx.conn, self.task(), self.ctx.now_iso, **changes)

    def publish_delivery(self):
        return commands.cmd_delivery_publish(self.ctx, self.payload)

    def sync(self):
        return commands.cmd_delivery_sync(self.ctx, self.payload)

    def test_push_real_bare_reuses_open_idempotent_comment(self):
        self.assertEqual(self.publish_delivery()["outcome"], "published")
        self.assertEqual(self.publish_delivery()["outcome"], "published")
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0], "[kaneopilot:delivery:task1234] 已创建 PR：https://github.com/owner/repo/pull/7")
        self.assertLess(self.order.index("find_marker_comment"), self.order.index("post_comment"))
        self.assertEqual(git(self.repo.parent / "origin.git", "rev-parse", "refs/heads/kaneo/task1234"), self.delivery["head_sha"])
        self.assertTrue(Path(self.delivery["worktree_path"]).is_dir())
        value = core.delivery_of(self.task())
        self.assertNotIn("repository", value)
        self.assertEqual(value["publish_failures"], 0)
        self.assertEqual(util.parse_ts(value["next_sync_at"]) - self.ctx.now, timedelta(minutes=5))
        for kind in ("delivery_pushed", "pr_opened", "kaneo_comment_posted"):
            self.assertEqual(self.ctx.conn.execute("SELECT count(*) FROM events WHERE kind=?", (kind,)).fetchone()[0], 1)

    def test_reuses_merged_even_with_closed(self):
        with patch.object(gh_client, "pr_list", side_effect=self.outside("pr_list", [self.pr("CLOSED", 1), self.pr("MERGED")])):
            self.assertEqual(self.publish_delivery()["outcome"], "published")
        self.assertEqual(core.delivery_of(self.task())["pr_state"], "MERGED")

    def test_only_closed_blocks(self):
        with patch.object(gh_client, "pr_list", side_effect=self.outside("pr_list", [self.pr("CLOSED")])):
            self.assertEqual(self.publish_delivery()["failure_code"], "pr_closed")
        self.assertEqual(self.task()["status"], "needs_user")
        self.assertFalse(self.posts)

    def test_create_body_title_and_crash_comment_recovery(self):
        self.comments.append({"id": "prior-post-before-crash"})
        with patch.object(gh_client, "pr_list", side_effect=self.outside("pr_list", [])), patch.object(
                gh_client, "pr_create", side_effect=self.outside("pr_create", self.pr())) as create:
            self.assertEqual(self.publish_delivery()["outcome"], "published")
        body = create.call_args.kwargs["body"]
        for text in ("task_id: task1234", "修复轮数：2", self.delivery["head_sha"][:12],
                     "python -m pytest tests -q", "由 KaneoPilot 自动创建", "kaneo(task1234):"):
            self.assertIn(text, body)
        self.assertNotIn(str(self.root), body)
        self.assertLessEqual(len(create.call_args.kwargs["title"]), 100)
        self.assertFalse(self.posts)

    def test_body_redacts_paths_hosts_and_credentials(self):
        with db.write_txn(self.ctx.conn):
            db.update_task(self.ctx.conn, "task1234", self.ctx.now_iso,
                           title="problem /Users/local/private token=secret12345 https://u:p@server.invalid")
            db.set_extra(self.ctx.conn, self.task(), self.ctx.now_iso,
                         plan_verify={"commands": ["python /Users/local/check.py --token=secret12345"]})
        body = commands.build_pr_body(self.task(), self.delivery)
        for secret in ("/Users/", "secret12345", "u:p@"):
            self.assertNotIn(secret, body)

    def test_push_rejected_blocks_without_gh(self):
        with patch.object(git_delivery, "push_branch", side_effect=git_delivery.GitDeliveryError("branch_diverged", "rejected")):
            self.assertEqual(self.publish_delivery()["failure_code"], "branch_diverged")
        self.assertNotIn("pr_list", self.order)

    def test_transient_three_failures_and_success_reset(self):
        with patch.object(gh_client, "pr_list", side_effect=gh_client.GhError("transient", "down")):
            self.assertEqual(self.publish_delivery()["outcome"], "retry")
            self.assertEqual(self.publish_delivery()["publish_failures"], 2)
        self.assertEqual(self.publish_delivery()["outcome"], "published")
        self.assertEqual(core.delivery_of(self.task())["publish_failures"], 0)
        with patch.object(git_delivery, "push_branch", side_effect=git_delivery.GitDeliveryError("push_failed", "down")):
            self.assertEqual(self.publish_delivery()["outcome"], "retry")
            self.assertEqual(self.publish_delivery()["outcome"], "retry")
            self.assertEqual(self.publish_delivery()["failure_code"], "gh_unavailable")
        self.assertEqual(core.delivery_of(self.task())["publish_failures"], 3)

    def test_auth_not_counted_and_credentials_never_persist(self):
        with patch.object(gh_client, "pr_list", side_effect=gh_client.GhError("auth", "https://u:secret@github.com token=secret12345")):
            self.assertEqual(self.publish_delivery()["failure_code"], "gh_unavailable")
        self.assertEqual(core.delivery_of(self.task())["publish_failures"], 0)
        self.assertNotIn("secret", self.task()["failure_reason"])

    def test_comment_failure_retries_without_duplicate_post(self):
        def uncertain(*args):
            self.post(*args)
            raise bridge.BridgeError("lost response")
        with patch.object(remote, "post_comment", side_effect=uncertain):
            self.assertEqual(self.publish_delivery()["outcome"], "retry")
        self.assertEqual(self.publish_delivery()["outcome"], "published")
        self.assertEqual(len(self.posts), 1)

    def test_identity_and_concurrent_changes(self):
        with self.assertRaises(util.KanpilotError) as caught:
            commands.cmd_delivery_publish(self.ctx, dict(self.payload, run_id="other"))
        self.assertEqual(caught.exception.code, "stale_run")
        def concurrent(*args, **kwargs):
            self.update(head_sha="changed")
            return self.pr()
        with patch.object(gh_client, "pr_list", return_value=[]), patch.object(gh_client, "pr_create", side_effect=concurrent):
            with self.assertRaises(util.KanpilotError) as caught:
                self.publish_delivery()
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(core.delivery_of(self.task())["head_sha"], "changed")

    def test_sync_merged_does_not_advance_and_event_idempotent(self):
        self.update(pr_number=7, pushed_sha=self.delivery["head_sha"])
        with patch.object(gh_client, "pr_view", side_effect=self.outside("pr_view", self.pr("MERGED"))):
            self.assertEqual(self.sync(), {"outcome": "merged"})
            self.assertEqual(self.sync(), {"outcome": "merged"})
        self.assertEqual(self.task()["status"], "in_review")
        self.assertEqual(core.delivery_of(self.task())["merged_at"], self.pr("MERGED")["mergedAt"])
        self.assertEqual(self.ctx.conn.execute("SELECT count(*) FROM events WHERE kind='pr_merged'").fetchone()[0], 1)

    def test_sync_kaneo_done_skips_gh(self):
        self.update(pr_number=7, pushed_sha=self.delivery["head_sha"])
        with patch.object(remote, "get_task", side_effect=self.outside("get_task", {"task": {"status": "done"}})):
            self.assertEqual(self.sync(), {"outcome": "kaneo_done"})
        self.assertNotIn("pr_view", self.order)

    def test_open_backoff_and_closed(self):
        self.update(pr_number=7, pushed_sha=self.delivery["head_sha"])
        self.assertEqual(util.parse_ts(self.sync()["next_sync_at"]) - self.ctx.now, timedelta(minutes=5))
        with db.write_txn(self.ctx.conn):
            db.set_extra(self.ctx.conn, self.task(), self.ctx.now_iso,
                         review_registered_at=util.to_iso(self.ctx.now - timedelta(hours=1)))
        self.assertEqual(util.parse_ts(self.sync()["next_sync_at"]) - self.ctx.now, timedelta(minutes=30))
        with patch.object(gh_client, "pr_view", side_effect=self.outside("pr_view", self.pr("CLOSED"))):
            self.assertEqual(self.sync()["failure_code"], "pr_closed")
        self.assertEqual(self.task()["status"], "needs_user")

    def test_sync_remote_error_is_protocol_error(self):
        self.update(pr_number=7, pushed_sha=self.delivery["head_sha"])
        with patch.object(remote, "get_task", side_effect=bridge.BridgeError("connection down")):
            with self.assertRaises(util.KanpilotError) as caught:
                self.sync()
        self.assertEqual(caught.exception.code, "remote_unavailable")

    def test_sync_retry_success_resets_consecutive_counter(self):
        self.update(pr_number=7, pushed_sha=self.delivery["head_sha"])
        with patch.object(gh_client, "pr_view", side_effect=gh_client.GhError("transient", "down")):
            self.assertEqual(self.sync()["outcome"], "retry")
            self.assertEqual(self.sync()["publish_failures"], 2)
        self.assertEqual(self.sync()["outcome"], "waiting")
        self.assertEqual(core.delivery_of(self.task())["publish_failures"], 0)
        with patch.object(gh_client, "pr_view", side_effect=gh_client.GhError("transient", "down")):
            self.assertEqual(self.sync()["outcome"], "retry")
            self.assertEqual(self.sync()["outcome"], "retry")
            self.assertEqual(self.sync()["failure_code"], "gh_unavailable")

    def test_registered_cli_contract(self):
        # Only fake clients and the disposable local bare origin are reachable.
        publish = cli.run("delivery-publish", self.payload, self.db_path)
        self.assertEqual(publish["outcome"], "published")
        self.assertEqual(cli.run("delivery-sync", self.payload, self.db_path)["outcome"], "waiting")

    def test_execution_cycle_changed_during_network_is_rejected(self):
        def change_cycle(*args, **kwargs):
            with db.write_txn(self.ctx.conn):
                db.update_task(self.ctx.conn, "task1234", self.ctx.now_iso, run_id="new-execution")
            return self.pr()
        self.update(pr_number=7, pushed_sha=self.delivery["head_sha"])
        with patch.object(gh_client, "pr_view", side_effect=change_cycle):
            with self.assertRaises(util.KanpilotError) as caught:
                self.sync()
        self.assertEqual(caught.exception.code, "conflict")
        self.assertNotIn("last_sync_at", core.delivery_of(self.task()))

    def test_wrong_stage_and_expired_lease_refuse_external_io(self):
        for changes, expected in (({"stage": "review"}, "validation_failed"),
                ({"stage": "delivery", "lease_expires_at": util.plus_seconds(-1, self.ctx.now)}, "stale_run")):
            with db.write_txn(self.ctx.conn):
                db.update_request(self.ctx.conn, self.payload["request_id"], self.ctx.now_iso, **changes)
            with self.assertRaises(util.KanpilotError) as caught:
                self.publish_delivery()
            self.assertEqual(caught.exception.code, expected)
        self.assertFalse(self.order)

    def _reset_delivery_stage(self):
        with db.write_txn(self.ctx.conn):
            db.update_task(self.ctx.conn, "task1234", self.ctx.now_iso, status="in_review",
                           **core.clear_block_fields())

    def test_publish_identity_matrix_blocks_before_comment_or_registration(self):
        cases = ({"headRefOid": "b" * 40}, {"isCrossRepository": True},
                 {"headRefName": "other"}, {"baseRefName": "develop"},
                 {"url": "https://github.com/other/repo/pull/7"},
                 {"url": "https://github.com/owner/repo/pull/8"})
        for changes in cases:
            with self.subTest(changes=changes), patch.object(gh_client, "pr_list", return_value=[self.pr(**changes)]):
                self.assertEqual(self.publish_delivery()["failure_code"], "branch_diverged")
                self.assertEqual(self.task()["status"], "needs_user")
                self.assertFalse(self.posts)
                self.assertNotIn("find_marker_comment", self.order)
                self.assertNotIn("pr_number", core.delivery_of(self.task()))
                self._reset_delivery_stage()

    def test_publish_does_not_replace_recorded_number(self):
        self.update(pr_number=8)
        self.assertEqual(self.publish_delivery()["failure_code"], "branch_diverged")
        self.assertEqual(core.delivery_of(self.task())["pr_number"], 8)
        self.assertFalse(self.posts)

    def test_created_pr_wrong_base_blocks_before_comment(self):
        with patch.object(gh_client, "pr_list", return_value=[]), patch.object(
                gh_client, "pr_create", return_value=self.pr(baseRefName="other")):
            self.assertEqual(self.publish_delivery()["failure_code"], "branch_diverged")
        self.assertFalse(self.posts)
        self.assertNotIn("pr_number", core.delivery_of(self.task()))

    def test_merged_identity_matrix_cannot_register_merge_or_complete(self):
        self.update(pr_number=7, pushed_sha=self.delivery["head_sha"])
        cases = ({"headRefOid": "b" * 40}, {"isCrossRepository": True},
                 {"headRefName": "other"}, {"baseRefName": "develop"},
                 {"url": "https://github.com/other/repo/pull/7"},
                 {"url": "https://github.com/owner/repo/pull/8"},
                 {"number": 8, "url": "https://github.com/owner/repo/pull/8"})
        for changes in cases:
            with self.subTest(changes=changes), patch.object(gh_client, "pr_view", return_value=self.pr("MERGED", **changes)):
                self.assertEqual(self.sync()["failure_code"], "branch_diverged")
                self.assertEqual(self.task()["status"], "needs_user")
                self.assertNotIn("merged_at", core.delivery_of(self.task()))
                self.assertEqual(self.ctx.conn.execute("SELECT count(*) FROM events WHERE kind='pr_merged'").fetchone()[0], 0)
                self.assertFalse(self.posts)
                self._reset_delivery_stage()

    def test_sync_rejects_stale_pushed_snapshot(self):
        self.update(pr_number=7, pushed_sha="b" * 40)
        with patch.object(gh_client, "pr_view", return_value=self.pr("MERGED", headRefOid="b" * 40)):
            self.assertEqual(self.sync()["failure_code"], "branch_diverged")
        self.assertNotIn("merged_at", core.delivery_of(self.task()))

    def test_missing_identity_metadata_is_never_trusted(self):
        self.update(pr_number=7, pushed_sha=self.delivery["head_sha"])
        for key in ("baseRefName", "headRefName", "isCrossRepository", "headRefOid"):
            incomplete = self.pr("MERGED")
            del incomplete[key]
            with self.subTest(key=key), patch.object(gh_client, "pr_view", return_value=incomplete):
                self.assertEqual(self.sync()["outcome"], "retry")
                self.assertNotIn("merged_at", core.delivery_of(self.task()))
                self.assertFalse(self.posts)
                self.update(publish_failures=0)

    def test_publish_missing_metadata_does_not_comment(self):
        incomplete = self.pr()
        del incomplete["isCrossRepository"]
        with patch.object(gh_client, "pr_list", return_value=[incomplete]):
            self.assertEqual(self.publish_delivery()["outcome"], "retry")
        self.assertNotIn("pr_number", core.delivery_of(self.task()))
        self.assertFalse(self.posts)

    def test_expired_during_io_stops_every_later_boundary_without_registration(self):
        for name in ("push_branch", "pr_list", "pr_create", "find_marker_comment", "post_comment"):
            with self.subTest(boundary=name):
                self.clock = self.ctx.now
                self.posts.clear()
                self.comments.clear()
                self.order.clear()
                before = core.delivery_of(self.task())
                module = git_delivery if name == "push_branch" else gh_client if name.startswith("pr_") else remote
                original = getattr(module, name)
                def expires(*args, **kwargs):
                    result = original(*args, **kwargs)
                    self.clock += timedelta(hours=2)
                    return result
                list_result = [] if name == "pr_create" else [self.pr()]
                with patch.object(gh_client, "pr_list", side_effect=self.outside("pr_list", list_result)), patch.object(module, name, side_effect=expires):
                    with self.assertRaises(util.KanpilotError) as caught:
                        self.publish_delivery()
                self.assertEqual(caught.exception.code, "stale_run")
                self.assertEqual(core.delivery_of(self.task()), before)
                self.assertEqual(self.ctx.conn.execute("SELECT count(*) FROM events WHERE kind='pr_opened'").fetchone()[0], 0)
                if name != "post_comment":
                    self.assertFalse(self.posts)
                if name == "push_branch":
                    self.assertNotIn("pr_list", self.order)
                if name in ("pr_list", "pr_create"):
                    self.assertNotIn("find_marker_comment", self.order)

    def test_expired_during_sync_io_never_records_result(self):
        self.update(pr_number=7, pushed_sha=self.delivery["head_sha"])
        before = core.delivery_of(self.task())
        def expires(*args):
            self.clock += timedelta(hours=2)
            return self.pr("MERGED")
        with patch.object(gh_client, "pr_view", side_effect=expires), self.assertRaises(util.KanpilotError) as caught:
            self.sync()
        self.assertEqual(caught.exception.code, "stale_run")
        self.assertEqual(core.delivery_of(self.task()), before)

    def test_current_review_pass_required_before_any_io(self):
        initial = db.task_extra(self.task())
        cases = [{"review_history": []}, {"review_conclusion": "blocked"}]
        for changes in ({"execution_run_id": "old-run"}, {"head_sha": "b" * 40},
                        {"fix_round": 1}, {"conclusion": "blocked"}, {"report_sha256": "b" * 64}):
            cases.append({"review_history": [dict(initial["review_history"][0], **changes)]})
        for changes in cases:
            with self.subTest(changes=changes):
                with db.write_txn(self.ctx.conn):
                    db.set_extra(self.ctx.conn, self.task(), self.ctx.now_iso,
                                 review_history=initial["review_history"], review_conclusion="pass")
                    db.set_extra(self.ctx.conn, self.task(), self.ctx.now_iso, **changes)
                with self.assertRaises(util.KanpilotError) as caught:
                    self.publish_delivery()
                self.assertEqual(caught.exception.code, "validation_failed")
                self.assertFalse(self.order)
                self.assertFalse(self.posts)

    def test_changed_review_report_refuses_push(self):
        Path(self.task()["review_report_path"]).write_text("different report")
        with self.assertRaises(util.KanpilotError) as caught:
            self.publish_delivery()
        self.assertEqual(caught.exception.code, "validation_failed")
        self.assertFalse(self.order)

    def test_origin_cannot_replace_registered_pr_repository(self):
        self.update(pr_number=7, pr_url="https://github.com/owner/repo/pull/7",
                    pushed_sha=self.delivery["head_sha"])
        with patch.object(git_delivery, "origin_github_repo", return_value="other/repo"), patch.object(
                git_delivery, "push_branch") as push:
            self.assertEqual(self.publish_delivery()["failure_code"], "branch_diverged")
        push.assert_not_called()
        self.assertFalse(self.posts)
        self._reset_delivery_stage()
        with patch.object(git_delivery, "origin_github_repo", return_value="other/repo"), patch.object(
                gh_client, "pr_view") as view:
            self.assertEqual(self.sync()["failure_code"], "branch_diverged")
        view.assert_not_called()
