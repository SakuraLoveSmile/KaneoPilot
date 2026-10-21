import json

import pytest

from kanpilot import auto_dispatch, util


class FakeClock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def claim(backend="pi_desktop"):
    return {"claimed": True, "request_id": "request", "run_id": "dispatch-run", "context": {
        "task_id": "task", "backend": backend, "session_marker": "marker",
        "repository": "repo", "exec_config": {}, "lease_seconds": 180}}


def session(session_id="session"):
    return {"session": {"task_id": "task", "run_id": "dispatch-run", "session_id": session_id},
            "round_detail": {"turn_id": "turn"}}


def invoke(monkeypatch, tmp_path, responses=None, backend="pi_desktop", timeout=600):
    responses = responses or {}
    clock, calls, records = FakeClock(), [], []
    claims = [claim(backend), {"claimed": False}]

    def run(command, payload, db_path):
        calls.append((command, dict(payload), clock()))
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
        if command == "dispatch-start":
            return {"status": "started", "session_id": "session"}
        if command == "get-session":
            return session()
        return {}

    monkeypatch.setattr(auto_dispatch.cli, "run", run)
    rc = auto_dispatch.run_stage("dispatch", conn=None, db_path=tmp_path / "unused.sqlite",
        timeout=timeout, log_path=tmp_path / "dispatch.jsonl", record_run=lambda **kw: records.append(kw),
        sleep=clock.sleep, clock=clock)
    return rc, calls, records


def test_normal_dispatch(monkeypatch, tmp_path):
    rc, calls, records = invoke(monkeypatch, tmp_path)
    assert rc == 0
    assert [c[0] for c in calls] == ["claim", "dispatch-prepare", "dispatch-begin-send", "dispatch-start", "get-session", "dispatch-confirm", "claim"]
    assert calls[-2][1]["evidence"]["protocol_seen"] is True
    assert records[0]["pid"] and records[-1]["exit_code"] == 0
    logs = [json.loads(line) for line in (tmp_path / "dispatch.jsonl").read_text().splitlines()]
    assert all(set(log) == {"time", "step", "task_id", "request_id", "code"} for log in logs)


def test_starting_polls_and_heartbeats(monkeypatch, tmp_path):
    rc, calls, _ = invoke(monkeypatch, tmp_path, {
        "dispatch-start": {"status": "starting"},
        "get-session": [{"session": {}}, {"session": {}}, session("late"), session("late")]})
    assert rc == 0
    assert [c[2] for c in calls if c[0] == "heartbeat"] == [60]
    assert [c[2] for c in calls if c[0] == "get-session"] == [30, 60, 90, 90]
    assert next(c[1]["evidence"]["session_id"] for c in calls if c[0] == "dispatch-confirm") == "late"


def test_wait_timeout_unknown(monkeypatch, tmp_path):
    rc, calls, _ = invoke(monkeypatch, tmp_path, {"dispatch-start": {"status": "starting"}, "get-session": {"session": {}}})
    assert rc == 0
    fail = next(c[1] for c in calls if c[0] == "dispatch-fail")
    assert fail["send_state"] == "unknown" and "worker 会话未建立" in fail["reason"]
    assert [c[2] for c in calls if c[0] == "heartbeat"] == [60, 120, 180, 240, 300]


@pytest.mark.parametrize("command,code,send_state", [("dispatch-prepare", "validation_failed", "not_sent"),
    ("dispatch-begin-send", "remote_conflict", "unknown"), ("dispatch-start", "validation_failed", "unknown"),
    ("dispatch-confirm", "validation_failed", "unknown")])
def test_failure_send_boundary(monkeypatch, tmp_path, command, code, send_state):
    rc, calls, _ = invoke(monkeypatch, tmp_path, {command: util.KanpilotError(code, "failure"),
                                               "dispatch-fail": {"already_recorded": True}})
    assert rc == 0
    assert next(c[1] for c in calls if c[0] == "dispatch-fail")["send_state"] == send_state


@pytest.mark.parametrize("command", ["dispatch-prepare", "dispatch-begin-send", "dispatch-start", "dispatch-confirm", "get-session", "heartbeat"])
@pytest.mark.parametrize("code", ["stale_run", "not_found"])
def test_stale_stops_all_calls(monkeypatch, tmp_path, command, code):
    responses = {command: util.KanpilotError(code, "taken over")}
    if command in ("get-session", "heartbeat"):
        responses["dispatch-start"] = {"status": "starting"}
        if command == "heartbeat":
            responses["get-session"] = {"session": {}}
    rc, calls, _ = invoke(monkeypatch, tmp_path, responses)
    assert rc == 0 and calls[-1][0] == command


def test_unsupported_zero_post_claim_writes(monkeypatch, tmp_path):
    assert [c[0] for c in invoke(monkeypatch, tmp_path, backend="dsh")[1]] == ["claim"]


def test_paused_and_insufficient_time(monkeypatch, tmp_path):
    assert invoke(monkeypatch, tmp_path, {"claim": {"claimed": False, "reason": "paused"}})[0] == 0
    assert invoke(monkeypatch, tmp_path, timeout=119)[1] == []


def test_pi_path_does_not_require_plus_turn(monkeypatch, tmp_path):
    rc, calls, _ = invoke(monkeypatch, tmp_path, backend="pi")
    assert rc == 0 and not any(c[0] == "get-session" for c in calls)
    assert "turn_id" not in next(c[1]["evidence"] for c in calls if c[0] == "dispatch-confirm")


@pytest.mark.parametrize("field,value", [("task_id", "other"), ("run_id", "other"), ("session_id", "other")])
def test_confirm_rejects_plus_identity_mismatch(monkeypatch, tmp_path, field, value):
    observed = session()
    observed["session"][field] = value
    rc, calls, _ = invoke(monkeypatch, tmp_path, {"get-session": observed})
    assert rc == 0
    assert next(c[1] for c in calls if c[0] == "dispatch-fail")["send_state"] == "unknown"
    assert not any(c[0] == "dispatch-confirm" for c in calls)


def test_confirm_requires_turn_id(monkeypatch, tmp_path):
    observed = session()
    observed["round_detail"] = {}
    _, calls, _ = invoke(monkeypatch, tmp_path, {"get-session": observed})
    assert next(c[1] for c in calls if c[0] == "dispatch-fail")["send_state"] == "unknown"


def test_quick_goal_completion_confirms_startup_turn(monkeypatch, tmp_path):
    observed = session()
    observed["round_detail"].update(startup_turn_id="planning-turn", turn_id="execution-turn",
                                    run_status="finished", terminal={"outcome": "completed"}, exit_code=0)
    rc, calls, _ = invoke(monkeypatch, tmp_path, {"get-session": observed})
    assert rc == 0
    evidence = next(c[1]["evidence"] for c in calls if c[0] == "dispatch-confirm")
    assert evidence["turn_id"] == "planning-turn"
    assert not any(c[0] == "dispatch-fail" for c in calls)


def pending_startup():
    observed = session(None)
    observed["session"]["state"] = "pending"
    observed["connection"] = {"state": "degraded"}
    observed["round_detail"].update(startup_turn_id=None, turn_id="execution-turn",
                                    run_status="running")
    return observed


@pytest.mark.parametrize("backend", ["pi_desktop", "codex"])
def test_confirm_rechecks_pending_startup_without_resending(monkeypatch, tmp_path, backend):
    ready = session()
    ready["round_detail"].update(startup_turn_id="planning-turn", turn_id="execution-turn")
    rc, calls, _ = invoke(monkeypatch, tmp_path,
                         {"get-session": [pending_startup(), ready]}, backend=backend)
    assert rc == 0
    assert [c[2] for c in calls if c[0] == "get-session"] == [0, 1]
    evidence = next(c[1]["evidence"] for c in calls if c[0] == "dispatch-confirm")
    assert evidence["turn_id"] == "planning-turn"
    assert not any(c[0] == "dispatch-fail" for c in calls)
    for command in ("dispatch-prepare", "dispatch-begin-send", "dispatch-start"):
        assert sum(c[0] == command for c in calls) == 1


@pytest.mark.parametrize("timeout,deadline", [(600, 300), (180, 180)])
def test_confirm_pending_startup_timeout_keeps_unknown(monkeypatch, tmp_path, timeout, deadline):
    rc, calls, _ = invoke(monkeypatch, tmp_path, {"get-session": pending_startup()},
                         timeout=timeout)
    assert rc == 0
    reads = [c for c in calls if c[0] == "get-session"]
    assert reads[-1][2] == deadline
    assert len(reads) == deadline + 1
    assert next(c[1] for c in calls if c[0] == "dispatch-fail")["send_state"] == "unknown"
    assert not any(c[0] == "dispatch-confirm" for c in calls)
    assert sum(c[0] == "dispatch-start" for c in calls) == 1
    assert [c[2] for c in calls if c[0] == "heartbeat"] == list(range(60, deadline + 1, 60))


@pytest.mark.parametrize("field,value", [("task_id", "foreign"), ("run_id", "foreign"),
                                         ("session_id", "foreign")])
def test_confirm_pending_with_conflicting_identity_fails_immediately(monkeypatch, tmp_path, field, value):
    observed = pending_startup()
    observed["session"][field] = value
    _, calls, _ = invoke(monkeypatch, tmp_path, {"get-session": observed})
    assert [c[2] for c in calls if c[0] == "get-session"] == [0]
    assert not any(c[0] == "dispatch-confirm" for c in calls)
    assert next(c[1] for c in calls if c[0] == "dispatch-fail")["send_state"] == "unknown"


@pytest.mark.parametrize("state", ["associated", "unavailable"])
def test_confirm_nonpending_missing_startup_fails_immediately(monkeypatch, tmp_path, state):
    observed = pending_startup()
    observed["session"].update(state=state, session_id="session")
    _, calls, _ = invoke(monkeypatch, tmp_path, {"get-session": observed})
    assert [c[2] for c in calls if c[0] == "get-session"] == [0]
    assert not any(c[0] == "dispatch-confirm" for c in calls)


def test_confirm_pending_unavailable_connection_fails_immediately(monkeypatch, tmp_path):
    observed = pending_startup()
    observed["connection"]["state"] = "unavailable"
    _, calls, _ = invoke(monkeypatch, tmp_path, {"get-session": observed})
    assert [c[2] for c in calls if c[0] == "get-session"] == [0]
    assert not any(c[0] == "dispatch-confirm" for c in calls)


@pytest.mark.parametrize("code", ["stale_run", "not_found"])
def test_confirm_pending_heartbeat_losing_ownership_stops_all_calls(monkeypatch, tmp_path, code):
    rc, calls, _ = invoke(monkeypatch, tmp_path, {"get-session": pending_startup(),
        "heartbeat": util.KanpilotError(code, "taken over")})
    assert rc == 0
    assert calls[-1][0] == "heartbeat" and calls[-1][2] == 60
    assert not any(c[0] in ("dispatch-confirm", "dispatch-fail") for c in calls)
    assert sum(c[0] == "dispatch-start" for c in calls) == 1


@pytest.mark.parametrize("startup_turn_id", [None, "", 42])
def test_missing_startup_turn_does_not_fall_back_to_current_turn(monkeypatch, tmp_path, startup_turn_id):
    observed = session()
    observed["round_detail"].update(startup_turn_id=startup_turn_id, turn_id="execution-turn")
    rc, calls, _ = invoke(monkeypatch, tmp_path, {"get-session": observed})
    assert rc == 0
    assert next(c[1] for c in calls if c[0] == "dispatch-fail")["send_state"] == "unknown"
    assert not any(c[0] == "dispatch-confirm" for c in calls)


def test_log_does_not_serialize_sensitive_context(monkeypatch, tmp_path):
    responses = {"dispatch-prepare": util.KanpilotError("validation_failed", "https://user:secret@host/plan body")}
    invoke(monkeypatch, tmp_path, responses)
    contents = (tmp_path / "dispatch.jsonl").read_text()
    assert "secret" not in contents and "https://" not in contents and "plan body" not in contents
