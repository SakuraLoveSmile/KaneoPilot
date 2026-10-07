"""Identity and zero-write contract on disposable SQLite/Git fixtures."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kanpilot import cmd_activity, db, util
from test_git_delivery import make_repo, branch, git, new_goal, commit


@pytest.fixture
def activity(tmp_path):
    repo = make_repo(tmp_path / "git")
    delivery = branch(repo, tmp_path / "trees")
    conn = db.connect(tmp_path / "test.db")
    now = util.now_iso()
    conn.execute("INSERT INTO tasks(task_id,project_id,project_name,repository,stage,status,run_id,extra_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 ("task1234", "project", "fixture", str(repo), "monitor", "running", "run1",
                  json.dumps({"delivery": {"mode": "pr", **delivery}}), now, now))
    conn.execute("INSERT INTO requests(request_id,idempotency_key,task_id,stage,kind,status,run_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                 ("dispatch1", "dispatch1", "task1234", "dispatch", "dispatch", "succeeded", "run1", now, now))
    conn.execute("INSERT INTO execution_rounds(task_id,run_id,backend,created_at,updated_at) VALUES (?,?,?,?,?)",
                 ("task1234", "run1", "pi_desktop", now, now))
    yield SimpleNamespace(conn=conn), delivery
    conn.close()


def test_only_count_is_returned_with_no_database_or_git_write(activity):
    ctx, delivery = activity
    wt = Path(delivery["worktree_path"])
    new_goal(wt)
    before = ctx.conn.total_changes, git(wt, "status", "--porcelain"), git(wt, "rev-parse", "HEAD")
    assert cmd_activity.cmd_execution_activity(ctx, {"task_id": "task1234", "run_id": "run1"}) == {"effective_change_count": 0, "delivery_recovered": False}
    assert before == (ctx.conn.total_changes, git(wt, "status", "--porcelain"), git(wt, "rev-parse", "HEAD"))
    (wt / "implementation.txt").write_text("work")
    assert cmd_activity.cmd_execution_activity(ctx, {"task_id": "task1234", "run_id": "run1"}) == {"effective_change_count": 1, "delivery_recovered": False}
    commit(delivery)
    assert cmd_activity.cmd_execution_activity(ctx, {"task_id": "task1234", "run_id": "run1"}) == {"effective_change_count": 1, "delivery_recovered": True}


def test_stale_execution_and_non_dispatch_identity_are_rejected(activity):
    ctx, _ = activity
    with pytest.raises(util.KanpilotError) as error:
        cmd_activity.cmd_execution_activity(ctx, {"task_id": "task1234", "run_id": "other"})
    assert error.value.code == "stale_run"
    ctx.conn.execute("UPDATE requests SET stage='plan'")
    with pytest.raises(util.KanpilotError) as error:
        cmd_activity.cmd_execution_activity(ctx, {"task_id": "task1234", "run_id": "run1"})
    assert error.value.code == "conflict"


def test_identity_change_during_io_is_rejected(activity):
    ctx, _ = activity
    def changed(*args, **kwargs):
        ctx.conn.execute("UPDATE tasks SET run_id='new-execution'")
        return {"effective_change_count": 0, "delivery_recovered": False}
    with patch.object(cmd_activity.git_delivery, "execution_activity", side_effect=changed):
        with pytest.raises(util.KanpilotError) as error:
            cmd_activity.cmd_execution_activity(ctx, {"task_id": "task1234", "run_id": "run1"})
    assert error.value.code == "stale_run"


def test_git_errors_do_not_disclose_paths(activity):
    ctx, delivery = activity
    (Path(delivery["worktree_path"]) / "implementation.txt").write_text("work")
    git(delivery["worktree_path"], "add", "implementation.txt")
    git(delivery["worktree_path"], "commit", "-m", "unrelated model commit")
    with pytest.raises(util.KanpilotError) as error:
        cmd_activity.cmd_execution_activity(ctx, {"task_id": "task1234", "run_id": "run1"})
    assert error.value.code == "observation_blocked"
    assert error.value.message == "执行改动集无法核验"
