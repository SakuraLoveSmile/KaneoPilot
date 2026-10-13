#!/usr/bin/env python3
"""Isolated real-HTTP fixtures for Native Desktop formal queue acceptance.

    python3 tests/desktop_queue_acceptance.py create --root /tmp/kp-native-001
    python3 tests/desktop_queue_acceptance.py status --root /tmp/kp-native-001
    # Desktop consumes the plan via desktop-queue-mcp using env.json and --db.
    python3 tests/desktop_queue_acceptance.py after-plan --root /tmp/kp-native-001
    # Desktop consumes Review via the same MCP; no model is invoked here.
    python3 tests/desktop_queue_acceptance.py stop --root /tmp/kp-native-001

create starts a loopback HTTP server; serve runs it in the foreground if needed.
stop uses an authenticated HTTP shutdown, never an unverified persisted PID.
The execution phase is a clearly labelled local fixture, not a model execution.
Only actual Desktop submissions can create plan/Review acceptance evidence.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

REPOSITORY = Path(__file__).resolve().parent.parent
CORE = REPOSITORY / "core"
TASK_ID = "desktop-square-acceptance"
PROJECT_ID = "desktop-square-project"
MARKER = "kanpilot-desktop-formal-acceptance-v1"
FAKE_TOKEN = "isolated-fixture-not-a-production-token"
REQUIREMENTS = """实现纯 Python 函数 square(raw: str) -> int，无 GUI、网络或依赖。
输入必须是 ASCII 十进制整数文本且数值为 1 到 10（包含边界）。允许首尾空白。
空白、非数字、小数（包括 1.0）、负数、0、11、NaN、Infinity 必须抛 ValueError。
1、5、10 分别返回 1、25、100；非字符串抛 ValueError。
只规划现有 square.py 的实现和标准库 unittest 验证，不要实现业务源码。
本次任务为隔离验收：先真实 Desktop 规划并回传，然后夹具构造正确实现，
再真实 Desktop 只读 Review。所有远端接口为本机 Fake Kaneo，禁止访问生产。
"""
IMPLEMENTATION = '''"""Integer square for inputs in the inclusive range 1..10."""


def square(raw: str) -> int:
    if not isinstance(raw, str):
        raise ValueError("expected an integer string")
    text = raw.strip()
    if not text or not text.isascii() or not text.isdecimal():
        raise ValueError("expected ASCII decimal digits")
    significant = text.lstrip("0")
    if not significant or len(significant) > 2:
        raise ValueError("integer must be between 1 and 10")
    number = int(significant)
    if not 1 <= number <= 10:
        raise ValueError("integer must be between 1 and 10")
    return number * number
'''
UNIT_TESTS = '''import unittest
from square import square


class SquareTests(unittest.TestCase):
    def test_valid(self):
        for raw, expected in [("1", 1), ("5", 25), ("10", 100), (" 5 ", 25), ("01", 1)]:
            with self.subTest(raw=raw):
                self.assertEqual(square(raw), expected)

    def test_invalid(self):
        for raw in ["", " ", "x", "1.0", "1.5", "-1", "0", "11", "NaN", "Infinity", "１", "²", None, 5]:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    square(raw)


if __name__ == "__main__":
    unittest.main()
'''


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def loopback(base):
    parsed = urlsplit(base)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or not parsed.port or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise RuntimeError("fixture API must be an explicit http://127.0.0.1:port")
    return base.rstrip("/")


def within(root, value):
    path = Path(value).resolve()
    if root not in path.parents:
        raise RuntimeError("fixture path escapes root")
    return path


def load(root):
    if (root / "fixture.marker").read_text(encoding="utf-8").strip() != MARKER:
        raise RuntimeError("not an acceptance fixture root")
    env = json.loads((root / "env.json").read_text(encoding="utf-8"))
    for key, value in env.items():
        if key.startswith("KANPILOT_") and key != "KANPILOT_PI_START_WAIT":
            within(root, value)
    credentials = within(root, env["KANEO_CREDENTIALS_PATH"])
    loopback(json.loads(credentials.read_text(encoding="utf-8"))["baseUrl"])
    # Explicit overrides are installed before any core command/provider lookup.
    os.environ.update(env)
    return env


def modules():
    sys.path.insert(0, str(CORE))
    sys.path.insert(0, str(CORE / "_vendor"))
    from kanpilot import cli, db, round_store, util
    return cli, db, round_store, util


def call(env, command, payload):
    cli, _, _, _ = modules()
    return cli.run(command, payload, Path(env["KANPILOT_DB"]))


def http(root, path, method="GET", body=None):
    env = load(root)
    credentials = json.loads(Path(env["KANEO_CREDENTIALS_PATH"]).read_text(encoding="utf-8"))
    request = Request(loopback(credentials["baseUrl"]) + path, method=method,
                      headers={"Authorization": "Bearer " + credentials["accessToken"],
                               "Content-Type": "application/json"})
    if body is not None:
        request.data = json.dumps(body).encode("utf-8")
    with urlopen(request, timeout=5) as response:
        return json.load(response)


def serve(root):
    # Do not load credentials before the server assigns its ephemeral port.
    if (root / "fixture.marker").read_text().strip() != MARKER:
        raise RuntimeError("not an acceptance fixture root")
    state_path = root / "remote.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.setdefault("fix_tasks", {})
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.handle_request("GET")

        def do_POST(self):
            self.handle_request("POST")

        def do_PUT(self):
            self.handle_request("PUT")

        def handle_request(self, method):
            if self.headers.get("Authorization") != "Bearer " + FAKE_TOKEN:
                self.respond(401, {"error": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > 1024 * 1024:
                    raise ValueError("request too large")
                body = json.loads(self.rfile.read(length)) if length else {}
                parsed_path = urlsplit(self.path)
                path = parsed_path.path
                with lock:
                    state["requests"].append({"method": method, "path": path})
                    if path == "/_fixture/stop" and method == "POST":
                        result = {"stopping": True}
                        threading.Thread(target=self.server.shutdown, daemon=True).start()
                    elif path == "/api/project" and method == "GET":
                        result = {"data": state["projects"]}
                    elif path == "/api/task/" + TASK_ID and method == "GET":
                        result = {"data": state["task"]}
                    elif path.startswith("/api/task/") and method == "GET" and path.rsplit("/", 1)[-1] in state["fix_tasks"]:
                        result = {"data": state["fix_tasks"][path.rsplit("/", 1)[-1]]}
                    elif path == "/api/task/" + PROJECT_ID and method == "POST":
                        fix_id = "fixture-fix-" + str(len(state["fix_tasks"]) + 1)
                        fix = {"id": fix_id, "projectId": PROJECT_ID, "customFields": {},
                               "attachments": [], **body}
                        state["fix_tasks"][fix_id] = fix
                        result = {"data": fix}
                    elif path == "/api/task/status/" + TASK_ID and method == "PUT":
                        state["task"]["status"] = body["status"]
                        result = {"data": state["task"]}
                    elif path == "/api/comment/" + TASK_ID:
                        if method == "POST":
                            comment = {"id": "comment-" + str(len(state["comments"]) + 1),
                                       "content": body["content"],
                                       "createdAt": datetime.now(timezone.utc).isoformat()}
                            state["comments"].append(comment)
                            result = {"data": comment}
                        elif method == "GET":
                            result = {"data": state["comments"]}
                        else:
                            raise ValueError("unsupported comment method")
                    elif method == "GET" and path in (
                            "/api/task-relation/" + TASK_ID, "/api/activity/" + TASK_ID,
                            "/api/label/task/" + TASK_ID):
                        result = {"data": []}
                    elif method == "GET" and any(path == prefix + fix_id
                            for prefix in ("/api/comment/", "/api/task-relation/", "/api/activity/", "/api/label/task/")
                            for fix_id in state["fix_tasks"]):
                        result = {"data": []}
                    elif path == "/api/column/" + PROJECT_ID and method == "GET":
                        result = {"data": [{"slug": slug} for slug in
                                          ("to-do", "in-progress", "in-review", "done")]}
                    elif path == "/api/task/tasks/" + PROJECT_ID and method == "GET":
                        candidates = [state["task"], *state["fix_tasks"].values()]
                        expected = parse_qs(parsed_path.query).get("status", [None])[0]
                        tasks = [task for task in candidates if expected is None or task.get("status") == expected]
                        result = {"data": {"columns": [{"tasks": tasks}]},
                                  "pagination": {"totalPages": 1}}
                    else:
                        self.respond(404, {"error": "unsupported fixture endpoint"})
                        return
                    write_json(state_path, state)
                self.respond(200, result)
            except (ValueError, KeyError):
                self.respond(400, {"error": "invalid fixture request"})

        def respond(self, code, value):
            data = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base = loopback("http://127.0.0.1:" + str(server.server_port))
    write_json(root / "credentials.json", {"baseUrl": base, "accessToken": FAKE_TOKEN})
    (root / "credentials.json").chmod(0o600)
    write_json(root / "server.json", {"base_url": base, "port": server.server_port,
                                       "pid": os.getpid(), "running": True})
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
        write_json(root / "server.json", {"base_url": base, "port": server.server_port,
                                           "pid": os.getpid(), "running": False})


def git(repo, *arguments):
    return subprocess.run(["git", "-C", str(repo), *arguments], check=True,
                          capture_output=True, text=True, timeout=30).stdout.strip()


def create(root):
    root.mkdir(parents=True, exist_ok=False)
    (root / "fixture.marker").write_text(MARKER + "\n", encoding="utf-8")
    paths = {"KANPILOT_APP_SUPPORT": root / "support", "KANPILOT_DB": root / "support/kanpilot.db",
             "KANPILOT_LEGACY_ROOT": root / "legacy", "KANPILOT_PLAN_ROOT": root / "plans",
             "KANPILOT_REPOS_ROOT": root / "repos", "KANPILOT_REPORTS_ROOT": root / "reports",
             "KANPILOT_SOURCES_DIR": root / "sources", "KANPILOT_PLANNER_INPUTS": root / "inputs",
             "KANPILOT_WORKTREES_ROOT": root / "worktrees", "KANPILOT_PI_HOME": root / "pi-home",
             "KANPILOT_CODEX_HOME": root / "codex-home", "KANPILOT_AGENTS_DIR": root / "agents"}
    for key, path in paths.items():
        (path.parent if key == "KANPILOT_DB" else path).mkdir(parents=True, exist_ok=True)
    (root / "support/runs").mkdir()
    env = {key: str(path) for key, path in paths.items()}
    env.update({"KANEO_CREDENTIALS_PATH": str(root / "credentials.json"),
                "CLAUDE_CONFIG_DIR": str(root / "claude-config")})
    # These are isolated non-model executables/configs, not Desktop providers.
    for name in ("pi", "codex"):
        binary = root / (name + "-unavailable")
        binary.write_text("#!/bin/sh\nexit 78\n", encoding="utf-8")
        binary.chmod(0o755)
        env["KANPILOT_" + name.upper() + "_BIN"] = str(binary)
    write_json(root / "env.json", env)
    write_json(root / "remote.json", {
        "projects": [{"id": PROJECT_ID, "name": "Desktop Square Acceptance"}],
        "task": {"id": TASK_ID, "projectId": PROJECT_ID, "title": "Desktop 正式规划与 Review 验收",
                 "description": REQUIREMENTS, "status": "to-do", "priority": "medium",
                 "customFields": {}, "attachments": []}, "comments": [], "requests": []})
    with (root / "server.log").open("ab") as log:
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "serve", "--root", str(root)],
                                   stdout=log, stderr=log, start_new_session=True)
    try:
        for _ in range(50):
            if (root / "server.json").exists():
                break
            if process.poll() is not None:
                raise RuntimeError("fixture server exited; inspect server.log")
            time.sleep(0.1)
        else:
            raise RuntimeError("fixture server did not start")
        env = load(root)
        repo = root / "repos/square"
        repo.mkdir()
        git(repo, "init", "-b", "main")
        git(repo, "config", "user.name", "Isolated Fixture")
        git(repo, "config", "user.email", "fixture@localhost")
        git(repo, "config", "core.hooksPath", str(root / "no-hooks"))
        (repo / "square.py").write_text('def square(raw: str) -> int:\n    raise NotImplementedError("implement the confirmed plan")\n', encoding="utf-8")
        (repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "Initialize isolated square stub")
        _, db, _, _ = modules()
        conn = db.connect(Path(env["KANPILOT_DB"]))
        try:
            with db.write_txn(conn):
                db.set_setting(conn, "session_runs_root", str(root / "support/runs"))
        finally:
            conn.close()
        mappings = call(env, "get-project-mappings", {})
        call(env, "set-project-mapping", {"project_id": PROJECT_ID, "repository": str(repo),
                                           "enabled": True, "expected_revision": mappings["revision"]})
        call(env, "set-review-config", {"backend": "codex_desktop"})
        call(env, "set-planner-config", {"backend": "codex_desktop"})
        call(env, "set-desktop-queue-config", {"enabled": True})
        prepared = call(env, "prepare-plan-input", {"task_id": TASK_ID})
        if not prepared.get("can_start"):
            raise RuntimeError("planner materials blocked: " + json.dumps(prepared, ensure_ascii=False))
        started = call(env, "start-plans", {"items": [{"input_id": prepared["input_id"],
            "input_sha256": prepared["input_sha256"], "planner_backend": "codex_desktop", "planner_method": "builtin"}]})
        if started.get("started") != 1:
            raise RuntimeError("formal plan enqueue failed: " + json.dumps(started, ensure_ascii=False))
        write_json(root / "created.json", {"task_id": TASK_ID, "prepared": prepared, "started": started})
        return status(root)
    except Exception:
        if (root / "credentials.json").exists():
            http(root, "/_fixture/stop", "POST", {})
        else:
            process.terminate()
        raise


def after_plan(root):
    env = load(root)
    _, db, round_store, util = modules()
    conn = db.connect(Path(env["KANPILOT_DB"]))
    try:
        task = db.require_task(conn, TASK_ID)
        if task["status"] == "in_review" and task["run_id"] == "fixture-execution-1":
            # Recover a crash after fixture execution setup but before enqueue.
            call(env, "enqueue", {"task_id": TASK_ID, "kind": "review"})
            return status(root)
        plan_request = conn.execute("SELECT * FROM requests WHERE task_id=? AND stage='plan' ORDER BY created_at DESC LIMIT 1", (TASK_ID,)).fetchone()
        if (task["status"] != "ready" or not plan_request or plan_request["status"] != "succeeded"
                or db.request_payload(plan_request).get("planner_backend") != "codex_desktop"
                or not db.request_payload(plan_request).get("desktop_claim")
                or not task["plan_path"] or util.sha256_file(Path(task["plan_path"])) != task["plan_sha256"]):
            raise RuntimeError("after-plan requires a genuinely published Desktop plan; no result is seeded")
        execution = root / "worktrees" / TASK_ID / "execution"
        execution.parent.mkdir(parents=True, exist_ok=True)
        git(root / "repos/square", "worktree", "add", "-b", "fixture-execution", str(execution))
        (execution / "square.py").write_text(IMPLEMENTATION, encoding="utf-8")
        (execution / "test_square.py").write_text(UNIT_TESTS, encoding="utf-8")
        git(execution, "add", "square.py", "test_square.py")
        checked = subprocess.run([sys.executable, "-m", "unittest", "-v"], cwd=execution,
                                 capture_output=True, text=True, timeout=30)
        (root / "execution-validation.txt").write_text(checked.stdout + checked.stderr, encoding="utf-8")
        if checked.returncode:
            raise RuntimeError("isolated square validation failed")
        run_id = "fixture-execution-1"
        now = util.now_iso()
        run_dir = root / "support/runs" / TASK_ID / run_id
        run_dir.mkdir(parents=True)
        write_json(run_dir / "job.json", {"task_id": TASK_ID, "run_id": run_id, "backend": "codex",
            "status": "finished", "exit_code": 0, "started_at": now, "ended_at": now,
            "worktree_path": str(execution), "plan_sha256": task["plan_sha256"],
            "fixture": True, "note": "Local fixture execution; no model called. Correct staged diff remains against stub HEAD.",
            "validation_command": [sys.executable, "-m", "unittest", "-v"], "validation_exit_code": checked.returncode})
        (run_dir / "events.jsonl").write_text(json.dumps({"type": "system", "at": now,
            "content": "Local isolated fixture validation (not Desktop execution):\n" + checked.stdout + checked.stderr}) + "\n", encoding="utf-8")
        # Standard isolated fixture helpers construct only execution identity/state.
        # They never mark a plan/Review claim or submit a plan/Review result.
        with db.write_txn(conn):
            extra = db.task_extra(task)
            extra.pop("plan_execution_hold", None)
            db.update_task(conn, TASK_ID, now, status="in_review", stage="review", run_id=run_id,
                           extra_json=extra, last_remote_status="in-review")
            round_store.upsert_on_claim(conn, TASK_ID, run_id, {"fixture": True, "plan_sha256": task["plan_sha256"]}, now, backend="codex")
            round_store.mark_started(conn, TASK_ID, run_id, now, worktree_path=str(execution))
            round_store.upsert_discovered(conn, TASK_ID, run_id, now, backend="codex", run_status="finished")
            db.add_event(conn, now, "fixture_execution_prepared", task_id=TASK_ID, actor="acceptance_fixture",
                         detail={"run_id": run_id, "validation_exit_code": checked.returncode, "not_model_execution": True})
        http(root, "/api/task/status/" + TASK_ID, "PUT", {"status": "in-review"})
        enqueued = call(env, "enqueue", {"task_id": TASK_ID, "kind": "review"})
        write_json(root / "after-plan.json", {"execution_run_id": run_id, "worktree": str(execution),
            "plan_sha256": task["plan_sha256"], "review_request": enqueued})
    finally:
        conn.close()
    return status(root)


def status(root):
    env = load(root)
    _, db, _, _ = modules()
    conn = db.connect(Path(env["KANPILOT_DB"]))
    try:
        requests = [dict(row) for row in conn.execute("SELECT request_id,stage,status,run_id,lease_expires_at FROM requests WHERE task_id=? ORDER BY created_at", (TASK_ID,))]
        task = db.get_task(conn, TASK_ID)
        task_status = {key: task[key] for key in ("task_id", "status", "run_id", "plan_path", "plan_sha256", "review_report_path")} if task else None
    finally:
        conn.close()
    return {"root": str(root), "env_file": str(root / "env.json"), "db": env["KANPILOT_DB"],
            "server": json.loads((root / "server.json").read_text()), "task": task_status,
            "requests": requests, "queue": call(env, "desktop-queue-status", {})}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "serve", "after-plan", "status", "stop"))
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    if args.action == "serve":
        serve(root)
        return
    if args.action == "create":
        result = create(root)
    elif args.action == "after-plan":
        result = after_plan(root)
    elif args.action == "stop":
        load(root)
        info = json.loads((root / "server.json").read_text())
        result = http(root, "/_fixture/stop", "POST", {}) if info["running"] else {"already_stopped": True}
    else:
        result = status(root)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
