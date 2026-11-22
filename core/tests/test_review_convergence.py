"""Review convergence after an AI action or a repair round (KP-MG02): materials, rules and the repair fence.

Materials are built with the real review code on a real Git branch; no model runs. What these tests can
prove is that the right facts and rules reach the reviewer and the repairer. Whether a reviewer then
classifies findings the way the rules say is checked on frozen reports and, if approved, on a paid replay.
"""
from pathlib import Path

import json

import test_task_branch_review as branch_fixtures
from kanpilot import (constants, core, db, pi_desktop_review as review, repair_prompt, review_convergence,
                      review_repair, review_settle, task_ai_action, util)
from test_discard_task_ai_action import DiscardFixture

CHECK = {"name": "analyze-test", "workflow": "app-ci", "conclusion": "failure",
         "run_id": 44211608370, "job_id": 134491096466}
LOGS = "PathNotFoundException: index.json.tmp (errno 2)\n  at offline_repository.dart:466"
BLOCKER = {"title": "修复评审发现", "description": "旧快照覆盖完成记录；只处理这一项", "priority": "high"}
RUBRIC_PHRASES = ("收敛准则", "冻结目标未达成或被回退", "净回退", "触发步骤", "两个及以上相互独立的罕见事件叠加",
                  "凭据、安全问题", "遗留风险", "结论为 pass", "fix.description 只列阻断项", "删减或简化",
                  "仅供测试使用的注入点或开关")


class ConvergenceMaterials(branch_fixtures.TaskBranchReview):
    """One task branch: ``first`` was reviewed and passed, ``second`` is what the next review looks at."""

    def build(self, *, action=True, state="awaiting_review", rounds=0, previous=None, target=True, ran=True):
        first = self.commit()
        report = self.reports_root / "previous.md"
        report.write_text("Previous review")
        history = [dict(head_sha=first["sha"], report_path=str(report), conclusion="pass")]
        if previous:
            history = [dict(history[0], conclusion="changes_requested", fix=previous)]
        db.update_task(self.ctx.conn, "task1234", self.ctx.now_iso, run_id="exec2")
        self.payload["expected_execution_run_id"] = "exec2"
        second = self.commit("fix.py", "fixed\n")
        fields = {"review_history": history}
        if rounds:
            fields["review_repair_round"] = rounds
        if action:
            fields["ai_action_history"] = [dict(
                action_id="act1", kind="check_failure", state=state, head_sha=first["sha"],
                base_sha=self.delivery["base_sha"], title="修复失败的检查",
                execution_run_id="exec2" if ran else "never-ran")]
            if target:
                fields["ai_action_target"] = dict(
                    action_id="act1", kind="check_failure", title="修复失败的检查",
                    description="修复合并门中失败的检查：analyze-test。只做让检查通过的最小改动。",
                    head_sha=first["sha"], github_repo="owner/repo", check_runs=[CHECK], failure_logs=LOGS)
        db.set_extra(self.ctx.conn, self.task(), self.ctx.now_iso, **fields)
        return first, second

    def materials(self, second):
        task = dict(self.task())
        root = self.root / "trees" / "task1234" / ("review-" + second["sha"][:12])
        root.parent.mkdir(parents=True, exist_ok=True)
        self.git(self.worktree, "worktree", "add", "--detach", str(root), "HEAD")
        files = review.collect_materials(db_path=self.db_path, conn=self.ctx.conn, materials=self.root / "materials",
                                         task=task, execution_worktree=self.worktree, execution_run_id="exec2")
        instruction = review.review_instruction("Review", job=dict(execution_run_id="exec2"), review_root=root,
                                                materials=files, facts=dict(task=task))
        return files, instruction

    def material(self, files):
        return Path(files["files"][review_convergence.MATERIAL]).read_text()

    def test_review_after_an_action_is_judged_against_its_target(self):
        first, second = self.build(rounds=1, previous=BLOCKER)
        files, instruction = self.materials(second)
        text = self.material(files)
        for fact in (
                "修复失败的检查（check_failure）", "修复合并门中失败的检查：analyze-test",
                "[app-ci] analyze-test：failure",
                "https://github.com/owner/repo/actions/runs/44211608370/job/134491096466",
                "PathNotFoundException: index.json.tmp", "只做让上述检查通过的最小改动",
                "处理前已通过评审的提交：`%s`" % first["sha"], "基线 `%s`" % self.delivery["base_sha"],
                "固定提交：`%s`" % second["sha"], "评审修复第 1 轮之后的复审",
                "上一轮评审的阻断项", "旧快照覆盖完成记录；只处理这一项"):
            assert fact in text, fact
        assert str(files["files"][review_convergence.MATERIAL]) in instruction
        for phrase in RUBRIC_PHRASES:
            assert phrase in instruction, phrase
        # The rules come last, after the result frame, so they are the final word before the prohibitions.
        assert instruction.index("收敛准则") > instruction.index("整个答复只包含上述一份分段结果")
        assert instruction.index("收敛准则") < instruction.index("禁止 SubmitPlan/SubmitGoal")

    def test_a_repair_round_without_an_action_gets_the_previous_blockers_only(self):
        first, second = self.build(action=False, rounds=1, previous=BLOCKER)
        files, instruction = self.materials(second)
        text = self.material(files)
        assert "上一轮已评审的提交：`%s`" % first["sha"] in text and "评审修复第 1 轮之后的复审" in text
        assert "上一轮评审的阻断项" in text and "旧快照覆盖完成记录" in text
        assert "处理目标" not in text and "处理前已通过评审" not in text
        assert all(phrase in instruction for phrase in RUBRIC_PHRASES)

    def test_older_actions_without_a_frozen_target_say_so(self):
        _, second = self.build(target=False)
        text = self.material(self.materials(second)[0])
        assert "修复失败的检查（check_failure）" in text and "该处理启动时未冻结目标细节" in text
        assert "失败信号摘录" not in text

    def assert_ordinary(self, **options):
        first, second = self.build(**options)
        files, instruction = self.materials(second)
        assert review_convergence.MATERIAL not in files["files"]
        assert not any(phrase in instruction for phrase in ("收敛准则", "遗留风险", "action-target"))
        assert "action-target" not in Path(files["files"]["context.json"]).read_text()
        # What a first-round review gets today.
        assert {"changes.diff", "commits.log", "changes.status", "plan.md"} <= set(files["files"])

    def test_a_first_review_keeps_its_materials_and_instruction(self):
        self.assert_ordinary(action=False)

    def test_an_action_that_never_produced_commits_does_not_change_the_review(self):
        self.assert_ordinary(ran=False)

    def test_a_discarded_action_does_not_change_the_review(self):
        self.assert_ordinary(state="discarded")

    def test_the_context_lists_the_material_so_it_is_frozen_with_the_others(self):
        _, second = self.build(rounds=1)
        files, _ = self.materials(second)
        context = json.loads(Path(files["files"]["context.json"]).read_text())
        assert context["materials"][review_convergence.MATERIAL] == files["files"][review_convergence.MATERIAL]
        assert review._material_hashes(files)[files["files"][review_convergence.MATERIAL]] == util.sha256_file(
            Path(files["files"][review_convergence.MATERIAL]))


# Only the fixture is reused; the branch-review cases it inherits run in their own module.
for _name in dir(branch_fixtures.TaskBranchReview):
    if _name.startswith("test_"):
        setattr(ConvergenceMaterials, _name, None)


class TestContext:
    """Which reviews the rules reach; a task here is the get-task DTO shape."""

    @staticmethod
    def task(*, entries=(), commits=("exec1",), rounds=0, automation=None, history=()):
        extra = {"ai_action_history": list(entries), "review_repair_round": rounds, "review_history": list(history)}
        if automation:
            extra["automation"] = automation
        return {"extra": extra, "delivery": {"commits": [{"execution_run_id": run, "sha": run} for run in commits]}}

    @staticmethod
    def entry(state="needs_followup", run="act", kind="check_failure"):
        return {"action_id": "a-" + state, "kind": kind, "state": state, "execution_run_id": run, "head_sha": "h0"}

    def test_applies_only_after_an_action_with_commits_or_a_repair_round(self):
        entry = self.entry()
        assert review_convergence.context(self.task()) is None
        assert review_convergence.context(self.task(entries=[entry])) is None, "the action has no commits yet"
        assert review_convergence.context(self.task(entries=[entry], commits=("exec1", "act")))["action"] == entry
        assert review_convergence.context(self.task(rounds=1))["action"] is None
        assert review_convergence.context(self.task(automation={"fix_round": 2}))["rounds"] == 2

    def test_discarded_and_unknown_actions_do_not_count_and_only_the_latest_does(self):
        commits = ("exec1", "act", "later")
        discarded = self.entry("discarded", "later")
        assert review_convergence.context(self.task(entries=[discarded], commits=commits)) is None
        earlier = self.entry("completed", "act")
        assert review_convergence.context(self.task(entries=[earlier, discarded], commits=commits))["action"] == earlier
        unknown = self.entry(kind="something_else")
        assert review_convergence.context(self.task(entries=[earlier, unknown], commits=commits)) is None
        assert review_convergence.context(self.task(entries=[dict(self.entry(), execution_run_id=None)],
                                                    commits=(None, "act"))) is None

    def test_target_is_used_only_for_the_action_it_was_frozen_for(self):
        entry = self.entry()
        task = self.task(entries=[entry], commits=("act",))
        task["extra"]["ai_action_target"] = {"action_id": entry["action_id"], "kind": "check_failure"}
        assert review_convergence.context(task)["target"]["kind"] == "check_failure"
        task["extra"]["ai_action_target"]["action_id"] = "another"
        assert review_convergence.context(task)["target"] is None

    def test_previous_review_skips_discarded_ones(self):
        history = [{"head_sha": "kept", "conclusion": "pass"},
                   {"head_sha": "dropped", "conclusion": "changes_requested", "discarded": {"action_id": "x"}}]
        assert review_convergence.context(self.task(rounds=1, history=history))["previous"]["head_sha"] == "kept"

    def test_target_keeps_facts_only_and_bounds_the_log(self):
        value = review_convergence.target_of({
            "source_kind": "check_failure", "action_id": "a1", "title": "T", "description": "D", "head_sha": "h",
            "check_runs": [CHECK], "github_repo": "owner/repo", "failure_logs": "x" * 20000,
            "action_config": {"secret": "token"}, "plan_sha256": "p", "report_path": "/reports/a.md"})
        assert value["kind"] == "check_failure" and value["check_runs"] == [CHECK]
        assert len(value["failure_logs"]) == review_convergence.LOG_LIMIT
        assert not {"action_config", "plan_sha256", "report_path"} & set(value)
        assert "failure_logs" not in review_convergence.target_of({"source_kind": "merge_conflict", "failure_logs": ""})



class TestRules:
    def test_blocking_classes_stay_explicit_and_everything_else_passes_with_a_residual_section(self):
        text = "\n".join(review_convergence.RUBRIC)
        assert "阻断」只限三类" in text
        for blocking in ("冻结目标未达成或被回退", "相对处理前已评审提交的净回退", "破坏原计划已验收的行为"):
            assert blocking in text
        assert "安全问题不受第 2 类单故障限制" in text
        assert "处理前就存在的缺口、多故障叠加的交错场景、可选加固" in text
        assert "「遗留风险」" in text and "没有阻断项时结论为 pass" in text

    def test_a_pass_with_residual_risk_settles_as_an_ordinary_pass_without_a_fix(self):
        report = "# 评审\n结论：目标已达成。\n\n## 遗留风险\n\n- 两个独立故障叠加时旧快照可能覆盖完成记录（触发条件：…）。\n"
        result = review_settle.validate_result({"conclusion": "pass", "report_markdown": report, "fix": None})
        assert result["conclusion"] == "pass" and result["fix"] is None and "遗留风险" in result["report_markdown"]
        assert "pass" in constants.REVIEW_CONCLUSIONS and "residual" not in "".join(constants.REVIEW_CONCLUSIONS)


class RepairPromptFixture:
    """Frozen files a repair prompt reads."""

    @staticmethod
    def files(tmp_path):
        plan = tmp_path / "plan.md"
        plan.write_text("# Plan\n```verify\ntrue\n```\n")
        report = tmp_path / "review.md"
        report.write_text("# Review\nfinding\n")
        return plan, report

    def fix(self, tmp_path, **over):
        plan, report = self.files(tmp_path)
        return dict({"mode": "direct", "title": "修复", "description": "只处理这一项",
                     "plan_sha256": util.sha256_file(plan), "report_path": str(report),
                     "report_sha256": util.sha256_file(report), "head_sha": "h", "branch": "b"}, **over), plan


class TestRepairFence(RepairPromptFixture):
    FENCE = ("修复边界", "不为防范所列材料（失败检查、冲突或评审报告）之外的场景新增并发、重试、恢复或状态机制",
             "未被要求的加固不写进代码，如实写进执行报告", "仅供测试使用的注入点、开关或可见性放宽", "优先删减或简化该机制")

    def test_new_repairs_carry_the_fence_and_no_longer_widen_the_scope(self, tmp_path):
        fix, plan = self.fix(tmp_path, prompt_rev=repair_prompt.PROMPT_REV)
        body = repair_prompt._body(fix, plan)
        assert "必要的关联缺陷" not in body and "与它们同一根因的缺陷" in body
        assert all(text in body for text in self.FENCE)
        assert body.index("修复边界") < body.index("修复标题")

    def test_repairs_frozen_before_the_revision_still_reproduce_their_frozen_text(self, tmp_path):
        fix, plan = self.fix(tmp_path)
        body = repair_prompt._body(fix, plan)
        assert "必要的关联缺陷" in body and "修复边界" not in body
        # The frozen body is recomputed and compared on every send; it must be byte-stable.
        assert repair_prompt._body(dict(fix), plan) == body

    def test_one_shot_action_prompts_carry_the_fence_only_for_new_actions(self, tmp_path):
        fix, plan = self.fix(tmp_path, source_kind="check_failure", github_repo="owner/repo",
                             check_runs=[CHECK], failure_logs=LOGS)
        old = task_ai_action.prompt_body(fix, plan)
        new = task_ai_action.prompt_body(dict(fix, prompt_rev=repair_prompt.PROMPT_REV), plan)
        assert "修复边界" not in old and new == old + "\n\n" + repair_prompt.scope_fence({"prompt_rev": 2})
        assert all(text in new for text in self.FENCE) and "最小改动" in new
        assert repair_prompt._body(dict(fix, prompt_rev=2), plan).endswith("不是继续补全它。")

    def test_the_verification_recovery_turn_is_never_fenced(self, tmp_path):
        assert repair_prompt.scope_fence({"prompt_rev": 2, "verification_recovery": {"source_run_id": "r"}}) == ""
        assert repair_prompt.scope_fence({"prompt_rev": "2"}) == "" and repair_prompt.scope_fence({}) == ""


class ConvergenceFlow(DiscardFixture):
    """The real chain (action, reviews asking for changes, direct repair rounds) and what each step is handed.

    Built with the real commands; only the executor, GitHub and Plus are simulated.
    """

    def spec(self, fields):
        """A job spec as dispatch builds it, so the frozen text goes through the same recomputation as a send."""
        fix = fields["review_repair"]
        return dict(fields, task_id=fix["task_id"], repository=fix["repository"], cwd=fix["worktree_path"],
                    plan_sha256=fix["plan_sha256"], run_id=fix["repair_execution_run_id"],
                    plan_path=self.task()["plan_path"])

    def test_the_action_freezes_its_prompt_revision_and_its_target(self):
        self.start()
        task = self.task()
        frozen = core.review_repair_of(task)
        assert frozen["prompt_rev"] == repair_prompt.PROMPT_REV
        target = db.task_extra(task)["ai_action_target"]
        assert target["action_id"] == frozen["action_id"] and target["kind"] == "check_failure"
        assert [check["name"] for check in target["check_runs"]] == ["analyze-test"]
        assert "PathNotFoundException: index.json.tmp" in target["failure_logs"]
        assert not {"action_config", "report_path", "plan_sha256", "worktree_path"} & set(target)

    def test_the_action_prompt_is_fenced_and_survives_the_recomputation_of_every_send(self):
        self.claim_action()
        fields = repair_prompt.freeze(self.task())
        body = fields["repair_prompt"]["body"]
        assert body.startswith("## 本轮一次性 AI 处理：修复失败的检查") and "只做让它们通过的最小改动" in body
        assert "PathNotFoundException: index.json.tmp" in body
        assert body.endswith(repair_prompt.scope_fence({"prompt_rev": 2}))
        assert repair_prompt.execution_body(self.spec(fields)) == body

    def test_an_action_queued_before_the_revision_keeps_its_old_text(self):
        self.claim_action()
        fix = dict(core.review_repair_of(self.task()))
        fix.pop("prompt_rev")
        db.set_extra(self.conn, self.task(), self.now, fix_request=fix)
        fields = repair_prompt.freeze(self.task())
        assert "修复边界" not in fields["repair_prompt"]["body"]
        assert repair_prompt.execution_body(self.spec(fields)) == fields["repair_prompt"]["body"]

    def test_the_target_outlives_the_review_that_replaces_the_action_repair(self):
        self.stuck(rounds=0)
        task = self.task()
        # The review asked for changes: the frozen action repair is gone, a plain one stands in its place.
        assert core.review_repair_of(task).get("source_kind") is None
        value = review_convergence.context(task)
        assert value["action"]["action_id"] == self.action_id and value["target"]["kind"] == "check_failure"
        text = review_convergence.material_text(value)
        for fact in ("analyze-test", "PathNotFoundException: index.json.tmp", "最小改动", "上一轮评审的阻断项",
                     "处理前已通过评审的提交：`%s`" % self.head):
            assert fact in text, fact

    def test_plain_repairs_started_now_are_fenced_and_compared_on_recovery(self):
        self.stuck(rounds=0)
        review_repair.cmd_start_review_repair(self.ctx, self.identity(core.review_repair_of(self.task())))
        assert core.review_repair_of(self.task())["prompt_rev"] == repair_prompt.PROMPT_REV
        self.claim_repair()
        fields = repair_prompt.freeze(self.task())
        body = fields["repair_prompt"]["body"]
        assert "与它们同一根因的缺陷" in body and "必要的关联缺陷" not in body
        assert body.index("修复边界") < body.index("修复标题")
        assert repair_prompt.execution_body(self.spec(fields)) == body

    def test_a_discarded_action_stops_the_rules_from_reaching_the_fresh_review(self):
        self.stuck(rounds=1)
        with self.confirmed():
            self.discard()
        value = review_convergence.context(self.task())
        assert value is None, "the action and its repair rounds are off the reviewed chain"


# Only the fixtures are reused; the discard cases they inherit run in their own module.
for _name in dir(DiscardFixture):
    if _name.startswith("test_"):
        setattr(ConvergenceFlow, _name, None)
