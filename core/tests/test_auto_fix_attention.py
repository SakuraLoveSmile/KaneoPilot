"""Auto-fix suspensions surface as informational attention items (protocol §5.27.20)."""
from kanpilot import core, db
from kanpilot.engine import attention, commands

import test_auto_fix as auto


class AutoFixAttentionCase(auto.AutoFixCase):
    def items(self):
        return commands.attention_items(self.conn, self.ctx.now, self.ctx.now_iso)

    def find(self):
        return next((item for item in self.items() if item["task_id"] == self.task_id), None)

    def receipt(self, state, reason, extra=None):
        value = {"state": state, "reason": reason, "attempt": 1, "max": 1,
                 "head_sha": self.head, "updated_at": self.now}
        value.update(extra or {})
        core.update_delivery(self.conn, self.task(), self.now, auto_fix=value)


class AutoFixAttentionTest(AutoFixAttentionCase):
    def test_suspended_state_is_an_informational_item(self):
        self.enable_auto_fix()
        db.set_setting(self.conn, "paused_global", True)
        self.auto()
        item = self.find()
        self.assertIsNotNone(item)
        self.assertEqual(item["reason_code"], attention.AUTO_FIX_SUSPENDED)
        self.assertEqual(item["attention_state"], attention.INFORMATIONAL)
        self.assertEqual(item["allowed_actions"], [])
        self.assertIsNone(item["recommended"])
        self.assertIn("暂停", item["blocked_summary"])
        self.assertEqual(item["attention_id"], "attn_%s_auto_fix_suspended" % self.task_id)

    def test_filed_state_is_an_informational_item(self):
        self.enable_auto_fix()
        self.receipt("filed", "评论需另立任务", {"followup": {"dedupe_key": "pr-comment:21",
                                                       "title": "处理评论", "description": "x"}})
        item = self.find()
        self.assertEqual(item["reason_code"], attention.AUTO_FIX_SUSPENDED)
        self.assertIn("另立任务", item["blocked_summary"])

    def test_internal_or_progress_states_do_not_raise_attention(self):
        for state in ("checked", "waiting_rerun", "started"):
            self.receipt(state, "进度中")
            self.assertIsNone(self.find(), state)

    def test_item_disappears_when_the_suspension_clears(self):
        self.receipt("suspended", "次数已用尽")
        self.assertIsNotNone(self.find())
        self.receipt("waiting_rerun", "已重跑失败检查，等待新的检查结果")
        self.assertIsNone(self.find())

    def test_task_summary_inline_attention_matches_attention_list(self):
        from kanpilot import derive
        self.receipt("suspended", "次数已用尽")
        inline = derive.task_summary(self.conn, self.task(), self.ctx.now)["attention"]
        listed = self.find()
        self.assertIsNotNone(inline)
        self.assertEqual(inline, listed)

    def test_needs_user_block_keeps_its_own_reason_code(self):
        self.receipt("suspended", "次数已用尽")
        core.block_task(self.conn, self.task(), self.now, "PR 合并门连续 3 次未就绪",
                        "merge_checks_stalled", "kaneo-delivery", stage="delivery")
        item = self.find()
        self.assertEqual(item["reason_code"], attention.DELIVERY_BLOCKED)
        self.assertEqual(item["failure_code"], "merge_checks_stalled")
        self.assertNotEqual(item["reason_code"], attention.AUTO_FIX_SUSPENDED)
