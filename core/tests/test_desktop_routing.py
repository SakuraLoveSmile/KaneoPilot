"""Formal Desktop queue routing with isolated SQLite, no provider/Kaneo access."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace

import pytest

from kanpilot import cmd_config, cmd_exec, cmd_read, db, executor_runner, planner_runner, review_config, util


@pytest.fixture
def scene(tmp_path, monkeypatch):
    path = tmp_path / "queue.sqlite"
    conn = db.connect(path)
    now = util.now_dt()
    monkeypatch.setenv("KANPILOT_APP_SUPPORT", str(tmp_path / "support"))
    monkeypatch.setenv("KANPILOT_DB", str(path))
    ctx = SimpleNamespace(conn=conn, now=now, now_iso=util.to_iso(now))
    db.set_setting(conn, "desktop_queue_config", {"enabled": True})
    conn.commit()
    yield ctx, path
    conn.close()


def enqueue(ctx, name, role="review", backend="codex_desktop", priority=100):
    state = "needs_plan" if role == "plan" else "in_review"
    ctx.conn.execute(
        "INSERT INTO tasks(task_id,project_id,project_name,stage,status,run_id,plan_sha256,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (name, "project", "Project", role, state, "exec-" + name, "a" * 64, ctx.now_iso, ctx.now_iso),
    )
    if role == "review":
        db.set_setting(ctx.conn, "review_config", {"backend": backend, "mode": "inherit"})
    row, _ = db.create_request(ctx.conn, ctx.now_iso, role, role + ":" + name, task_id=name,
                               priority=priority,
                               payload={"planner_backend": backend, "manual_authorized": True})
    ctx.conn.commit()
    return row["request_id"]


def metadata(op="op1", token_hash="b" * 64):
    return {"operation_id": op, "token_hash": token_hash, "thread_id": "thread1", "pid": 123, "ppid": 100}


def claim(ctx, role="review", desktop=False, request_id=None, op="op1"):
    payload = {"stage": role, "executor": "desktop-test" if desktop else "legacy-test"}
    if request_id:
        payload["request_id"] = request_id
    return cmd_exec.cmd_claim(ctx, payload, desktop_claim=metadata(op) if desktop else None)


@pytest.mark.parametrize("role", ["plan", "review"])
def test_backend_filter_precedes_candidate_limit(scene, role):
    ctx, _ = scene
    for index in range(25):
        enqueue(ctx, "legacy" + str(index), role, "codex", priority=index)
    expected = enqueue(ctx, "desktop", role, priority=99)
    response = claim(ctx, role, desktop=True)
    assert response["request_id"] == expected
    assert ctx.conn.execute("SELECT COUNT(*) FROM requests WHERE status='claimed'").fetchone()[0] == 1


@pytest.mark.parametrize("role", ["plan", "review"])
def test_normal_claim_skips_desktop_and_pinned_claim_rejects_cross_backend(scene, role):
    ctx, _ = scene
    desktop = enqueue(ctx, "desktop", role, priority=1)
    normal = enqueue(ctx, "normal", role, "codex", priority=2)
    assert claim(ctx, role, request_id=desktop)["reason"] == "backend_mismatch"
    assert claim(ctx, role, desktop=True, request_id=normal)["reason"] == "backend_mismatch"
    assert claim(ctx, role)["request_id"] == normal
    assert db.get_request(ctx.conn, desktop)["status"] == "queued"


def test_concurrent_normal_and_desktop_review_never_cross_claim(scene):
    ctx, path = scene
    desktop = enqueue(ctx, "desktop", priority=1)
    normal = enqueue(ctx, "normal", backend="codex", priority=2)
    def run(is_desktop):
        conn = db.connect(path)
        try:
            local = SimpleNamespace(conn=conn, now=ctx.now, now_iso=ctx.now_iso)
            return claim(local, desktop=is_desktop)
        finally:
            conn.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        ordinary, native = list(pool.map(run, [False, True]))
    assert ordinary["request_id"] == normal
    assert native["request_id"] == desktop


def test_concurrent_same_operation_and_lost_response_replay_same_run(scene):
    ctx, path = scene
    first = enqueue(ctx, "first", priority=1)
    second = enqueue(ctx, "second", priority=2)
    def run(_):
        conn = db.connect(path)
        try:
            return claim(SimpleNamespace(conn=conn, now=ctx.now, now_iso=ctx.now_iso), desktop=True)
        finally:
            conn.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(run, [1, 2]))
    assert responses[0] == responses[1]
    assert responses[0]["request_id"] == first
    db.update_request(ctx.conn, first, ctx.now_iso, status="succeeded")
    ctx.conn.commit()
    assert claim(ctx, desktop=True) == responses[0]
    assert db.get_request(ctx.conn, second)["status"] == "queued"
    assert ctx.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1


@pytest.mark.parametrize("role,token", [("plan", "b" * 64), ("review", "c" * 64)])
def test_operation_identity_conflict_is_not_another_claim(scene, role, token):
    ctx, _ = scene
    enqueue(ctx, "first")
    enqueue(ctx, "second")
    claim(ctx, desktop=True)
    with pytest.raises(util.KanpilotError, match="identity conflict"):
        cmd_exec.cmd_claim(ctx, {"stage": role, "executor": "desktop"}, desktop_claim=metadata(token_hash=token))
    assert ctx.conn.execute("SELECT COUNT(*) FROM requests WHERE status='claimed'").fetchone()[0] == 1


def test_new_review_freezes_config_execution_plan_and_dedup_does_not_refresh(scene):
    ctx, _ = scene
    request_id = enqueue(ctx, "frozen", backend="codex")
    frozen = db.request_payload(db.get_request(ctx.conn, request_id))
    assert frozen["review_backend"] == "codex"
    assert frozen["expected_execution_run_id"] == "exec-frozen"
    assert frozen["plan_sha256"] == "a" * 64
    db.set_setting(ctx.conn, "review_config", {"backend": "codex_desktop", "legacy_config": {"backend": "pi", "mode": "inherit"}})
    db.update_task(ctx.conn, "frozen", ctx.now_iso, run_id="new-execution")
    same, made = db.create_request(ctx.conn, ctx.now_iso, "review", "review:frozen", task_id="frozen")
    assert not made and same["request_id"] == request_id
    assert db.request_payload(same) == frozen
    assert cmd_exec.request_backend(ctx.conn, same) == "codex"
    assert review_config.for_request(ctx.conn, frozen) == frozen["review_config"]


def test_legacy_review_does_not_follow_global_desktop_switch(scene):
    ctx, _ = scene
    request_id = enqueue(ctx, "legacy", backend="codex")
    db.update_request(ctx.conn, request_id, ctx.now_iso, payload_json={})
    db.set_setting(ctx.conn, "review_config", {"backend": "codex_desktop", "legacy_config": {"backend": "codex", "mode": "inherit"}})
    ctx.conn.commit()
    assert cmd_exec.request_backend(ctx.conn, db.get_request(ctx.conn, request_id)) == "codex"
    assert claim(ctx, desktop=True)["claimed"] is False
    assert claim(ctx)["request_id"] == request_id


def test_desktop_review_requires_explicit_request_backend(scene):
    ctx, _ = scene
    request_id = enqueue(ctx, "desktop")
    payload = db.request_payload(db.get_request(ctx.conn, request_id))
    del payload["review_backend"]
    db.update_request(ctx.conn, request_id, ctx.now_iso, payload_json=payload)
    ctx.conn.commit()
    assert claim(ctx, desktop=True)["claimed"] is False
    assert claim(ctx, desktop=True, request_id=request_id)["reason"] == "backend_mismatch"
    assert db.get_request(ctx.conn, request_id)["status"] == "queued"


@pytest.mark.parametrize("change", ["expired", "archive", "purged", "execution", "plan"])
def test_desktop_heartbeat_cannot_revive_invalid_claim(scene, change):
    ctx, _ = scene
    request_id = enqueue(ctx, "desktop")
    response = claim(ctx, desktop=True)
    before = db.get_request(ctx.conn, request_id)["heartbeat_at"]
    if change == "expired":
        db.update_request(ctx.conn, request_id, ctx.now_iso, lease_expires_at=util.to_iso(ctx.now - timedelta(seconds=1)))
    else:
        field, value = {"archive": ("save_state", "archived"), "purged": ("save_state", "purged"),
                        "execution": ("run_id", "new-execution"), "plan": ("plan_sha256", "c" * 64)}[change]
        db.update_task(ctx.conn, "desktop", ctx.now_iso, **{field: value})
    ctx.conn.commit()
    with pytest.raises(util.KanpilotError) as error:
        cmd_exec.cmd_heartbeat(ctx, {"request_id": request_id, "run_id": response["run_id"]})
    assert error.value.code == "stale_run"
    assert db.get_request(ctx.conn, request_id)["heartbeat_at"] == before


@pytest.mark.parametrize("role", ["plan", "review"])
def test_active_desktop_claim_can_heartbeat_and_report_after_auto_pause(scene, role):
    ctx, _ = scene
    request_id = enqueue(ctx, "desktop", role=role)
    response = claim(ctx, role, desktop=True)
    db.update_task(ctx.conn, "desktop", ctx.now_iso, auto_advance_paused=1)
    ctx.conn.commit()
    identity = {"request_id": request_id, "run_id": response["run_id"]}
    assert cmd_exec.cmd_heartbeat(ctx, identity)["ok"] is True
    assert cmd_exec.cmd_report(ctx, dict(identity, outcome="succeeded"))["request_status"] == "succeeded"


def test_disabled_desktop_queue_is_checked_atomically_but_existing_receipt_replays(scene):
    ctx, _ = scene
    first = enqueue(ctx, "first", priority=1)
    second = enqueue(ctx, "second", priority=2)
    response = claim(ctx, desktop=True)
    db.set_setting(ctx.conn, "desktop_queue_config", {"enabled": False})
    ctx.conn.commit()
    assert claim(ctx, desktop=True) == response
    assert claim(ctx, desktop=True, op="op2") == {"claimed": False, "reason": "disabled"}
    assert db.get_request(ctx.conn, first)["run_id"] == response["run_id"]
    assert db.get_request(ctx.conn, second)["status"] == "queued"


def test_plan_writer_can_use_pre_persisted_fixed_path_and_rejects_escape(tmp_path, monkeypatch):
    root = tmp_path / "plans"
    root.mkdir()
    monkeypatch.setenv("KANPILOT_PLAN_ROOT", str(root))
    context = {"task_id": "task", "task": {"title": "计划"}, "payload": {}}
    path = root / "persisted.md"
    args = (context, {}, "# 正文", util.now_iso())
    assert planner_runner.write_plan_file(*args, target_path=path) == path
    content = path.read_bytes()
    assert planner_runner.write_plan_file(*args, target_path=path) == path
    assert path.read_bytes() == content
    assert list(root.iterdir()) == [path]
    with pytest.raises(util.KanpilotError, match="outside allowed root"):
        planner_runner.write_plan_file(*args, target_path=tmp_path / "escape.md")
    assert not (tmp_path / "escape.md").exists()


def test_ordinary_legacy_heartbeat_keeps_compatible_expired_behavior(scene):
    ctx, _ = scene
    request_id = enqueue(ctx, "legacy", backend="codex")
    response = claim(ctx)
    db.update_request(ctx.conn, request_id, ctx.now_iso, lease_expires_at=util.to_iso(ctx.now - timedelta(seconds=1)))
    ctx.conn.commit()
    cmd_exec.cmd_heartbeat(ctx, {"request_id": request_id, "run_id": response["run_id"]})
    assert util.parse_ts(db.get_request(ctx.conn, request_id)["lease_expires_at"]) > ctx.now


def test_executor_status_counts_desktop_waiting_without_claimable(scene):
    ctx, _ = scene
    enqueue(ctx, "desktop-plan", role="plan")
    enqueue(ctx, "desktop-review")
    enqueue(ctx, "legacy-review", backend="codex")
    result = cmd_config.cmd_executor_status(ctx, {})
    assert result["stages"]["plan"]["claimable"] == 0
    assert result["stages"]["plan"]["desktop_waiting"] == 1
    assert result["stages"]["review"]["claimable"] == 1
    assert result["stages"]["review"]["desktop_waiting"] == 1


@pytest.mark.parametrize("global_backend", ["codex_desktop", "pi", "codex"])
def test_old_executor_never_starts_provider_when_only_desktop_review_queued(scene, monkeypatch, global_backend):
    ctx, _ = scene
    enqueue(ctx, "desktop")
    db.set_setting(ctx.conn, "review_config", {"backend": global_backend})
    ctx.conn.commit()
    monkeypatch.setattr(executor_runner.subprocess, "Popen", lambda *a, **kw: pytest.fail("Desktop queue spawned CLI provider"))
    assert executor_runner.run_stage("review") == 0
    assert db.get_request(ctx.conn, ctx.conn.execute("SELECT request_id FROM requests").fetchone()[0])["status"] == "queued"


def test_desktop_review_round_index_preserves_execution_identity(scene):
    ctx, _ = scene
    enqueue(ctx, "desktop")
    response = claim(ctx, desktop=True)
    row = ctx.conn.execute("SELECT * FROM execution_rounds WHERE task_id=? AND run_id=?", ("desktop", response["run_id"])).fetchone()
    assert row["backend"] == "codex_desktop"
    assert db.get_task(ctx.conn, "desktop")["run_id"] == "exec-desktop"
    binding = db.request_payload(db.get_request(ctx.conn, response["request_id"]))["desktop_claim"]
    assert binding == dict(metadata(), role="review", run_id=response["run_id"])


@pytest.mark.parametrize("guard", ["unauthorized", "archived", "paused", "global_pause"])
def test_pinned_desktop_plan_keeps_existing_authorization_and_pause_guards(scene, guard):
    ctx, _ = scene
    request_id = enqueue(ctx, "desktop", role="plan")
    if guard == "unauthorized":
        db.merge_request_payload(ctx.conn, request_id, ctx.now_iso, manual_authorized=False)
    elif guard == "global_pause":
        db.set_setting(ctx.conn, "paused_global", True)
    else:
        field, value = ("save_state", "archived") if guard == "archived" else ("auto_advance_paused", 1)
        db.update_task(ctx.conn, "desktop", ctx.now_iso, **{field: value})
    ctx.conn.commit()
    response = claim(ctx, "plan", desktop=True, request_id=request_id)
    assert response["claimed"] is False
    assert db.get_request(ctx.conn, request_id)["status"] == "queued"
    assert not db.get_setting(ctx.conn, "desktop_claim_receipts")


def test_desktop_review_claim_rejects_changed_frozen_identity_atomically(scene):
    ctx, _ = scene
    request_id = enqueue(ctx, "desktop")
    db.update_task(ctx.conn, "desktop", ctx.now_iso, run_id="new-execution")
    ctx.conn.commit()
    with pytest.raises(util.KanpilotError, match="identity changed"):
        claim(ctx, desktop=True)
    assert db.get_request(ctx.conn, request_id)["status"] == "queued"
    assert not db.get_setting(ctx.conn, "desktop_claim_receipts")
    assert ctx.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


def test_request_dtos_show_frozen_desktop_backend(scene):
    ctx, _ = scene
    request_id = enqueue(ctx, "desktop")
    db.set_setting(ctx.conn, "review_config", {"backend": "codex", "mode": "inherit"})
    ctx.conn.commit()
    assert cmd_read.cmd_get_request(ctx, {"request_id": request_id})["request"]["backend"] == "codex_desktop"
    assert cmd_read.cmd_list_requests(ctx, {})["requests"][0]["backend"] == "codex_desktop"


def test_executor_routes_review_by_request_snapshot_after_global_desktop_switch(scene, monkeypatch):
    from kanpilot import cli_review
    ctx, _ = scene
    request_id = enqueue(ctx, "old", backend="codex")
    frozen = db.request_payload(db.get_request(ctx.conn, request_id))["review_config"]
    db.set_setting(ctx.conn, "review_config", {"backend": "codex_desktop"})
    ctx.conn.commit()
    calls = []
    monkeypatch.setattr(cli_review, "run_stage", lambda stage, **kw: calls.append(kw) or 0)
    monkeypatch.setattr(executor_runner.subprocess, "Popen", lambda *a, **kw: pytest.fail("Wrong backend"))
    assert executor_runner.run_stage("review") == 0
    assert calls[0]["request_id"] == request_id
    assert calls[0]["config_override"] == frozen


def test_executor_resume_routes_saved_review_backend_after_settings_change(scene, monkeypatch):
    from kanpilot import cli_review
    ctx, _ = scene
    db.set_setting(ctx.conn, "review_config", {"backend": "codex_desktop"})
    db.set_setting(ctx.conn, "executor_runs", {"review": {"backend": "codex", "invocation_id": "saved"}})
    ctx.conn.commit()
    calls = []
    monkeypatch.setattr(cli_review, "run_stage", lambda stage, **kw: calls.append(kw) or 0)
    assert executor_runner.run_stage("review", resume=True) == 0
    assert calls[0]["resume"] is True
