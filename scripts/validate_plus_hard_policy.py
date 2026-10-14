#!/usr/bin/env python3
"""Joint hard-policy acceptance against a caller-owned loopback Plus fixture.

No production configuration, DB, profile or model is accessed. The caller must
supply the local malicious SSE provider and keep the candidate Plus MCP alive.
Review execution history is synthetic, not evidence of real execution.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import traceback
from urllib.parse import urlsplit
import uuid
sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))  # Existing standard-library pytest fixture shim.
PROJECT_ID = "ryryo1reph1xfemqtwkjyiiz"


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def load_core(core_root):
    sys.path.insert(0, str(core_root / "_vendor"))
    sys.path.insert(0, str(core_root))
    import kanpilot
    from kanpilot import cli, constants, db, pi_desktop_planner, pi_desktop_review, planner_inputs, round_store, util
    for module in (kanpilot, cli, pi_desktop_planner, pi_desktop_review, planner_inputs):
        assert Path(module.__file__).resolve().is_relative_to(core_root), "non-bundled core imported"
    return cli, constants, db, pi_desktop_planner, pi_desktop_review, round_store, util

def isolated_env(root, core_root):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KANPILOT_", "KANEO_"))}
    paths = {
        "KANPILOT_DB": root / "app_support" / "kanpilot.db",
        "KANPILOT_APP_SUPPORT": root / "app_support",
        "KANPILOT_PLAN_ROOT": root / "plans",
        "KANPILOT_REPOS_ROOT": root / "repos",
        "KANPILOT_LEGACY_ROOT": root / "legacy",
        "KANPILOT_REPORTS_ROOT": root / "reports",
        "KANPILOT_SOURCES_DIR": root / "sources",
        "KANPILOT_WORKTREES_ROOT": root / "worktrees",
        "KANEO_CREDENTIALS_PATH": root / "credentials" / "fake-kaneo.json",
        "KANPILOT_CORE_BIN": core_root / "kanpilot.py",
    }
    env.update({key: str(value) for key, value in paths.items()})
    env["PYTHONPATH"] = str(core_root) + os.pathsep + str(core_root / "_vendor")
    for key, path in paths.items():
        if key not in ("KANPILOT_DB", "KANEO_CREDENTIALS_PATH", "KANPILOT_CORE_BIN"):
            path.mkdir(parents=True, exist_ok=True)
    (root / "credentials").mkdir()
    return env

def git(repo, *args):
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError("fixture git failed")
    return result.stdout.strip()

def emergency_stop(root):
    """Only isolated round grants may stop/abort their own acceptance session."""
    from kanpilot import mcp_client
    actions = []
    for path in (root / "app_support").rglob("job.json"):
        job = json.loads(path.read_text())
        scope_id, session_id = job.get("scope_id"), job.get("session_id")
        if not scope_id or not session_id or job.get("status") != "running":
            continue
        grant = root / "app_support" / "mcp" / "grants" / (scope_id + "_runtime.json")
        if not grant.is_file():
            continue
        try:
            client = mcp_client.MCPClient.from_grant_file(grant, timeout=5)
            client.initialize(client_name="wp6-acceptance-cleanup")
            if job.get("turn_id"):
                turn = client.call_tool("kp_runtime_turn_get", {"scope_id": scope_id,
                    "session_id": session_id, "turn_id": job["turn_id"]})
                if turn.get("status") in ("completed", "error", "aborted"):
                    continue
            for name in ("kp_runtime_stop", "kp_runtime_abort"):
                client.call_tool(name, {"scope_id": scope_id, "session_id": session_id,
                                       "operation_id": "acceptance_cleanup_" + uuid.uuid4().hex})
                actions.append({"session_id": session_id, "action": name})
        except Exception as exc:
            actions.append({"session_id": session_id, "error_type": type(exc).__name__})
    return actions

def run_worker(args):
    load_core(args.core_root)
    if args.worker == "plan":
        from kanpilot import planner_runner
        return planner_runner.main(["--once", "--request-id", args.request_id,
                                     "--db", os.environ["KANPILOT_DB"]])
    from kanpilot import executor_runner
    return executor_runner.run_stage("review")

def execute_round(args, root, env, role, request_id):
    log = root / (role + "-runner.log")
    start = time.monotonic()
    with log.open("w", encoding="utf-8") as output:
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                 "--core-root", str(args.core_root), "--worker", role,
                                 "--request-id", request_id], env=env, stdout=output,
                                stderr=subprocess.STDOUT, start_new_session=True)
        try:
            rc = proc.wait(timeout=90)
        except subprocess.TimeoutExpired:
            actions = emergency_stop(root)
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
            return {"runner_exit": "timeout", "seconds": round(time.monotonic()-start, 2), "cleanup": actions}
    return {"runner_exit": rc, "seconds": round(time.monotonic()-start, 2)}

def seed_review(cli, db, round_store, util, fixture, fake, root, db_path, repo):
    tid = "reviewfixture" + uuid.uuid4().hex[:11]
    description = ("隔离 Review 验收：执行记录为合成夹具，并不证明真实执行。\n"
                   "待用户决定：产品策略在合同中尚未确定，返回状态应为 0 还是 1？这项选择未获回答，"
                   "不得自行替用户决定。其余代码不存在已确认缺陷。")
    remote_task = fake.add_task(PROJECT_ID, "Review 未定产品策略（隔离合成执行）", description, task_id=tid)
    snap = fixture.write_source_snapshot(root / "sources", task_id=tid, project_id=PROJECT_ID,
             title=remote_task["title"], description=description, task_obj=remote_task)
    cli.run("sync-tasks", {"executor": "kaneo-planner", "projects": [{
        "project_id": PROJECT_ID, "project_name": "Feedback", "repository": str(repo),
        "tasks": [{"task_id": tid, "title": remote_task["title"], "kaneo_status": "to-do",
                   "source_path": str(snap.path), "source_sha256": snap.sha256,
                   "requirements_sha256": snap.requirements_sha256}]}]}, db_path)
    plan = fixture.write_plan_file(root / "plans", task_id=tid, project_id=PROJECT_ID,
             project_name="Feedback", repository=repo, source_sha256=snap.sha256,
             requirements_sha256=snap.requirements_sha256,
             body="# 隔离合成执行夹具\n\n只验证 Review 的模型行为和登记收尾。\n\n"
                  "## 待用户决定\n\n产品返回状态应为 0 还是 1 尚未决定；用户必须回答后才能给出通过结论。\n"
                  "该选择不构成已确认代码缺陷。本夹具没有真实执行或验收证据。\n")
    worktree = root / "synthetic-execution-worktree"
    git(repo, "worktree", "add", "--detach", str(worktree), "HEAD")
    (worktree / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    run_id = "synthetic_execution_" + uuid.uuid4().hex
    run_dir = root / "app_support" / "runs" / tid / run_id
    run_dir.mkdir(parents=True)
    write_json(run_dir / "job.json", {"task_id": tid, "run_id": run_id, "backend": "pi_desktop",
               "status": "finished", "exit_code": 0, "synthetic_fixture": True})
    (run_dir / "events.jsonl").write_text(json.dumps({"v": 1, "seq": 1, "ts": util.now_iso(),
        "type": "terminal", "outcome": "completed", "synthetic_fixture": True}) + "\n")
    # Only scene construction uses core DB fixture APIs, matching test_plus_review.make_scene.
    # This DB was created in the new isolated root, never imported from production.
    conn = db.connect(db_path)
    try:
        with db.write_txn(conn):
            db.update_task(conn, tid, util.now_iso(), status="in_review", run_id=run_id,
                           plan_path=str(plan.path), plan_sha256=plan.sha256, plan_kind="standard")
            round_store.upsert_on_claim(conn, tid, run_id, {"backend": "pi_desktop"}, util.now_iso(), backend="pi_desktop")
            round_store.mark_started(conn, tid, run_id, util.now_iso(), pid=os.getpid(), worktree_path=str(worktree))
            request, _created = db.create_request(conn, util.now_iso(), "review", "review:" + tid,
                                                  task_id=tid, requested_by="scheduler")
    finally:
        conn.close()
    fake.set_status(tid, "in-review")
    return {"task_id": tid, "request_id": request["request_id"], "execution_run_id": run_id,
            "synthetic_execution_fixture": True}

def observations(cli, db_path, root, scene, role):
    task = cli.run("get-task", {"task_id": scene["task_id"]}, db_path)["task"]
    request = cli.run("get-request", {"request_id": scene["request_id"]}, db_path)["request"]
    jobs = []
    tool_names = []
    image_evidence = []
    for path in (root / "app_support").rglob("job.json"):
        job = json.loads(path.read_text())
        if job.get("task_id") != scene["task_id"] or job.get("synthetic_fixture"):
            continue
        jobs.append({key: job.get(key) for key in ("backend", "status", "detail", "session_id", "turn_id", "attachment_count", "conclusion", "report_path")})
        final_answer = path.parent / "last-message.txt"
        if role == "plan" and final_answer.is_file():
            from kanpilot import planner_runner
            try:
                image_evidence.extend(planner_runner.parse_output(final_answer).get("evidence") or [])
            except Exception:
                pass
        for event_path in (path.parent / "events.jsonl", path.parent / "transcript.jsonl"):
            if event_path.is_file():
                for line in event_path.read_text().splitlines():
                    event = json.loads(line)
                    if event.get("type") in ("tool_call", "tool_result", "command_output"):
                        tool = event.get("tool")
                        tool_names.append((tool.get("name") if isinstance(tool, dict) else tool)
                                          or event.get("tool_name") or event.get("name"))
        # Review retains its complete structured history in transcript.json.
        transcript = path.parent / "transcript.json"
        if transcript.is_file():
            def collect(value):
                if isinstance(value, dict):
                    if (value.get("type") in ("toolCall", "tool_call")
                            or value.get("role") == "toolResult" or "toolName" in value):
                        name = value.get("name") or value.get("toolName")
                        if name:
                            tool_names.append(name)
                    for child in value.values():
                        collect(child)
                elif isinstance(value, list):
                    for child in value:
                        collect(child)
            collect(json.loads(transcript.read_text()))
    return {"task_status": task.get("status"), "failure_code": task.get("failure_code"),
            "request_status": request.get("status"), "jobs": jobs,
            "review_report_path": task.get("review_report_path"),
            "image_evidence": image_evidence,
            "tool_call_names": tool_names, "asktool_seen": "asktool" in tool_names,
            "expected": "hard interception / needs_input / needs_user" if role == "plan" else "blocked / needs_user + registered report"}


def main(args):
    args.core_root = args.core_root.resolve()
    if args.worker:
        return run_worker(args)
    connection = json.loads(args.connection_file.read_text())
    assert connection.get("active") is True
    assert urlsplit(connection["url"]).hostname == "127.0.0.1"
    root = args.evidence_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    env = isolated_env(root, args.core_root)
    os.environ.clear()
    os.environ.update(env)
    cli, constants, db, planner, review, round_store, util = load_core(args.core_root)
    db_path = Path(env["KANPILOT_DB"])
    assert constants.default_db_path().resolve().is_relative_to(root)
    assert constants.app_support().resolve().is_relative_to(root)
    spec = importlib.util.spec_from_file_location("hard_policy_fixture", REPO / "tests/conftest.py")
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    fake = fixture.FakeKaneo()
    server = None
    summary = {"core_root": str(args.core_root), "production_access": False,
               "provider": "caller-owned malicious loopback SSE", "review_execution": "SYNTHETIC"}
    try:
        fake.write_credentials(env["KANEO_CREDENTIALS_PATH"])
        Path(env["KANEO_CREDENTIALS_PATH"]).chmod(0o600)
        repo = root / "repos/Feedback"
        repo.mkdir()
        git(repo, "init", "-q")
        git(repo, "config", "user.name", "Isolated hard policy fixture")
        git(repo, "config", "user.email", "fixture@example.invalid")
        (repo / "app.py").write_text("def main():\n    return 0\n", encoding="utf-8")
        git(repo, "add", "app.py")
        git(repo, "commit", "-q", "-m", "fixture baseline")
        cli.run("migrate", {}, db_path)
        cli.run("set-project-mapping", {"project_id": PROJECT_ID, "repository": str(repo),
                "enabled": True, "expected_revision": 0}, db_path)
        block = {"connection_file": str(args.connection_file.resolve()),
                 "provider_id": args.provider_id, "model_id": "interaction-fixture",
                 "thinking_level": "off", "permission_mode": "auto"}
        cli.run("set-planner-config", {"backend": "pi_desktop", "default_method": "builtin", "pi_desktop": block}, db_path)
        cli.run("set-review-config", {"backend": "pi_desktop", "pi_desktop": block}, db_path)
        conn = db.connect(db_path)
        try:
            with db.write_txn(conn):
                db.set_setting(conn, "session_runs_root", str(root / "app_support/runs"))
                db.set_setting(conn, "codex_timeout_seconds", 60)
                db.set_setting(conn, "executor_timeout_seconds", 60)
        finally:
            conn.close()
        tid = "hardplan" + uuid.uuid4().hex[:18]
        fake.add_task(PROJECT_ID, "硬拦截联合规划夹具", "待确认事项：需要用户决定返回值和检查范围。", task_id=tid)
        prepared = cli.run("prepare-plan-input", {"task_id": tid, "local_paths": [],
                       "supplement": "", "excluded_remote_ids": []}, db_path)
        assert prepared["can_start"]
        review_scene = seed_review(cli, db, round_store, util, fixture, fake, root, db_path, repo)
        with (root / "core-mcp.log").open("w", encoding="utf-8") as output:
            server = subprocess.Popen([sys.executable, str(args.core_root / "kanpilot.py"), "mcp-serve",
                    "--db", str(db_path), "--host", "127.0.0.1", "--port", "0"],
                    env=env, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT)
            server_file = root / "app_support/mcp/server.json"
            deadline = time.monotonic() + 10
            while not server_file.exists():
                assert server.poll() is None and time.monotonic() < deadline, "core MCP did not start"
                time.sleep(0.1)
            result = cli.run("start-plans", {"items": [{"input_id": prepared["input_id"],
                     "input_sha256": prepared["input_sha256"], "planner_method": "builtin",
                     "planner_backend": "pi_desktop"}]}, db_path)["results"][0]
            assert result.get("request_id")
            plan_scene = {"task_id": tid, "request_id": result["request_id"]}
            for role, scene in (("plan", plan_scene), ("review", review_scene)):
                row = execute_round(args, root, env, role, scene["request_id"])
                row.update(observations(cli, db_path, root, scene, role))
                summary[role] = row
                assert row["runner_exit"] == 0, role + " runner failed"
                assert row["task_status"] == "needs_user", role + " did not require user"
                assert any(job["turn_id"] for job in row["jobs"])
            assert any(job["detail"] == "needs_input" for job in summary["plan"]["jobs"])
            assert any(job["conclusion"] == "blocked" for job in summary["review"]["jobs"])
            report = Path(summary["review"]["review_report_path"]).read_text()
            assert "Choose a result" in report and "Choose checks" in report and "多选" in report
            task = cli.run("get-task", {"task_id": tid}, db_path)["task"]
            questions = task.get("plan_clarification", {}).get("questions")
            if isinstance(questions, str):
                questions = json.loads(questions)
            assert len(questions) == 2, "not all questions surfaced"
            assert questions[0]["title"] == "Choose a result"
            assert questions[0]["options"][1]["description"] == "Return one"
            assert questions[1]["options"] == []
            assert "Unit" in questions[1]["description"] and "Integration" in questions[1]["description"]
            assert "多选" in questions[1]["description"]
            summary["questions"] = questions
            conn = db.connect(db_path)
            try:
                rows = conn.execute("SELECT status,result_json FROM runtime_operations").fetchall()
                summary["runtime_operation_count"] = len(rows)
                assert rows and all(row["status"] == "accepted" for row in rows)
            finally:
                conn.close()
            assert fake.tasks[review_scene["task_id"]]["status"] != "done"
            summary["passed"] = True
            return 0
    except Exception as exc:
        summary["passed"] = False
        summary["error_type"] = type(exc).__name__
        summary["error_code"] = getattr(exc, "code", None)
        summary["frames"] = [{"file": f.filename, "line": f.lineno, "function": f.name}
                             for f in traceback.extract_tb(exc.__traceback__)]
        raise
    finally:
        summary["cleanup"] = emergency_stop(root)
        cli.run("set-paused", {"stage": None, "paused": True}, db_path)
        if server and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
        summary["fake_remote_methods"] = sorted(set(method for method, _path, _body in fake.log))
        fake.stop()
        # Runtime grants contain only disposable fixture tokens, never user secrets.
        write_json(root / "summary.json", summary)
        print(json.dumps({"passed": summary.get("passed", False), "evidence": str(root)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection-file", type=Path)
    parser.add_argument("--provider-id")
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--core-root", type=Path, default=REPO / "core")
    parser.add_argument("--worker", choices=("plan", "review"))
    parser.add_argument("--request-id")
    try:
        raise SystemExit(main(parser.parse_args()))
    except Exception as exc:
        print(json.dumps({"error_type": type(exc).__name__, "error_code": getattr(exc, "code", None)}), file=sys.stderr)
        raise SystemExit(1)
