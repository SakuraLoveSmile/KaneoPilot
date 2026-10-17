"""enqueue offer withdraws while a request is active; get-task exposes the offered kind.

protocol.md §5.2 `enqueue_kind` / §5.3 「排队中不再提供 enqueue」（2026-10-17）。
每个用例使用隔离的临时 SQLite，不访问 live DB。
"""
from types import SimpleNamespace

import pytest

from kanpilot import cmd_read, constants, db, derive, util

NOW = "2026-10-17T01:00:00+00:00"
TASK_ID = "enqueue-offer-task"


@pytest.fixture
def conn(tmp_path, tmp_env):
    conn = db.connect(tmp_path / "enqueue-offer.db")
    yield conn
    conn.close()


def _insert_task(conn, tmp_path, status, stage):
    conn.execute(
        "INSERT INTO tasks(task_id,project_id,project_name,repository,status,stage,"
        "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (TASK_ID, "project1", "Test", str(tmp_path), status, stage, NOW, NOW),
    )


@pytest.mark.parametrize(
    ("status", "stage", "kind", "request_status"),
    [
        ("ready", "dispatch", "dispatch", "queued"),
        ("running", "monitor", "monitor", "queued"),
        ("running", "monitor", "monitor", "claimed"),
        ("running", "monitor", "monitor", "claimed-expired"),
        ("in_review", "review", "review", "queued"),
        # 同一任务的其他 kind 活动请求同样撤回 enqueue，避免叠加第二个请求。
        ("in_review", "review", "delivery", "queued"),
    ],
)
def test_active_request_withdraws_enqueue_until_it_ends(tmp_path, conn, status, stage, kind, request_status):
    now = util.parse_ts(NOW)
    _insert_task(conn, tmp_path, status, stage)
    assert "enqueue" in derive.allowed_actions(conn, db.require_task(conn, TASK_ID), now)
    request, created = db.create_request(conn, NOW, kind, f"{kind}:{TASK_ID}",
                                         task_id=TASK_ID, requested_by="user")
    assert created
    if request_status != "queued":
        lease = "2026-10-17T00:30:00+00:00" if request_status == "claimed-expired" else "2026-10-17T02:00:00+00:00"
        db.update_request(conn, request["request_id"], NOW, status="claimed",
                          claimed_by="test-executor", run_id="run-test", lease_expires_at=lease)
    view = derive.task_detail(conn, db.require_task(conn, TASK_ID), now)
    assert "enqueue" not in view["allowed_actions"]
    assert view["enqueue_kind"] is None
    # 请求结束（撤回、释放或完成）后，enqueue 按 task.status 推断重新出现。
    db.update_request(conn, request["request_id"], NOW, status="cancelled")
    view = derive.task_detail(conn, db.require_task(conn, TASK_ID), now)
    assert "enqueue" in view["allowed_actions"]
    assert view["enqueue_kind"] == constants.ENQUEUE_INFER[status]


@pytest.mark.parametrize(
    ("status", "stage", "hold", "expected"),
    [
        ("needs_plan", "plan", False, None),
        ("ready", "dispatch", False, "dispatch"),
        # 待确认执行时以 confirm_dispatch 替代 enqueue，不存在可排队的 kind。
        ("ready", "dispatch", True, None),
        ("running", "monitor", False, "monitor"),
        ("in_review", "review", False, "review"),
        ("done", "review", False, None),
    ],
)
def test_get_task_enqueue_kind_matches_the_offered_enqueue(tmp_path, conn, status, stage, hold, expected):
    _insert_task(conn, tmp_path, status, stage)
    if hold:
        db.set_extra(conn, db.require_task(conn, TASK_ID), NOW, plan_execution_hold=True)
    ctx = SimpleNamespace(conn=conn, now_iso=NOW, now=util.parse_ts(NOW))
    task = cmd_read.cmd_get_task(ctx, {"task_id": TASK_ID})["task"]
    assert task["enqueue_kind"] == expected
    assert ("enqueue" in task["allowed_actions"]) is (expected is not None)
    if expected is not None:
        assert expected == constants.ENQUEUE_INFER[status]
