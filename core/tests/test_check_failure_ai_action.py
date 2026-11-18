"""Explicit check-failure repair keeps GitHub identity and bounded materials."""
import pytest

from kanpilot import db, task_ai_action, task_ai_check_failure as kind, util
from test_delivery_pr import DeliveryPR


@pytest.fixture
def case():
    value = DeliveryPR()
    value.setUp()
    try:
        value.update(pr_number=7, pr_state="OPEN", pushed_sha=value.delivery["head_sha"])
        yield value
    finally:
        value.doCleanups()
        value.tearDown()


def blocked(case, reason="检查未通过：app-ci/analyze-test，修复或重跑后会自动合并"):
    case.update(auto_merge={"state": "blocked", "reason": reason,
                            "head_sha": case.delivery["head_sha"]})


def test_gate_needs_a_named_failing_check(case):
    blocked(case)
    assert kind.gate(case.ctx.conn, case.task()) is None
    blocked(case, "检查仍在运行：app-ci/analyze-test，等待结果")
    assert "没有可确认的失败检查" in kind.gate(case.ctx.conn, case.task())


def test_gate_refuses_a_second_repair_of_the_same_head(case):
    blocked(case)
    db.set_extra(case.ctx.conn, case.task(), case.ctx.now_iso,
                 ai_action_history=[{"kind": kind.KIND, "head_sha": case.delivery["head_sha"]}])
    assert "已安排过一次检查修复" in kind.gate(case.ctx.conn, case.task())


def test_failing_checks_keep_run_and_job_identity():
    rollup = [
        {"__typename": "CheckRun", "name": "analyze-test", "workflowName": "app-ci",
         "status": "COMPLETED", "conclusion": "FAILURE",
         "detailsUrl": "https://github.com/owner/repo/actions/runs/44211608370/job/134491096466"},
        {"__typename": "CheckRun", "name": "android", "workflowName": "app-ci",
         "status": "COMPLETED", "conclusion": "SUCCESS",
         "detailsUrl": "https://github.com/owner/repo/actions/runs/44211608370/job/1"},
    ]
    assert kind._failing({"statusCheckRollup": rollup}, "owner/repo") == [
        dict(name="analyze-test", workflow="app-ci", conclusion="FAILURE",
             run_id=44211608370, job_id=134491096466)]
    with pytest.raises(util.KanpilotError):
        kind._failing({"statusCheckRollup": rollup[1:]}, "owner/repo")


def test_materials_refuse_a_moved_run(monkeypatch):
    monkeypatch.setattr(kind.release_github, "run", lambda repo, run_id: {
        "head_sha": "f" * 40, "status": "completed", "run_attempt": 1, "conclusion": "failure"})
    with pytest.raises(util.KanpilotError):
        kind._materials("owner/repo", "a" * 40,
                        [{"run_id": 1, "job_id": 2, "name": "analyze-test",
                          "workflow": "app-ci", "conclusion": "FAILURE"}])


def test_materials_freeze_bounded_redacted_logs(monkeypatch):
    monkeypatch.setattr(kind.release_github, "run", lambda repo, run_id: {
        "head_sha": "a" * 40, "status": "completed", "run_attempt": 1, "conclusion": "failure"})
    monkeypatch.setattr(kind.release_github, "failure_logs", lambda repo, run_id, attempt: [
        {"job_id": 2, "name": "analyze-test", "conclusion": "failure",
         "content": "x" * (kind._MAX_LOG_CHARS + 100)}])
    text = kind._materials("owner/repo", "a" * 40,
                           [{"run_id": 1, "job_id": 2, "name": "analyze-test",
                             "workflow": "app-ci", "conclusion": "FAILURE"}])
    assert len(text) <= kind._MAX_LOG_CHARS
    assert "run 1" in text
    assert text.endswith("x")


def test_prompt_body_names_checks_logs_and_verify(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text("# plan\n\n```verify\npython3 -m pytest tests -q\n```\n")
    fix = {"check_runs": [{"name": "analyze-test", "workflow": "app-ci", "conclusion": "FAILURE",
                            "run_id": 1, "job_id": 2}],
           "failure_logs": "PathNotFoundException: index.json.tmp",
           "head_sha": "a" * 40, "branch": "kaneo/x", "plan_sha256": util.sha256_file(plan)}
    body = kind.prompt_body(fix, str(plan))
    assert "[app-ci] analyze-test：FAILURE" in body
    assert "PathNotFoundException" in body
    assert "python3 -m pytest tests -q" in body
    assert "不要删除或跳过检查" in body


def test_projection_offers_check_failure_when_the_gate_names_one(case):
    blocked(case)
    value = task_ai_action.projection(case.ctx.conn, case.task(), case.ctx.now)
    assert value["kind"] == kind.KIND