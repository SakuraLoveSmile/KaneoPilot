"""Queued-plan cancellation uses isolated SQLite and never stops an executor."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest

from kanpilot import auto_intake, cmd_exec, cmd_queue, cmd_read, core, db, derive, util

NOW = "2026-10-07T01:00:00+00:00"
TASK_ID = "cancel-plan-task"


@pytest.fixture
def queued(tmp_path, tmp_env):
    path = tmp_path / "cancel.db"
    conn = db.connect(path)
    conn.execute(
        "INSERT INTO tasks(task_id,project_id,project_name,repository,status,stage,"
        "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (TASK_ID, "project1", "Test", str(tmp_path), "needs_plan", "plan", NOW, NOW),
    )
    manifest = {"task": {"task_id": TASK_ID}, "files": [], "issues": []}
    db.insert_planner_input(conn, "input1", TASK_ID, "a" * 64, "b" * 64,
                            "c" * 64, manifest, NOW)
    request, _ = db.create_request(conn, NOW, "plan", "plan:" + TASK_ID,
                                  task_id=TASK_ID, requested_by="user",
                                  payload={"manual_authorized": True, "input_id": "input1",
                                           "planner_input_sha256": "a" * 64, "manifest": manifest})
    ctx = SimpleNamespace(conn=conn, now_iso=NOW, now=util.parse_ts(NOW))
    yield SimpleNamespace(path=path, conn=conn, ctx=ctx, request_id=request["request_id"],
                          payload={"task_id": TASK_ID, "request_id": request["request_id"]})
    conn.close()


def test_cancel_retains_inputs_blocks_requeue_and_is_idempotent(queued):
    task = db.require_task(queued.conn, TASK_ID)
    core.update_automation(queued.conn, task, NOW, manual_takeover=False, plan_wanted=True)
    before = db.get_request(queued.conn, queued.request_id)["payload_json"]
    result = cmd_queue.cmd_cancel_request(queued.ctx, queued.payload)
    assert result["cancelled"] is True and result["deduplicated"] is False
    assert result["request"]["status"] == "cancelled"
    task = db.require_task(queued.conn, TASK_ID)
    assert (task["status"], task["failure_stage"], task["failure_code"]) == (
        "needs_user", "plan", "plan_cancelled")
    assert core.automation_of(task)["plan_wanted"] is False
    assert db.get_request(queued.conn, queued.request_id)["payload_json"] == before
    assert db.get_planner_input(queued.conn, "input1") is not None
    assert auto_intake._candidate(queued.conn, TASK_ID) is False
    with db.write_txn(queued.conn):
        assert cmd_read.tick_inline(queued.conn, queued.ctx.now)["created"] == []
    events = queued.conn.execute("SELECT * FROM events WHERE kind='request_cancelled'").fetchall()
    assert len(events) == 1 and events[0]["actor"] == "user"
    event_count = queued.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    again = cmd_queue.cmd_cancel_request(queued.ctx, queued.payload)
    assert again["deduplicated"] is True
    assert queued.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == event_count


@pytest.mark.parametrize("authorized", [True, False])
def test_request_and_task_views_offer_cancel_only_while_queued(queued, authorized):
    db.merge_request_payload(queued.conn, queued.request_id, NOW, manual_authorized=authorized)
    details = cmd_read.cmd_get_request(queued.ctx, {"request_id": queued.request_id})
    assert "cancel-request" in details["allowed_actions"]
    listed = cmd_read.cmd_list_requests(queued.ctx, {"status": "queued"})
    assert "cancel-request" in listed["requests"][0]["allowed_actions"]
    task = db.require_task(queued.conn, TASK_ID)
    assert derive.task_detail(queued.conn, task, queued.ctx.now)["queued_plan_request_id"] == queued.request_id
    assert "cancel-request" in derive.allowed_actions(queued.conn, task, queued.ctx.now)
    cmd_queue.cmd_cancel_request(queued.ctx, queued.payload)
    assert "cancel-request" not in cmd_read.cmd_get_request(
        queued.ctx, {"request_id": queued.request_id})["allowed_actions"]
    task = db.require_task(queued.conn, TASK_ID)
    assert derive.task_summary(queued.conn, task, queued.ctx.now)["queued_plan_request_id"] is None
    assert "cancel-request" not in derive.allowed_actions(queued.conn, task, queued.ctx.now)


@pytest.mark.parametrize("status", ["claimed", "succeeded", "failed", "cancelled"])
def test_nonqueued_request_rejected_without_changes(queued, status):
    db.update_request(queued.conn, queued.request_id, NOW, status=status)
    before = [dict(r) for r in queued.conn.execute("SELECT * FROM tasks")]
    with pytest.raises(util.KanpilotError) as error:
        cmd_queue.cmd_cancel_request(queued.ctx, queued.payload)
    assert error.value.code == "conflict"
    assert db.get_request(queued.conn, queued.request_id)["status"] == status
    assert [dict(r) for r in queued.conn.execute("SELECT * FROM tasks")] == before


@pytest.mark.parametrize("change", ["wrong_task", "non_plan", "purged", "missing"])
def test_request_identity_and_kind_guards(queued, change):
    payload = dict(queued.payload)
    if change == "wrong_task":
        payload["task_id"] = "another-task"
    elif change == "missing":
        payload["request_id"] = "missing"
    elif change == "non_plan":
        db.update_request(queued.conn, queued.request_id, NOW, kind="dispatch")
    else:
        db.update_task(queued.conn, TASK_ID, NOW, save_state="purged")
    with pytest.raises(util.KanpilotError) as error:
        cmd_queue.cmd_cancel_request(queued.ctx, payload)
    assert error.value.code == ("not_found" if change == "missing" else "conflict")
    assert db.get_request(queued.conn, queued.request_id)["status"] == "queued"


def test_stale_queued_plan_does_not_change_an_active_task(queued):
    db.update_task(queued.conn, TASK_ID, NOW, status="running", stage="monitor")
    cmd_queue.cmd_cancel_request(queued.ctx, queued.payload)
    assert db.require_task(queued.conn, TASK_ID)["status"] == "running"


def test_cancel_and_claim_have_only_one_winner(queued):
    barrier = Barrier(2)
    def compete(action):
        conn = db.connect(queued.path)
        ctx = SimpleNamespace(conn=conn, now_iso=NOW, now=util.parse_ts(NOW))
        try:
            barrier.wait(timeout=5)
            if action == "cancel":
                return cmd_queue.cmd_cancel_request(ctx, queued.payload)
            return cmd_exec.cmd_claim(ctx, {"stage": "plan", "executor": "test-planner",
                                             "request_id": queued.request_id})
        except util.KanpilotError as error:
            return {"error": error.code}
        finally:
            conn.close()
    with ThreadPoolExecutor(max_workers=2) as executor:
        cancel_future = executor.submit(compete, "cancel")
        claim_future = executor.submit(compete, "claim")
        cancelled, claimed = cancel_future.result(timeout=10), claim_future.result(timeout=10)
    request = db.get_request(queued.conn, queued.request_id)
    task = db.require_task(queued.conn, TASK_ID)
    if request["status"] == "cancelled":
        assert cancelled["cancelled"] is True and claimed == {"error": "conflict"}
        assert task["status"] == "needs_user"
        assert queued.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
    else:
        assert request["status"] == "claimed" and claimed["claimed"] is True
        assert cancelled == {"error": "conflict"} and task["status"] == "planning"
        assert queued.conn.execute("SELECT COUNT(*) FROM events WHERE kind='request_cancelled'").fetchone()[0] == 0
