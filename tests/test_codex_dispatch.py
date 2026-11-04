"""Real managed worker + synthetic Codex CLI, isolated Git/DB/Kaneo only."""
import json
from pathlib import Path
import shlex
import sys
import time
from unittest.mock import patch

import pytest
from kanpilot import auto_monitor, codex_execution, db, pi_desktop_review
from kanpilot.engine import loop

try:
    from conftest import make_due, write_plan_file
except ImportError:
    from tests.conftest import make_due, write_plan_file


VERIFY = "git diff --check"


def install_cli(env, mode, verification=VERIFY):
    # This child really edits the isolated tree and runs the frozen verify;
    # only the language model's decisions and JSONL transport are simulated.
    env.codex_bin.write_text("#!" + sys.executable + "\n" + r'''
import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
if '--version' in args:
    print('codex-cli fixture'); sys.exit(0)
prompt = sys.stdin.read()
Path(args[args.index('-o')+1] + '.prompt').write_text(prompt)
with Path(INVOCATIONS).open('a') as stream:
    stream.write(json.dumps(args) + '\n')
def emit(event): print(json.dumps(event), flush=True)
emit({'type':'thread.started','thread_id':'fixture-thread'})
emit({'type':'turn.started'})
needs = MODE == 'questions' and 'resume' not in args
checks = []
if not needs:
    Path('feature.txt').write_text('implemented\n')
    emit({'type':'item.completed','item':{'id':'edit-1','type':'file_change','status':'completed','changes':[]}})
    item = {'id':'verify-1','type':'command_execution','command':VERIFY_COMMAND,'status':'in_progress'}
    emit({'type':'item.started','item':dict(item)})
    result = subprocess.run(VERIFY_COMMAND, shell=True, capture_output=True, text=True)
    item.update(status='completed',exit_code=result.returncode,aggregated_output=result.stdout + result.stderr)
    emit({'type':'item.completed','item':item})
    checks = [{'command':VERIFY_COMMAND,'exit_code':result.returncode}]
report = {'status':'needs_input' if needs else ('failed' if MODE == 'failed' else 'completed'),'summary':'Fixture task',
          'checks':checks,'questions':[{'title':'Choose','options':['A','B']}] if needs else []}
emit({'type':'item.completed','item':{'id':'response','type':'agent_message','text':json.dumps(report)}})
Path(args[args.index('-o')+1]).write_text(json.dumps(report))
emit({'type':'turn.completed','usage':{}})
'''.replace("INVOCATIONS", repr(str(env.root / "codex-invocations.jsonl"))).replace("MODE", repr(mode)).replace("VERIFY_COMMAND", repr(verification)))
    env.codex_bin.chmod(0o755)


def wait_for(predicate, directory, seconds=12):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.05)
    log = directory / "worker.log"
    raise AssertionError("Worker did not reach expected state: " + (log.read_text() if log.exists() else "no log"))


def prepare(factory, cli, tmp_env, mode="completed", verification=VERIFY):
    install_cli(tmp_env, mode, verification)
    cli("set-exec-config", {"backend": "codex", "codex": {"model": "gpt-6.1-sol", "reasoning_effort": "high"}})
    task = factory.sync_task(title="Codex isolated execution")
    plan = write_plan_file(tmp_env.plan_root, task_id=task.task_id, project_id=task.project_id,
        project_name=task.project_name, repository=task.repository, source_sha256=task.source_sha256,
        requirements_sha256=task.requirements_sha256,
        body="# Fixture\nImplement feature.txt.\n```verify\n" + verification + "\n```\n")
    factory.publish(task, plan_file=plan)
    factory.enqueue_dispatch(task.task_id)
    request = factory.find_request(task.task_id, "dispatch")
    claim = factory.claim("dispatch", request_id=request["request_id"])
    assert claim["claimed"], claim
    ctx = claim["context"]
    evidence = {"backend": "codex", "config": ctx["exec_config"],
                "session_marker": ctx["session_marker"], "repository": ctx["repository"]}
    identity = {"task_id": task.task_id, "run_id": claim["run_id"]}
    cli("dispatch-prepare", {**identity, "evidence": evidence})
    cli("dispatch-begin-send", identity)
    started = cli("dispatch-start", identity)
    assert started["status"] == "started", started
    directory = Path(cli("overview", {})["paths"]["session_runs_root"]) / task.task_id / claim["run_id"]
    return task, identity, evidence, started, directory


def confirm(cli, identity, evidence, started, directory):
    job = json.loads((directory / "job.json").read_text())
    result = cli("dispatch-confirm", {**identity, "evidence": {**evidence,
        "session_id": started["session_id"], "turn_id": job["turn_id"], "protocol_seen": True}})
    assert result["status"] == "running"


def finished(directory):
    job = json.loads((directory / "job.json").read_text())
    if job["status"] in ("failed", "aborted"):
        raise AssertionError(job)
    return job if job["status"] == "finished" and not codex_execution.job_alive(job) else None


def test_dispatch_freezes_and_passes_only_flutter_cache_to_native_worker(factory, cli, tmp_env):
    sdk = tmp_env.root / 'Flutter SDK'
    (sdk / 'bin/cache').mkdir(parents=True)
    (sdk / 'bin/internal').mkdir()
    (sdk / 'bin/internal/shared.sh').write_text('# fixture\n')
    binary = sdk / 'bin/flutter'
    binary.write_text('#!' + sys.executable + '\nfrom pathlib import Path\n'
                      'Path(__file__).parent.joinpath("cache/engine.realm").write_text("fixture")\n')
    binary.chmod(0o755)
    verification = 'LANG=en_US.UTF-8 ' + shlex.quote(str(binary)) + ' test --no-pub'
    task, identity, evidence, started, directory = prepare(factory, cli, tmp_env, verification=verification)
    confirm(cli, identity, evidence, started, directory)
    wait_for(lambda: finished(directory), directory)
    spec = json.loads((directory / 'spec.json').read_text())
    roots = [str((sdk / 'bin/cache').resolve())]
    assert spec['sandbox_writable_roots'] == roots
    assert spec['stage_contract']['inputs']['sandbox_writable_roots'] == roots
    invocations = [json.loads(line) for line in (tmp_env.root / 'codex-invocations.jsonl').read_text().splitlines()]
    assert len(invocations) == 1
    argv = invocations[0]
    assert argv.count('--add-dir') == 1
    assert argv[argv.index('--add-dir') + 1] == roots[0]
    assert 'sandbox_mode="workspace-write"' in argv
    assert 'approval_policy="never"' in argv
    assert (sdk / 'bin/cache/engine.realm').read_text() == 'fixture'
    report = json.loads((directory / 'codex-final-1.json').read_text())
    assert report['checks'] == [{'command': verification, 'exit_code': 0}]


def test_failed_codex_worker_moves_task_to_attention_without_resending(factory, cli, tmp_env):
    verification = shlex.quote(sys.executable) + ' -c ' + shlex.quote('raise SystemExit(1)')
    task, identity, evidence, started, directory = prepare(factory, cli, tmp_env,
        mode='failed', verification=verification)
    confirm(cli, identity, evidence, started, directory)
    def failed():
        job = json.loads((directory / 'job.json').read_text())
        return job if job['status'] == 'failed' and not codex_execution.job_alive(job) else None
    job = wait_for(failed, directory)
    assert job['exit_code'] == 3
    assert job['detail'] == 'Codex 报告执行失败'
    assert cli('round-observe', identity)['status'] == 'failed'
    cli('set-project-mapping', {'project_id': task.project_id, 'repository': str(task.repository),
        'enabled': True, 'expected_revision': cli('get-project-mappings', {})['revision']})
    monitor = factory.find_request(task.task_id, 'monitor')
    make_due(tmp_env.db, monitor['request_id'])
    rc = auto_monitor.run_stage('monitor', conn=None, db_path=tmp_env.db, timeout=180,
        log_path=tmp_env.root / 'monitor.jsonl', record_run=lambda **kwargs: None)
    detail = factory.get_task(task.task_id)
    assert rc == 0 and detail['status'] == 'needs_user', detail
    assert detail['failure_code'] == 'execution_failed'
    assert 'Codex 报告执行失败' in detail['failure_reason']
    assert (Path(started['worktree_path']) / 'feature.txt').read_text() == 'implemented\n'
    assert len((tmp_env.root / 'codex-invocations.jsonl').read_text().splitlines()) == 1


@pytest.mark.parametrize("mode", ["legacy", "shadow", "live"])
def test_managed_codex_dispatch_to_verified_delivery_and_review(factory, cli, tmp_env, mode):
    task, identity, evidence, started, directory = prepare(factory, cli, tmp_env)
    spec = json.loads((directory / 'spec.json').read_text())
    assert spec['sandbox_writable_roots'] == []
    assert spec['stage_contract']['inputs']['sandbox_writable_roots'] == []
    assert 'SubmitGoalReport' not in spec['instruction']
    assert 'exit_code' in spec['instruction']
    prompt = (directory / 'codex-final-1.json.prompt').read_text()
    assert 'SubmitGoalReport' not in prompt
    assert spec['instruction'] in prompt
    duplicate = cli("dispatch-start", identity)
    assert duplicate["deduplicated"] and duplicate["pid"] == started["pid"]
    confirm(cli, identity, evidence, started, directory)
    job = wait_for(lambda: finished(directory), directory)
    cli("set-project-mapping", {"project_id": task.project_id, "repository": str(task.repository),
        "enabled": True, "expected_revision": cli("get-project-mappings", {})["revision"]})
    # Seed a pre-upgrade mapping: the retired modes must receive the same
    # strict report checks and delivery behavior without invoking their setter.
    conn = db.connect(tmp_env.db)
    try:
        with db.write_txn(conn):
            mappings = db.get_setting(conn, "project_mappings")
            mappings["items"][task.project_id]["engine_mode"] = mode
            db.set_setting(conn, "project_mappings", mappings)
    finally:
        conn.close()
    mappings = cli("get-project-mappings", {})
    assert next(item for item in mappings["items"] if item["project_id"] == task.project_id)["engine_mode"] == "live"
    # Starting the new engine migrates saved modes without changing this
    # already-sent execution. It must still finish from its one original job.
    conn = db.connect(tmp_env.db)
    try:
        before_task = dict(db.require_task(conn, task.task_id))
        before_requests = [dict(row) for row in conn.execute(
            "SELECT * FROM requests WHERE task_id=? ORDER BY request_id", (task.task_id,))]
    finally:
        conn.close()
    loop.Reconciler(tmp_env.db).run(max_ticks=0, instance_id="upgrade-" + mode)
    revision = cli("get-project-mappings", {})["revision"]
    loop.Reconciler(tmp_env.db).run(max_ticks=0, instance_id="upgrade-again-" + mode)
    assert cli("get-project-mappings", {})["revision"] == revision
    conn = db.connect(tmp_env.db)
    try:
        assert db.get_setting(conn, "project_mappings")["items"][task.project_id]["engine_mode"] == "live"
        assert dict(db.require_task(conn, task.task_id)) == before_task
        assert [dict(row) for row in conn.execute(
            "SELECT * FROM requests WHERE task_id=? ORDER BY request_id", (task.task_id,))] == before_requests
    finally:
        conn.close()
    assert job["verification_evidence_version"] == 1
    refused = cli("advance-task", {"task_id": task.task_id, "target": "in-review", "expected_current": "in-progress"}, expect=1)
    assert refused["code"] == "conflict" and "核心判定" in refused["error"]
    assert factory.kaneo.tasks[task.task_id]["status"] == "in-progress"
    observed = cli("round-observe", identity)
    assert observed["status"] == "finished"
    # A live PID with a different birth belongs to a new process, not this run.
    conn = db.connect(tmp_env.db)
    try:
        original_birth = codex_execution.process_owner.process_birth
        def reused_birth(pid):
            return {"started_seconds": job["worker_birth"]["started_seconds"] + 1, "started_microseconds": 0} if pid == job["pid"] else original_birth(pid)
        with patch.object(codex_execution.process_owner, "process_birth", side_effect=reused_birth):
            assert codex_execution.read_evidence(conn, db.require_task(conn, task.task_id))["coverage"]["ok"]
    finally:
        conn.close()
    monitor = factory.find_request(task.task_id, "monitor")
    make_due(tmp_env.db, monitor["request_id"])
    rc = auto_monitor.run_stage("monitor", conn=None, db_path=tmp_env.db, timeout=180,
        log_path=tmp_env.root / "monitor.jsonl", record_run=lambda **kwargs: None)
    detail = factory.get_task(task.task_id)
    assert rc == 0 and detail["status"] == "in_review", detail
    assert detail["extra"]["engine_verdict"]["evidence_source"] == "codex_report"
    assert detail["delivery"]["commits"][-1]["execution_run_id"] == identity["run_id"]
    assert factory.find_request(task.task_id, "review")["status"] == "queued"
    # The review input includes the exact admitted report and native command log.
    conn = db.connect(tmp_env.db)
    try:
        materials = pi_desktop_review.collect_materials(db_path=tmp_env.db, conn=conn,
            materials=tmp_env.root / "review-materials", task=dict(db.require_task(conn, task.task_id)),
            execution_worktree=Path(started["worktree_path"]), execution_run_id=identity["run_id"])
    finally:
        conn.close()
    assert "execution-codex-report.json" in materials["files"]
    assert "execution-codex-turn-1.jsonl" in materials["files"]
    assert len((tmp_env.root / "codex-invocations.jsonl").read_text().splitlines()) == 1


def test_fast_question_before_confirm_waits_and_resumes_exactly_once(factory, cli, tmp_env):
    task, identity, evidence, started, directory = prepare(factory, cli, tmp_env, "questions")
    wait_for(lambda: json.loads((directory / "job.json").read_text()).get("pending_input"), directory)
    assert factory.get_task(task.task_id)["status"] == "submitting"
    confirm(cli, identity, evidence, started, directory)
    q = wait_for(lambda: factory.get_task(task.task_id).get("execution_question"), directory)
    before = (tmp_env.root / "codex-invocations.jsonl").read_bytes()
    assert cli("round-observe", identity)["status"] == "observing"
    assert (tmp_env.root / "codex-invocations.jsonl").read_bytes() == before
    answers = [{"question_id": item["id"], "option_ids": [item["options"][0]["id"]], "text": "", "skipped": False} for item in q["questions"]]
    answer = {**identity, "request_id": q["request_id"], "questions_sha256": q["questions_sha256"],
              "operation_id": "answer-once", "answers": answers}
    cli("submit-execution-answers", answer)
    cli("submit-execution-answers", answer)
    job = wait_for(lambda: finished(directory), directory)
    assert len(job["turns"]) == 2
    assert job["turns"][0]["answer_operation_id"] == "answer-once"
    assert len((tmp_env.root / "codex-invocations.jsonl").read_text().splitlines()) == 2
    result = cli("reconcile-execution-question", identity)
    assert result["execution_question"]["state"] == "answered"
    conn = db.connect(tmp_env.db)
    try:
        assert codex_execution.read_evidence(conn, db.require_task(conn, task.task_id))["coverage"]["ok"]
    finally:
        conn.close()
