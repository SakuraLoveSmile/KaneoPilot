"""Isolated local Git, fake GitHub/Kaneo, and the actual CLI result consumer."""

import copy
import unittest
from unittest.mock import patch

import test_delivery_pr as fixtures
from kanpilot import bridge, core, db, delivery_contract, gh_client, git_delivery, remote, util
from test_git_delivery import git

_REAL_PR_CREATE = gh_client.pr_create


class DeliveryStageContract(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.DeliveryPR("runTest")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def snapshot(self):
        task = dict(self.f.task())
        task["delivery"] = core.delivery_of(self.f.task())
        task.pop("extra_json")
        return task

    def claim(self, run_id):
        self.f._reset_delivery_stage()
        with db.write_txn(self.f.ctx.conn):
            db.update_request(self.f.ctx.conn, self.f.payload["request_id"], self.f.ctx.now_iso, run_id=run_id)
        self.f.payload["run_id"] = run_id

    def test_core_result_is_consumed_and_persisted(self):
        before = self.snapshot()
        result = self.f.publish_delivery()
        envelope = delivery_contract.consume(result, before, self.f.payload, "publish")
        self.assertEqual(envelope["outcome"], "succeeded")
        self.assertEqual(envelope["harness_id"], "core_delivery")
        stored = core.delivery_of(self.f.task())
        self.assertEqual(stored["stage_contracts"]["publish"], result["stage_contract"])
        self.assertEqual(stored["stage_results"]["publish"], envelope)
        self.assertEqual(result["stage_contract"]["inputs"]["operation"], "publish")
        statuses = self.f.ctx.conn.execute("SELECT tool,status FROM runtime_operations").fetchall()
        self.assertTrue(statuses)
        self.assertTrue(all(row["status"] == "accepted" for row in statuses))

    def test_result_with_wrong_head_or_old_run_is_rejected(self):
        before = self.snapshot()
        result = self.f.publish_delivery()
        wrong_head = copy.deepcopy(before)
        wrong_head["delivery"]["head_sha"] = "b" * 40
        for task, payload in ((wrong_head, self.f.payload),
                              (before, dict(self.f.payload, run_id="new-delivery-run"))):
            with self.subTest(payload=payload), self.assertRaises(util.KanpilotError):
                delivery_contract.consume(result, task, payload, "publish")
        self.assertEqual(self.f.task()["status"], "in_review")

    def test_missing_or_changed_result_never_authorizes_advance(self):
        before = self.snapshot()
        result = self.f.publish_delivery()
        changed = dict(result, pr_number=999)
        for candidate in ({"outcome": "published"}, changed):
            with self.assertRaises(util.KanpilotError):
                delivery_contract.consume(candidate, before, self.f.payload, "publish")

    def test_unknown_create_is_not_sent_twice(self):
        with patch.object(gh_client, "pr_list", return_value=[]), patch.object(
                gh_client, "pr_create", side_effect=gh_client.GhError("timeout", "lost")) as create:
            for _ in range(2):
                result = self.f.publish_delivery()
                self.assertEqual(result["operation_state"], "unknown")
                self.assertEqual(result["stage_result"]["outcome"], "blocked")
            self.assertEqual(create.call_count, 1)
        self.assertNotIn("pr_number", core.delivery_of(self.f.task()))
        # An identity-checked read settles the original request without resend.
        self.assertEqual(self.f.publish_delivery()["outcome"], "published")
        row = self.f.ctx.conn.execute("SELECT status FROM runtime_operations WHERE tool='github_pr_create'").fetchone()
        self.assertEqual(row["status"], "accepted")

    def test_unknown_comment_survives_new_claim_without_resend(self):
        with patch.object(remote, "post_comment", side_effect=bridge.BridgeError("lost")) as post:
            self.assertEqual(self.f.publish_delivery()["operation_state"], "unknown")
            with db.write_txn(self.f.ctx.conn):
                db.update_request(self.f.ctx.conn, self.f.payload["request_id"], self.f.ctx.now_iso,
                                  run_id="delivery-new")
            self.f.payload["run_id"] = "delivery-new"
            self.assertEqual(self.f.publish_delivery()["operation_state"], "unknown")
            self.assertEqual(post.call_count, 1)
        self.assertNotIn("kaneo_comment_at", core.delivery_of(self.f.task()))

    def test_lost_push_response_reconciles_remote_without_resend(self):
        original = git_delivery.push_branch
        def lost(*args, **kwargs):
            original(*args, **kwargs)
            raise git_delivery.GitDeliveryError("push_failed", "lost")
        with patch.object(git_delivery, "push_branch", side_effect=lost) as push:
            self.assertEqual(self.f.publish_delivery()["operation_state"], "unknown")
            self.assertEqual(self.f.publish_delivery()["outcome"], "published")
            self.assertEqual(push.call_count, 1)

    def test_accepted_push_still_checks_actual_remote_head(self):
        self.f.publish_delivery()
        git(self.f.repo.parent / "origin.git", "update-ref", "refs/heads/" + self.f.delivery["branch"],
            self.f.delivery["base_sha"])
        with patch.object(git_delivery, "push_branch") as push:
            self.assertEqual(self.f.publish_delivery()["failure_code"], "branch_diverged")
            push.assert_not_called()

    def test_acceptance_without_pr_identity_is_unknown(self):
        with patch.object(gh_client, "pr_list", return_value=[]), patch.object(
                gh_client, "pr_create", return_value={"accepted": True}) as create:
            for _ in range(2):
                result = self.f.publish_delivery()
                self.assertEqual(result["operation_state"], "unknown")
            self.assertEqual(create.call_count, 1)
        self.assertNotIn("pr_number", core.delivery_of(self.f.task()))

    def test_publish_and_sync_freeze_distinct_operations(self):
        published = self.f.publish_delivery()
        before = self.snapshot()
        with patch.object(gh_client, "pr_view", return_value=self.f.pr("MERGED")):
            result = self.f.sync()
        delivery_contract.consume(result, before, self.f.payload, "sync")
        self.assertEqual(result["stage_contract"]["inputs"]["operation"], "sync")
        self.assertEqual(result["stage_contract"]["inputs"]["pr_number"], 7)
        self.assertNotEqual(result["stage_contract"]["contract_sha256"], published["stage_contract"]["contract_sha256"])
        with self.assertRaises(util.KanpilotError):
            delivery_contract.consume(result, before, self.f.payload, "publish")

    def test_damaged_existing_contract_never_falls_back_to_legacy(self):
        self.f.publish_delivery()
        stored = copy.deepcopy(core.delivery_of(self.f.task())["stage_contracts"])
        stored["publish"]["contract_sha256"] = "f" * 64
        self.f.update(stage_contracts=stored)
        self.f.order.clear()
        with self.assertRaises(util.KanpilotError) as caught:
            self.f.publish_delivery()
        self.assertEqual(caught.exception.code, "validation_failed")
        self.assertFalse(self.f.order)

    def test_definitive_auth_rejection_retries_once_in_new_claim(self):
        with patch.object(gh_client, "pr_list", return_value=[]), patch.object(
                gh_client, "pr_create", side_effect=gh_client.GhError("auth", "authentication denied")) as denied:
            self.assertEqual(self.f.publish_delivery()["failure_code"], "gh_not_logged_in")
            self.f._reset_delivery_stage()
            # Re-entering the same claim cannot acquire another send slot.
            self.assertEqual(self.f.publish_delivery()["failure_code"], "gh_not_logged_in")
            self.assertEqual(denied.call_count, 1)
        created = []
        def create(*args, **kwargs):
            created.append(self.f.pr())
            return created[-1]
        self.claim("delivery-retry")
        with patch.object(gh_client, "pr_list", side_effect=lambda *_: list(created)), patch.object(
                gh_client, "pr_create", side_effect=create) as retried:
            self.assertEqual(self.f.publish_delivery()["outcome"], "published")
            self.assertEqual(self.f.publish_delivery()["outcome"], "published")
            self.claim("delivery-after-success")
            self.assertEqual(self.f.publish_delivery()["outcome"], "published")
            self.assertEqual(retried.call_count, 1)
        rows = self.f.ctx.conn.execute("SELECT * FROM runtime_operations WHERE tool='github_pr_create'").fetchall()
        self.assertEqual(sorted(row["status"] for row in rows), ["accepted", "rejected"])
        root = next(row for row in rows if row["status"] == "rejected")
        accepted = next(row for row in rows if row["status"] == "accepted")
        self.assertEqual(util.jload(accepted["result_json"])["intent"]["retry_of"], root["operation_id"])

    def test_unknown_attempt_blocks_new_claim_and_changed_parameters(self):
        with patch.object(gh_client, "pr_list", return_value=[]), patch.object(
                gh_client, "pr_create", side_effect=gh_client.GhError("timeout", "lost")) as create:
            self.assertEqual(self.f.publish_delivery()["operation_state"], "unknown")
            self.claim("delivery-after-unknown")
            with db.write_txn(self.f.ctx.conn):
                db.update_task(self.f.ctx.conn, "task1234", self.f.ctx.now_iso, title="changed title")
            with self.assertRaises(util.KanpilotError) as caught:
                self.f.publish_delivery()
            self.assertEqual(caught.exception.code, "conflict")
            self.assertEqual(create.call_count, 1)
        rows = self.f.ctx.conn.execute("SELECT * FROM runtime_operations WHERE tool='github_pr_create'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "unknown")

    def test_unknown_retry_outranks_rejected_root_in_later_claim(self):
        failures = [gh_client.GhError("auth", "denied"), gh_client.GhError("timeout", "lost retry")]
        with patch.object(gh_client, "pr_list", return_value=[]), patch.object(
                gh_client, "pr_create", side_effect=failures) as create:
            self.assertEqual(self.f.publish_delivery()["failure_code"], "gh_not_logged_in")
            self.claim("delivery-retry-unknown")
            self.assertEqual(self.f.publish_delivery()["operation_state"], "unknown")
            self.claim("delivery-must-not-resend")
            self.assertEqual(self.f.publish_delivery()["operation_state"], "unknown")
            self.assertEqual(create.call_count, 2)
        statuses = self.f.ctx.conn.execute("SELECT status FROM runtime_operations WHERE tool='github_pr_create'").fetchall()
        self.assertEqual(sorted(row["status"] for row in statuses), ["rejected", "unknown"])

    def test_create_success_followed_by_auth_read_failure_stays_unknown(self):
        def read_failure(*args, **kwargs):
            raise gh_client.GhError("auth", "view denied after create")
        with patch.object(gh_client, "pr_list", return_value=[]), patch.object(
                gh_client, "_run", return_value=None) as write, patch.object(
                gh_client, "pr_view", side_effect=read_failure), patch.object(
                gh_client, "pr_create", side_effect=_REAL_PR_CREATE):
            self.assertEqual(self.f.publish_delivery()["operation_state"], "unknown")
            self.claim("delivery-after-read-denial")
            self.assertEqual(self.f.publish_delivery()["operation_state"], "unknown")
            self.assertEqual(write.call_count, 1)
        row = self.f.ctx.conn.execute("SELECT status FROM runtime_operations WHERE tool='github_pr_create'").fetchone()
        self.assertEqual(row["status"], "unknown")
