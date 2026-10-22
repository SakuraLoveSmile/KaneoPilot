#!/usr/bin/env python3
"""Execution ask relay rehearsal against a caller-owned loopback Plus candidate.

The caller keeps the real Rust Host, production sidecar and deterministic SSE
provider alive. This script starts a real isolated KaneoPilot MCP service and
dispatch worker. Only initial ready-task/plan construction uses fixture DB APIs;
all acceptance transitions use fixed core JSON commands. No paid model is used.

Provider contract: plan contains EXECUTION_ASK_RELAY_SCENARIO=<scenario>. Goal
contract approval follows the normal worker path; execution then asks two
questions and writes execution-answer.txt from the accepted tool result. The
file must contain the original selected labels (One/Unit or Zero/Integration).
The provider then runs exactly `python3 verify_execution_answer.py` through
the real Bash tool. Normal completion continues through deterministic monitor
to in_review and queues Review without starting a Review model.
The provider holds the final SSE response behind an explicit per-instance
control gate, so a read-only observer distinguishes disappearance from terminal
state without an arbitrary delay. --caller-fixture-root/execution-ask-fixture.json
must identify the supplied connection, control URL, provider and model.

Native scene handshake: native-bootstrap.json is emitted before any dispatch;
the caller starts the isolated App, establishes its empty attention baseline,
then writes native-start.json {"action":"start"}. native-ready.json later holds
the execution ask for UI acceptance; native-finish.json {"action":"verify"}
continues verification. Both waits share --native-wait-seconds as one deadline.

Explicit-only plus_stop and sidecar_restart scenarios check interrupted asks.
sidecar_restart requires --fixture-main-pid and verifies scratch process/DB
ownership before killing only that Main's isolated sidecar child.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime
import hashlib
import http.client
import http.server
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
from urllib.parse import urlsplit
import uuid

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
PROJECT_ID = "fixture-project-feedback"
SCENARIOS = ("core_answer", "plus_first", "race", "unknown", "engine_off", "fast_completed")
TERMINAL_SCENARIOS = ("plus_stop", "sidecar_restart")
VERIFY_COMMAND = "python3 verify_execution_answer.py"
VERIFY_SOURCE = '''from pathlib import Path
import sys

expected = {
    "Choose a result：One\\n---\\nChoose checks：Unit\\n",
    "Choose a result：Zero\\n---\\nChoose checks：Integration\\n",
}
try:
    answer = Path("execution-answer.txt").read_text(encoding="utf-8")
except OSError:
    sys.exit("execution-answer.txt is missing or unreadable")
if answer not in expected:
    sys.exit("execution-answer.txt does not contain one complete accepted answer")
print("accepted original answer verified")
'''


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def within(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def git(repo, *args):
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
                            cwd=repo, capture_output=True, text=True, timeout=20)
    require(result.returncode == 0, "isolated git command failed")
    return result.stdout.strip()


def wait_for(read, predicate, *, seconds=15, label="condition", interval=0.1):
    deadline = time.monotonic() + seconds
    while True:
        value = read()
        if predicate(value):
            return value
        require(time.monotonic() < deadline, label + " timed out")
        time.sleep(interval)


def release_fixture(args, instance_id, scenario):
    """Release only this fixture instance's final SSE response (idempotent)."""
    url = urlsplit(args.fixture_control_url)
    remote = http.client.HTTPConnection(url.hostname, url.port, timeout=10)
    try:
        remote.request("POST", url.path, body=json.dumps({"action": "release_final",
                       "instanceId": instance_id, "scenario": scenario}).encode(),
                       headers={"Content-Type": "application/json"})
        response = remote.getresponse()
        value = json.loads(response.read())
        require(response.status == 200 and value.get("ok") is True and value.get("instanceId") == instance_id,
                "fixture final gate release failed")
        return value
    finally:
        remote.close()


def isolated_env(root, core_root):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("KANPILOT_", "KANEO_", "PI_DESKTOP_"))}
    paths = {
        "KANPILOT_DB": root / "app_support/kanpilot.db",
        "KANPILOT_APP_SUPPORT": root / "app_support",
        "KANPILOT_PLAN_ROOT": root / "plans",
        "KANPILOT_REPOS_ROOT": root / "repos",
        "KANPILOT_LEGACY_ROOT": root / "legacy",
        "KANPILOT_REPORTS_ROOT": root / "reports",
        "KANPILOT_SOURCES_DIR": root / "sources",
        "KANPILOT_WORKTREES_ROOT": root / "worktrees",
        "KANEO_CREDENTIALS_PATH": root / "credentials/fake-kaneo.json",
    }
    for key, path in paths.items():
        require(within(path, root), key + " escaped isolated root")
        env[key] = str(path)
        if key not in ("KANPILOT_DB", "KANEO_CREDENTIALS_PATH"):
            path.mkdir(parents=True)
    (root / "credentials").mkdir()
    env["KANPILOT_CORE_BIN"] = str(core_root / "kanpilot.py")
    env["PYTHONPATH"] = str(core_root) + os.pathsep + str(core_root / "_vendor")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env, paths


class LoopbackProxy:
    """Real HTTP forwarding with a single response-loss fault, no internal mock.

    Stores only tool names and monotonic timestamps, never headers or arguments.
    The unknown fault forwards the actual answer, then disconnects the caller.
    """
    def __init__(self, connection):
        self.connection = connection
        self.calls = []
        self.drop_response = False
        self.lock = threading.Lock()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                if length > 2 * 1024 * 1024:
                    self.send_error(413)
                    return
                raw = self.rfile.read(length)
                request = json.loads(raw)
                name = (request.get("params") or {}).get("name") if request.get("method") == "tools/call" else None
                drop = False
                entry = {"name": name, "at": time.monotonic()}
                with owner.lock:
                    if name:
                        owner.calls.append(entry)
                    if name == "pi_asks_resolve" and owner.drop_response:
                        owner.drop_response = False
                        drop = True
                url = urlsplit(owner.connection["url"])
                remote = http.client.HTTPConnection(url.hostname, url.port, timeout=15)
                try:
                    headers = {key: value for key, value in self.headers.items()
                               if key.lower() not in ("host", "connection", "content-length")}
                    remote.request("POST", url.path, body=raw, headers=headers)
                    response = remote.getresponse()
                    body = response.read()
                    if name == "pi_asks_pending":
                        # Safe identity/timing evidence from the real response.
                        def pending_asks(value):
                            if isinstance(value, dict):
                                if isinstance(value.get("asks"), list):
                                    return value["asks"]
                                for child in value.values():
                                    found = pending_asks(child)
                                    if found:
                                        return found
                            return []
                        try:
                            asks = pending_asks(json.loads(body))
                            with owner.lock:
                                entry["request_ids"] = [ask.get("requestId") for ask in asks]
                                entry["ask_metadata"] = [{key: ask.get(key) for key in
                                                          ("requestId", "toolCallId", "createdAt")}
                                                         for ask in asks]
                        except ValueError:
                            pass
                    if drop:
                        self.close_connection = True
                        self.connection.shutdown(socket.SHUT_RDWR)
                        return
                    self.send_response(response.status)
                    for key, value in response.getheaders():
                        if key.lower() not in ("content-length", "connection", "transfer-encoding"):
                            self.send_header(key, value)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    if body:
                        self.wfile.write(body)
                except (OSError, http.client.HTTPException):
                    self.close_connection = True
                finally:
                    remote.close()

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def write_connection(self, path):
        write_json(path, {**self.connection,
                         "url": "http://127.0.0.1:%d/mcp" % self.server.server_port,
                         "pid": os.getpid()})
        path.chmod(0o600)

    def count(self, name):
        with self.lock:
            return sum(row["name"] == name for row in self.calls)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def load_modules(core_root):
    sys.path.insert(0, str(REPO))  # Repository's existing standard-library fixture shim.
    sys.path.insert(0, str(core_root / "_vendor"))
    sys.path.insert(0, str(core_root))
    from kanpilot import cli, constants, db, pi_desktop_backend, util
    for module in (cli, constants, db, pi_desktop_backend, util):
        require(within(module.__file__, core_root), "wrong core provenance")
    spec = importlib.util.spec_from_file_location("execution_rehearsal_fixture", REPO / "tests/conftest.py")
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    return cli, constants, db, pi_desktop_backend, util, fixture


def seed_ready(call, db, util, fixture, fake, root, scenario):
    from kanpilot import verify_evidence
    repo = root / "repos/Feedback"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Isolated execution relay fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "baseline.txt").write_text("isolated fixture\n", encoding="utf-8")
    (repo / "verify_execution_answer.py").write_text(VERIFY_SOURCE, encoding="utf-8")
    git(repo, "add", "baseline.txt", "verify_execution_answer.py")
    git(repo, "commit", "-q", "-m", "isolated fixture baseline")
    call("set-project-mapping", {"project_id": PROJECT_ID, "repository": str(repo),
                                 "enabled": True, "expected_revision": 0})
    tid = "askrelay" + uuid.uuid4().hex[:18]
    title = "执行问答隔离演练 " + scenario
    description = "EXECUTION_ASK_RELAY_SCENARIO=" + scenario + "\nEXECUTION_ASK_RELAY_INSTANCE=" + tid
    remote = fake.add_task(PROJECT_ID, title, description, task_id=tid)
    snapshot = fixture.write_source_snapshot(root / "sources", task_id=tid, project_id=PROJECT_ID,
             title=title, description=description, task_obj=remote)
    call("sync-tasks", {"executor": "kaneo-planner", "projects": [{"project_id": PROJECT_ID,
         "project_name": "Feedback", "repository": str(repo), "tasks": [{"task_id": tid,
         "title": title, "kaneo_status": "to-do", "source_path": str(snapshot.path),
         "source_sha256": snapshot.sha256, "requirements_sha256": snapshot.requirements_sha256}]}]})
    plan = fixture.write_plan_file(root / "plans", task_id=tid, project_id=PROJECT_ID,
             project_name="Feedback", repository=repo, source_sha256=snapshot.sha256,
             requirements_sha256=snapshot.requirements_sha256, title=title,
             body="# Execution ask relay fixture\n\n" + description + "\n\n"
                  "Use the normal goal contract approval, then ask the fixture questions. "
                  "Use only the accepted answer to write execution-answer.txt. "
                  "Then execute the declared verification command with Bash before reporting completion. "
                  "Do not commit, push, install dependencies or access external services.\n\n"
                  "```verify\n" + VERIFY_COMMAND + "\n```\n")
    declaration = verify_evidence.parse_verify_block(plan.path.read_text(encoding="utf-8"))
    require(declaration.get("ok") is True and declaration["commands"] == [VERIFY_COMMAND],
            "fixture plan verification declaration changed")
    # Only fixture construction: no simulated execution, answer or success record.
    conn = db.connect(root / "app_support/kanpilot.db")
    try:
        with db.write_txn(conn):
            db.update_task(conn, tid, util.now_iso(), status="ready", plan_path=str(plan.path),
                           plan_sha256=plan.sha256, plan_kind="standard")
            # Normal published plans are held until explicit confirm_plan. Keep
            # the native App's startup scheduler from dispatching before its
            # empty-question baseline has been established.
            db.set_extra(conn, db.require_task(conn, tid), util.now_iso(), plan_execution_hold=True,
                         plan_verify={"plan_sha256": plan.sha256, "commands": declaration["commands"]})
            db.set_setting(conn, "session_runs_root", str(root / "app_support/runs"))
            db.set_setting(conn, "executor_timeout_seconds", 120)
            db.set_setting(conn, "monitor_interval_seconds", 1)
    finally:
        conn.close()
    return tid, plan


def answers_for(question, labels):
    require(len(question["questions"]) == len(labels), "provider question count changed")
    answers = []
    for item, label in zip(question["questions"], labels):
        option = next((opt for opt in item["options"] if opt["label"] == label), None)
        require(option is not None, "fixture option missing")
        answers.append({"question_id": item["id"], "option_ids": [option["id"]],
                        "text": "", "skipped": False})
    return answers


def safe_call(call, command, payload):
    try:
        value = call(command, payload)
        return {"ok": True, "result": value}
    except Exception as exc:
        return {"ok": False, "error_type": type(exc).__name__, "error_code": getattr(exc, "code", None)}


def scan_secrets(root, secrets):
    """Credential files are intentional; logs/events/SQLite may not contain them."""
    values = {value for value in secrets if isinstance(value, str) and len(value) >= 12}
    secrets = {encoded for value in values for encoded in
               (value.encode(), json.dumps(value, ensure_ascii=False)[1:-1].encode(),
                json.dumps(value, ensure_ascii=True)[1:-1].encode())}
    paths = [path for path in root.rglob("*") if path.is_file() and
             (path.suffix in (".log", ".jsonl", ".db") or path.name in
              ("summary.json", "transcript.json", "native-ready.json", "native-bootstrap.json",
               "native-start.json", "native-finish.json") or
              path.name.endswith((".db-wal", ".db-shm")))]
    violations = []
    for path in paths:
        data = path.read_bytes()
        if any(secret in data for secret in secrets):
            violations.append(str(path.relative_to(root)))
    require(not violations, "credential leaked into evidence: " + ", ".join(violations))
    return {"files_scanned": len(paths), "secret_values_checked": len(values), "passed": True}


def owned_worker(pid, spec_path, *, resume=False):
    """Read the exact process arguments before signaling a fixture worker."""
    if not pid:
        return None
    result = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "stat=", "-o", "command="], capture_output=True,
                            text=True, timeout=5)
    if result.returncode or not result.stdout.strip():
        return None
    if result.stdout.strip().split()[0].startswith("Z"):
        return None
    _state, command = result.stdout.strip().split(None, 1)
    # macOS ps prints arguments without quoting paths containing spaces. Match
    # the complete known worker suffix, instead of splitting the spec pathname.
    candidates = [(" -m kanpilot.pi_desktop_worker --spec " + path + extra, path, bool(extra))
                  for path in {str(spec_path), str(Path(spec_path).resolve())} for extra in ("", " --resume")]
    matched = next((candidate for candidate in candidates if command.endswith(candidate[0])), None)
    require(matched is not None, "worker PID identity changed; cleanup refused")
    suffix, process_spec_path, resumed = matched
    executable = command[:-len(suffix)]
    require(Path(executable).name.lower().startswith("python"), "worker executable identity changed")
    require(not resume or resumed, "observer did not start with --resume")
    arguments = [executable, "-m", "kanpilot.pi_desktop_worker", "--spec", process_spec_path]
    if resumed:
        arguments.append("--resume")
    return {"pid": pid, "argv": arguments, "spec_path": str(spec_path)}


def cleanup_worker(pid, spec_path):
    """Never signal a PID without the current, isolated spec in its arguments."""
    if owned_worker(pid, spec_path) is None:
        return "already_exited"
    os.kill(pid, signal.SIGTERM)
    return "terminated_owned_worker"


def process_snapshot():
    result = subprocess.run(["ps", "-ww", "-axo", "pid=,ppid=,stat=,lstart=,command="],
                            capture_output=True, text=True, timeout=5)
    require(result.returncode == 0, "process identity snapshot failed")
    processes = {}
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 8)
        if len(fields) == 9 and fields[0].isdigit() and fields[1].isdigit():
            pid = int(fields[0])
            processes[pid] = {"pid": pid, "ppid": int(fields[1]), "state": fields[2],
                              "started_at": " ".join(fields[3:8]), "command": fields[8]}
    return processes


def fixture_processes(args):
    """Read-only proof that Main, Host DB and sole sidecar belong to scratch."""
    require(type(args.fixture_main_pid) is int and args.fixture_main_pid > 1,
            "sidecar restart requires an explicit fixture Main PID")
    root = args.caller_fixture_root.resolve()
    require(root != Path.home() and (root / "desktop-fixture.cjs").is_file()
            and (root / "resources/agent-runtime/sidecar.js").is_file()
            and (root / "data/pi.sqlite").is_file(), "scratch Desktop resources or DB missing")
    aliases = {str(args.caller_fixture_root), str(root),
               str(Path(json.loads((root / "execution-ask-fixture.json").read_text())["connection_file"]).parent.parent)}
    processes = process_snapshot()
    main = processes.get(args.fixture_main_pid)
    require(main and not main["state"].startswith("Z") and any(
            main["command"].endswith(" --user-data-dir=" + alias + "/profile " + alias + "/desktop-fixture.cjs")
            for alias in aliases), "Main PID does not identify the caller scratch Desktop")
    children = [process for process in processes.values() if process["ppid"] == main["pid"]
                and not process["state"].startswith("Z")]
    sidecars = [child for child in children if any(
                child["command"].endswith(" " + alias + "/resources/agent-runtime/sidecar.js") for alias in aliases)]
    require(len(sidecars) == 1 and len([child for child in children if "sidecar.js" in child["command"]]) == 1,
            "scratch Main must have exactly one matching sidecar child")
    hosts = [child for child in children if Path(child["command"]).name == "pi-desktop-host-core"]
    require(len(hosts) == 1, "scratch Main must have exactly one Rust Host child")
    host = hosts[0]
    opened = subprocess.run(["lsof", "-a", "-p", str(host["pid"]), "-Fn"],
                            capture_output=True, text=True, timeout=5)
    require(opened.returncode == 0 and any(line.startswith("n")
            and Path(line[1:]).resolve() == (root / "data/pi.sqlite").resolve()
            for line in opened.stdout.splitlines()), "Rust Host has no verified scratch DB open")
    return {"main": main, "host": host, "sidecar": sidecars[0], "db_path": str(root / "data/pi.sqlite")}


def same_process(before, after):
    return all(before[key] == after[key] for key in ("pid", "ppid", "started_at", "command"))


def interrupted_ask(args, scenario, *, row, call, direct, proxy, connection, util,
                    identity, sid, question, draft, plus_args, read_job, read_events, worktree):
    """Expected failure paths: never answer the execution ask or enter Review."""
    creates, prompts = proxy.count("pi_session_create"), proxy.count("pi_agent_prompt")
    resolves = proxy.count("pi_asks_resolve")
    status = direct.call_tool("pi_agent_status", {"sessionId": sid})
    inner = status.get("status") or status
    pending = direct.call_tool("pi_asks_pending", {"sessionId": sid})
    require(inner.get("currentTurnId") == question["turn_id"] and inner.get("isRunning") is True
            and pending.get("sessionId") == sid and any(ask.get("requestId") == question["request_id"]
            and ask.get("toolCallId") == question["tool_call_id"] and ask.get("turnId") == question["turn_id"]
            for ask in pending.get("asks", [])), "terminal scenario has no original live execution ask")
    if scenario == "plus_stop":
        row["plus_stop"] = direct.call_tool("pi_agent_stop", {"sessionId": sid})
    else:
        before = fixture_processes(args)
        # Recheck all identities immediately before the only destructive signal.
        checked = fixture_processes(args)
        require(all(same_process(before[key], checked[key]) for key in ("main", "host", "sidecar")),
                "scratch process identity changed before sidecar signal")
        row["sidecar_restart"] = {"before": before, "signal": "SIGKILL to verified sidecar only"}
        os.kill(before["sidecar"]["pid"], signal.SIGKILL)
        def replacement_started(processes):
            for key in ("main", "host"):
                current = processes.get(before[key]["pid"])
                require(current and not current["state"].startswith("Z") and same_process(before[key], current),
                        "sidecar restart changed scratch " + key)
            suffix = "/resources/agent-runtime/sidecar.js"
            candidates = [process for process in processes.values()
                          if process["ppid"] == before["main"]["pid"] and not process["state"].startswith("Z")
                          and process["command"].endswith(suffix)]
            return len(candidates) == 1 and candidates[0]["pid"] != before["sidecar"]["pid"]
        wait_for(process_snapshot, replacement_started, seconds=30, interval=0.5,
                 label="scratch supervisor replacement sidecar")
        after = fixture_processes(args)
        require(all(same_process(before[key], after[key]) for key in ("main", "host"))
                and after["sidecar"]["pid"] != before["sidecar"]["pid"]
                and not util.pid_alive(before["sidecar"]["pid"]),
                "restart did not replace only the verified sidecar")
        current_connection = json.loads(args.connection_file.read_text(encoding="utf-8"))
        require(current_connection.get("active") is True
                and all(current_connection.get(key) == connection[key] for key in ("url", "token")),
                "sidecar restart replaced the Main MCP connection or token")
        row["sidecar_restart"].update(after=after, main_host_unchanged=True, mcp_connection_unchanged=True)
    def read_turn():
        result = safe_call(lambda _command, payload: direct.call_tool("pi_turn_get", payload),
                           "pi_turn_get", {"sessionId": sid, "turnId": question["turn_id"]})
        value = result.get("result") or {}
        return value.get("turn") if isinstance(value.get("turn"), dict) else value
    turn = wait_for(read_turn, lambda value: value.get("status") in ("completed", "aborted", "error"),
                    seconds=30, interval=0.5, label="interrupted original durable Plus turn")
    expected = "completed" if scenario == "plus_stop" else "aborted"
    require(turn.get("sessionId") == sid and turn.get("turnId") == question["turn_id"]
            and turn["status"] == expected, "interrupted Plus turn identity or terminal status mismatch")
    row["plus_terminal"] = turn
    pending = direct.call_tool("pi_asks_pending", {"sessionId": sid})
    require(pending.get("sessionId") == sid and pending.get("asks") == [],
            "interrupted Plus waiter remained pending")
    stale = direct.call_tool("pi_asks_resolve", plus_args)
    require(stale.get("status") == "not_pending" and stale.get("requestId") == question["request_id"],
            "old interrupted request could still accept an answer")
    row["stale_answer"] = stale
    closed_task = wait_for(lambda: call("get-task", {"task_id": identity["task_id"]})["task"],
                          lambda task: (task.get("execution_question") or {}).get("state") == "expired",
                          seconds=20, label="core expiration of interrupted original question")
    closed = closed_task["execution_question"]
    require(closed_task["status"] == "needs_user" and closed_task["failure_code"] == "execution_failed"
            and closed_task["run_id"] == identity["run_id"] and closed["session_id"] == sid
            and closed["turn_id"] == question["turn_id"] and closed["request_id"] == question["request_id"]
            and closed["draft"] == draft and closed.get("answered_by") is None,
            "interrupted question lost its draft/identity or was recorded as answered")
    row["closed"] = {key: closed.get(key) for key in
                     ("state", "delivery", "answered_by", "draft", "request_id", "session_id", "turn_id")}
    row["execution_failure"] = {"task_status": closed_task["status"], "failure_code": closed_task["failure_code"]}
    failed = wait_for(read_job, lambda job: job.get("status") in ("failed", "finished"),
                      seconds=20, label="real worker interruption settlement")
    require(failed.get("status") == "failed" and type(failed.get("exit_code")) is int
            and failed["exit_code"] != 0 and failed.get("session_id") == sid
            and failed.get("turn_id") == question["turn_id"], "interrupted worker did not fail in its original turn")
    row["worker_failed"] = {key: failed.get(key) for key in ("status", "exit_code", "session_id", "turn_id", "detail")}
    events = wait_for(read_events, lambda values: any(event.get("type") == "terminal" for event in values),
                      seconds=5, label="interrupted worker terminal event")
    terminal = next(event for event in reversed(events) if event.get("type") == "terminal")
    require(terminal.get("outcome") == "failed", "interrupted worker wrote a success terminal")
    row["terminal"] = terminal
    if scenario == "plus_stop":
        stopped = [event for event in events if event.get("type") == "tool_result"
                   and event.get("tool_call_id") == question["tool_call_id"]
                   and event.get("turn_id") == question["turn_id"]]
        require(stopped, "Plus stop lacks the original same-turn ask tool result")
        result = json.loads(stopped[-1]["output"])
        require((result.get("details") or {}).get("resolutionKind") == "stopped"
                and result["details"].get("answers") == [None, None],
                "Plus stop was misrepresented as an ordinary answer")
        row["original_tool_results"] = stopped
        row["durable_resolution_kind"] = "stopped"
    require(not (Path(worktree) / "execution-answer.txt").exists()
            and not any(event.get("type") == "command_output" and event.get("turn_id") == question["turn_id"]
                        for event in events), "interrupted question proceeded to write or verify")
    requests = call("list-requests", {})["requests"]
    require(not any(request.get("task_id") == identity["task_id"] and request["kind"] == "review" for request in requests),
            "interrupted execution entered the Review queue")
    require(proxy.count("pi_session_create") == creates == 1 and proxy.count("pi_agent_prompt") == prompts == 1
            and proxy.count("pi_asks_resolve") == resolves == 1,
            "interruption created another session/prompt or resent the unanswered ask")
    if scenario == "sidecar_restart":
        final_processes = fixture_processes(args)
        require(all(same_process(row["sidecar_restart"]["after"][key], final_processes[key])
                    for key in ("main", "host", "sidecar")),
                "scratch process identity changed during interruption acceptance")
        row["sidecar_restart"]["after_terminal"] = final_processes
    row.update(session_create_count=creates, prompt_count=prompts,
               expected_failure_verified=True, normal_verify_and_review="NOT RUN: expected interruption", passed=True)


def run_scenario(args, evidence_root, connection, scenario, modules):
    root = evidence_root / scenario
    root.mkdir()
    env, paths = isolated_env(root, args.core_root)
    os.environ.clear()
    os.environ.update(env)
    cli, constants, db, backend, util, fixture = modules
    require(within(constants.default_db_path(), root), "default DB is not isolated")
    require(within(constants.app_support(), root), "app support is not isolated")
    command_lock = threading.Lock()
    def call(command, payload):
        # Preserve original core JSON data, not a synthesized success receipt.
        value = cli.run(command, payload, paths["KANPILOT_DB"])
        with command_lock, (root / "commands.jsonl").open("a", encoding="utf-8") as output:
            output.write(json.dumps({"command": command, "input": payload,
                         "transport": "core.cli.run", "result": value}, ensure_ascii=False) + "\n")
        return value
    fake = fixture.FakeKaneo()
    proxy = LoopbackProxy(connection)
    server = None
    worker_pid = None
    tid = run_id = spec_path = None
    native_deadline = None
    row = {"scenario": scenario, "initial_task_and_plan": "SEEDED READY FIXTURE",
           "runtime": "REAL CORE HTTP / REAL PLUS HOST AND SIDECAR / DETERMINISTIC LOOPBACK MODEL"}
    try:
        fake.write_credentials(paths["KANEO_CREDENTIALS_PATH"])
        paths["KANEO_CREDENTIALS_PATH"].chmod(0o600)
        local_connection = root / "credentials/plus-control.json"
        proxy.write_connection(local_connection)
        call("migrate", {})
        call("set-exec-config", {"backend": "pi_desktop", "pi_desktop": {
             "connection_file": str(local_connection), "model_source": "custom",
             "provider_id": args.provider_id, "model_id": args.model_id,
             "thinking_level": "off", "permission_mode": "auto"}})
        tid, plan = seed_ready(call, db, util, fixture, fake, root, scenario)
        with (root / "core-mcp.log").open("w", encoding="utf-8") as output:
            server = subprocess.Popen([sys.executable, str(args.core_root / "kanpilot.py"),
                     "mcp-serve", "--db", str(paths["KANPILOT_DB"]), "--host", "127.0.0.1", "--port", "0"],
                     env=env, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT)
        wait_for(lambda: root / "app_support/mcp/server.json", lambda p: p.is_file(),
                 seconds=10, label="isolated core MCP startup")
        if args.native_scene:
            bootstrap_file = root / "native-bootstrap.json"
            start_file = root / "native-start.json"
            native_deadline = time.monotonic() + args.native_wait_seconds
            require(not call("attention-list", {})["items"], "native startup question baseline is not empty")
            bootstrap_at = datetime.datetime.now(datetime.timezone.utc)
            write_json(bootstrap_file, {"task_id": tid, "db_path": str(paths["KANPILOT_DB"]),
                       "server_file": str(root / "app_support/mcp/server.json"),
                       "paths": {key: str(value) for key, value in paths.items()},
                       "environment": {key: value for key, value in env.items()
                                       if key.startswith("KANPILOT_") or key == "KANEO_CREDENTIALS_PATH"},
                       "start_file": str(start_file), "start_protocol": {"action": "start"},
                       "ready_file": str(root / "native-ready.json"),
                       "empty_question_baseline": True,
                       "bootstrap_at": bootstrap_at.isoformat(),
                       "deadline_at": (bootstrap_at + datetime.timedelta(seconds=args.native_wait_seconds)).isoformat(),
                       "total_native_wait_seconds": args.native_wait_seconds})
            print(json.dumps({"native_bootstrap": str(bootstrap_file), "production_access": False}), flush=True)
            wait_for(lambda: start_file, lambda path: path.is_file(), seconds=args.native_wait_seconds,
                     interval=0.5, label="caller native startup baseline handshake")
            require(not start_file.is_symlink() and json.loads(start_file.read_text())["action"] == "start",
                    "native startup stopped by caller")
            require(not call("attention-list", {})["items"], "native startup acquired questions before dispatch")
            row["native_start"] = {"received_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                   "empty_question_baseline": True}
        call("enqueue", {"task_id": tid, "kind": "dispatch", "confirm_plan": True,
                         "expected_plan_sha256": plan.sha256})
        claim = call("claim", {"stage": "dispatch", "executor": "kaneo-dispatch"})
        require(claim.get("claimed") is True, "real dispatch claim failed")
        run_id = claim["run_id"]
        identity = {"task_id": tid, "run_id": run_id}
        context = claim["context"]
        evidence = {key: context[key] for key in ("session_marker", "repository", "backend")}
        evidence["config"] = context["exec_config"]
        call("dispatch-prepare", {**identity, "evidence": evidence})
        call("dispatch-begin-send", identity)
        started = call("dispatch-start", identity)
        worker_pid = started["pid"]
        run_dir = root / "app_support/runs" / tid / run_id
        spec_path = run_dir / "spec.json"
        def read_job():
            try:
                return json.loads((run_dir / "job.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}
        def read_events():
            path = run_dir / "events.jsonl"
            if not path.is_file():
                return []
            events = []
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass  # Concurrent writer may still be appending the final line.
            return events
        require(within(started["worktree_path"], root), "execution worktree escaped fixture")
        session = wait_for(lambda: call("get-session", identity),
                           lambda v: bool((v.get("session") or {}).get("session_id")),
                           seconds=35, label="worker startup evidence")
        sid = session["session"]["session_id"]
        detail = session["round_detail"]
        turn_id = detail.get("startup_turn_id") or detail["turn_id"]
        call("dispatch-confirm", {**identity, "evidence": {**evidence, "session_id": sid,
             "turn_id": turn_id, "protocol_seen": True}})
        row.update({"task_id": tid, "run_id": run_id, "session_id": sid, "worker_pid": worker_pid})
        if scenario == "engine_off":
            status = call("engine-status", {})
            row["engine_status"] = status
            require(not any(instance["active"] for instance in status["instances"]),
                    "engine unexpectedly active in isolated fixture")
        goal_question = wait_for(lambda: call("get-task", {"task_id": tid})["task"].get("execution_question"),
                            lambda q: bool(q and q["state"] == "open"), seconds=30,
                            label="real Goal contract question discovery")
        goal_draft = answers_for(goal_question, ("One", "Unit"))
        goal_payload = {**identity, "request_id": goal_question["request_id"],
                        "questions_sha256": goal_question["questions_sha256"], "answers": goal_draft}
        call("save-execution-answers", {key: value for key, value in goal_payload.items() if key != "run_id"})
        require(proxy.count("pi_asks_resolve") == 0, "saving Goal draft performed external answer")
        goal_result = call("submit-execution-answers", {**goal_payload, "operation_id": "goal_" + uuid.uuid4().hex})
        goal_closed = goal_result["execution_question"]
        require(goal_closed["request_id"] == goal_question["request_id"] and goal_closed["state"] == "answered",
                "Goal contract question did not settle answered")
        require(goal_closed["session_id"] == sid and goal_closed["turn_id"] == goal_question["turn_id"],
                "Goal answer changed session/turn")
        row["goal_contract_question"] = {"request_id": goal_question["request_id"],
                    "turn_id": goal_question["turn_id"], "state": goal_closed["state"],
                    "delivery": goal_closed["delivery"], "same_turn_continuation": True}
        question = wait_for(lambda: call("get-task", {"task_id": tid})["task"].get("execution_question"),
                            lambda q: bool(q and q["state"] == "open" and q["request_id"] != goal_question["request_id"]),
                            seconds=40, label="real execution turn question discovery")
        require(question["turn_id"] != goal_question["turn_id"], "execution question did not use a new Goal execution turn")
        row["question_identity"] = {key: question[key] for key in
                                   ("request_id", "session_id", "turn_id", "tool_call_id", "questions_sha256")}
        # Start the two-cycle check at the actual upstream pending observation.
        pending_at = next((entry["at"] for entry in proxy.calls if question["request_id"] in entry.get("request_ids", [])), None)
        require(pending_at is not None, "worker did not read real Plus pending asks")
        inbox = wait_for(lambda: call("attention-list", {}),
                        lambda v: any(item["task_id"] == tid and item["reason_code"] == "execution_question"
                                      for item in v["items"]), seconds=4, label="attention within two polls")
        row["discovery_to_attention_seconds"] = round(time.monotonic() - pending_at, 3)
        require(row["discovery_to_attention_seconds"] <= 4.5, "attention exceeded two polling cycles")
        metadata = next((ask for entry in proxy.calls for ask in entry.get("ask_metadata", [])
                         if ask["requestId"] == question["request_id"]), {})
        created_at = metadata.get("createdAt")
        try:
            registered = datetime.datetime.fromisoformat(created_at.replace("Z", "+00:00")) if created_at else None
            if registered is None or registered.tzinfo is None:
                raise ValueError("missing real Host registration timestamp")
            latency = (datetime.datetime.now(datetime.timezone.utc) - registered).total_seconds()
            require(0 <= latency <= 10, "Host ask registration to attention exceeded ten seconds")
            row["registration_to_attention"] = {"source": "real Plus pi_asks_pending.createdAt",
                                                "registered_at": created_at, "seconds": round(latency, 3)}
        except (ValueError, AttributeError):
            row["registration_to_attention"] = {"status": "NOT RUN", "reason": "Host pending response lacks valid createdAt"}
        require(call("session-view", identity)["session"]["execution_question"]["request_id"] == question["request_id"],
                "session DTO disagrees with task DTO")
        row["question_preview"] = next(item["question_preview"] for item in inbox["items"] if item["task_id"] == tid)
        draft = answers_for(question, ("One", "Unit"))
        payload = {**identity, "request_id": question["request_id"],
                   "questions_sha256": question["questions_sha256"], "answers": draft}
        call("save-execution-answers", {key: value for key, value in payload.items() if key != "run_id"})
        saved = call("get-task", {"task_id": tid})["task"]["execution_question"]
        require(saved["draft"] == draft, "draft not durably saved")
        resolve_baseline = proxy.count("pi_asks_resolve")
        require(resolve_baseline == 1, "saving draft performed external answer")
        creates, prompts = proxy.count("pi_session_create"), proxy.count("pi_agent_prompt")
        direct = backend.MCPClient(connection["url"], connection["token"], timeout=10)
        direct.connect()
        plus_args = {"sessionId": sid, "requestId": question["request_id"],
                     "expectedToolCallId": question["tool_call_id"],
                     "answers": [["Zero"], ["Integration"]], "confirm": True}
        submit_args = {**payload, "operation_id": "rehearsal_" + uuid.uuid4().hex}
        expected_labels = ("One", "Unit")
        if scenario in TERMINAL_SCENARIOS:
            interrupted_ask(args, scenario, row=row, call=call, direct=direct, proxy=proxy,
                            connection=connection, util=util, identity=identity, sid=sid,
                            question=question, draft=draft, plus_args=plus_args, read_job=read_job,
                            read_events=read_events, worktree=started["worktree_path"])
            return row
        if scenario == "engine_off":
            original_job = read_job()
            require(original_job.get("task_id") == tid and original_job.get("run_id") == run_id
                    and original_job.get("session_id") == sid and original_job.get("turn_id") == question["turn_id"]
                    and original_job.get("pid") == worker_pid and not original_job.get("observe_pid"),
                    "engine-off original observer identity changed")
            original_process = owned_worker(worker_pid, spec_path)
            require(original_process is not None, "engine-off original observer already exited")
            os.kill(worker_pid, signal.SIGTERM)  # Observer loss only; never stop the Plus turn.
            wait_for(lambda: util.pid_alive(worker_pid), lambda alive: not alive, seconds=5,
                     label="owned original observer exit")
            absent = wait_for(lambda: call("get-task", {"task_id": tid})["task"]["execution_question"],
                              lambda q: q.get("observer_alive") is False, seconds=5,
                              label="core confirms original observer absence")
            require(absent["request_id"] == question["request_id"] and absent["state"] == "open",
                    "observer exit changed the open question")
            upstream = direct.call_tool("pi_agent_status", {"sessionId": sid})
            upstream_status = upstream.get("status") or upstream
            require(upstream_status.get("currentTurnId") == question["turn_id"]
                    and upstream_status.get("isRunning") is True,
                    "observer exit stopped or replaced the Plus turn")
            pending = direct.call_tool("pi_asks_pending", {"sessionId": sid})
            require(pending.get("sessionId") == sid and any(
                    ask.get("requestId") == question["request_id"]
                    and ask.get("toolCallId") == question["tool_call_id"]
                    and ask.get("turnId") == question["turn_id"] for ask in pending.get("asks", [])),
                    "original Plus question did not remain pending after observer exit")
            row["observer_exit"] = {"process": original_process, "core_observer_alive": False,
                                    "request_id": question["request_id"], "Plus_turn_running": True}
        if args.native_scene:
            ready_file = root / "native-ready.json"
            finish_file = root / "native-finish.json"
            remaining_native = native_deadline - time.monotonic()
            require(remaining_native > 0, "total native acceptance wait budget elapsed")
            write_json(ready_file, {"task_id": tid, "run_id": run_id, "session_id": sid,
                       "question": saved, "db_path": str(paths["KANPILOT_DB"]),
                       "server_file": str(root / "app_support/mcp/server.json"),
                       "environment": {key: value for key, value in env.items()
                                       if key.startswith("KANPILOT_") or key == "KANEO_CREDENTIALS_PATH"},
                       "finish_file": str(finish_file), "expected_answers": ["One", "Unit"],
                       "finish_protocol": {"action": "verify"},
                       "timeout_seconds": round(remaining_native, 3),
                       "total_native_wait_seconds": args.native_wait_seconds})
            print(json.dumps({"native_ready": str(ready_file), "production_access": False}), flush=True)
            wait_for(lambda: finish_file, lambda path: path.is_file(), seconds=remaining_native,
                     interval=0.5, label="caller native acceptance completion")
            require(json.loads(finish_file.read_text())["action"] == "verify", "native acceptance stopped by caller")
            row["native_completion"] = "caller requested verification; UI screenshots remain caller-owned evidence"
        elif scenario in ("plus_first", "fast_completed"):
            row["plus_result"] = direct.call_tool("pi_asks_resolve", plus_args)
            expected_labels = ("Zero", "Integration")
        elif scenario == "race":
            with ThreadPoolExecutor(max_workers=2) as pool:
                core_result = pool.submit(safe_call, call, "submit-execution-answers", submit_args)
                plus_result = pool.submit(direct.call_tool, "pi_asks_resolve", plus_args)
                row["core_result"] = core_result.result(timeout=20)
                row["plus_result"] = plus_result.result(timeout=20)
            core_accepted = row["core_result"]["ok"] and row["core_result"]["result"].get("status") == "accepted"
            plus_accepted = row["plus_result"].get("status") == "answered"
            require(int(core_accepted) + int(plus_accepted) == 1, "race did not yield exactly one accepted answer")
            row["race_accepted_count"] = 1
            expected_labels = None  # Determine the actual winning answer from the file.
        else:
            if scenario == "unknown":
                proxy.drop_response = True
            row["core_result"] = safe_call(call, "submit-execution-answers", submit_args)
            if scenario == "unknown":
                after = call("get-task", {"task_id": tid})["task"]["execution_question"]
                row["after_response_loss"] = {key: after[key] for key in ("state", "delivery", "answered_by")}
                require(after["delivery"] == "unknown", "response loss not recorded unknown")
                require(after["answered_by"] in (None, "unknown"), "unknown delivery attributed to a known actor")
                before = proxy.count("pi_asks_resolve")
                row["reconcile"] = safe_call(call, "reconcile-execution-question", identity)
                # Observe for two cycles; unknown must never cause an automatic resend.
                until = time.monotonic() + 4.1
                while time.monotonic() < until:
                    call("get-task", {"task_id": tid})
                    time.sleep(0.1)
                require(proxy.count("pi_asks_resolve") == before == resolve_baseline + 1, "unknown answer was automatically resent")
                row["automatic_resends"] = 0
        if scenario == "engine_off":
            response = row["core_result"]
            require(response["ok"] and response["result"].get("status") == "accepted",
                    "engine-off answer was not accepted")
            observer = response["result"].get("observer") or {}
            new_pid = observer.get("pid")
            require(observer.get("status") == "observing" and type(new_pid) is int and new_pid != worker_pid,
                    "answer submission did not launch a new attached observer")
            resumed = wait_for(read_job, lambda job: job.get("observe_pid") == new_pid
                               and bool(job.get("observation_resumed_at")), seconds=10,
                               label="answer-triggered observer persistent attachment")
            require(resumed.get("task_id") == tid and resumed.get("run_id") == run_id
                    and resumed.get("session_id") == sid and resumed.get("turn_id") == question["turn_id"],
                    "observer recovery changed execution identity")
            new_process = owned_worker(new_pid, run_dir / "observer-spec.json", resume=True)
            require(new_process is not None, "resumed observer exited before the final gate")
            deduplicated = call("round-observe", {**identity, "executor": "execution-input"})
            require(deduplicated.get("status") == "observing" and deduplicated.get("pid") == new_pid
                    and deduplicated.get("deduplicated") is True,
                    "observer ensure did not reuse the single resumed worker")
            require(proxy.count("pi_asks_resolve") == resolve_baseline + 1
                    and proxy.count("pi_session_create") == creates and proxy.count("pi_agent_prompt") == prompts,
                    "observer recovery replayed an answer, session or prompt")
            engine_status = call("engine-status", {})
            require(not any(instance["active"] for instance in engine_status["instances"]),
                    "engine started during answer-triggered observer recovery")
            row["observer_recovery"] = {"via": "submit-execution-answers", "observer": observer,
                         "process": new_process, "observation_resumed_at": resumed["observation_resumed_at"],
                         "run_id": run_id, "session_id": sid, "turn_id": question["turn_id"],
                         "deduplicated": deduplicated, "engine_inactive": True}
        artifact = Path(started["worktree_path"]) / "execution-answer.txt"
        wait_for(lambda: artifact, lambda p: p.is_file(), seconds=30, label="real answer-dependent tool file")
        text = artifact.read_text(encoding="utf-8")
        core_won = "One" in text and "Unit" in text
        plus_won = "Zero" in text and "Integration" in text
        require(core_won != plus_won, "artifact does not contain exactly one winning answer")
        if expected_labels:
            require(all(label in text for label in expected_labels), "file does not reflect accepted answer")
        row["winning_answer"] = "kaneopilot" if core_won else "plus"
        if scenario == "race":
            require(core_won == core_accepted and plus_won == plus_accepted,
                    "race winning tool result does not match answer-dependent file")
        row["artifact"] = {"path": str(artifact), "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                           "content": text}
        def original_asks_returned(events):
            return all(any(event.get("type") == "tool_result" and
                           event.get("tool_call_id") == ask["tool_call_id"] and
                           event.get("turn_id") == ask["turn_id"] for event in events)
                       for ask in (goal_question, question))
        events = wait_for(read_events, original_asks_returned, seconds=15,
                          label="both original asktool results in real worker transcript")
        events = wait_for(read_events, lambda values: any(event.get("type") == "command_output"
                         and event.get("turn_id") == question["turn_id"] for event in values),
                         seconds=15, label="answer-dependent Bash result in the same turn")
        answer_command = next(event for event in events if event.get("type") == "command_output"
                              and event.get("turn_id") == question["turn_id"])
        require(type(answer_command.get("exit_code")) is int and answer_command["exit_code"] == 0
                and answer_command.get("is_error") is False, "answer-dependent Bash failed")
        row["original_tool_results"] = [{key: event.get(key) for key in
                ("type", "name", "turn_id", "tool_call_id", "source_message_id", "output")}
                for event in events if event.get("tool_call_id") in
                (goal_question["tool_call_id"], question["tool_call_id"]) and event.get("type") == "tool_result"]
        if scenario == "fast_completed":
            execution_result = next(event for event in row["original_tool_results"]
                                    if event["tool_call_id"] == question["tool_call_id"])
            raw_result = json.loads(execution_result["output"])
            require(isinstance(raw_result, dict) and (raw_result.get("details") or {}).get("resolutionKind") == "answered",
                    "fast completed ask lacks durable answered proof")
            row["durable_resolution_kind"] = "answered"
        settled = wait_for(lambda: call("get-task", {"task_id": tid})["task"]["execution_question"],
                           lambda q: q["state"] != "open", seconds=8, label="question closure")
        require(settled["session_id"] == sid and settled["turn_id"] == question["turn_id"], "answer changed session/turn")
        require(settled["state"] == "answered", "execution question did not close as answered")
        require(proxy.count("pi_session_create") == creates == 1, "answer created another session")
        require(proxy.count("pi_agent_prompt") == prompts == 1, "answer sent another prompt")
        require(call("get-task", {"task_id": tid})["task"]["run_id"] == run_id, "answer created another run")
        require(not any(item["task_id"] == tid and item["reason_code"] == "execution_question"
                        for item in call("attention-list", {})["items"]), "answered inbox item remained")
        if scenario in ("plus_first", "fast_completed"):
            require(settled["answered_by"] == "plus", "Plus-first answer source incorrect")
        row["closed"] = {key: settled[key] for key in ("state", "delivery", "answered_by")}
        row["same_turn_continuation"] = True
        row["session_create_count"] = creates
        row["prompt_count"] = prompts
        def verification_events(events):
            matched = []
            for event in events:
                if event.get("type") != "command_output" or event.get("turn_id") != question["turn_id"]:
                    continue
                try:
                    arguments = json.loads(event.get("input") or "{}")
                except (TypeError, ValueError):
                    continue
                if isinstance(arguments, dict) and arguments.get("command") == VERIFY_COMMAND:
                    matched.append(event)
            return matched
        verified_events = wait_for(read_events, lambda events: bool(verification_events(events)),
                                  seconds=30, label="real declared Bash verification before final gate")
        checks = verification_events(verified_events)
        require(len(checks) == 1 and type(checks[0].get("exit_code")) is int
                and checks[0]["exit_code"] == 0 and checks[0].get("is_error") is False,
                "declared Bash verification did not execute exactly once successfully")
        row["verification"] = {"command": VERIFY_COMMAND, "event": checks[0],
                               "plan_sha256": plan.sha256, "before_final_gate": scenario != "fast_completed"}
        if scenario == "fast_completed":
            row["final_gate"] = "DISABLED: regression for immediate completed after Plus answer"
        else:
            row["final_gate_release"] = release_fixture(args, tid, scenario)
        completed_job = wait_for(read_job, lambda job: job.get("status") in ("finished", "failed"),
                                 seconds=45, label="normal real worker terminal settlement")
        require(completed_job.get("status") == "finished" and completed_job.get("exit_code") == 0,
                "real worker did not complete normally: " + str(completed_job.get("detail")))
        require(completed_job.get("session_id") == sid and completed_job.get("turn_id") == question["turn_id"],
                "worker terminal identity changed")
        terminal_events = wait_for(read_events, lambda events: any(event.get("type") == "terminal" for event in events),
                                   seconds=5, label="real worker completed event")
        terminal = next(event for event in reversed(terminal_events) if event.get("type") == "terminal")
        require(terminal.get("outcome") == "completed" and terminal.get("reason") == "completed",
                "worker completion came from stop/error rather than normal execution")
        row["worker_completed"] = {key: completed_job.get(key) for key in
                                    ("status", "exit_code", "session_id", "turn_id", "detail")}
        row["terminal"] = terminal
        def read_report():
            result = safe_call(call, "get-goal-report", identity)
            value = result.get("result") or {}
            row["last_goal_report"] = value if result.get("ok") else result
            require(value.get("state") not in ("corrupt", "failed", "truncated"),
                    "Goal report is " + str(value.get("state")) + ": " + str(value.get("detail")))
            return value
        report = wait_for(read_report, lambda value: value.get("state") == "ready", seconds=30,
                          label="normal structured Goal report")
        require(report["integrity_kind"] == "structured" and report["session_id"] == sid
                and report["turn_id"] == question["turn_id"], "Goal report identity or integrity mismatch")
        row["goal_report"] = {key: report.get(key) for key in
                               ("state", "integrity_kind", "report_id", "report_sha256", "execution_status", "verdict")}
        require(proxy.count("pi_session_create") == creates and proxy.count("pi_agent_prompt") == prompts,
                "normal completion created another session or prompt")
        from kanpilot import auto_monitor
        runner_records = []
        monitor_conn = db.connect(paths["KANPILOT_DB"])
        try:
            monitor_exit = auto_monitor.run_stage("monitor", conn=monitor_conn, db_path=paths["KANPILOT_DB"],
                           timeout=180, log_path=root / "monitor.log",
                           record_run=lambda **record: runner_records.append(record))
        finally:
            monitor_conn.close()
        row["monitor_runner"] = {"exit_code": monitor_exit, "records": runner_records}
        require(monitor_exit == 0, "deterministic monitor runner failed")
        reviewed = call("get-task", {"task_id": tid})["task"]
        require(reviewed["status"] == "in_review" and fake.tasks[tid]["status"] == "in-review",
                "deterministic monitor did not advance local and FakeKaneo to Review")
        require(reviewed["run_id"] == run_id, "monitor changed execution run identity")
        final_session = call("get-session", identity)
        final_detail = final_session["round_detail"]
        require(final_session["session"]["session_id"] == sid and final_detail["turn_id"] == question["turn_id"],
                "monitor changed execution session or turn identity")
        requests = call("list-requests", {})["requests"]
        monitors = [request for request in requests if request.get("task_id") == tid and request["kind"] == "monitor"]
        require(monitors and all(request["status"] == "succeeded" for request in monitors),
                "monitor request was not completed successfully")
        row["monitor"] = {"task_status": reviewed["status"], "remote_status": fake.tasks[tid]["status"],
                          "requests": monitors, "execution_run_id": run_id, "session_id": sid,
                          "turn_id": final_detail["turn_id"]}
        row["review_tick"] = call("tick", {})
        requests = call("list-requests", {})["requests"]
        reviews = [request for request in requests if request.get("task_id") == tid and request["kind"] == "review"]
        require(len(reviews) == 1 and reviews[0]["status"] == "queued" and not reviews[0].get("run_id"),
                "Review was not queued without being claimed")
        row["review_queue"] = reviews[0]
        require(proxy.count("pi_session_create") == creates and proxy.count("pi_agent_prompt") == prompts,
                "monitor or Review queue started another model turn")
        row["passed"] = True
    except (Exception, KeyboardInterrupt) as exc:
        row.update({"passed": False, "error_type": type(exc).__name__, "error_code": getattr(exc, "code", None),
                    "failure": str(exc) if isinstance(exc, AssertionError) else None,
                    "frames": [{"file": frame.filename, "line": frame.lineno, "function": frame.name}
                               for frame in traceback.extract_tb(exc.__traceback__)]})
    finally:
        row["cleanup"] = {}
        if tid and run_id and scenario not in TERMINAL_SCENARIOS + ("fast_completed",) and not row.get("final_gate_release"):
            try:
                row["cleanup"]["final_gate_release"] = release_fixture(args, tid, scenario)
            except Exception as exc:
                row["cleanup"]["final_gate_release"] = {"error_type": type(exc).__name__}
        if tid and run_id and not (row.get("worker_completed") or row.get("worker_failed")):
            row["cleanup"]["stop"] = safe_call(call, "stop-execution", {"task_id": tid, "run_id": run_id,
                                                "operation_id": "cleanup_" + uuid.uuid4().hex})
        if worker_pid and spec_path:
            try:
                worker_specs = {worker_pid: spec_path}
                job_path = spec_path.parent / "job.json"
                if job_path.is_file():
                    job = json.loads(job_path.read_text())
                    if job.get("task_id") == tid and job.get("run_id") == run_id:
                        for key in ("pid", "observe_pid"):
                            if type(job.get(key)) is int:
                                worker_specs[job[key]] = spec_path.parent / "observer-spec.json" if key == "observe_pid" else spec_path
                row["cleanup"]["workers"] = {str(pid): cleanup_worker(pid, path) for pid, path in worker_specs.items()}
            except Exception as exc:
                row["cleanup"]["worker"] = {"error_type": type(exc).__name__}
                row["passed"] = False
        if server and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
        row["proxy_tool_counts"] = {name: proxy.count(name) for name in sorted({item["name"] for item in proxy.calls})}
        proxy.close()
        row["fake_remote_methods"] = sorted({method for method, _path, _body in fake.log})
        fake.stop()
        secrets = [connection["token"]]
        def collect_secrets(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key.lower() in ("token", "access_token", "api_key") and isinstance(child, str):
                        secrets.append(child)
                    else:
                        collect_secrets(child)
            elif isinstance(value, list):
                for child in value:
                    collect_secrets(child)
        credential_json = list((root / "app_support/mcp").rglob("*.json")) + list((root / "credentials").glob("*.json"))
        for path in credential_json:
            try:
                obj = json.loads(path.read_text())
                collect_secrets(obj)
            except (ValueError, OSError):
                pass
        for path in (root / "app_support/mcp").rglob("*.token"):
            secrets.append(path.read_text().strip())
        write_json(root / "summary.json", row)
        try:
            row["token_scan"] = scan_secrets(root, secrets)
        except AssertionError as exc:
            row.update({"passed": False, "token_scan": {"passed": False, "failure": str(exc)}})
        write_json(root / "summary.json", row)
    return row


def main(args):
    args.core_root = args.core_root.resolve()
    connection_path = args.connection_file.resolve()
    fixture_root = (args.caller_fixture_root or connection_path.parent).resolve()
    require(within(connection_path, fixture_root), "caller connection escaped fixture root")
    require(connection_path != (Path.home() / ".pi-desktop-plus/mcp-control.json").resolve(),
            "production Plus connection is forbidden")
    require(not args.connection_file.is_symlink(), "caller connection must not be a symlink")
    connection = json.loads(connection_path.read_text(encoding="utf-8"))
    require(connection.get("active") is True and urlsplit(connection["url"]).hostname == "127.0.0.1",
            "caller must supply active loopback Plus fixture")
    require(args.caller_fixture_root is not None, "explicit caller fixture root is required for control provenance")
    control = urlsplit(args.fixture_control_url)
    require(control.scheme == "http" and control.hostname == "127.0.0.1" and control.port is not None
            and control.path == "/execution-ask/control" and not control.query and not control.fragment,
            "fixture control must be loopback /execution-ask/control")
    manifest_path = fixture_root / "execution-ask-fixture.json"
    require(manifest_path.is_file() and not manifest_path.is_symlink(), "caller fixture manifest missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    require(Path(manifest["connection_file"]).resolve() == connection_path
            and manifest["control_url"] == args.fixture_control_url
            and manifest["provider_id"] == args.provider_id and manifest["model_id"] == args.model_id,
            "caller fixture manifest does not match supplied control/connection/provider/model")
    evidence_root = args.evidence_dir.resolve()
    selected = args.scenarios.split(",")
    require(selected and len(set(selected)) == len(selected) and all(scene in SCENARIOS + TERMINAL_SCENARIOS for scene in selected),
            "invalid or duplicate rehearsal scenarios")
    require("sidecar_restart" not in selected or type(args.fixture_main_pid) is int and args.fixture_main_pid > 1,
            "sidecar_restart requires --fixture-main-pid")
    if "sidecar_restart" in selected:
        fixture_processes(args)  # Read-only preflight before creating any task.
    require(not args.native_scene or selected == ["core_answer"], "native scene requires --scenarios core_answer")
    require(1 <= args.native_wait_seconds <= 1800, "native wait must be bounded to 1..1800 seconds")
    evidence_root.mkdir(parents=True, exist_ok=False)
    source_manifest = {str(path.relative_to(args.core_root)): hashlib.sha256(path.read_bytes()).hexdigest()
                       for path in sorted((args.core_root / "kanpilot").rglob("*.py"))}
    summary = {"core_root": str(args.core_root), "source_base": "d6c8d7e",
               "source_head": git(REPO, "rev-parse", "HEAD"),
               "provider_id": args.provider_id, "model_id": args.model_id,
               "caller_fixture_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
               "core_source_sha256": hashlib.sha256(json.dumps(source_manifest, sort_keys=True).encode()).hexdigest(),
               "core_source_files": source_manifest,
               "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               "production_access": False, "paid_model_calls": False,
               "not_run": {"native_question_card_and_notifications": "requires isolated native UI acceptance",
                           "six_hour_and_max_wait": "requires controlled fake-clock core tests; no wall-clock claim",
                           "scope_expiry_recovery": "no caller-controlled scope clock protocol",
                           "Plus_Host_restart_waiter_loss": "Host/Main remain alive; explicit sidecar_restart only replaces the runtime generation"},
               "scenarios": []}
    original_env = dict(os.environ)
    original_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt()
    for sig in original_handlers:
        signal.signal(sig, interrupted)
    try:
        # Set isolation before importing modules that derive paths from the environment.
        bootstrap = evidence_root / "bootstrap"
        bootstrap.mkdir()
        env, _paths = isolated_env(bootstrap, args.core_root)
        os.environ.clear()
        os.environ.update(env)
        modules = load_modules(args.core_root)
        for scenario in selected:
            row = run_scenario(args, evidence_root, connection, scenario, modules)
            summary["scenarios"].append(row)
            print(json.dumps({"scenario": scenario, "passed": row["passed"],
                              "evidence": str(evidence_root / scenario)}, ensure_ascii=False), flush=True)
            if not row["passed"]:
                break  # No automatic retry of execution or answers.
        summary["passed"] = all(row["passed"] for row in summary["scenarios"])
        return 0 if summary["passed"] else 1
    finally:
        os.environ.clear()
        os.environ.update(original_env)
        for sig, handler in original_handlers.items():
            signal.signal(sig, handler)
        write_json(evidence_root / "summary.json", summary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-root", type=Path, default=REPO / "core")
    parser.add_argument("--connection-file", type=Path, required=True)
    parser.add_argument("--provider-id", required=True)
    parser.add_argument("--model-id", default="execution-ask-fixture")
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--caller-fixture-root", type=Path)
    parser.add_argument("--fixture-control-url", required=True)
    parser.add_argument("--fixture-main-pid", type=int,
                        help="explicit scratch Electron Main PID required only for sidecar_restart")
    parser.add_argument("--scenarios", default=",".join(SCENARIOS))
    parser.add_argument("--native-scene", action="store_true", help="hold execution question for caller's isolated App acceptance")
    parser.add_argument("--native-wait-seconds", type=int, default=900,
                        help="total deadline shared by native startup and UI completion handshakes")
    try:
        raise SystemExit(main(parser.parse_args()))
    except Exception as exc:
        print(json.dumps({"error_type": type(exc).__name__, "error_code": getattr(exc, "code", None),
                          "failure": str(exc) if isinstance(exc, AssertionError) else None}), file=sys.stderr)
        raise SystemExit(1)
