"""Shared test harness: isolated tempdir roots + in-process CLI calls."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path

CORE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CORE))
sys.path.insert(0, str(CORE / "_vendor"))

import yaml  # noqa: E402
from kanpilot import bridge, cli, constants, db, remote, util  # noqa: E402




def make_pi_fixture(root: Path):
    """pi 隔离夹具（protocol §5.17.2 测试约定，v1.6）：伪造 PI_HOME
    （agent/models.json 含 CloseAI/deepseek-flash 思考等级 minimal/low/medium/xhigh）+
    伪 pi 可执行（--version/auth check 均退出 0）+ agents 目录含
    default.md + 环境接缝变量。"""
    pi_home = root / "pi-home"
    (pi_home / "agent").mkdir(parents=True, exist_ok=True)
    (pi_home / "agent" / "models.json").write_text(
        json.dumps({
            "providers": {
                "CloseAI": {
                    "displayName": "CloseAI",
                    "apiKey": "test-key-placeholder",
                    "models": [{
                        "id": "deepseek-flash",
                        "name": "DeepSeek Flash",
                        "reasoning": True,
                        "thinkingLevelMap": {
                            "minimal": None, "low": None,
                            "medium": None, "xhigh": None,
                        },
                    }],
                },
                # 受限模型夹具（思考等级子集校验用）
                "Restricted": {
                    "apiKey": "x",
                    "models": [{
                        "id": "small", "reasoning": True,
                        "thinkingLevelMap": {"low": None, "medium": None},
                    }],
                },
            }
        }),
        encoding="utf-8",
    )
    agents = root / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "default.md").write_text("# test agent\n", encoding="utf-8")
    pi_bin = root / "pi-bin"
    pi_bin.write_text(
        '#!/bin/sh\n'
        'if [ "$1" = "--version" ]; then echo "0.0.0-test"; exit 0; fi\n'
        'if [ "$1" = "auth" ] && [ "$2" = "check" ]; then exit 0; fi\n'
        'exit 0\n',
        encoding="utf-8",
    )
    pi_bin.chmod(0o755)
    return {
        "KANPILOT_PI_HOME": str(pi_home),
        "KANPILOT_PI_BIN": str(pi_bin),
        "KANPILOT_AGENTS_DIR": str(agents),
        "KANPILOT_PI_FAKE": "finished",
        "KANPILOT_PI_START_WAIT": "5",
    }


def make_codex_fixture(root: Path, with_provider: bool = True,
                       inline_token: bool = True):
    """codex 隔离夹具（v2.1，protocol §5.22.3 服务商解析测试约定）：

    伪造 CODEX_HOME（config.toml + auth.json）与 codex 可执行 stub。
    ``with_provider=True`` 时写入 custom/Sub2API 形态的 model_providers 段；
    ``inline_token=True`` 用 experimental_bearer_token（内联凭据→env 路径），
    False 用 env_key=KANPILOT_TEST_CODEX_KEY（环境引用路径）。返回 env dict。
    token 为假值 ``test-bearer-token-000000``；测试断言它不出现在 argv/输出。
    """
    codex_home = root / "codex-home"
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "auth.json").write_text("{}", encoding="utf-8")
    lines = [
        'model = "gpt-6-astra"',
        'model_reasoning_effort = "medium"',
    ]
    if with_provider:
        lines.append('model_provider = "custom"')
        lines += [
            "",
            "[model_providers.custom]",
            'name = "Sub2API"',
            'base_url = "http://127.0.0.1:9/v1"',
            'wire_api = "responses"',
            "requires_openai_auth = true",
        ]
        if inline_token:
            lines.append('experimental_bearer_token = "test-bearer-token-000000"')
        else:
            lines.append('env_key = "KANPILOT_TEST_CODEX_KEY"')
    (codex_home / "config.toml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    codex_bin = root / "codex-bin"
    codex_bin.write_text(
        "#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo "
        "\"codex-cli 0.157.0\"; fi\nexit 0\n",
        encoding="utf-8",
    )
    codex_bin.chmod(0o755)
    env = {
        "KANPILOT_CODEX_HOME": str(codex_home),
        "KANPILOT_CODEX_BIN": str(codex_bin),
    }
    if with_provider and not inline_token:
        env["KANPILOT_TEST_CODEX_KEY"] = "test-env-key-00000000"
    return env


FAKE_BEARER_TOKEN = "test-bearer-token-000000"


def goal_checks(commands):
    """Plus Goal report ``checks`` for ``commands``: each frozen command passes with exit 0."""
    return [{"id": f"check-{number}", "command": command, "exitCode": 0,
             "result": "passed", "detail": "ok", "evidenceRefs": []}
            for number, command in enumerate(commands, start=1)]


def goal_report_read(checks, *, turn_id="execution-turn", approved=None, state="ready", detail=None,
                     structured=True, execution_status="completed", verdict="met"):
    """``cmd_read.read_goal_report`` result shape for a fake Plus Goal report.

    The floor fields (structured integrity, execution status, verdict) default to a
    completed, met, structured report; tests override one to exercise the S1 floor.
    """
    ready = state == "ready"
    report = ({"sessionId": "session", "proposalId": "proposal", "executionId": "execution",
               "turnId": turn_id, "checks": checks} if ready else None)
    return {"identity": {"approved_turns": list(approved or [turn_id])}, "state": state,
            "detail": detail, "report": report,
            "report_sha256": util.sha256_bytes(util.jdump(report).encode()) if ready else None,
            "report_id": "report-1" if ready else None, "session_id": "session",
            "proposal_id": "proposal", "execution_id": "execution",
            "turn_id": turn_id if ready else None, "ready": ready,
            "structured": ready and structured,
            "execution_status": execution_status if ready else None,
            "verdict": verdict if ready else None}


def simulated_plus_goal_report(conn, task_id, run_id, **_):
    """``cmd_read.read_goal_report`` double: Plus's report mirrors the round's own command results.

    Each frozen verify command gets one check from its last result in the execution
    window, with that result's output as ``detail``. A command without a result has no check.
    """
    from kanpilot import dsh_events, headless_adapter, verify_evidence
    task = db.require_task(conn, task_id)
    commands = (db.task_extra(task).get("plan_verify") or {}).get("commands") or []
    run_dir = headless_adapter.resolve_source(conn).run_dir(task_id, run_id)
    job = json.loads((run_dir / "job.json").read_text())
    events, _error = dsh_events.iter_events(run_dir / dsh_events.EVENT_LOG_NAME)
    messages = dsh_events.to_messages(events)
    selected, _window = verify_evidence.execution_window(messages, job.get("turn_id"))
    rows = verify_evidence.extract_command_results(selected)
    outputs = {message.get("id"): str((message.get("tool") or {}).get("output") or "") for message in messages}
    checks = []
    for number, command in enumerate(commands, start=1):
        found = [row for row in rows if not verify_evidence.match_verification([command], [row])["missing"]]
        if found:
            code = found[-1]["exit_code"]
            checks.append({"id": f"C{number}", "command": command, "exitCode": code,
                           "result": "passed" if code == 0 else "failed",
                           "detail": outputs.get(found[-1]["message_id"], "")})
    return goal_report_read(checks, turn_id=job.get("turn_id"))


def use_simulated_plus_goal_reports(case):
    """Route ``read_goal_report`` through the simulator for one test case (cleanup registered)."""
    from unittest import mock
    from kanpilot import cmd_read
    patcher = mock.patch.object(cmd_read, "read_goal_report", side_effect=simulated_plus_goal_report)
    patcher.start()
    case.addCleanup(patcher.stop)


def path_qs(path):
    """解析 url query 段 → {key: [values]}（fake 路由按 status 过滤用）。"""
    from urllib.parse import parse_qs, urlsplit

    return parse_qs(urlsplit(path).query)


class FakeKaneoClient:
    """In-memory Kaneo API stub implementing the endpoints the bridge uses."""

    def __init__(self):
        self.tasks = {}          # task_id -> task dict
        self.projects = [
            {"id": pid, "name": name}
            for name, pid in constants.PROJECTS.items()
        ]
        self.columns = [
            {"slug": "to-do"}, {"slug": "in-progress"},
            {"slug": "in-review"}, {"slug": "done"},
        ]
        self._seq = 0
        self.fail_next = None    # set to an exception to fail the next request
        self.requests_log = []

    def add_project(self, project_id: str, name: str):
        for p in self.projects:
            if p["id"] == project_id:
                p["name"] = name
                return
        self.projects.append({"id": project_id, "name": name})

    def add_task(self, task_id, project_id, status="to-do", **fields):
        task = {
            "id": task_id,
            "projectId": project_id,
            "title": fields.pop("title", f"task {task_id}"),
            "description": fields.pop("description", ""),
            "status": status,
            "priority": fields.pop("priority", "medium"),
            "customFields": fields.pop("customFields", {}),
            "attachments": fields.pop("attachments", []),
        }
        task.update(fields)
        self.tasks[task_id] = task
        return task

    def request(self, path, method="GET", body=None):
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc
        self.requests_log.append((method, path, body))
        query = path_qs(path)
        path = path.split("?", 1)[0]
        parts = [part for part in path.split("/") if part]
        # /api/task/<id> GET
        if parts[:2] == ["api", "task"] and len(parts) == 3 and method == "GET":
            task = self.tasks[parts[2]]
            return {"data": dict(task)}
        # /api/task/status/<id> PUT
        if parts[:3] == ["api", "task", "status"] and method == "PUT":
            task = self.tasks[parts[3]]
            task["status"] = body["status"]
            return {"data": dict(task)}
        # /api/task/<projectId> POST (create)
        if parts[:2] == ["api", "task"] and len(parts) == 3 and method == "POST":
            self._seq += 1
            task_id = f"fix{self._seq:08d}"
            task = self.add_task(
                task_id, parts[2], status=body["status"],
                title=body["title"], description=body["description"],
                priority=body.get("priority", "high"),
            )
            return {"data": dict(task)}
        # /api/task/tasks/<project>?status=<slug>&page=&limit= GET
        if parts[:3] == ["api", "task", "tasks"] and method == "GET":
            project_id = parts[3]
            status_filter = query.get("status", [None])[0]
            tasks = [
                dict(task) for task in self.tasks.values()
                if task["projectId"] == project_id
                and (status_filter is None
                     or task.get("status") == status_filter)
            ]
            return {
                "data": {"columns": [{"tasks": tasks}]},
                "pagination": {"totalPages": 1},
            }
        # /api/project?workspaceId= GET（fetch-todos 枚举项目）
        if parts[:2] == ["api", "project"] and method == "GET":
            return {"data": [dict(p) for p in self.projects]}
        # /api/column/<project> GET
        if parts[:2] == ["api", "column"] and method == "GET":
            return {"data": [dict(c) for c in self.columns]}
        # /api/comment|task-relation|activity/<id> GET
        if parts[0] == "api" and parts[1] in (
            "comment", "task-relation", "activity"
        ):
            return {"data": []}
        raise bridge.BridgeError(f"fake: unhandled {method} {path}")


class KanpilotTestCase(unittest.TestCase):
    """Base case: tempdir roots, env overrides, in-process CLI, fake remote."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="kanpilot-test-"))
        self.db_path = self.root / "kanpilot.db"
        self.plan_root = self.root / "Plan"
        self.repos_root = self.root / "repos"
        self.legacy_root = self.root / "legacy"
        self.app_support = self.root / "app_support"
        self.runs_root = self.app_support / "runs"
        # v4（§5.24.5）核心默认根已是 <APP_SUPPORT>/reports；这里刻意用
        # legacy 形状 + 显式覆盖，保留「KANPILOT_REPORTS_ROOT 覆盖」接缝。
        self.reports_root = self.legacy_root / "KaneoReviewer" / "reports"
        self.sources_root = (
            self.legacy_root / "KaneoPlanner" / "state" / "sources"
        )
        for path in (
            self.plan_root, self.repos_root, self.legacy_root,
            self.app_support, self.runs_root, self.reports_root, self.sources_root,
        ):
            path.mkdir(parents=True, exist_ok=True)
        self._env = {
            "CLAUDE_CONFIG_DIR": str(self.root / "claude-config"),
            "KANPILOT_PLAN_ROOT": str(self.plan_root),
            "KANPILOT_REPOS_ROOT": str(self.repos_root),
            "KANPILOT_LEGACY_ROOT": str(self.legacy_root),
            "KANPILOT_APP_SUPPORT": str(self.app_support),
            "KANPILOT_REPORTS_ROOT": str(self.reports_root),
        }
        # v1.6：pi 隔离夹具（dispatch 可用性门 §5.17.3 依赖）。
        self._env.update(make_pi_fixture(self.root))
        # v2.1：codex 隔离夹具（custom/Sub2API 形态，T1 服务商解析测试）。
        self._env.update(make_codex_fixture(self.root))
        self._saved = {k: os.environ.get(k) for k in self._env}
        os.environ.update(self._env)
        self.fake = FakeKaneoClient()
        remote.set_client_factory(lambda: self.fake)
        self.addCleanup(self._restore)

    def _restore(self):
        remote.set_client_factory(None)
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    # ------------------------------------------------------------- helpers

    def call(self, command, payload=None):
        result = cli.run(command, payload or {}, self.db_path)
        return result

    def call_main(self, argv, stdin=""):
        """Exercise the real main() entry (arg parsing + JSON IO)."""
        out, err = io.StringIO(), io.StringIO()
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(stdin)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(argv)
        finally:
            sys.stdin = old_stdin
        return code, out.getvalue().strip(), err.getvalue()

    def call_err(self, command, payload=None):
        try:
            self.call(command, payload)
        except Exception as exc:
            return exc
        self.fail(f"{command} should have failed")

    def make_repo(self, project_name):
        path = self.repos_root / project_name
        path.mkdir(parents=True, exist_ok=True)
        # New planning requires a committed Git baseline. These repositories
        # remain wholly inside the test's temporary roots and have no remotes.
        if not (path / ".git").exists():
            for args in (("init", "-b", "main"),
                         ("config", "user.name", "KaneoPilot Fixture"),
                         ("config", "user.email", "fixture@example.invalid"),
                         ("commit", "--allow-empty", "-m", "isolated baseline")):
                subprocess.run(["git", "-C", str(path), *args], check=True,
                               capture_output=True, timeout=10)
        return path

    def make_source_snapshot(self, task):
        """Persist a snapshot shaped like bridge.task_context output."""
        source = bridge.stable(
            {
                "task": dict(task),
                "comments": [],
                "relations": [],
                "activity": [],
            }
        )
        source = bridge.add_hash(source)
        requirements = bridge.requirements_hash(source)
        path = self.sources_root / f"{task['id']}.json"
        path.write_text(json.dumps(source), encoding="utf-8")
        return source["source_sha256"], requirements, str(path)

    def write_plan(self, project_name, task, meta_overrides=None, body="# plan\n"):
        meta = {
            "task_id": task["id"],
            "project_id": task["projectId"],
            "project_name": project_name,
            "repository": str(self.repos_root / project_name),
            "source_sha256": "",
            "requirements_sha256": "",
            "planner_model": "gpt-6-astra",
            "reasoning_effort": "medium",
            "implementation_status": "未开始",
            "generated_at": "2026-09-20T00:00:00+00:00",
        }
        meta.update(meta_overrides or {})
        directory = self.plan_root / project_name
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"plan-{task['id']}.md"
        frontmatter = yaml.safe_dump(meta, allow_unicode=True, sort_keys=True)
        path.write_text(f"---\n{frontmatter}---\n{body}", encoding="utf-8")
        return path

    def make_ready_task(
        self,
        task_id="taskready000000000000001",
        project_name="Feedback",
        project_id="ryryo1reph1xfemqtwkjyiiz",
        remote_status="to-do",
        description="",
        plan_overrides=None,
        publish=True,
    ):
        """Full fixture: remote task + repo + snapshot + plan -> ready."""
        remote_task = self.fake.add_task(
            task_id, project_id, status=remote_status, description=description
        )
        self.make_repo(project_name)
        source_sha, req_sha, source_path = self.make_source_snapshot(remote_task)
        self.sync_task(
            remote_task, project_name=project_name,
            requirements_sha256=req_sha, source_sha256=source_sha,
            source_path=source_path,
        )
        overrides = {"source_sha256": source_sha,
                     "requirements_sha256": req_sha}
        overrides.update(plan_overrides or {})
        plan_path = self.write_plan(project_name, remote_task, overrides)
        if publish:
            self.publish(
                task_id, plan_path,
                plan_kind=overrides.get("plan_kind", "standard"),
                source_sha256=source_sha,
                requirements_sha256=req_sha,
                planner_model=overrides.get("planner_model", "gpt-6-astra"),
                reasoning_effort=overrides.get("reasoning_effort", "medium"),
            )
        return {
            "remote": remote_task, "plan_path": plan_path,
            "source_sha256": source_sha, "requirements_sha256": req_sha,
            "source_path": source_path,
        }

    def make_planner_input(self, task_id, source_sha256=None,
                           requirements_sha256=None, manifest_extra=None):
        """v2：直接登记一条 planner_inputs 行（绕过远端材料下载）。

        返回 {input_id, input_sha256, manifest}；manifest 与目录外材料
        （files/）为空——测试只需登记行让请求 payload 携带 input_id/sha。
        """
        from kanpilot import planner_inputs

        conn = db.connect(self.db_path)
        try:
            input_id = util.new_uuid()
            manifest = {
                "input_id": input_id,
                "task": {"task_id": task_id},
                "requirements_sha256": requirements_sha256,
                "source_sha256": source_sha256,
                "supplement": "",
                "excluded_remote_ids": [],
                "files": [],
                "issues": [],
                "created_at": util.now_iso(),
            }
            manifest.update(manifest_extra or {})
            input_sha = planner_inputs._manifest_sha(manifest)
            with db.write_txn(conn):
                db.insert_planner_input(
                    conn, input_id, task_id, input_sha,
                    requirements_sha256, source_sha256, manifest,
                    util.now_iso(),
                )
        finally:
            conn.close()
        return {"input_id": input_id, "input_sha256": input_sha,
                "manifest": manifest}

    def queue_plan(self, task_id, source_sha256=None,
                   requirements_sha256=None, plan_kind="standard",
                   planner_model="gpt-6-astra", reasoning_effort="medium"):
        """v2：登记材料 + 创建已授权 plan 请求（不经 start-plans 的远端
        重取）。返回 (request_id, input_sha256)。任务须为 needs_plan。"""
        info = self.make_planner_input(
            task_id, source_sha256=source_sha256,
            requirements_sha256=requirements_sha256,
        )
        payload = {
            "manual_authorized": True,
            "input_id": info["input_id"],
            "planner_input_sha256": info["input_sha256"],
            "manifest": info["manifest"],
            "model_config": {"planner_model": planner_model,
                             "reasoning_effort": reasoning_effort},
            "confirmed_at": util.now_iso(),
        }
        if plan_kind == "review_fix":
            task_row = self.task(task_id)
            payload.update({
                "plan_kind": "review_fix",
                "review_key": task_row.get("review_key"),
                "review_source_task_id": task_row.get(
                    "review_source_task_id"),
                "fix_task_id": task_id,
            })
        conn = db.connect(self.db_path)
        try:
            with db.write_txn(conn):
                request, _ = db.create_request(
                    conn, util.now_iso(), "plan", f"plan:{task_id}",
                    task_id=task_id, payload=payload, requested_by="user",
                )
                request_id = request["request_id"]
        finally:
            conn.close()
        return request_id, info["input_sha256"]

    def enqueue_dispatch(self, task_id, expected_plan_sha256=None):
        """v2：「安排执行」——dispatch 排队 + confirm_plan（hold 任务须带
        计划哈希）。expected_plan_sha256 缺省时从任务当前登记取。"""
        if expected_plan_sha256 is None:
            expected_plan_sha256 = self.task(task_id).get("plan_sha256")
        return self.call("enqueue", {
            "task_id": task_id, "kind": "dispatch",
            "confirm_plan": True,
            "expected_plan_sha256": expected_plan_sha256,
        })

    def publish(self, task_id, plan_path, plan_kind="standard",
                source_sha256="", requirements_sha256="",
                planner_model="gpt-6-astra", reasoning_effort="medium",
                generated_at="2026-09-20T00:00:00+00:00"):
        """v2 发布助手：登记材料 → 授权 plan 请求 → claim → publish-plan。

        publish-plan v2 要求 request_id/run_id/planner_input_sha256 且请求
        须为本任务当前 claimed 规划请求；本助手走完整真实链路。
        """
        request_id, input_sha = self.queue_plan(
            task_id, source_sha256=source_sha256,
            requirements_sha256=requirements_sha256, plan_kind=plan_kind,
            planner_model=planner_model, reasoning_effort=reasoning_effort,
        )
        claim = self.call("claim", {
            "stage": "plan", "executor": "test-planner",
            "request_id": request_id,
        })
        assert claim["claimed"], f"plan claim failed: {claim}"
        result = self.call(
            "publish-plan",
            {
                "task_id": task_id, "plan_path": str(plan_path),
                "plan_kind": plan_kind,
                "source_sha256": source_sha256,
                "requirements_sha256": requirements_sha256,
                "generated_at": generated_at,
                "planner_model": planner_model,
                "reasoning_effort": reasoning_effort,
                "request_id": request_id, "run_id": claim["run_id"],
                "planner_input_sha256": input_sha,
            },
        )
        return result

    def valid_evidence(self, task_view, plan_path, **overrides):
        evidence = {
            "session_marker": task_view["session_marker"],
            "repository": task_view["repository"],
            "mode": "Agent", "model": "SWE-2 Max",
            "execution_mode": "Local",
            "attachment_name": Path(plan_path).name,
            "attachment_visible": True,
            "instruction_visible": True,
            "message_visible": False,
            "session_title": None, "session_id": None,
        }
        evidence.update(overrides)
        return evidence

    def sync_task(self, task, **extra):
        payload = {
            "executor": "test",
            "projects": [{
                "project_id": task["projectId"],
                "project_name": extra.pop("project_name", "Feedback"),
                "repository": extra.pop(
                    "repository", str(self.repos_root / "Feedback")
                ),
                "tasks": [{
                    "task_id": task["id"],
                    "title": task.get("title"),
                    "kaneo_status": task.get("status", "to-do"),
                    **extra,
                }],
            }],
        }
        return self.call("sync-tasks", payload)

    def set_task_status(self, task_id, status, **fields):
        import sqlite3
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        assignments = ", ".join(f"{k}=?" for k in ("status", *fields))
        conn.execute(
            f"UPDATE tasks SET {assignments} WHERE task_id=?",
            (status, *fields.values(), task_id),
        )
        conn.commit()
        conn.close()

    def task(self, task_id):
        return self.call("get-task", {"task_id": task_id})["task"]

    def requests(self, **kw):
        return self.call("list-requests", kw)["requests"]
