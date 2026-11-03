"""KP-E01: reject new pi execution and retain historical rounds.

Only the pre-upgrade fixture writes historical DB state: today's CLI cannot
create a pi claim. Assertions use the public CLI and isolated saved evidence;
no worker or model is called.
"""
import json
from pathlib import Path

import pytest
from kanpilot import db, exec_config, round_store, util


@pytest.fixture(autouse=True)
def pi_execution_config(factory, cli):
    cli("set-exec-config", {"backend": "pi"})


def _historical_claim(factory, cli, ready, *, status="dispatching", started=False):
    """Seed a pre-upgrade claim without bypassing the current claim guard."""
    tid = ready.task.task_id
    factory.enqueue_dispatch(tid)
    request = factory.find_request(tid, "dispatch")
    run_id, now = "historical-pi-run", util.now_iso()
    marker = tid + "/" + run_id
    directory = Path(cli("overview", {})["paths"]["session_runs_root"]) / tid / run_id
    conn = db.connect(factory.env.db)
    try:
        with db.write_txn(conn):
            config = exec_config.snapshot(conn)
            assert config["backend"] == "pi"
            db.update_request(conn, request["request_id"], now, status="claimed", run_id=run_id,
                              claimed_by="kaneo-dispatch", lease_expires_at=util.plus_seconds(600))
            db.update_task(conn, tid, now, status=status, run_id=run_id,
                           session_marker=marker,
                           transition_verified_at=now if status == "submitting" else None)
            round_store.upsert_on_claim(conn, tid, run_id, config, now, backend="pi")
            if started:
                round_store.mark_started(conn, tid, run_id, now, pid=9999999,
                    worktree_path=str(factory.env.root / "historical-worktree"))
    finally:
        conn.close()
    if status == "submitting":
        factory.kaneo.set_status(tid, "in-progress")
    if started:
        directory.mkdir(parents=True)
        (directory / "job.json").write_text(json.dumps({
            "v": 1, "backend": "pi", "task_id": tid, "run_id": run_id,
            "status": "finished", "exit_code": 0, "pid": 9999999,
            "session_id": "historical-session", "session_title": "Historical pi",
        }), encoding="utf-8")
        events = [
            {"seq": 1, "type": "session", "session_id": "historical-session", "session_title": "Historical pi"},
            {"seq": 2, "type": "terminal", "outcome": "completed", "reason": "ok"},
        ]
        (directory / "events.jsonl").write_text(
            "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    return {"task_id": tid, "run_id": run_id}, {
        "backend": "pi", "config": config, "session_marker": marker,
        "repository": str(ready.task.repository),
    }


def _round(cli, identity):
    row = cli("get-task", {"task_id": identity["task_id"]})["task"]["execution_round"]
    assert row["run_id"] == identity["run_id"]
    return row


@pytest.mark.parametrize("available", [True, False])
def test_new_pi_claim_rejected_before_send_or_round(factory, cli, tmp_env, available):
    ready = factory.make_ready()
    factory.enqueue_dispatch(ready.task.task_id)
    request = factory.find_request(ready.task.task_id, "dispatch")
    before_task = factory.get_task(ready.task.task_id)
    before = before_task.get("execution_round")
    if not available:
        empty = tmp_env.root / "unavailable-pi-home"
        empty.mkdir()
        cli.env["KANPILOT_PI_HOME"] = str(empty)
    result = factory.claim("dispatch", request_id=request["request_id"])
    assert result["claimed"] is False and result["reason"] == "guard_failed"
    assert result["code"] == "exec_backend_not_plus"
    current = factory.get_task(ready.task.task_id)
    assert current["status"] == "needs_user" and current["sent"] == before_task["sent"]
    assert current.get("execution_round") == before
    assert factory.kaneo.tasks[ready.task.task_id]["status"] == "to-do"
    root = Path(cli("overview", {})["paths"]["session_runs_root"]) / ready.task.task_id
    assert not (root / (result.get("run_id") or "new-pi-run")).exists()


def test_historical_snapshot_survives_config_change(factory, cli):
    identity, evidence = _historical_claim(factory, cli, factory.make_ready())
    before = _round(cli, identity)
    assert before["backend"] == "pi" and before["config"] == evidence["config"]
    assert before["save_state"] == "active" and before["started_at"] is None
    cli("set-exec-config", {"reasoning_effort": "high"})
    assert _round(cli, identity)["config"] == before["config"]


def test_historical_prepare_checks_frozen_config(factory, cli):
    identity, evidence = _historical_claim(factory, cli, factory.make_ready())
    bad = {**evidence, "config": {**evidence["config"], "model": "other-model"}}
    assert cli("dispatch-prepare", {**identity, "evidence": bad}, expect=1)["code"] == "evidence_invalid"
    assert cli("dispatch-prepare", {**identity, "evidence": evidence})["status"] == "prepared"


def test_historical_unstarted_round_cannot_send_or_start(factory, cli):
    identity, evidence = _historical_claim(factory, cli, factory.make_ready())
    cli("dispatch-prepare", {**identity, "evidence": evidence})
    before = _round(cli, identity)
    assert cli("dispatch-begin-send", identity, expect=1)["code"] == "exec_backend_not_plus"
    assert factory.get_task(identity["task_id"])["status"] == "prepared"
    assert factory.kaneo.tasks[identity["task_id"]]["status"] == "to-do"
    assert _round(cli, identity) == before
    # An old installation may already have completed begin-send. Its pi
    # worker must still not start after upgrading.
    ready = factory.make_ready(title="Previously submitted pi")
    old, _ = _historical_claim(factory, cli, ready, status="submitting")
    before = _round(cli, old)
    assert cli("dispatch-start", old, expect=1)["code"] == "exec_backend_not_plus"
    assert _round(cli, old) == before


def test_historical_started_round_observed_and_confirmed_once(factory, cli):
    identity, evidence = _historical_claim(factory, cli, factory.make_ready(), status="submitting", started=True)
    before = _round(cli, identity)
    first, second = cli("dispatch-start", identity), cli("dispatch-start", identity)
    assert first == second and first["deduplicated"] is True
    assert first["pid"] == before["pid"] == 9999999
    assert first["worktree_path"] == before["worktree_path"]
    assert first["session_id"] == "historical-session"
    assert _round(cli, identity) == before
    confirmed = {**evidence, "session_id": first["session_id"], "protocol_seen": True}
    assert cli("dispatch-confirm", {**identity, "evidence": confirmed})["status"] == "running"
    assert cli("dispatch-confirm", {**identity, "evidence": confirmed})["already_confirmed"] is True
    current = factory.get_task(identity["task_id"])
    assert current["sent"] == 1 and current["send_state"] == "sent"
    assert current["session_id"] == "historical-session"
    assert _round(cli, identity)["config"] == before["config"]
    queued = cli("list-requests", {"status": "queued"})["requests"]
    assert len([r for r in queued if r["task_id"] == identity["task_id"] and r["kind"] == "monitor"]) == 1
    detail = cli("get-session", identity)["round_detail"]
    assert detail["backend"] == "pi" and detail["exit_code"] == 0
    assert detail["terminal"] == {"outcome": "completed", "reason": "ok"}


@pytest.mark.parametrize("started", [False, True])
def test_historical_confirm_requires_start_and_matching_session(factory, cli, started):
    identity, evidence = _historical_claim(factory, cli, factory.make_ready(), status="submitting", started=started)
    result = cli("dispatch-confirm", {**identity, "evidence": {
        **evidence, "session_id": "unknown-session", "protocol_seen": True,
    }}, expect=1)
    assert result["code"] == "evidence_invalid"
    assert factory.get_task(identity["task_id"])["status"] == "submitting"


def load_tests(loader, standard_tests, pattern):
    import sys
    try:
        import unittest_compat
    except ImportError:
        from tests import unittest_compat
    return unittest_compat.collect_module(sys.modules[__name__])
