"""Detached process ownership; all processes/DBs and Plus endpoints are fake."""

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from kanpilot import cli, cmd_config, cmd_dispatch, db, pi_desktop_worker, pi_worker, process_owner, round_store, util


@pytest.fixture
def scene(tmp_path, monkeypatch):
    monkeypatch.delenv(process_owner.INSTANCE_ENV, raising=False)
    monkeypatch.setenv("KANPILOT_APP_SUPPORT", str(tmp_path / "app-support"))
    db_path = tmp_path / "state.sqlite"
    ctx = cli.Context(db_path)
    with db.write_txn(ctx.conn):
        ctx.conn.execute("INSERT INTO tasks(task_id,project_id,project_name,stage,status,run_id,created_at,updated_at) "
                         "VALUES('task','project','Test','monitor','running','run',?,?)", (ctx.now_iso, ctx.now_iso))
        round_store.upsert_on_claim(ctx.conn, "task", "run", {}, ctx.now_iso, backend="pi_desktop")
    owner = str(uuid.uuid4())
    cli.run("executor-instance", {"instance_id": owner, "action": "start"}, db_path)
    run_dir = tmp_path / "runs"
    run_dir.mkdir()
    spec = {"v": 1, "task_id": "task", "run_id": "run", "runs_dir": str(run_dir),
            "executor_instance_id": owner, "db_path": str(db_path), "instruction": "fake"}
    spec_path = run_dir / "spec.json"
    spec_path.write_text(json.dumps(spec))
    yield ctx, owner, spec, spec_path
    ctx.close()


def register(scene, **changes):
    ctx, owner, spec, path = scene
    return cli.run("executor-instance", dict(instance_id=owner, action="register-worker",
                   task_id="task", run_id="run", spec_path=str(path), **changes), ctx.db_path)


def test_real_kernel_birth_is_stable_and_precision_matched():
    birth = process_owner.process_birth(os.getpid())
    assert birth and birth == process_owner.process_birth(os.getpid())
    assert birth["started_seconds"] > 0 and 0 <= birth["started_microseconds"] < 1_000_000
    assert process_owner.process_birth(-1) is None
    assert process_owner.process_birth(True) is None


@pytest.mark.parametrize("value", [None, "bad", "", uuid.uuid4().hex, "{" + str(uuid.uuid4()) + "}", "00000000-0000-0000-0000-000000000000"])
def test_strict_uuid(value):
    with pytest.raises(util.KanpilotError) as caught:
        process_owner.validate_instance_id(value)
    assert caught.value.code == "bad_request"


def test_start_stop_idempotence_and_no_restart(scene):
    ctx, owner, _, _ = scene
    assert cli.run("executor-instance", {"instance_id": owner, "action": "start"}, ctx.db_path)["state"] == "active"
    original_task = dict(db.require_task(ctx.conn, "task"))
    for _ in range(2):
        assert cli.run("executor-instance", {"instance_id": owner, "action": "stop"}, ctx.db_path) == {
            "instance_id": owner, "state": "stopped", "owned_workers": []}
    assert dict(db.require_task(ctx.conn, "task")) == original_task
    with pytest.raises(util.KanpilotError) as caught:
        cli.run("executor-instance", {"instance_id": owner, "action": "start"}, ctx.db_path)
    assert caught.value.code == "conflict"


def test_start_captures_scheduler_parent_identity_without_rebinding(scene, monkeypatch):
    ctx, _, _, _ = scene
    owner = str(uuid.uuid4())
    birth = {"started_seconds": 100, "started_microseconds": 200}
    monkeypatch.setenv(process_owner.SCHEDULER_PID_ENV, "12345")
    monkeypatch.setattr(process_owner.os, "getppid", lambda: 12345)
    monkeypatch.setattr(process_owner, "process_birth", lambda pid: birth if pid == 12345 else None)
    cli.run("executor-instance", {"instance_id": owner, "action": "start"}, ctx.db_path)
    entry = db.get_setting(ctx.conn, process_owner.SETTING)[owner]
    assert entry["scheduler_pid"] == 12345
    assert entry["scheduler_started_seconds"] == 100
    assert entry["scheduler_started_microseconds"] == 200
    monkeypatch.setattr(process_owner.os, "getppid", lambda: 54321)
    cli.run("executor-instance", {"instance_id": owner, "action": "start"}, ctx.db_path)
    assert db.get_setting(ctx.conn, process_owner.SETTING)[owner] == entry


@pytest.mark.parametrize("parent_marker", [None, "54321"])
def test_unmarked_or_shim_parent_keeps_conservative_identity(scene, monkeypatch, parent_marker):
    ctx, _, _, _ = scene
    owner = str(uuid.uuid4())
    monkeypatch.delenv(process_owner.SCHEDULER_PID_ENV, raising=False)
    if parent_marker:
        monkeypatch.setenv(process_owner.SCHEDULER_PID_ENV, parent_marker)
    monkeypatch.setattr(process_owner.os, "getppid", lambda: 12345)
    cli.run("executor-instance", {"instance_id": owner, "action": "start"}, ctx.db_path)
    entry = db.get_setting(ctx.conn, process_owner.SETTING)[owner]
    assert "scheduler_pid" not in entry
    assert process_owner.scheduler_active(entry)


def test_unreadable_scheduler_birth_does_not_prove_exit(monkeypatch):
    entry = {"state": "active", "scheduler_pid": 12345,
             "scheduler_started_seconds": 100, "scheduler_started_microseconds": 200}
    monkeypatch.setattr(process_owner, "process_birth", lambda pid: None)
    monkeypatch.setattr(process_owner.util, "pid_alive", lambda pid: True)
    assert process_owner.scheduler_active(entry)
    monkeypatch.setattr(process_owner.util, "pid_alive", lambda pid: False)
    assert not process_owner.scheduler_active(entry)


def test_registration_uses_actual_process_and_is_idempotent(scene):
    first = register(scene)
    assert first == register(scene)
    worker = first["owned_workers"][0]
    assert worker == dict(process_owner.process_birth(os.getpid()), pid=os.getpid(), task_id="task", run_id="run",
                          spec_path=str(scene[3]))
    status = cli.run("executor-status", {"instance_id": scene[1]}, scene[0].db_path)
    assert status["owned_workers"] == [worker]
    assert "owned_workers" not in cli.run("executor-status", {}, scene[0].db_path)


def test_stopped_owner_keeps_live_registration_for_cleanup(scene):
    worker = register(scene)["owned_workers"]
    ctx, owner, _, _ = scene
    assert cli.run("executor-instance", {"instance_id": owner, "action": "stop"}, ctx.db_path)["owned_workers"] == worker
    assert cli.run("executor-status", {"instance_id": owner}, ctx.db_path)["owned_workers"] == worker
    with pytest.raises(util.KanpilotError):
        register(scene)


@pytest.mark.parametrize("existing", [False, True])
def test_missing_or_stopped_owner_rejects_registration(scene, existing):
    ctx, owner, _, _ = scene
    if existing:
        cli.run("executor-instance", {"instance_id": owner, "action": "stop"}, ctx.db_path)
    else:
        with db.write_txn(ctx.conn):
            db.set_setting(ctx.conn, process_owner.SETTING, {})
    with pytest.raises(util.KanpilotError) as caught:
        register(scene)
    assert caught.value.code == "conflict"


def test_stop_and_registration_are_serialized(scene):
    ctx, owner, _, _ = scene
    # Hold the same lock stop uses, while a worker begins its registration.
    with ThreadPoolExecutor(max_workers=1) as pool:
        with db.write_txn(ctx.conn):
            instances = db.get_setting(ctx.conn, process_owner.SETTING)
            instances[owner]["state"] = "stopped"
            db.set_setting(ctx.conn, process_owner.SETTING, instances)
            future = pool.submit(register, scene)
        with pytest.raises(util.KanpilotError) as caught:
            future.result(timeout=3)
        assert caught.value.code == "conflict"
    assert process_owner.owned_workers(ctx.conn, owner) == []


def test_pid_reuse_is_not_returned(scene, monkeypatch):
    register(scene)
    real_birth = process_owner.process_birth(os.getpid())
    monkeypatch.setattr(process_owner, "process_birth", lambda pid: dict(real_birth, started_microseconds=(real_birth["started_microseconds"] + 1) % 1_000_000))
    assert process_owner.owned_workers(scene[0].conn, scene[1]) == []


def test_cross_owner_process_cannot_be_adopted(scene):
    register(scene)
    ctx, old_owner, spec, path = scene
    other = str(uuid.uuid4())
    cli.run("executor-instance", {"instance_id": other, "action": "start"}, ctx.db_path)
    spec["executor_instance_id"] = other
    path.write_text(json.dumps(spec))
    with pytest.raises(util.KanpilotError) as caught:
        cli.run("executor-instance", {"instance_id": other, "action": "register-worker", "task_id": "task",
                                       "run_id": "run", "spec_path": str(path)}, ctx.db_path)
    assert caught.value.code == "conflict"
    assert process_owner.owned_workers(ctx.conn, other) == []
    assert len(process_owner.owned_workers(ctx.conn, old_owner)) == 1


@pytest.mark.parametrize("key,value", [("task_id", "other"), ("run_id", "other"),
    ("executor_instance_id", str(uuid.uuid4())), ("db_path", "/tmp/other-db")])
def test_spec_identity_must_match(scene, key, value):
    scene[2][key] = value
    scene[3].write_text(json.dumps(scene[2]))
    with pytest.raises(util.KanpilotError) as caught:
        register(scene)
    assert caught.value.code == "conflict"


def test_no_birth_proof_fails_closed(scene, monkeypatch):
    monkeypatch.setattr(process_owner, "process_birth", lambda pid: None)
    with pytest.raises(util.KanpilotError) as caught:
        register(scene)
    assert caught.value.code == "validation_failed"


def test_only_dead_stopped_owners_are_pruned(scene):
    ctx, owner, _, _ = scene
    register(scene)
    cli.run("executor-instance", {"instance_id": owner, "action": "stop"}, ctx.db_path)
    stopped_dead = str(uuid.uuid4())
    cli.run("executor-instance", {"instance_id": stopped_dead, "action": "stop"}, ctx.db_path)
    new_owner = str(uuid.uuid4())
    cli.run("executor-instance", {"instance_id": new_owner, "action": "start"}, ctx.db_path)
    values = db.get_setting(ctx.conn, process_owner.SETTING)
    assert owner in values and new_owner in values and stopped_dead not in values


def test_owned_spec_without_env_rejected_legacy_spec_unaffected(scene, monkeypatch):
    ctx, owner, spec, _ = scene
    assert pi_desktop_worker.run_fake(spec, "finished") == 3
    assert not (Path(spec["runs_dir"]) / "events.jsonl").exists()
    legacy = dict(spec)
    legacy.pop("executor_instance_id")
    assert pi_desktop_worker.run_fake(legacy, "finished") == 0
    assert process_owner.owned_workers(ctx.conn, owner) == []


@pytest.mark.parametrize("module,fake_env", [("pi_worker", "KANPILOT_PI_FAKE"),
                                            ("pi_desktop_worker", "KANPILOT_PI_DESKTOP_FAKE")])
def test_detached_worker_outlives_runner_and_stops_without_fake_terminal(scene, module, fake_env):
    ctx, owner, spec, path = scene
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]),
               KANPILOT_EXECUTOR_INSTANCE_ID=owner, **{fake_env: "running"})
    launch = "import subprocess,sys; p=subprocess.Popen([sys.executable,'-m','kanpilot.'+sys.argv[1],'--spec',sys.argv[2]],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True); print(p.pid)"
    runner = subprocess.run([sys.executable, "-c", launch, module, str(path)], env=env,
                            capture_output=True, text=True, timeout=5)
    assert runner.returncode == 0
    worker_pid = int(runner.stdout.strip())
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            owned = process_owner.owned_workers(ctx.conn, owner)
            if owned and (path.parent / "events.jsonl").exists():
                break
            time.sleep(0.02)
        assert owned and owned[0]["pid"] == worker_pid
        assert process_owner.process_birth(worker_pid) is not None
        events_before = (path.parent / "events.jsonl").read_bytes()
        cli.run("executor-instance", {"instance_id": owner, "action": "stop"}, ctx.db_path)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and process_owner.process_birth(worker_pid) is not None:
            time.sleep(0.02)
        assert process_owner.process_birth(worker_pid) is None
        assert process_owner.owned_workers(ctx.conn, owner) == []
        job = json.loads((path.parent / "job.json").read_text())
        assert job["status"] == "running" and job["exit_code"] is None and job["ended_at"] is None
        assert "owner_observation_stopped_at" in job
        assert (path.parent / "events.jsonl").read_bytes() == events_before
    finally:
        if process_owner.process_birth(worker_pid) is not None:
            os.kill(worker_pid, signal.SIGKILL)


@pytest.fixture
def plus_scene():
    import test_pi_desktop_backend as fixtures
    case = fixtures.PiDesktopCase(methodName="runTest")
    case.setUp()
    try:
        run_dir = case.runs_root / fixtures.TASK_ID / fixtures.RUN_ID
        plan = case.write_spec_plan("# fake execution")
        cwd = case.root / "worktree"
        cwd.mkdir()
        spec = case.make_spec(run_dir, cwd, plan)
        case.server.current_turn = fixtures.TURN_ID
        fixtures._sync(case.server)
        with db.connect(case.db_path) as conn:
            conn.execute("UPDATE tasks SET run_id=?,status='running',stage='monitor' WHERE task_id=?",
                         (fixtures.RUN_ID, fixtures.TASK_ID))
        owner = str(uuid.uuid4())
        case.call("executor-instance", {"instance_id": owner, "action": "start"})
        spec["executor_instance_id"] = owner
        path = run_dir / "spec.json"
        path.write_text(json.dumps(spec))
        yield case, owner, spec, path, fixtures
    finally:
        case.tearDown()
        case.doCleanups()


def test_stopped_owner_rejects_plus_resume_without_sending(plus_scene, monkeypatch):
    case, owner, spec, path, _ = plus_scene
    before = case.write_lost_observer_scene(spec, path.parent, status="running")
    before_events = (path.parent / "events.jsonl").read_bytes()
    monkeypatch.setenv(process_owner.INSTANCE_ENV, owner)
    case.call("executor-instance", {"instance_id": owner, "action": "stop"})
    assert pi_desktop_worker.run_real(spec, resume=True) == 3
    after = case.read_job(path.parent)
    for field in ("status", "exit_code", "ended_at", "session_id", "turn_id", "send_state"):
        assert after[field] == before[field]
    assert (path.parent / "events.jsonl").read_bytes() == before_events
    assert case.server.calls == []


def write_owned_execution_scene(case, spec, path):
    # 只为已确认执行的 v5 场景固化策略，不改变其余历史 Goal 恢复夹具。
    spec["completion_policy"] = "command_verification"
    path.write_text(json.dumps(spec))
    job = case.write_lost_observer_scene(spec, path.parent, status="running")
    job["completion_policy"] = spec["completion_policy"]
    job["plan_sha256"] = spec["plan_sha256"]
    job["goal_approval"] = {"status": "approved", "planning_turn_id": "prior-planning-turn",
                            "execution_turn_id": job["turn_id"]}
    (path.parent / "job.json").write_text(json.dumps(job))
    return job


def test_new_owner_resumes_plus_without_prompt_or_new_session(plus_scene, monkeypatch):
    case, owner, spec, path, fixtures = plus_scene
    write_owned_execution_scene(case, spec, path)
    case.call("executor-instance", {"instance_id": owner, "action": "stop"})
    new_owner = str(uuid.uuid4())
    case.call("executor-instance", {"instance_id": new_owner, "action": "start"})
    monkeypatch.setenv(process_owner.INSTANCE_ENV, new_owner)
    spec["executor_instance_id"] = new_owner
    path = path.parent / "observer-spec.json"
    path.write_text(json.dumps(spec))
    spec["_spec_path"] = str(path)
    case.server.turn_status = "completed"
    case.server.messages = case.default_messages()
    fixtures._sync(case.server)
    with case.fast_clock():
        assert pi_desktop_worker.run_real(spec, resume=True) == 0
    assert case.server.prompt_count == 0
    assert not any(name in ("pi_session_create", "pi_agent_prompt", "pi_session_configure", "pi_agent_stop", "pi_agent_abort")
                   for name, args in case.server.calls)
    assert case.call("executor-instance", {"instance_id": new_owner, "action": "status"})["owned_workers"][0]["pid"] == os.getpid()


def test_owner_closed_during_plus_observation_preserves_recoverable_scene(plus_scene, monkeypatch):
    case, owner, spec, path, fixtures = plus_scene
    before = case.write_lost_observer_scene(spec, path.parent, status="running")
    monkeypatch.setenv(process_owner.INSTANCE_ENV, owner)
    case.server.turn_status = "running"
    fixtures._sync(case.server)
    original = case.server.tool_call

    def read_then_close(name, args):
        response = original(name, args)
        if name == "pi_turn_get":
            case.call("executor-instance", {"instance_id": owner, "action": "stop"})
        return response

    case.server.tool_call = read_then_close
    fixtures._sync(case.server)
    with case.fast_clock():
        assert pi_desktop_worker.run_real(spec, resume=True) == 3
    after = case.read_job(path.parent)
    assert after["status"] == "running" and after["send_state"] == "sent"
    assert after["session_id"] == before["session_id"] and after["turn_id"] == before["turn_id"]
    assert not any(event["type"] == "terminal" for event in case.read_events(path.parent))
    assert not any(name in ("pi_agent_stop", "pi_agent_abort", "pi_agent_prompt", "pi_plans_resolve") for name, _ in case.server.calls)


def test_manual_round_observe_uses_unowned_copy_preserves_startup_spec(plus_scene, monkeypatch):
    case, owner, spec, path, fixtures = plus_scene
    monkeypatch.delenv(process_owner.INSTANCE_ENV, raising=False)
    write_owned_execution_scene(case, spec, path)
    original = path.read_bytes()
    case.call("executor-instance", {"instance_id": owner, "action": "stop"})
    case.server.turn_status = "completed"
    case.server.messages = case.default_messages()
    fixtures._sync(case.server)
    with case.env(KANPILOT_PI_DESKTOP_FAKE=None):
        result = case.call("round-observe", {"task_id": spec["task_id"], "run_id": spec["run_id"], "executor": "kaneo-monitor"})
    assert result["status"] in ("observing", "settled")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and case.read_job(path.parent)["status"] != "finished":
        time.sleep(0.05)
    assert case.read_job(path.parent)["status"] == "finished"
    assert path.read_bytes() == original
    assert "executor_instance_id" not in json.loads((path.parent / "observer-spec.json").read_text())
    assert case.server.prompt_count == 0
    assert not any(name in ("pi_session_create", "pi_agent_prompt", "pi_session_configure") for name, _ in case.server.calls)


def test_app_round_observe_registers_new_owner_and_keeps_startup_audit(plus_scene, monkeypatch):
    case, old_owner, spec, path, fixtures = plus_scene
    write_owned_execution_scene(case, spec, path)
    original = path.read_bytes()
    case.call("executor-instance", {"instance_id": old_owner, "action": "stop"})
    owner = str(uuid.uuid4())
    case.call("executor-instance", {"instance_id": owner, "action": "start"})
    monkeypatch.setenv(process_owner.INSTANCE_ENV, owner)
    case.server.turn_status = "running"
    fixtures._sync(case.server)
    worker_pid = None
    try:
        with case.env(KANPILOT_PI_DESKTOP_FAKE=None):
            result = case.call("round-observe", {"task_id": spec["task_id"], "run_id": spec["run_id"], "executor": "kaneo-monitor"})
        worker_pid = result["pid"]
        assert result["status"] == "observing"
        workers = case.call("executor-status", {"instance_id": owner})["owned_workers"]
        assert len(workers) == 1 and workers[0]["pid"] == worker_pid
        assert workers[0]["spec_path"].endswith("observer-spec.json")
        assert case.call("executor-status", {"instance_id": old_owner})["owned_workers"] == []
        assert path.read_bytes() == original
        assert json.loads((path.parent / "observer-spec.json").read_text())["executor_instance_id"] == owner
        assert case.server.prompt_count == 0
        case.call("executor-instance", {"instance_id": owner, "action": "stop"})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and process_owner.process_birth(worker_pid):
            time.sleep(0.05)
        assert process_owner.process_birth(worker_pid) is None
        assert case.read_job(path.parent)["status"] == "running"
    finally:
        if worker_pid and process_owner.process_birth(worker_pid):
            os.kill(worker_pid, signal.SIGKILL)


def test_owner_closed_before_goal_approval_does_not_approve_or_stop_plus(plus_scene, monkeypatch):
    case, owner, spec, path, fixtures = plus_scene
    monkeypatch.setenv(process_owner.INSTANCE_ENV, owner)
    case.server.turn_status = "completed"
    case.server.messages = case.default_messages()
    case.server.planning_state = "awaiting_approval"
    case.server.plans = [fixtures.TestRunReal._goal_contract(case)]
    original = case.server.tool_call

    def pending_then_close(name, args):
        response = original(name, args)
        if name == "pi_plans_pending":
            case.call("executor-instance", {"instance_id": owner, "action": "stop"})
        return response

    case.server.tool_call = pending_then_close
    fixtures._sync(case.server)
    with case.fast_clock():
        assert pi_desktop_worker.run_real(spec) == 3
    assert case.server.plans_resolve_calls == []
    assert case.server.stop_calls == case.server.abort_calls == 0
    job = case.read_job(path.parent)
    assert job["status"] == "running" and job["send_state"] == "sent"
    assert not any(event["type"] == "terminal" for event in case.read_events(path.parent))


def test_parent_registers_before_worker_bootstrap_then_worker_rechecks_stop(scene):
    ctx, owner, spec, path = scene
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]),
               KANPILOT_EXECUTOR_INSTANCE_ID=owner, KANPILOT_PI_DESKTOP_FAKE="finished")
    bootstrap = "import time,sys; time.sleep(0.3); from kanpilot import pi_desktop_worker; raise SystemExit(pi_desktop_worker.main(['--spec',sys.argv[1]]))"
    proc = subprocess.Popen([sys.executable, "-c", bootstrap, str(path)], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        cmd_dispatch._register_started_worker(ctx, spec, proc, path)
        assert process_owner.owned_workers(ctx.conn, owner)[0]["pid"] == proc.pid
        cli.run("executor-instance", {"instance_id": owner, "action": "stop"}, ctx.db_path)
        assert proc.wait(timeout=5) == 3
        assert not (path.parent / "events.jsonl").exists()
        assert not (path.parent / "job.json").exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=3)


def test_app_closes_between_spawn_and_parent_registration_child_is_reaped(scene):
    ctx, owner, spec, path = scene
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        cli.run("executor-instance", {"instance_id": owner, "action": "stop"}, ctx.db_path)
        with pytest.raises(process_owner.OwnerStoppedError):
            cmd_dispatch._register_started_worker(ctx, spec, proc, path)
        assert proc.poll() is not None
        assert process_owner.owned_workers(ctx.conn, owner) == []
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=3)


def test_live_child_without_birth_proof_is_reaped(scene, monkeypatch):
    ctx, owner, spec, path = scene
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        monkeypatch.setattr(process_owner, "process_birth", lambda pid: None)
        with pytest.raises(util.KanpilotError) as caught:
            cmd_dispatch._register_started_worker(ctx, spec, proc, path)
        assert caught.value.code == "validation_failed"
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=3)


def test_naturally_finished_child_does_not_create_false_live_registration(scene):
    ctx, owner, spec, path = scene
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=3)
    cmd_dispatch._register_started_worker(ctx, spec, proc, path)
    assert process_owner.owned_workers(ctx.conn, owner) == []


def test_pi_owned_shutdown_reaps_term_ignoring_local_cli_before_app_escalation(scene, monkeypatch):
    ctx, owner, spec, path = scene
    fake_pi = path.parent / "fake-pi"
    fake_pi.write_text("#!" + sys.executable + "\n" +
        "import json,os,signal,time\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "open(os.environ['FAKE_PI_PID_FILE'],'w').write(str(os.getpid()))\n"
        "print(json.dumps({'type':'session','id':'fake-session'}),flush=True)\n"
        "while True: time.sleep(0.1)\n")
    fake_pi.chmod(0o755)
    pid_path = path.parent / "pi.pid"
    spec.update(cwd=str(path.parent), config={}, pi_home=str(path.parent), timeout_seconds=30)
    path.write_text(json.dumps(spec))
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]),
               KANPILOT_EXECUTOR_INSTANCE_ID=owner, KANPILOT_PI_BIN=str(fake_pi), FAKE_PI_PID_FILE=str(pid_path))
    env.pop("KANPILOT_PI_FAKE", None)
    proc = subprocess.Popen([sys.executable, "-m", "kanpilot.pi_worker", "--spec", str(path)], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    pi_pid = None
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not pid_path.exists():
            time.sleep(0.02)
        assert pid_path.exists()
        pi_pid = int(pid_path.read_text())
        cli.run("executor-instance", {"instance_id": owner, "action": "stop"}, ctx.db_path)
        assert proc.wait(timeout=2) == 3
        assert process_owner.process_birth(pi_pid) is None
        job = json.loads((path.parent / "job.json").read_text())
        assert job["status"] == "running" and job["exit_code"] is None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=3)
        if pi_pid and process_owner.process_birth(pi_pid):
            os.kill(pi_pid, signal.SIGKILL)


def test_registered_worker_alive_requires_exact_birth(scene, monkeypatch):
    ctx, owner, _, _ = scene
    assert process_owner.registered_worker_alive(ctx.conn, "task", "run", os.getpid()) is None
    register(scene)
    assert process_owner.registered_worker_alive(ctx.conn, "task", "run", os.getpid()) is True
    assert process_owner.registered_worker_alive(ctx.conn, "task", "other", os.getpid()) is None
    monkeypatch.setattr(process_owner, "process_birth", lambda pid: None)
    assert process_owner.registered_worker_alive(ctx.conn, "task", "run", os.getpid()) is False


def test_live_owner_registration_deduplicates_observer_without_remote_calls(plus_scene):
    case, owner, spec, path, _ = plus_scene
    job = case.write_lost_observer_scene(spec, path.parent, status="running")
    job["pid"] = os.getpid()
    (path.parent / "job.json").write_text(json.dumps(job))
    case.call("executor-instance", {"action": "register-worker", "instance_id": owner,
        "task_id": spec["task_id"], "run_id": spec["run_id"], "spec_path": str(path)})
    with db.connect(case.db_path) as conn:
        conn.execute("UPDATE execution_rounds SET pid=? WHERE task_id=? AND run_id=?",
                     (os.getpid(), spec["task_id"], spec["run_id"]))
    result = case.call("round-observe", {"task_id": spec["task_id"], "run_id": spec["run_id"]})
    assert result == {"status": "observing", "pid": os.getpid(), "deduplicated": True}
    assert case.server.calls == []


@pytest.mark.parametrize("pruned", [False, True])
def test_reused_pid_or_pruned_owner_record_does_not_block_read_only_resume(plus_scene, monkeypatch, pruned):
    case, owner, spec, path, fixtures = plus_scene
    monkeypatch.delenv(process_owner.INSTANCE_ENV, raising=False)
    job = write_owned_execution_scene(case, spec, path)
    job["pid"] = os.getpid()
    (path.parent / "job.json").write_text(json.dumps(job))
    case.call("executor-instance", {"action": "register-worker", "instance_id": owner,
        "task_id": spec["task_id"], "run_id": spec["run_id"], "spec_path": str(path)})
    with db.connect(case.db_path) as conn:
        conn.execute("UPDATE execution_rounds SET pid=? WHERE task_id=? AND run_id=?",
                     (os.getpid(), spec["task_id"], spec["run_id"]))
        instances = db.get_setting(conn, process_owner.SETTING)
        if pruned:
            instances.pop(owner)
        else:
            instances[owner]["workers"][0]["started_microseconds"] += 1
        db.set_setting(conn, process_owner.SETTING, instances)
    case.server.turn_status = "completed"
    case.server.messages = case.default_messages()
    fixtures._sync(case.server)
    with case.env(KANPILOT_PI_DESKTOP_FAKE=None):
        result = case.call("round-observe", {"task_id": spec["task_id"], "run_id": spec["run_id"]})
    assert result["status"] in ("observing", "settled") and result["pid"] != os.getpid()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and case.read_job(path.parent)["status"] != "finished":
        time.sleep(0.05)
    assert case.read_job(path.parent)["status"] == "finished"
    assert case.server.prompt_count == 0 and case.server.plans_resolve_calls == []


def test_current_manual_observer_copy_retains_legacy_dedup(plus_scene):
    case, owner, spec, path, _ = plus_scene
    job = case.write_lost_observer_scene(spec, path.parent, status="running")
    job["observe_pid"] = os.getpid()
    (path.parent / "job.json").write_text(json.dumps(job))
    manual = dict(spec)
    manual.pop("executor_instance_id")
    (path.parent / "observer-spec.json").write_text(json.dumps(manual))
    result = case.call("round-observe", {"task_id": spec["task_id"], "run_id": spec["run_id"]})
    assert result == {"status": "observing", "pid": os.getpid(), "deduplicated": True}
    assert case.server.calls == []


def test_resume_waiting_goal_is_deferred_without_approval_or_terminal(plus_scene, monkeypatch):
    case, owner, spec, path, fixtures = plus_scene
    monkeypatch.setenv(process_owner.INSTANCE_ENV, owner)
    before = case.write_lost_observer_scene(spec, path.parent, status="running")
    case.server.turn_status = "completed"
    case.server.messages = case.default_messages()
    case.server.planning_state = "awaiting_approval"
    case.server.plans = [fixtures.TestRunReal._goal_contract(case)]
    fixtures._sync(case.server)
    with case.fast_clock():
        assert pi_desktop_worker.run_real(spec, resume=True) == 3
    after = case.read_job(path.parent)
    assert after["status"] == before["status"] and after["session_id"] == before["session_id"]
    assert "goal_approval_observation_pending" in after["resume_detail"]
    assert case.server.plans_resolve_calls == [] and case.server.prompt_count == 0
    assert not any(event["type"] == "terminal" for event in case.read_events(path.parent))


def prepare_approved_goal_recovery(plus_scene, monkeypatch, fault=None):
    case, owner, spec, path, fixtures = plus_scene
    monkeypatch.setenv(process_owner.INSTANCE_ENV, owner)
    before = case.write_lost_observer_scene(spec, path.parent, status="running")
    candidate = "verified-execution-turn"
    case.server.current_turn = candidate
    case.server.turn_status = "completed"
    case.server.planning_state = "inactive"
    case.server.messages = case.default_messages()
    case.server.messages[0]["content"] = case.expected_prompt(spec)
    approval = fixtures.TestRunReal._goal_contract(case, status="approved")
    approval["executionId"] = "opaque-goal-execution-id-not-a-turn"
    if fault == "wrong_session":
        approval["sessionId"] = "other-session"
    elif fault == "wrong_planning_turn":
        approval["turnId"] = "other-planning-turn"
    elif fault == "not_approved":
        approval["status"] = "pending"
    elif fault == "extra_user":
        case.server.messages.append({"id": "extra-user", "role": "user", "content": "new instruction"})
    elif fault == "wrong_anchor":
        case.server.messages[0]["content"] = "other task's prompt"
    elif fault == "same_planning_turn":
        case.server.current_turn = before["turn_id"]
    elif fault == "truncated":
        case.server.before_broken = True
        case.server.page_size = 2
    original = case.server.tool_call

    def with_history(name, args):
        response = original(name, args)
        if name == "pi_plans_pending":
            response["structuredContent"]["history"] = [] if fault == "missing_history" else [approval]
            if fault == "ambiguous_history":
                response["structuredContent"]["history"].append(dict(approval, id="other-goal"))
        elif name == "pi_turn_get" and args.get("turnId") == candidate and fault == "wrong_execution_identity":
            response["structuredContent"]["sessionId"] = "other-session"
        elif name == "pi_agent_status" and fault == "missing_current_turn":
            response["structuredContent"]["status"].pop("currentTurnId", None)
        elif name == "pi_session_get" and fault == "truncated":
            response["structuredContent"]["session"]["hasMoreAfter"] = True
        return response

    case.server.tool_call = with_history
    fixtures._sync(case.server)
    return case, spec, path, before, approval, candidate


def test_resume_recovers_approved_goal_from_strict_read_only_history(plus_scene, monkeypatch):
    case, spec, path, before, approval, candidate = prepare_approved_goal_recovery(plus_scene, monkeypatch)
    with case.fast_clock():
        assert pi_desktop_worker.run_real(spec, resume=True) == 0
    job = case.read_job(path.parent)
    assert job["status"] == "finished" and job["turn_id"] == candidate
    assert job["goal_approval"]["planning_turn_id"] == before["turn_id"]
    assert job["goal_approval"]["execution_turn_id"] == candidate
    assert job["goal_approval"]["execution_turn_id"] != approval["executionId"]
    assert job["goal_approval"]["approved_by"] == "plus_history"
    assert case.server.prompt_count == 0 and case.server.plans_resolve_calls == []
    assert case.server.stop_calls == case.server.abort_calls == 0
    assert len([event for event in case.read_events(path.parent) if event["type"] == "session"]) == 1


@pytest.mark.parametrize("fault", ["missing_history", "ambiguous_history", "wrong_session", "wrong_planning_turn",
    "not_approved", "extra_user", "wrong_anchor", "truncated", "wrong_execution_identity", "missing_current_turn", "same_planning_turn"])
def test_resume_missing_goal_recovery_proof_defers_without_wrong_terminal(plus_scene, monkeypatch, fault):
    case, spec, path, before, _, _ = prepare_approved_goal_recovery(plus_scene, monkeypatch, fault)
    with case.fast_clock():
        assert pi_desktop_worker.run_real(spec, resume=True) == 3
    job = case.read_job(path.parent)
    assert job["status"] == "running" and job["turn_id"] == before["turn_id"]
    assert job["session_id"] == before["session_id"] and job["send_state"] == before["send_state"]
    assert "goal_resume_unconfirmed" in job["resume_detail"]
    assert not any(event["type"] == "terminal" for event in case.read_events(path.parent))
    assert case.server.prompt_count == 0 and case.server.plans_resolve_calls == []
    assert case.server.stop_calls == case.server.abort_calls == 0


def test_deferred_goal_recovery_is_not_misreported_as_settled(tmp_path):
    job = {"observation_resumed_at": "new-resume", "resume_deferred_at": "new-defer",
           "resume_detail": "goal_resume_unconfirmed"}
    (tmp_path / "job.json").write_text(json.dumps(job))

    class ExitedObserver:
        def poll(self):
            return 3

    assert cmd_dispatch._wait_observe_attach(tmp_path, ExitedObserver(), {}) == (
        "deferred", "goal_resume_unconfirmed")


@pytest.mark.parametrize("manual_from_owned", [False, True])
def test_owned_origin_without_execution_checkpoint_defers_same_planning_turn(plus_scene, monkeypatch, manual_from_owned):
    case, owner, spec, path, fixtures = plus_scene
    case.write_lost_observer_scene(spec, path.parent, status="running")
    case.server.turn_status = "completed"
    case.server.current_turn = fixtures.TURN_ID
    case.server.messages = case.default_messages()
    fixtures._sync(case.server)
    if manual_from_owned:
        monkeypatch.delenv(process_owner.INSTANCE_ENV, raising=False)
        spec["original_executor_instance_id"] = spec.pop("executor_instance_id")
        spec["requires_goal_identity"] = True
    else:
        monkeypatch.setenv(process_owner.INSTANCE_ENV, owner)
    with case.fast_clock():
        assert pi_desktop_worker.run_real(spec, resume=True) == 3
    job = case.read_job(path.parent)
    assert job["status"] == "running" and job["turn_id"] == fixtures.TURN_ID
    assert "goal_resume_unconfirmed" in job["resume_detail"]
    assert not any(event["type"] == "terminal" for event in case.read_events(path.parent))
    assert case.server.prompt_count == 0 and case.server.plans_resolve_calls == []


def test_manual_observer_copy_retains_owned_goal_origin(plus_scene, monkeypatch):
    case, owner, spec, path, fixtures = plus_scene
    monkeypatch.delenv(process_owner.INSTANCE_ENV, raising=False)
    case.write_lost_observer_scene(spec, path.parent, status="running")
    case.call("executor-instance", {"instance_id": owner, "action": "stop"})
    case.server.turn_status = "completed"
    case.server.current_turn = fixtures.TURN_ID
    case.server.messages = case.default_messages()
    fixtures._sync(case.server)
    with case.env(KANPILOT_PI_DESKTOP_FAKE=None):
        result = case.call("round-observe", {"task_id": spec["task_id"], "run_id": spec["run_id"]})
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not case.read_job(path.parent).get("resume_deferred_at"):
        time.sleep(0.05)
    observer_spec = json.loads((path.parent / "observer-spec.json").read_text())
    assert "executor_instance_id" not in observer_spec
    assert observer_spec["original_executor_instance_id"] == owner and observer_spec["requires_goal_identity"] is True
    assert case.read_job(path.parent)["status"] == "running"
    assert not any(event["type"] == "terminal" for event in case.read_events(path.parent))
    assert case.server.prompt_count == 0 and case.server.plans_resolve_calls == []
