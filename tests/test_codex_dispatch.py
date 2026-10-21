"""Real managed worker + synthetic Codex CLI, isolated Git/DB/Kaneo only."""
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

import pytest
from kanpilot import auto_monitor, codex_execution, db, pi_desktop_review

try:
    from conftest import make_due, write_plan_file
except ImportError:
    from tests.conftest import make_due, write_plan_file


VERIFY = "git diff --check"


def install_cli(env, mode):
    # This child really edits the isolated tree and runs the frozen verify;
    # only the language model's decisions and JSONL transport are simulated.
    env.codex_bin.write_text("#!" + sys.executable + "\n" + r'''
import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
if '--version' in args:
    print('codex-cli fixture'); sys.exit(0)
sys.stdin.read()
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
    item = {'id':'verify-1','type':'command_execution','command':'git diff --check','status':'in_progress'}
    emit({'type':'item.started','item':dict(item)})
    result = subprocess.run('git diff --check', shell=True, capture_output=True, text=True)
    item.update(status='completed',exit_code=result.returncode,aggregated_output=result.stdout + result.stderr)
    emit({'type':'item.completed','item':item})
    checks = [{'command':'git diff --check','exit_code':result.returncode}]
report = {'status':'needs_input' if needs else 'completed','summary':'Fixture task',
          'checks':checks,'questions':[{'title':'Choose','options':['A','B']}] if needs else []}
emit({'type':'item.completed','item':{'id':'response','type':'agent_message','text':json.dumps(report)}})
Path(args[args.index('-o')+1]).write_text(json.dumps(report))
emit({'type':'turn.completed','usage':{}})
'''.replace("INVOCATIONS", repr(str(env.root / "codex-invocations.jsonl"))).replace("MODE", repr(mode)))
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


def prepare(factory, cli, tmp_env, mode="completed"):
    install_cli(tmp_env, mode)
    cli("set-exec-config", {"backend": "codex", "codex": {"model": "gpt-6.1-sol", "reasoning_effort": "high"}})
    task = factory.sync_task(title="Codex isolated execution")
    plan = write_plan_file(tmp_env.plan_root, task_id=task.task_id, project_id=task.project_id,
        project_name=task.project_name, repository=task.repository, source_sha256=task.source_sha256,
        requirements_sha256=task.requirements_sha256,
        body="# Fixture\nImplement feature.txt.\n```verify\ngit diff --check\n```\n")
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


@pytest.mark.parametrize("mode", ["legacy", "shadow", "live"])
def test_managed_codex_dispatch_to_verified_delivery_and_review(factory, cli, tmp_env, mode):
    task, identity, evidence, started, directory = prepare(factory, cli, tmp_env)
    duplicate = cli("dispatch-start", identity)
    assert duplicate["deduplicated"] and duplicate["pid"] == started["pid"]
    confirm(cli, identity, evidence, started, directory)
    job = wait_for(lambda: finished(directory), directory)
    cli("set-project-mapping", {"project_id": task.project_id, "repository": str(task.repository),
        "enabled": True, "expected_revision": cli("get-project-mappings", {})["revision"]})
    cli("set-project-engine-mode", {"project_id": task.project_id, "engine_mode": mode})
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
