#!/usr/bin/env python3
"""Fixture-only Mac/Android LAN acceptance scene. No production paths or services.

Run without arguments, keep this process alive, then launch the candidate App
using the emitted env.json and `-executorSchedulingEnabled NO`. The generated
core wrapper rejects executor-starting commands even if scheduling is enabled.
`--self-test` exercises mobile writes through fresh subprocesses and exits.
All roots and fake credentials are generated in a private temporary directory;
no path, endpoint, credential, or environment override arguments are accepted.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPOSITORY = Path(__file__).resolve().parents[1]
CORE = REPOSITORY / "core"
sys.path.insert(0, str(REPOSITORY))
sys.path.insert(0, str(CORE))
sys.path.insert(0, str(CORE / "tests"))

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import cli, db, remote, round_store, util  # noqa: E402
from tests.conftest import FakeKaneo  # noqa: E402
from test_session import pi_event, pi_job, write_json, write_jsonl  # noqa: E402

READY_TITLE = "安卓联合验收：计划确认与长标题阅读检查"
QUESTION_TITLE = "安卓联合验收：回答规划问题"
QUESTION_OPTION = "沿用现有布局"
PROJECT_ID = "fixture-project-feedback"

PLAN = """# 安卓控制台联合验收计划

这是一份隔离夹具中的中文 Markdown，不会交给真实模型执行。

## 目标

- 在手机查看较长标题、段落、列表和代码。
- 阅读计划后确认执行，只产生一条本地排队请求。
- 暂停后已有的模拟会话继续可读。

## 验证

```sh
python3 -c 'print("fixture only")'
```

> 联合验收使用临时数据库和 FakeKaneo，任务、计划与报告均为演示材料。
"""


class MobilePreview:
    def __init__(self):
        self.case = KanpilotTestCase(methodName="runTest")
        self.kaneo = None
        self.saved = {}
        self.environment = {}
        self.tasks = {}

    def _setenv(self, values):
        for key, value in values.items():
            self.saved.setdefault(key, os.environ.get(key))
            os.environ[key] = str(value)
        self.environment.update({key: str(value) for key, value in values.items()})

    def start(self):
        self.case.setUp()
        # macOS tempfile may spell /private/var as the /var symlink alias.
        # Use its canonical root so strict no-symlink report reads stay valid.
        original_root = self.case.root
        canonical_root = original_root.resolve()
        for name in ("root", "db_path", "plan_root", "repos_root", "legacy_root", "app_support", "runs_root", "reports_root", "sources_root"):
            value = getattr(self.case, name)
            setattr(self.case, name, canonical_root / value.relative_to(original_root))
        for key, value in list(self.case._env.items()):
            if str(value).startswith(str(original_root)):
                self.case._env[key] = str(canonical_root) + str(value)[len(str(original_root)):]
        os.environ.update(self.case._env)
        self.root = self.case.root
        self.root.chmod(0o700)
        self.environment.update(self.case._env)
        self._setenv({
            "KANPILOT_DB": self.case.db_path,
            "KANPILOT_SESSION_RUNS_ROOT": self.case.runs_root,
            "KANPILOT_WORKTREES_ROOT": self.root / "worktrees",
            "KANPILOT_PLANNER_INPUTS": self.case.app_support / "planner_inputs",
            "KANEO_CREDENTIALS_PATH": self.root / "credentials.json",
            "PYTHONPATH": CORE,
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        self.case.call("migrate")
        self._ready("mobile-preview-ready", READY_TITLE)
        self._questions()
        self._exceptions()
        self._session("mobile-preview-session", "安卓联合验收：执行会话与工具消息", running=True)
        self._session("mobile-preview-review", "安卓联合验收：当前评审报告", running=False)
        self._review()

        # Transfer precisely the seeded tasks to the reusable HTTP fake. Fresh
        # core workers use only this credential file, never an in-process patch.
        self.kaneo = FakeKaneo()
        self.kaneo.token = secrets.token_urlsafe(32)
        for task in self.case.fake.tasks.values():
            copied = self.kaneo.add_task(task["projectId"], task["title"], task.get("description", ""),
                                        status=task["status"], task_id=task["id"])
            copied.update(task)
        credentials = Path(self.environment["KANEO_CREDENTIALS_PATH"])
        self.kaneo.write_credentials(credentials)
        credentials.chmod(0o600)
        remote.set_client_factory(None)
        with db.connect(self.case.db_path) as connection:
            db.set_setting(connection, "session_runs_root", str(self.case.runs_root))
            db.set_setting(connection, "desktop_queue_config", {"enabled": False})
            db.set_setting(connection, "paused_global", False)
        self._wrapper()
        self.environment_file = self.root / "env.json"
        # An environment-only object is directly consumable by Process/Popen.
        self.environment_file.write_text(json.dumps(self.environment, ensure_ascii=False, indent=2), encoding="utf-8")
        self.environment_file.chmod(0o600)
        self.scene_file = self.root / "scene.json"
        self.scene_file.write_text(json.dumps({"fixture_only": True, "tasks": self.tasks,
                                               "app_arguments": ["-executorSchedulingEnabled", "NO"]},
                                              ensure_ascii=False, indent=2), encoding="utf-8")
        self.scene_file.chmod(0o600)
        return self

    def _ready(self, task_id, title):
        case = self.case
        task = case.fake.add_task(task_id, PROJECT_ID, title=title, description="仅用于安卓与 Mac 联合验收，保留原有布局。")
        case.make_repo("Feedback")
        source, requirements, source_path = case.make_source_snapshot(task)
        case.sync_task(task, requirements_sha256=requirements, source_sha256=source, source_path=source_path)
        plan = case.write_plan("Feedback", task, {"source_sha256": source, "requirements_sha256": requirements}, body=PLAN)
        case.publish(task_id, plan, source_sha256=source, requirements_sha256=requirements)
        self.tasks[task_id] = {"task_id": task_id, "title": title}

    def _questions(self):
        case = self.case
        task_id = "mobile-preview-questions"
        case.fake.add_task(task_id, PROJECT_ID, title=QUESTION_TITLE, description="请先选择界面布局，再继续规划。")
        prepared = case.call("prepare-plan-input", {"task_id": task_id, "local_paths": [], "supplement": "这是联合验收夹具，不启动任何规划执行器。"})
        started = case.call("start-plans", {"items": [{"input_id": prepared["input_id"], "input_sha256": prepared["input_sha256"]}]})["results"][0]
        claimed = case.call("claim", {"stage": "plan", "request_id": started["request_id"], "executor": "mobile-preview-fixture"})
        if not claimed.get("claimed"):
            raise RuntimeError("无法创建隔离规划问题")
        question = {"id": "layout", "title": "手机任务详情采用哪种布局？",
                    "description": "确认后只排队继续规划，不会运行真实模型。",
                    "options": [{"id": "preserve", "label": QUESTION_OPTION, "description": "保留现有信息结构与设计系统"},
                                {"id": "compact", "label": "使用紧凑列表", "description": "减少同屏留白"}],
                    "recommended_option_id": "preserve", "allow_custom": True, "required": True}
        case.call("report", {"request_id": started["request_id"], "run_id": claimed["run_id"], "outcome": "needs_user",
                             "failure_code": "reported", "note": "等待用户回答规划问题",
                             "evidence": {"questions": [question], "plan_markdown": PLAN}})
        self.tasks[task_id] = {"task_id": task_id, "title": QUESTION_TITLE, "option_label": QUESTION_OPTION}

    def _exceptions(self):
        # Seeded before the running session: the monitor claim below may only take this task's request.
        lost = "mobile-preview-lost"
        self._ready(lost, "安卓联合验收：执行端失联")
        self.case.fake.tasks[lost]["status"] = "in-progress"
        self.case.set_task_status(lost, "running", sent=1, send_state="sent", session_marker=f"{lost}/run-1")
        claim = self.case.call("claim", {"stage": "monitor", "executor": "kaneo-monitor"})
        if not claim.get("claimed"):
            raise RuntimeError("无法创建隔离的监控请求")
        self._expire_lease(claim["request_id"])
        self._blocked("mobile-preview-review-blocked", "安卓联合验收：评审受阻", "in-review",
                      failure_code="review_blocked", failure_stage="in_review",
                      failure_reason="演示材料：评审受阻", blocked_summary="演示材料：评审受阻")
        self._blocked("mobile-preview-failed", "安卓联合验收：执行失败", "in-progress",
                      failure_code="execution_failed", failure_stage="running",
                      failure_reason="演示材料：执行失败", blocked_summary="演示材料：执行失败")

    def _blocked(self, task_id, title, remote_status, **fields):
        self._ready(task_id, title)
        self.case.fake.tasks[task_id]["status"] = remote_status
        self.case.set_task_status(task_id, "needs_user", send_state="sent", **fields)

    def _expire_lease(self, request_id):
        connection = db.connect(self.case.db_path)
        try:
            with db.write_txn(connection):
                connection.execute("UPDATE requests SET lease_expires_at=? WHERE request_id=?",
                                   ((util.now_dt() - timedelta(seconds=60)).isoformat(), request_id))
        finally:
            connection.close()

    def _session(self, task_id, title, *, running):
        self._ready(task_id, title)
        case = self.case
        run_id = "preview-running" if running else "preview-reviewed"
        now = util.now_iso()
        session = "fixture-session-" + run_id
        status = "running" if running else "in_review"
        case.fake.tasks[task_id]["status"] = "in-progress" if running else "in-review"
        case.set_task_status(task_id, status, run_id=run_id, sent=1, send_state="sent", session_id=session, session_title=title)
        directory = case.runs_root / task_id / run_id
        job = pi_job(task_id, run_id, backend="pi_desktop", status="running" if running else "finished",
                     started_at=now, ended_at=None if running else now, pid=os.getpid() if running else None,
                     exit_code=None if running else 0, session_id=session, session_title=title,
                     turn_id="fixture-turn", config={"backend": "pi_desktop"})
        write_json(directory / "job.json", job)
        events = [pi_event(1, "session", ts=now, session_id=session, session_title=title, turn_id="fixture-turn"),
                  pi_event(2, "user", ts=now, content="请核对计划和手机上的长标题阅读效果。"),
                  pi_event(3, "assistant_message", ts=now, content="已读取隔离计划。下一步检查布局、工具折叠与会话分页。"),
                  pi_event(4, "tool_call", ts=now, name="read", arguments={"fixture": "示例材料"}),
                  pi_event(5, "tool_result", ts=now, name="read", output="读取完成：这是一段较长的中文工具输出。\n" * 8),
                  pi_event(6, "assistant_message", ts=now, content="工具输出默认折叠，展开后可阅读完整内容。这里没有连接模型服务。")]
        if not running:
            events.append(pi_event(7, "terminal", ts=now, outcome="completed", reason="隔离示例完成"))
        write_jsonl(directory / "events.jsonl", events)
        connection = db.connect(case.db_path)
        try:
            with db.write_txn(connection):
                round_store.upsert_discovered(connection, task_id, run_id, now, backend="pi_desktop",
                    session_id=session, session_title=title, run_status="running" if running else "completed")
                connection.execute("UPDATE execution_rounds SET started_at=?,pid=?,config_json=? WHERE task_id=? AND run_id=?",
                    (now, os.getpid() if running else None, util.jdump(job["config"]), task_id, run_id))
                db.add_event(connection, now, "mobile_preview_seeded", task_id=task_id, actor="fixture", detail={"summary": title})
        finally:
            connection.close()
        self.tasks[task_id]["run_id"] = run_id

    def _review(self):
        task_id = "mobile-preview-review"
        report = self.case.reports_root / task_id / "preview.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("# 当前评审报告\n\n## 结论\n\n通过。该报告仅用于联合验收，不代表真实任务已完成。\n\n"
                          "## 检查记录\n\n- 中文长标题可以换行。\n- 计划与问题正文可以阅读。\n- 工具输出可以展开。\n\n"
                          "## 需要体验\n\n请在安卓深色模式和较大字体下复查上述内容。\n", encoding="utf-8")
        digest = util.sha256_file(report)
        review_key = util.sha256_bytes((task_id + digest).encode())
        connection = db.connect(self.case.db_path)
        try:
            with db.write_txn(connection):
                task = db.require_task(connection, task_id)
                extra = db.task_extra(task)
                extra.update(review_conclusion="pass", review_history=[{"review_key": review_key,
                    "execution_run_id": "preview-reviewed", "report_sha256": digest, "report_path": str(report), "conclusion": "pass"}])
                db.update_task(connection, task_id, util.now_iso(), review_key=review_key,
                               review_report_path=str(report), review_report_sha256=digest, extra_json=extra)
        finally:
            connection.close()

    def _wrapper(self):
        wrapper = self.root / "mobile-preview-core.py"
        allowed = {"overview", "attention-list", "list-tasks", "get-task", "get-plan", "list-events", "list-requests", "get-request",
                   "list-sessions", "get-session", "session-messages", "get-review-report", "engine-status", "executor-status",
                   "mobile-status", "set-mobile-access", "create-mobile-pairing", "revoke-mobile-device", "mobile-serve", "mobile-request",
                   "save-plan-answers", "submit-plan-answers", "set-paused", "enqueue", "get-executor-config", "get-session-source-config",
                   "get-planner-config", "get-project-mappings", "list-models", "get-config", "desktop-queue-status", "desktop-automation-status"}
        content = f'''#!/usr/bin/env python3
# Generated fixture-only boundary; never runs an executor.
import json, os, sys
from pathlib import Path
ROOT = Path({str(self.root)!r})
if len(sys.argv) < 2 or sys.argv[1] not in {allowed!r} or any(arg == "--db" or arg.startswith("--db=") for arg in sys.argv[2:]):
    print(json.dumps({{"error":"隔离夹具禁止启动执行器或改变数据库位置","code":"forbidden"}}, ensure_ascii=False))
    raise SystemExit(1)
for key in list(os.environ):
    if key.startswith(("KANPILOT_", "KANEO_", "PI_")):
        os.environ.pop(key, None)
os.environ.update(json.loads((ROOT / "env.json").read_text(encoding="utf-8")))
sys.path.insert(0, {str(CORE)!r})
from kanpilot import cli
raise SystemExit(cli.main())
'''
        wrapper.write_text(content, encoding="utf-8")
        wrapper.chmod(0o700)
        self.environment["KANPILOT_CORE"] = str(wrapper)

    def command(self, name, payload=None):
        process = subprocess.run([sys.executable, self.environment["KANPILOT_CORE"], name],
                                 input=json.dumps(payload or {}, ensure_ascii=False).encode(),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 env=dict(os.environ, **self.environment), timeout=30)
        if process.returncode:
            raise RuntimeError("隔离子进程命令失败：" + name + " " + process.stdout.decode("utf-8", errors="replace"))
        return json.loads(process.stdout)

    def self_test(self):
        # Real subprocesses (not remote's in-process factory) verify the two LAN
        # write paths without consuming the scene used for manual acceptance.
        self.command("set-mobile-access", {"enabled": True, "bind_host": "127.0.0.1"})
        uri = self.command("create-mobile-pairing")["pairing_uri"]
        raw = parse_qs(urlparse(uri).query)["data"][0]
        pairing = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        phone = self.command("mobile-request", {"kind": "pair", "data": {"pairing_token": pairing["pairing_token"], "device_name": "fixture-self-test"}})
        def mobile(kind, data):
            return self.command("mobile-request", {"kind": kind, "token": phone["device_token"], "data": data})
        ready = mobile("query", {"type": "get-task", "args": {"task_id": "mobile-preview-ready"}})["data"]["task"]
        assert "confirm-dispatch" in ready["allowed_actions"]
        result = mobile("action", {"action": "confirm-dispatch", "operation_id": "fixture-confirm",
            "args": {"task_id": ready["task_id"], "expected_plan_sha256": ready["plan_sha256"]}})
        assert result["status"] == "accepted", result
        question = mobile("query", {"type": "get-task", "args": {"task_id": "mobile-preview-questions"}})["data"]["task"]["plan_clarification"]
        result = mobile("action", {"action": "submit-plan-answers", "operation_id": "fixture-answer",
            "args": {"task_id": "mobile-preview-questions", "clarification_id": question["id"],
                     "answers": [{"question_id": "layout", "option_id": "preserve"}], "supplement": "手机验收回答"}})
        assert result["status"] == "accepted", result
        messages = mobile("query", {"type": "session-messages", "args": {"task_id": "mobile-preview-session", "run_id": "preview-running"}})["data"]
        assert messages["messages"], messages
        review = mobile("query", {"type": "get-task", "args": {"task_id": "mobile-preview-review"}})["data"]["task"]
        report = mobile("query", {"type": "get-review-report", "args": {"task_id": review["task_id"], "review_key": review["review_key"],
                    "expected_execution_run_id": review["run_id"], "expected_report_sha256": review["review_report_sha256"]}})["data"]
        assert "当前评审报告" in report["markdown"]
        rows = {row["task_id"]: row for row in mobile("query", {"type": "list-tasks", "args": {}})["data"]["tasks"]}
        assert rows["mobile-preview-lost"]["exception"]["kind"] == "executor_lost", rows.get("mobile-preview-lost")
        assert rows["mobile-preview-review-blocked"]["exception"]["label"] == "评审受阻", rows.get("mobile-preview-review-blocked")
        assert rows["mobile-preview-failed"]["exception"]["label"] == "执行失败", rows.get("mobile-preview-failed")
        denied = subprocess.run([sys.executable, self.environment["KANPILOT_CORE"], "engine-run"],
                                capture_output=True, env=dict(os.environ, **self.environment), timeout=10)
        assert denied.returncode != 0 and json.loads(denied.stdout)["code"] == "forbidden"
        return {"fixture_only": True, "self_test": "passed", "checks": ["independent subprocess writes", "plan confirmation", "planning answers via HTTP FakeKaneo", "session messages", "current review", "exception samples", "executor blocked"]}

    def close(self):
        if self.kaneo:
            self.kaneo.stop()
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.case.doCleanups()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", help="验证隔离子进程读写后退出并清理")
    arguments = parser.parse_args()
    scene = MobilePreview()
    stopping = threading.Event()
    previous = {}
    try:
        scene.start()
        if arguments.self_test:
            print(json.dumps(scene.self_test(), ensure_ascii=False), flush=True)
            return 0
        for name in (signal.SIGINT, signal.SIGTERM):
            previous[name] = signal.signal(name, lambda signum, frame: stopping.set())
        print(json.dumps({"fixture_only": True, "pid": os.getpid(), "root": str(scene.root), "db": str(scene.case.db_path),
                          "env_file": str(scene.environment_file), "scene_file": str(scene.scene_file),
                          "tasks": scene.tasks, "app_arguments": ["-executorSchedulingEnabled", "NO"],
                          "instruction": "用 env.json 启动隔离候选 App；在设置页启用手机连接。退出候选 App 后 Ctrl-C 清理。"},
                         ensure_ascii=False), flush=True)
        while not stopping.wait(1):
            pass
        return 0
    finally:
        for name, handler in previous.items():
            signal.signal(name, handler)
        scene.close()


if __name__ == "__main__":
    raise SystemExit(main())
