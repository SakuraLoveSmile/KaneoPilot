import copy
import json

import pytest

from kanpilot import auto_dispatch, auto_monitor, util
from test_auto_dispatch import FakeClock


def task():
    return {"task_id": "task", "run_id": "execution", "plan_sha256": "plan",
        "attempts": [{"stage": "dispatch", "request_id": "execution-request", "run_id": "execution"}],
        "extra": {"plan_verify": {"plan_sha256": "plan", "commands": ["pytest"]}}}


def detail():
    return {"backend": "pi_desktop", "observer_alive": True, "run_status": "finished",
            "terminal": {"outcome": "completed"}, "exit_code": 0, "send_state": "sent"}


def message(command="pytest", code=0, seq=1, run_id="execution"):
    return {"id": str(seq), "seq": seq, "run_id": run_id, "type": "command_output",
            "tool": {"input": json.dumps({"command": command}), "exit_code": code}}


def invoke(monkeypatch, tmp_path, *, task_value=None, round_value=None, responses=None, messages=None):
    clock, calls, records = FakeClock(), [], []
    task_value = copy.deepcopy(task_value if task_value is not None else task())
    round_value = copy.deepcopy(round_value if round_value is not None else detail())
    responses = dict(responses or {})
    claims = [{"claimed": True, "request_id": "monitor-request", "run_id": "monitor-run",
               "context": {"task_id": "task", "lease_seconds": 180}}, {"claimed": False}]

    def run(command, payload, db_path):
        calls.append((command, dict(payload)))
        result = responses.get(command)
        if isinstance(result, list):
            result = result.pop(0)
        if isinstance(result, Exception):
            raise result
        if callable(result):
            return result(payload, clock)
        if result is not None:
            return result
        if command == "claim":
            return claims.pop(0)
        if command == "get-task":
            return {"task": task_value}
        if command == "recheck":
            return {"kaneo_status": "in-progress"}
        if command == "get-session":
            return {"session": {"run_id": "execution"}, "round_detail": round_value}
        if command == "session-messages":
            return {"messages": messages if messages is not None else [message()], "has_more": False,
                    "session": {"run_id": "execution", "state": "associated"}}
        if command == "delivery-commit":
            return {"outcome": "committed"}
        return {}

    monkeypatch.setattr(auto_dispatch.cli, "run", run)
    rc = auto_monitor.run_stage("monitor", conn=None, db_path=tmp_path / "unused.sqlite", timeout=600,
        log_path=tmp_path / "monitor.jsonl", record_run=lambda **kw: records.append(kw),
        sleep=clock.sleep, clock=clock)
    return rc, calls, records


def report(calls):
    return next(p for command, p in calls if command == "report")


def test_completion_and_execution_identity(monkeypatch, tmp_path):
    rc, calls, records = invoke(monkeypatch, tmp_path)
    assert rc == 0
    advance = next(p for c, p in calls if c == "advance-task")
    assert advance == {"task_id": "task", "run_id": "monitor-run", "target": "in-review", "expected_current": "in-progress"}
    assert all(p["run_id"] == "execution" for c, p in calls if c in ("get-session", "session-messages"))
    assert report(calls)["outcome"] == "succeeded"
    assert next(p for c, p in calls if c == "record-cost") == {"task_id": "task", "request_id": "execution-request",
        "run_id": "execution", "stage": "dispatch", "source": "pi_desktop", "cost_usd": None}
    assert records[-1]["exit_code"] == 0


@pytest.mark.parametrize("result,code", [
    ({"verification_ok": True}, None),
    ({"verification_ok": False, "empty_diff": True}, "verification_unconfirmed"),
    ({"verification_ok": False, "checks": [{"exit_code": 1}]}, "verification_unconfirmed"),
    ({"verification_ok": False, "checks": [{"timed_out": True}]}, "verification_unconfirmed"),
    ({"verification_ok": False, "environment_failure": True}, "verification_environment_failure"),
])
def test_live_core_verdict_ignores_transcript(monkeypatch, tmp_path, result, code):
    value = task()
    value["project_id"] = "live-project"
    value["extra"]["delivery"] = {"mode": "pr"}
    result = dict(result, detail="core verdict")
    _, calls, _ = invoke(monkeypatch, tmp_path, task_value=value, messages=[], responses={
        "get-project-mappings": {"items": [{"project_id": "live-project", "engine_mode": "live"}]},
        "engine-verdict": result,
        "session-messages": util.KanpilotError("observation_blocked", "transcript unavailable"),
    })
    assert not any(c == "session-messages" for c, _ in calls)
    verdict_call = next(p for c, p in calls if c == "engine-verdict")
    assert verdict_call["expected_execution_run_id"] == "execution"
    assert verdict_call["expected_plan_sha256"] == "plan"
    assert verdict_call["budget_seconds"] <= 180 * 0.8
    if code:
        assert report(calls)["failure_code"] == code
        assert not any(c in ("delivery-commit", "advance-task") for c, _ in calls)
    else:
        assert report(calls)["outcome"] == "succeeded"
        assert any(c == "delivery-commit" for c, _ in calls)


def test_live_untrusted_binding_enters_attention(monkeypatch, tmp_path):
    value = dict(task(), project_id="p")
    _, calls, _ = invoke(monkeypatch, tmp_path, task_value=value, responses={
        "get-project-mappings": {"items": [{"project_id": "p", "engine_mode": "live"}]},
        "engine-verdict": util.KanpilotError("verification_identity_unconfirmed", "计划已变化"),
    })
    assert report(calls)["failure_code"] == "verification_identity_unconfirmed"
    assert not any(c == "advance-task" for c, _ in calls)


def test_shadow_retains_legacy_outcome(monkeypatch, tmp_path):
    value = dict(task(), project_id="p")
    _, calls, _ = invoke(monkeypatch, tmp_path, task_value=value, messages=[message(code=7)], responses={
        "get-project-mappings": {"items": [{"project_id": "p", "engine_mode": "shadow"}]},
        "engine-verdict": {"verification_ok": True},
    })
    assert report(calls)["failure_code"] == "verification_unconfirmed"
    assert any(c == "engine-verdict" for c, _ in calls)


def test_shadow_comparison_error_keeps_passing_legacy_result(monkeypatch, tmp_path):
    value = dict(task(), project_id="p")
    _, calls, _ = invoke(monkeypatch, tmp_path, task_value=value, responses={
        "get-project-mappings": {"items": [{"project_id": "p", "engine_mode": "shadow"}]},
        "engine-verdict": util.KanpilotError("verification_identity_unconfirmed", "副本验证失败"),
    })
    assert report(calls)["outcome"] == "succeeded"


def test_running_and_superseded_terminal(monkeypatch, tmp_path):
    value = dict(detail(), run_status="running", terminal=None, exit_code=None,
                 terminal_superseded={"outcome": "failed"})
    rc, calls, _ = invoke(monkeypatch, tmp_path, round_value=value)
    assert rc == 0 and report(calls)["outcome"] == "succeeded"
    assert any(c == "heartbeat" for c, _ in calls)
    assert not any(c == "advance-task" for c, _ in calls)


@pytest.mark.parametrize("messages,passed", [([], False), ([message(code=1)], False),
    ([message(code=1), message(code=0, seq=2)], True), ([message(), message(code=1, seq=2)], False),
    ([message("cd repo && pytest")], True), ([message(code=None)], False)])
def test_verification_evidence(monkeypatch, tmp_path, messages, passed):
    rc, calls, _ = invoke(monkeypatch, tmp_path, messages=messages)
    assert rc == 0
    assert (report(calls)["outcome"] == "succeeded") == passed
    if not passed:
        assert report(calls)["failure_code"] == "verification_unconfirmed"
        assert report(calls)["note"]


def test_path_prefixed_checks_advance_with_original_execution_identity(monkeypatch, tmp_path):
    value = task()
    commands = ["dart format --output=none --set-exit-if-changed lib/core/providers.dart",
                "flutter analyze --no-pub lib/core/providers.dart",
                "flutter test --no-pub test/daily_recommend_shuffle_test.dart"]
    value["extra"]["plan_verify"]["commands"] = commands
    messages = [message('export PATH="$PATH:/Users/developer/flutter/bin:/opt/homebrew/bin:/usr/local/bin"\n'
                        + command, seq=seq) for seq, command in zip((235, 237, 239), commands)]
    _, calls, _ = invoke(monkeypatch, tmp_path, task_value=value, messages=list(reversed(messages)))
    assert report(calls)["outcome"] == "succeeded"
    assert any(c == "advance-task" for c, _ in calls)


@pytest.mark.parametrize("command,code", [
    ("pytest", None),
    ('export PATH="$PATH:/bin"\npytest', None),
    ('export PATH="$PATH:/bin"\npytest', 7),
    ('export PATH="$PATH:/bin"\npytest\ntrue', 0),
    ('export PATH="$PATH:/bin"\npytest || true', 0),
    ('export PATH="$HOME:/bin"\npytest', 0),
])
def test_unknown_verification_note_is_not_reported_as_command_failure(monkeypatch, tmp_path, command, code):
    value = task()
    value["extra"]["delivery"] = {"mode": "pr"}
    _, calls, _ = invoke(monkeypatch, tmp_path, task_value=value,
                        messages=[message(), message(command, code=code, seq=2)])
    outcome = report(calls)
    assert outcome["outcome"] == "needs_user"
    assert outcome["failure_code"] == "verification_unconfirmed"
    assert "证据无法确认：pytest" in outcome["note"]
    assert "exit_code=None" not in outcome["note"] and "失败：" not in outcome["note"]
    assert not any(c in ("delivery-commit", "advance-task") for c, _ in calls)


def test_exact_verification_failure_keeps_real_exit_code(monkeypatch, tmp_path):
    _, calls, _ = invoke(monkeypatch, tmp_path, messages=[message(code=7)])
    assert "失败：pytest（exit_code=7）" == report(calls)["note"]


def test_path_wrapped_evidence_from_other_round_cannot_advance(monkeypatch, tmp_path):
    _, calls, _ = invoke(monkeypatch, tmp_path, messages=[message(
        'export PATH="$PATH:/bin"\npytest', run_id="previous-execution")])
    assert report(calls)["failure_code"] == "execution_failed"
    assert not any(c in ("delivery-commit", "advance-task") for c, _ in calls)


@pytest.mark.parametrize("command,verification,previous_pass", [
    (command, verification, previous_pass)
    for command, verification in [
    ("false && echo ok || true", "echo ok"),
    ("false && echo ok; true", "echo ok"),
    ("printf '%s' 'x && false && y'", "false"),
    ("exit 0 && pytest", "pytest"),
    ("exec true && pytest", "pytest"),
    ("pytest || true", "pytest"),
    ]
    for previous_pass in (False, True)
    if not (previous_pass and command.startswith("printf"))
])
def test_ambiguous_shell_success_does_not_commit_or_advance(monkeypatch, tmp_path,
                                                          command, verification, previous_pass):
    value = task()
    value["extra"]["plan_verify"]["commands"] = [verification]
    value["extra"]["delivery"] = {"mode": "pr"}
    messages = [message(verification)] if previous_pass else []
    messages.append(message(command, seq=2))
    rc, calls, _ = invoke(monkeypatch, tmp_path, task_value=value, messages=messages)
    assert rc == 0
    assert report(calls)["outcome"] == "needs_user"
    assert report(calls)["failure_code"] == "verification_unconfirmed"
    assert not any(c in ("delivery-commit", "advance-task") for c, _ in calls)


def test_literal_operator_argument_keeps_independent_previous_verification(monkeypatch, tmp_path):
    value = task()
    value["extra"]["delivery"] = {"mode": "pr"}
    _, calls, _ = invoke(monkeypatch, tmp_path, task_value=value, messages=[message(),
        message("printf '%s' 'x && pytest && y'", seq=2)])
    assert report(calls)["outcome"] == "succeeded"
    assert any(c == "delivery-commit" for c, _ in calls)
    assert any(c == "advance-task" for c, _ in calls)


@pytest.mark.parametrize("execution_run_id,passed", [("execution", True), ("old", False)])
def test_override_scoped_to_execution(monkeypatch, tmp_path, execution_run_id, passed):
    value = task()
    value["extra"] = {"verification_override": {"execution_run_id": execution_run_id}}
    _, calls, _ = invoke(monkeypatch, tmp_path, task_value=value, messages=[])
    assert (report(calls)["outcome"] == "succeeded") == passed


def test_override_does_not_bypass_exit_code(monkeypatch, tmp_path):
    value = task()
    value["extra"]["verification_override"] = {"execution_run_id": "execution"}
    _, calls, _ = invoke(monkeypatch, tmp_path, task_value=value, round_value=dict(detail(), exit_code=2))
    assert report(calls)["failure_code"] == "execution_failed"


@pytest.mark.parametrize("outcome", ["committed", "already_committed", "blocked"])
@pytest.mark.parametrize("delivery", [{"mode": "pr"}, {"mode": "manual", "workspace_version": 1}])
def test_delivery_commit(monkeypatch, tmp_path, outcome, delivery):
    value = task()
    value["extra"]["delivery"] = delivery
    _, calls, _ = invoke(monkeypatch, tmp_path, task_value=value, responses={"delivery-commit": {
        "outcome": outcome, "failure_code": "delivery_dirty"}})
    commit = next(p for c, p in calls if c == "delivery-commit")
    assert commit["run_id"] == "monitor-run" and commit["expected_execution_run_id"] == "execution"
    assert report(calls)["outcome"] == ("failed" if outcome == "blocked" else "succeeded")
    assert any(c == "advance-task" for c, _ in calls) == (outcome != "blocked")
    if outcome != "blocked":
        commands = [command for command, _ in calls]
        assert commands.index("delivery-commit") < commands.index("advance-task")


def test_manual_branch_remote_review_still_verifies_and_commits(monkeypatch, tmp_path):
    value = task()
    value["extra"]["delivery"] = {"mode": "manual", "workspace_version": 1}
    _, calls, _ = invoke(monkeypatch, tmp_path, task_value=value,
                        responses={"recheck": {"kaneo_status": "in-review"}})
    commands = [command for command, _ in calls]
    assert commands.index("session-messages") < commands.index("delivery-commit") < commands.index("advance-task")
    assert report(calls)["outcome"] == "succeeded"


@pytest.mark.parametrize("code", ["remote_conflict", "remote_unavailable"])
def test_advance_failure_is_retryable(monkeypatch, tmp_path, code):
    _, calls, _ = invoke(monkeypatch, tmp_path, responses={"advance-task": util.KanpilotError(code, "remote failed")})
    assert report(calls)["outcome"] == "failed" and report(calls)["note"] == code


@pytest.mark.parametrize("remote_status,managed,advanced", [("done", False, True),
    ("in-review", False, True), ("in-review", True, False)])
def test_passive_remote_states(monkeypatch, tmp_path, remote_status, managed, advanced):
    value = task()
    if managed:
        value["extra"]["automation"] = {"policy_revision": 1}
    _, calls, _ = invoke(monkeypatch, tmp_path, task_value=value,
        round_value=dict(detail(), terminal=None, run_status="running", exit_code=None),
        responses={"recheck": {"kaneo_status": remote_status}})
    advances = [p for c, p in calls if c == "advance-task"]
    assert bool(advances) == advanced
    if advanced:
        assert advances[0]["target"] == advances[0]["expected_current"] == remote_status


def test_automated_remote_in_review_waits_then_completes(monkeypatch, tmp_path):
    value = task()
    value["extra"]["automation"] = {"policy_revision": 1}
    _, calls, _ = invoke(monkeypatch, tmp_path, task_value=value, responses={"recheck": {"kaneo_status": "in-review"}})
    assert next(p for c, p in calls if c == "advance-task")["expected_current"] == "in-review"


@pytest.mark.parametrize("status,reopened,outcome", [("deferred", False, "failed"),
    ("exited", False, "failed"), ("starting", False, "failed"), ("observing", True, "succeeded"),
    ("observing", False, "succeeded"), ("finished", False, "succeeded"), ("settled", False, "succeeded")])
def test_observer_resume(monkeypatch, tmp_path, status, reopened, outcome):
    value = task()
    value['extra']['automation'] = {'manual_takeover': False}
    _, calls, _ = invoke(monkeypatch, tmp_path, task_value=value, round_value=dict(detail(), observer_alive=False),
        responses={"round-observe": {"status": status, "reopened": reopened}})
    assert next(p for c, p in calls if c == "round-observe")["run_id"] == "execution"
    assert next(p for c, p in calls if c == "round-observe")["continue_goal"] is True
    assert report(calls)["outcome"] == outcome
    if reopened:
        assert not any(c == "advance-task" for c, _ in calls)


def test_unmanaged_observer_resume_stays_readonly(monkeypatch, tmp_path):
    _, calls, _ = invoke(monkeypatch, tmp_path, round_value=dict(detail(), observer_alive=False),
        responses={"round-observe": {"status": "deferred"}})
    assert next(p for c, p in calls if c == "round-observe")["continue_goal"] is False


def test_alive_observer_not_restarted(monkeypatch, tmp_path):
    _, calls, _ = invoke(monkeypatch, tmp_path, round_value=dict(detail(), detail="heartbeat_failed:old"))
    assert not any(c == "round-observe" for c, _ in calls)


@pytest.mark.parametrize("code", ["goal_contract_missing", "goal_approval_failed", "goal_execution_unstarted"])
def test_goal_terminal_gate(monkeypatch, tmp_path, code):
    _, calls, _ = invoke(monkeypatch, tmp_path, round_value=dict(detail(), terminal={"outcome": "failed", "detail": code}))
    assert report(calls)["failure_code"] == "execution_failed"


def test_planning_completion_is_not_terminal(monkeypatch, tmp_path):
    _, calls, _ = invoke(monkeypatch, tmp_path, round_value=dict(detail(), terminal=None, run_status="running",
        planning_turn={"status": "completed"}))
    assert report(calls)["outcome"] == "succeeded" and not any(c == "advance-task" for c, _ in calls)


@pytest.mark.parametrize("backend", ["dsh", "headless", "atif"])
def test_old_backend_needs_manual_observation(monkeypatch, tmp_path, backend):
    _, calls, _ = invoke(monkeypatch, tmp_path, round_value=dict(detail(), backend=backend))
    assert report(calls)["failure_code"] == "observation_blocked"
    assert report(calls)["outcome"] == "needs_user"


def test_complete_message_pagination(monkeypatch, tmp_path):
    _, calls, _ = invoke(monkeypatch, tmp_path, responses={"session-messages": [
        {"messages": [message(code=1)], "has_more": True, "next_cursor": "seq:1"},
        {"messages": [message(code=0, seq=2)], "has_more": False}]})
    assert [p.get("cursor") for c, p in calls if c == "session-messages"] == [None, "seq:1"]
    assert report(calls)["outcome"] == "succeeded"


def test_stuck_pagination_is_retryable(monkeypatch, tmp_path):
    _, calls, _ = invoke(monkeypatch, tmp_path, responses={"session-messages": {
        "messages": [], "has_more": True, "next_cursor": "seq:1"}})
    assert report(calls)["outcome"] == "failed" and report(calls)["note"] == "observation_blocked"


@pytest.mark.parametrize("value", [dict(detail(), exit_code=None), dict(detail(), exit_code=False),
    dict(detail(), exit_code=1), dict(detail(), send_state="unknown"), dict(detail(), run_status="failed"),
    dict(detail(), terminal={"outcome": "truncated"})])
def test_execution_failures(monkeypatch, tmp_path, value):
    _, calls, _ = invoke(monkeypatch, tmp_path, round_value=value)
    assert report(calls)["failure_code"] == "execution_failed"


def test_wrong_message_identity(monkeypatch, tmp_path):
    _, calls, _ = invoke(monkeypatch, tmp_path, messages=[message(run_id="old")])
    assert report(calls)["failure_code"] == "execution_failed"


@pytest.mark.parametrize("command", ["get-task", "recheck", "get-session", "session-messages", "advance-task", "record-cost", "report", "heartbeat", "round-observe", "delivery-commit"])
@pytest.mark.parametrize("code", ["stale_run", "not_found"])
def test_stale_no_more_calls(monkeypatch, tmp_path, command, code):
    value, round_value = task(), detail()
    if command == "delivery-commit":
        value["extra"]["delivery"] = {"mode": "pr"}
    if command == "round-observe":
        round_value["observer_alive"] = False
    if command == "heartbeat":
        round_value.update(terminal=None, run_status="running", exit_code=None)
    rc, calls, _ = invoke(monkeypatch, tmp_path, task_value=value, round_value=round_value,
                          responses={command: util.KanpilotError(code, "taken over")})
    assert rc == 0 and calls[-1][0] == command


def test_missing_round_is_retryable(monkeypatch, tmp_path):
    _, calls, _ = invoke(monkeypatch, tmp_path, responses={"get-session": {"round_detail": None}})
    assert report(calls)["outcome"] == "failed"


def test_heartbeat_during_message_pagination(monkeypatch, tmp_path):
    pages = [0]

    def read(payload, clock):
        clock.sleep(30)
        pages[0] += 1
        return {"messages": [message(seq=pages[0])], "has_more": pages[0] < 3,
                "next_cursor": "seq:" + str(pages[0])}

    _, calls, _ = invoke(monkeypatch, tmp_path, responses={"session-messages": read})
    assert any(c == "heartbeat" for c, _ in calls)


@pytest.mark.parametrize("reason", ["transcript_incomplete", "turn_identity_mismatch", "执行轮次超时", "日志截断"])
def test_explicit_failure_details_block_completion(monkeypatch, tmp_path, reason):
    _, calls, _ = invoke(monkeypatch, tmp_path, round_value=dict(detail(), detail=reason))
    assert report(calls)["failure_code"] == "execution_failed"


def test_page_unavailable_blocks_override(monkeypatch, tmp_path):
    value = task()
    value["extra"]["verification_override"] = {"execution_run_id": "execution"}
    _, calls, _ = invoke(monkeypatch, tmp_path, task_value=value, responses={"session-messages": {
        "messages": [], "has_more": False, "session": {"state": "unavailable"}}})
    assert report(calls)["outcome"] == "failed" and report(calls)["note"] == "observation_blocked"


def test_legacy_round_detected_when_detail_missing(monkeypatch, tmp_path):
    value = task()
    value["execution_round"] = {"backend": "dsh"}
    _, calls, _ = invoke(monkeypatch, tmp_path, task_value=value, responses={"get-session": {"round_detail": None}})
    assert report(calls)["outcome"] == "needs_user"


def test_plan_verification_hash_mismatch(monkeypatch, tmp_path):
    value = task()
    value["extra"]["plan_verify"]["plan_sha256"] = "old"
    _, calls, _ = invoke(monkeypatch, tmp_path, task_value=value)
    assert report(calls)["failure_code"] == "verification_unconfirmed"
