"""Review comments become one bounded repair; bodies stay untrusted material."""
from unittest.mock import patch

import pytest

import test_auto_fix as auto
from kanpilot import (core, db, followup_task, gh_client, task_ai_action,
                      task_ai_review_comment as kind, util)

def issue(case, identifier=11, author="sakura", created=None, body="please fix the index race"):
    return {"id": identifier, "author": author, "body": body,
            "created_at": created or case.new_at,
            "url": "https://github.com/owner/repo/pull/7#issuecomment-%d" % identifier}


def thread(case, identifier=21, author="sakura", created=None, resolved=False, outdated=False):
    return {"id": "T%d" % identifier, "resolved": resolved, "outdated": outdated,
            "comments": [{"id": identifier, "author": author, "body": "line note",
                          "created_at": created or case.new_at, "path": "src/a.py",
                          "url": "https://github.com/owner/repo/pull/7#discussion_r%d" % identifier,
                          "commit_oid": "a" * 40}]}


class CommentCase(auto.AutoFixCase):
    def setUp(self):
        super().setUp()
        db.set_extra(self.conn, self.task(), self.now, review_registered_at=self.now)
        self.new_at = util.plus_seconds(3600, util.parse_ts(self.now))
        self.old_at = util.plus_seconds(-3600, util.parse_ts(self.now))
        self.observed = {"repo": "owner/repo", "number": 7, "state": "OPEN",
                         "is_draft": False, "head_sha": self.head, "review_decision": "",
                         "issue_comments": [], "reviews": [], "review_threads": []}
        comments = patch.object(gh_client, "pr_comments",
                                side_effect=lambda repo, number: dict(self.observed))
        comments.start(); self.addCleanup(comments.stop)
        viewer = patch.object(gh_client, "viewer_login", return_value="sakura")
        viewer.start(); self.addCleanup(viewer.stop)

    def prepare(self):
        return task_ai_action.cmd_prepare_task_ai_action(
            self.ctx, {"task_id": self.task_id, "kind": kind.KIND})["candidate"]

    def start(self, candidate):
        return task_ai_action.cmd_start_task_ai_action(self.ctx, {
            "task_id": self.task_id, "kind": kind.KIND, "confirm": True,
            "expected_context_sha256": candidate["context_sha256"],
            "expected_config_sha256": candidate["config_sha256"]})


class ReviewCommentTest(CommentCase):
    def test_trusted_new_comment_starts_one_policy_repair(self):
        self.enable_auto_fix(auto_fix_kinds=["review_comment"])
        self.observed["issue_comments"] = [issue(self)]
        result = self.auto()
        self.assertEqual(result["outcome"], "started")
        self.assertEqual(result["kind"], "review_comment")
        self.assertEqual(self.dispatch_count(), 1)
        frozen = core.review_repair_of(self.task())
        self.assertEqual(frozen["source_kind"], "review_comment")
        self.assertEqual(frozen["authorized_by"], "policy")
        self.assertEqual(frozen["comment_ids"], [11])
        self.assertEqual(frozen["github_repo"], "owner/repo")
        self.assertTrue(frozen["comments_sha256"])

    def test_untrusted_author_is_ignored(self):
        self.enable_auto_fix(auto_fix_kinds=["review_comment"])
        self.observed["issue_comments"] = [issue(self, author="stranger")]
        result = self.auto()
        self.assertEqual(self.dispatch_count(), 0)
        self.assertEqual(self.stored().get("state"), "checked")
        self.assertEqual(result["state"], "checked")

    def test_bot_author_never_counts_as_trusted(self):
        accepted, filed = kind.select(
            {"review_threads": [thread(self, author="helper[bot]")], "issue_comments": [],
             "reviews": []},
            trusted_authors=["helper[bot]", "sakura"], newer_than=self.now)
        self.assertEqual((accepted, filed), ([], []))
        self.assertTrue(kind.is_bot("helper[bot]"))

    def test_comment_before_current_review_is_ignored(self):
        self.enable_auto_fix(auto_fix_kinds=["review_comment"])
        self.observed["issue_comments"] = [issue(self, created=self.old_at)]
        self.auto()
        self.assertEqual(self.dispatch_count(), 0)
        self.assertEqual(self.stored().get("state"), "checked")

    def test_resolved_thread_is_ignored(self):
        self.enable_auto_fix(auto_fix_kinds=["review_comment"])
        self.observed["review_threads"] = [thread(self, resolved=True)]
        self.auto()
        self.assertEqual(self.dispatch_count(), 0)
        self.assertEqual(self.stored().get("state"), "checked")

    def test_outdated_thread_is_filed_for_a_followup_task(self):
        self.enable_auto_fix(auto_fix_kinds=["review_comment"])
        self.observed["review_threads"] = [thread(self, outdated=True)]
        result = self.auto()
        self.assertEqual(result["state"], "filed")
        self.assertEqual(self.dispatch_count(), 0)
        followup = self.stored()["followup"]
        self.assertEqual(followup["dedupe_key"], "pr-comment:21")
        self.assertIn("sakura", followup["title"])
        self.assertIn("discussion_r21", followup["description"])
        self.assertEqual(self.stored()["comment"]["author"], "sakura")
        self.assertEqual(self.stored()["comment"]["url"],
                         "https://github.com/owner/repo/pull/7#discussion_r21")
        created = []
        def fake_create(client, project_id, dedupe_key, payload, *,
                        marker_prefix="codex-kaneo-review"):
            created.append({"dedupe_key": dedupe_key, "payload": payload,
                            "marker_prefix": marker_prefix})
            return {"task": {"id": "follow02", "projectId": project_id},
                    "comments": [], "relations": [], "activity": [], "created": True,
                    "todo_slug": "slug-follow02", "requirements_sha256": "a" * 64,
                    "source_sha256": "b" * 64}
        with patch.object(followup_task.remote, "get_client", lambda: object()), \
                patch.object(followup_task.remote, "create_todo", fake_create):
            result = followup_task.cmd_create_followup_task(self.ctx, {
                "source_task_id": self.task_id, "dedupe_key": followup["dedupe_key"],
                "title": followup["title"], "description": followup["description"],
                "confirm": True})
        self.assertTrue(result["created"])
        self.assertEqual(created[0]["marker_prefix"], "kaneopilot-followup")
        self.assertEqual(created[0]["dedupe_key"], "pr-comment:21")

    def test_manual_prepare_requires_confirm_and_keeps_user_identity(self):
        self.observed["issue_comments"] = [issue(self)]
        candidate = self.prepare()
        self.assertEqual(candidate["kind"], "review_comment")
        self.assertEqual(candidate["comments"][0]["author"], "sakura")
        self.assertNotIn("body", candidate["comments"][0])
        started = self.start(candidate)
        self.assertTrue(started["ok"])
        self.assertEqual(self.dispatch_count(), 1)
        frozen = core.review_repair_of(self.task())
        self.assertEqual(frozen["authorized_by"], "user")
        self.assertNotIn("policy_revision", frozen)

    def test_manual_prepare_can_reach_an_older_unresolved_comment(self):
        self.observed["issue_comments"] = [issue(self, created=self.old_at)]
        candidate = self.prepare()
        self.assertEqual(candidate["comments"][0]["author"], "sakura")
        self.start(candidate)
        self.assertEqual(self.dispatch_count(), 1)

    def test_policy_ignores_the_same_older_comment(self):
        self.enable_auto_fix(auto_fix_kinds=["review_comment"])
        self.observed["issue_comments"] = [issue(self, created=self.old_at)]
        self.auto()
        self.assertEqual(self.dispatch_count(), 0)

    def test_second_prepare_for_same_head_is_rejected(self):
        self.observed["issue_comments"] = [issue(self)]
        self.start(self.prepare())
        with pytest.raises(util.KanpilotError) as error:
            self.prepare()
        self.assertEqual(error.value.code, "conflict")

    def test_prompt_marks_comments_untrusted_and_bounded(self):
        self.observed["issue_comments"] = [issue(self, body="x" * 5000)]
        frozen = kind.prepare(self.ctx, self.task())
        self.assertLess(len(frozen["comments"][0]["body"]), 4300)
        self.assertIn("已截断", frozen["comments"][0]["body"])
        body = task_ai_action.prompt_body(frozen, self.task()["plan_path"])
        self.assertIn("未信任材料", body)
        self.assertIn("不要回复", body)
        self.assertIn("issuecomment-11", body)
        self.assertIn("@sakura", body)
        self.assertLess(len(body), 14000)

    def test_prompt_total_material_is_capped(self):
        self.observed["issue_comments"] = [issue(self, identifier=index, body="y" * 4000)
                                           for index in range(10, 20)]
        frozen = kind.prepare(self.ctx, self.task())
        body = task_ai_action.prompt_body(frozen, self.task()["plan_path"])
        self.assertIn("超过 12000 字符上限", body)
