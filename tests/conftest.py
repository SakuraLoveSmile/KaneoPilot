"""KaneoPilot 集成测试共享基座。

覆盖契约条款：protocol §1（调用方式 / 错误信封 / DB 路径解析）、§6（常量）、
state-model §1（冻结 schema——仅用于测试后门时间操控，不用于业务断言）。

## 测试约定（集成时核对，详见 tests/README.md）

- 核心入口：`KANPILOT_CORE_BIN` 环境变量，默认 `<repo>/core/kanpilot.py`。
- 路径覆盖（契约只规定了 `--db`/`KANPILOT_DB`，其余为本套件约定）：
  `KANPILOT_PLAN_ROOT`、`KANPILOT_REPOS_ROOT`、`KANPILOT_APP_SUPPORT`、
  `KANPILOT_LEGACY_ROOT`、`KANEO_CREDENTIALS_PATH`。
- 指纹约定（契约未冻结哈希算法，属集成核对点）：
  - plan_sha256 / report_sha256 / repair_plan_sha256 = sha256(文件原始字节)。
  - source_sha256 = sha256(快照文件原始字节)；快照文件以规范化 JSON 落盘
    （sort_keys + 紧凑分隔符），因此「规范化重算」与「原始字节重算」同值。
  - requirements_sha256 = sha256(canonical_json({"title": t, "description": d}))。
    若核心算法不同，只需改 `requirements_sha256_for` 一处。
- 时间操控后门：`db_exec`/`expire_lease`/`rewind_finished_at` 直接 UPDATE
  SQLite（schema 已由 state-model §1 冻结），用于把租约/not_before/完成时间
  推到过去——CLI 无法表达时间旅行。业务断言一律经 CLI 输出完成。
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

# ---------------------------------------------------------------------------
# 契约常量（protocol §6）
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_BIN = os.environ.get("KANPILOT_CORE_BIN") or str(REPO_ROOT / "core" / "kanpilot.py")

WORKSPACE_ID = "example-record-id"

PROJECTS = {
    "Music": "example-project-01",
    "Library": "example-project-02",
    "StudyTool": "example-project-03",
    "Comic": "example-project-04",
    "Feedback": "example-project-05",
}

# fake kaneo 每个项目默认列（slug → 显示名）；远端状态写使用 slug。
DEFAULT_COLUMNS = [
    ("to-do", "To Do"),
    ("in-progress", "In Progress"),
    ("in-review", "In Review"),
    ("done", "Done"),
]

EXECUTORS = {
    "plan": "kaneo-plan",
    "dispatch": "kaneo-dispatch",
    "monitor": "kaneo-monitor",
    "review": "kaneo-review",
}

ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(\+00:00|Z)$")

LEASE_SECONDS_DEFAULT = 1200


# ---------------------------------------------------------------------------
# 时间与指纹工具
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utcnow().isoformat(timespec="microseconds")


def iso_past(seconds: float) -> str:
    return (utcnow() - timedelta(seconds=seconds)).isoformat(timespec="microseconds")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def assert_iso8601(value: str) -> None:
    assert isinstance(value, str), f"时间字段应为字符串：{value!r}"
    assert ISO_RE.match(value), f"时间字段不符合 ISO 8601 UTC：{value!r}"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def canonical_json(obj) -> str:
    """规范化 JSON：排序键 + 紧凑分隔符，保证「规范化重算」稳定。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def requirements_sha256_for(title: str, description: str) -> str:
    """需求指纹约定。契约未冻结算法，集成时若核心口径不同只需改这里。"""
    return sha256_bytes(
        canonical_json({"title": title, "description": description}).encode("utf-8")
    )


# ---------------------------------------------------------------------------
# CLI 调用（protocol §1）
# ---------------------------------------------------------------------------


def run_cli_raw(args, *, env, stdin=None, timeout=60):
    """最底层调用：不解析输出、不断言退出码。stdin 默认空串（立即 EOF 防阻塞）。"""
    if stdin is None:
        stdin = ""
    return subprocess.run(
        [sys.executable, CORE_BIN, *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


class CLI:
    """`cli(command, payload, db=None, expect=0)`：调核心、解析单个 JSON 对象、断言退出码。"""

    def __init__(self, env):
        self.env = dict(env)

    def __call__(self, command, payload=None, *, db=None, expect=0, use_stdin=False,
                 env_extra=None, timeout=60):
        args = [command]
        # §1：无 --json 时核心从 stdin 读 JSON（可为空对象 {}）——必须喂 stdin 防阻塞
        stdin = None
        if payload is not None:
            if use_stdin:
                stdin = json.dumps(payload)
            else:
                args += ["--json", json.dumps(payload)]
        else:
            stdin = "{}"
        if db is not None:
            args += ["--db", str(db)]
        env = dict(self.env)
        if env_extra:
            env.update(env_extra)
        proc = run_cli_raw(args, env=env, stdin=stdin, timeout=timeout)
        assert proc.returncode == expect, (
            f"kanpilot {command} 退出码 {proc.returncode}（预期 {expect}）\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
        # §1：stdout 必须可解析为单个 JSON 对象
        out = proc.stdout.strip()
        data = json.loads(out) if out else {}
        assert isinstance(data, dict), f"stdout 应为单个 JSON 对象：{out!r}"
        if expect == 0:
            assert "error" not in data, f"成功返回不应含 error：{data}"
        else:
            # §1：失败 = {"error": ..., "code": ...}
            assert "error" in data and "code" in data, (
                f"失败返回缺少 error/code 字段：{data}"
            )
        return data

    def raw(self, args, *, stdin=None, timeout=60):
        return run_cli_raw(list(args), env=self.env, stdin=stdin, timeout=timeout)


# ---------------------------------------------------------------------------
# 临时目录树（tmp_env）
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_env(tmp_path):
    """完整临时目录树：DB/Plan 根/五个项目仓库/应用支持/旧状态根/凭据路径/伪 HOME。"""
    home = tmp_path / "home"
    home.mkdir()
    app_support = tmp_path / "app_support" / "KaneoPilot"
    app_support.mkdir(parents=True)
    # reviewer reports 根（约定：<APP_SUPPORT>/KaneoReviewer/reports，见 README）
    (app_support / "KaneoReviewer" / "reports").mkdir(parents=True)
    (app_support / "sources").mkdir(parents=True)
    plan_root = tmp_path / "Plan"
    plan_root.mkdir()
    repos_root = tmp_path / "repos"
    repos_root.mkdir()
    for name in PROJECTS:
        (repos_root / name).mkdir()
    legacy_root = tmp_path / "legacy" / "Codex"
    for d in ("KaneoPlanner", "KaneoExecutor", "KaneoReviewer"):
        (legacy_root / d).mkdir(parents=True)
    (legacy_root / "KaneoReviewer" / "reports").mkdir(parents=True)
    credentials_path = tmp_path / "credentials" / "credentials.json"
    credentials_path.parent.mkdir(parents=True)
    db = app_support / "kanpilot.db"

    env = dict(os.environ)
    env.update(
        {
            "HOME": str(home),  # 绝不触碰真实 home
            "KANPILOT_DB": str(db),
            "KANPILOT_PLAN_ROOT": str(plan_root),
            "KANPILOT_REPOS_ROOT": str(repos_root),
            "KANPILOT_APP_SUPPORT": str(app_support),
            "KANPILOT_LEGACY_ROOT": str(legacy_root),
            "KANEO_CREDENTIALS_PATH": str(credentials_path),
        }
    )
    return SimpleNamespace(
        root=tmp_path,
        home=home,
        app_support=app_support,
        db=db,
        plan_root=plan_root,
        repos_root=repos_root,
        legacy_root=legacy_root,
        credentials_path=credentials_path,
        sources_dir=app_support / "sources",
        env=env,
    )


@pytest.fixture
def cli(tmp_env):
    return CLI(tmp_env.env)


# ---------------------------------------------------------------------------
# 假 Kaneo 服务（fake_kaneo）
# ---------------------------------------------------------------------------


def _new_task_id() -> str:
    return uuid.uuid4().hex[:24]


class FakeKaneo:
    """http.server 线程 stub。端点见契约要求；行为可编程（改状态/注入错误）。"""

    def __init__(self):
        self.token = "test"
        self.require_auth = True
        self.projects = [
            {"id": pid, "name": name, "workspaceId": WORKSPACE_ID, "slug": name.lower()}
            for name, pid in PROJECTS.items()
        ]
        self.columns = {
            pid: [
                {
                    "id": f"col-{pid}-{slug}",
                    "name": label,
                    "slug": slug,
                    "projectId": pid,
                    "position": i,
                }
                for i, (slug, label) in enumerate(DEFAULT_COLUMNS)
            ]
            for pid in PROJECTS.values()
        }
        self.tasks = {}
        self.comments = {}
        self.relations = {}
        self.activities = {}
        # 错误注入
        self._fail_all = None          # (status, remaining)
        self._fail_routes = {}         # (method, path_prefix) -> [status, remaining]
        self.log = []                  # (method, path, parsed_body)
        self._lock = threading.Lock()

        class Handler(self._make_handler()):
            pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    # -- 编程接口 -----------------------------------------------------------

    def add_task(self, project_id, title, description, status="to-do",
                 priority="medium", task_id=None):
        task_id = task_id or _new_task_id()
        task = {
            "id": task_id,
            "projectId": project_id,
            "title": title,
            "name": title,
            "description": description,
            "status": status,
            "columnId": f"col-{project_id}-{status}",
            "priority": priority,
            "position": len(self.tasks) + 1,
            "dueDate": None,
            "createdAt": iso_now(),
            "updatedAt": iso_now(),
        }
        self.tasks[task_id] = task
        return task

    def get_task(self, task_id):
        return self.tasks.get(task_id)

    def set_status(self, task_id, status):
        self.tasks[task_id]["status"] = status
        self.tasks[task_id]["columnId"] = f"col-{self.tasks[task_id]['projectId']}-{status}"
        self.tasks[task_id]["updatedAt"] = iso_now()

    def set_description(self, task_id, description):
        self.tasks[task_id]["description"] = description
        self.tasks[task_id]["updatedAt"] = iso_now()

    def find_task_by_marker(self, marker):
        return [t for t in self.tasks.values() if marker in (t.get("description") or "")]

    def fail_next(self, status=500, count=1):
        """接下来 count 个请求一律返回 status。"""
        self._fail_all = [status, count]

    def fail_route(self, method, path_prefix, status=500, count=100):
        self._fail_routes[(method.upper(), path_prefix)] = [status, count]

    def clear_failures(self):
        self._fail_all = None
        self._fail_routes.clear()

    def requests_to(self, method, path_prefix):
        return [
            (p, b) for (m, p, b) in self.log
            if m == method.upper() and p.startswith(path_prefix)
        ]

    # -- HTTP 层 -------------------------------------------------------------

    def _make_handler(self):
        outer = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # 静默
                pass

            # helpers
            def _send(self, status, obj):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    return json.loads(raw.decode("utf-8")) if raw else {}
                except json.JSONDecodeError:
                    return {"_raw": raw.decode("utf-8", "replace")}

            def _dispatch(self, method):
                parsed = urlparse(self.path)
                path = parsed.path.rstrip("/") or "/"
                query = parse_qs(parsed.query)
                body = self._body() if method in ("PUT", "POST", "PATCH") else None
                with outer._lock:
                    outer.log.append((method, path, body))
                    # 认证：Bearer <token>
                    if outer.require_auth:
                        auth = self.headers.get("Authorization", "")
                        if auth != f"Bearer {outer.token}":
                            return self._send(401, {"error": "unauthorized"})
                    # 全局错误注入
                    if outer._fail_all is not None:
                        status, remain = outer._fail_all
                        if remain > 0:
                            outer._fail_all[1] = remain - 1
                            if outer._fail_all[1] <= 0:
                                outer._fail_all = None
                            return self._send(status, {"error": f"injected {status}"})
                    # 路由级错误注入
                    for (m, prefix), (status, remain) in list(outer._fail_routes.items()):
                        if m == method and path.startswith(prefix) and remain > 0:
                            outer._fail_routes[(m, prefix)][1] = remain - 1
                            return self._send(status, {"error": f"injected {status}"})
                return self._route(method, path, query, body)

            def _route(self, method, path, query, body):
                seg = [s for s in path.split("/") if s]
                # GET /api/project?workspaceId=
                if method == "GET" and path == "/api/project":
                    ws = (query.get("workspaceId") or [None])[0]
                    return self._send(200, [
                        p for p in outer.projects
                        if ws is None or p["workspaceId"] == ws
                    ])
                # GET /api/task/tasks/<pid>?status=&page=&limit=
                if method == "GET" and len(seg) == 4 and seg[:3] == ["api", "task", "tasks"]:
                    pid = seg[3]
                    tasks = [t for t in outer.tasks.values() if t["projectId"] == pid]
                    status_f = (query.get("status") or [None])[0]
                    if status_f:
                        tasks = [t for t in tasks if t["status"] == status_f]
                    page = int((query.get("page") or ["1"])[0])
                    limit = int((query.get("limit") or ["100"])[0])
                    tasks = tasks[(page - 1) * limit: page * limit]
                    return self._send(200, tasks)
                # PUT /api/task/status/<id>
                if method == "PUT" and len(seg) == 4 and seg[:3] == ["api", "task", "status"]:
                    tid = seg[3]
                    task = outer.tasks.get(tid)
                    if not task:
                        return self._send(404, {"error": "task not found"})
                    body = body or {}
                    new = (body.get("status") or body.get("column") or
                           body.get("toStatus") or body.get("columnId"))
                    if new is None:
                        return self._send(400, {"error": "missing status"})
                    if str(new).startswith("col-"):
                        for c in outer.columns.get(task["projectId"], []):
                            if c["id"] == new:
                                new = c["slug"]
                                break
                    outer.set_status(tid, str(new))
                    return self._send(200, task)
                # POST /api/task/<pid>
                if method == "POST" and len(seg) == 3 and seg[:2] == ["api", "task"]:
                    pid = seg[2]
                    if pid not in outer.columns:
                        return self._send(404, {"error": "project not found"})
                    body = body or {}
                    task = outer.add_task(
                        project_id=pid,
                        title=body.get("title") or body.get("name") or "(untitled)",
                        description=body.get("description") or "",
                        status=body.get("status") or "to-do",
                        priority=body.get("priority") or "medium",
                    )
                    return self._send(201, task)
                # GET /api/task/<id>
                if method == "GET" and len(seg) == 3 and seg[:2] == ["api", "task"]:
                    task = outer.tasks.get(seg[2])
                    if not task:
                        return self._send(404, {"error": "task not found"})
                    return self._send(200, task)
                # GET /api/column/<pid>
                if method == "GET" and len(seg) == 3 and seg[:2] == ["api", "column"]:
                    return self._send(200, outer.columns.get(seg[2], []))
                # GET /api/comment/<id> / /api/task-relation/<id> / /api/activity/<id>
                if method == "GET" and len(seg) == 3 and seg[0] == "api" and seg[1] in (
                        "comment", "task-relation", "activity"):
                    store = {"comment": outer.comments,
                             "task-relation": outer.relations,
                             "activity": outer.activities}[seg[1]]
                    return self._send(200, store.get(seg[2], []))
                return self._send(404, {"error": f"no route {method} {path}"})

            do_GET = lambda s: s._dispatch("GET")
            do_PUT = lambda s: s._dispatch("PUT")
            do_POST = lambda s: s._dispatch("POST")
            do_PATCH = lambda s: s._dispatch("PATCH")
            do_DELETE = lambda s: s._dispatch("DELETE")

        return _Handler

    def write_credentials(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "baseUrl": self.base_url,
            "accessToken": self.token,
            "token": self.token,           # 兼容命名
            "workspaceId": WORKSPACE_ID,
        }), encoding="utf-8")

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def fake_kaneo(tmp_env):
    server = FakeKaneo()
    server.write_credentials(tmp_env.credentials_path)
    yield server
    server.stop()


# ---------------------------------------------------------------------------
# DB 后门（仅时间操控；schema 由 state-model §1 冻结）
# ---------------------------------------------------------------------------


def db_exec(db_path, sql, params=()):
    """测试后门：直接读写冻结 schema。业务断言不得使用。"""
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.fetchall()
    finally:
        conn.close()


def expire_lease(db_path, request_id, seconds=3600):
    """把请求租约推到过去 → 读取层应派生 lost（§4.3）。"""
    db_exec(
        db_path,
        "UPDATE requests SET lease_expires_at=? WHERE request_id=?",
        (iso_past(seconds), request_id),
    )


def rewind_finished_at(db_path, request_id, seconds):
    """把已完成请求的 finished_at 推到过去（用于 monitor 周期间隔测试 §4.8）。"""
    db_exec(
        db_path,
        "UPDATE requests SET finished_at=? WHERE request_id=?",
        (iso_past(seconds), request_id),
    )


def set_setting(db_path, key, value):
    db_exec(
        db_path,
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value) if not isinstance(value, str) else value),
    )


def set_task_send_state(db_path, task_id, send_state, sent=0):
    """构造 send_state=unknown 的 ready 任务（send 冲突守卫 §4.5-6 测试用）。"""
    db_exec(
        db_path,
        "UPDATE tasks SET send_state=?, sent=? WHERE task_id=?",
        (send_state, sent, task_id),
    )


# ---------------------------------------------------------------------------
# 计划 / 快照 / 评审报告 工件
# ---------------------------------------------------------------------------


def write_plan_file(plan_root, *, task_id, project_id, project_name, repository,
                    source_sha256, requirements_sha256, plan_kind="standard",
                    title=None, extra_frontmatter=None, body=None):
    """生成符合 protocol §6 frontmatter 必填项的计划文件。"""
    if plan_kind == "standard":
        planner_model, effort = "gpt-6-astra", "medium"
    else:
        planner_model, effort = "gpt-5.6-sol", "xhigh"
    fm = {
        "task_id": task_id,
        "project_id": project_id,
        "project_name": project_name,
        "repository": str(repository),
        "source_sha256": source_sha256,
        "requirements_sha256": requirements_sha256,
        "planner_model": planner_model,
        "reasoning_effort": effort,
        "implementation_status": "未开始",
        "generated_at": iso_now(),
    }
    if extra_frontmatter:
        fm.update(extra_frontmatter)
    project_dir = Path(plan_root) / project_name
    project_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^\w一-鿿-]+", "-", (title or task_id))[:40].strip("-")
    path = project_dir / f"{slug}_{int(time.time()*1000)}.md"
    lines = ["---"] + [f"{k}: {v}" for k, v in fm.items()] + [
        "---", "", body or f"# {title or task_id}\n\n## 实施步骤\n\n1. 读取需求\n2. 实现\n3. 验证\n"]
    path.write_text("\n".join(lines), encoding="utf-8")
    return SimpleNamespace(path=path, sha256=sha256_file(path), frontmatter=fm)


def write_source_snapshot(sources_dir, *, task_id, project_id, title, description,
                          status="to-do"):
    """以规范化 JSON 落盘来源快照 → source_sha256 = 原始字节哈希 == 规范化哈希。"""
    sources_dir = Path(sources_dir)
    sources_dir.mkdir(parents=True, exist_ok=True)
    req = requirements_sha256_for(title, description)
    snapshot = {
        "id": task_id,
        "projectId": project_id,
        "title": title,
        "description": description,
        "status": status,
        "requirements_sha256": req,
        "fetchedAt": iso_now(),
    }
    path = sources_dir / f"{task_id}.json"
    path.write_text(canonical_json(snapshot), encoding="utf-8")
    return SimpleNamespace(
        path=path, sha256=sha256_file(path), requirements_sha256=req,
        snapshot=snapshot,
    )


def write_review_report(app_support, *, task_id, conclusion="pass", body=None):
    """评审报告写入 <APP_SUPPORT>/KaneoReviewer/reports/<task_id>/<ts>.md。"""
    report_dir = Path(app_support) / "KaneoReviewer" / "reports" / task_id
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{int(time.time()*1000)}.md"
    path.write_text(body or f"# Review {task_id}\n\n结论：{conclusion}\n",
                    encoding="utf-8")
    return SimpleNamespace(path=path, sha256=sha256_file(path))


def ui_evidence(*, session_marker, repository, attachment_name,
                message_visible=False, instruction_visible=True,
                session_title=None, session_id=None, mode="Agent",
                model="SWE-2 Max", execution_mode="Local"):
    """§4.6 发送证据；字段全部来自「UI 观察」。"""
    return {
        "session_marker": session_marker,
        "repository": str(repository),
        "mode": mode,
        "model": model,
        "execution_mode": execution_mode,
        "attachment_name": attachment_name,
        "attachment_visible": True,
        "instruction_visible": instruction_visible,
        "message_visible": message_visible,
        "session_title": session_title,
        "session_id": session_id,
    }


# ---------------------------------------------------------------------------
# TaskFactory：经 CLI 正常流转造各状态任务
# ---------------------------------------------------------------------------


class TaskFactory:
    def __init__(self, cli, env, kaneo):
        self.cli = cli
        self.env = env
        self.kaneo = kaneo

    # -- 基础步骤 -----------------------------------------------------------

    def sync_task(self, project="StudyTool", title="测试任务", description="需求描述",
                  remote_status="to-do", remote=True, project_id=None):
        """远端造任务 + 写快照 + sync-tasks → task needs_plan + plan 请求。"""
        project_id = project_id or PROJECTS[project]
        repository = self.env.repos_root / project
        if remote:
            rt = self.kaneo.add_task(project_id, title, description,
                                     status=remote_status)
            task_id = rt["id"]
        else:
            task_id = _new_task_id()
        snap = write_source_snapshot(
            self.env.sources_dir, task_id=task_id, project_id=project_id,
            title=title, description=description, status=remote_status)
        resp = self.cli("sync-tasks", {
            "executor": EXECUTORS["plan"],
            "projects": [{
                "project_id": project_id,
                "project_name": project,
                "repository": str(repository),
                "tasks": [{
                    "task_id": task_id,
                    "title": title,
                    "kaneo_status": remote_status,
                    "requirements_sha256": snap.requirements_sha256,
                    "source_sha256": snap.sha256,
                    "source_path": str(snap.path),
                }],
            }],
        })
        return SimpleNamespace(
            task_id=task_id, project_id=project_id, project_name=project,
            title=title, description=description, repository=repository,
            source_path=snap.path, source_sha256=snap.sha256,
            requirements_sha256=snap.requirements_sha256, sync=resp,
        )

    def publish(self, task, plan_kind="standard", extra_frontmatter=None):
        plan = write_plan_file(
            self.env.plan_root, task_id=task.task_id,
            project_id=task.project_id, project_name=task.project_name,
            repository=task.repository,
            source_sha256=task.source_sha256,
            requirements_sha256=task.requirements_sha256,
            plan_kind=plan_kind, title=task.title,
            extra_frontmatter=extra_frontmatter)
        resp = self.cli("publish-plan", {
            "task_id": task.task_id,
            "plan_path": str(plan.path),
            "plan_kind": plan_kind,
            "source_sha256": task.source_sha256,
            "requirements_sha256": task.requirements_sha256,
            "generated_at": plan.frontmatter["generated_at"],
            "planner_model": plan.frontmatter["planner_model"],
            "reasoning_effort": plan.frontmatter["reasoning_effort"],
        })
        return SimpleNamespace(plan=plan, resp=resp)

    def claim(self, stage, request_id=None):
        payload = {"stage": stage, "executor": EXECUTORS[stage]}
        if request_id:
            payload["request_id"] = request_id
        return self.cli("claim", payload)

    # -- 组合状态 -----------------------------------------------------------

    def make_ready(self, **kw):
        """needs_plan → planning → publish → ready。"""
        task = self.sync_task(**kw)
        # 认领该 task 的 plan 请求（可能还有其他请求，按 request_id 精确领取）
        req = self.active_request(task.task_id, kind="plan")
        c = self.claim("plan", request_id=req["request_id"])
        assert c["claimed"] is True, f"plan claim 失败：{c}"
        pub = self.publish(task)
        self.cli("report", {"request_id": c["request_id"], "run_id": c["run_id"],
                            "outcome": "succeeded", "note": "plan done"})
        return SimpleNamespace(task=task, plan=pub.plan, publish=pub.resp,
                               plan_request_id=c["request_id"])

    def make_running(self, **kw):
        """ready → dispatch claim → prepare → begin-send → confirm → running。"""
        ready = self.make_ready(**kw)
        task = ready.task
        self.cli("enqueue", {"task_id": task.task_id})
        req = self.find_request(task.task_id, "dispatch")
        c = self.claim("dispatch", request_id=req["request_id"])
        assert c["claimed"] is True, f"dispatch claim 失败：{c}"
        run_id, req_id = c["run_id"], c["request_id"]
        ev = ui_evidence(
            session_marker=c["context"]["session_marker"],
            repository=c["context"]["repository"],
            attachment_name=c["context"]["attachment_name"])
        self.cli("dispatch-prepare",
                 {"task_id": task.task_id, "run_id": run_id, "evidence": ev})
        self.cli("dispatch-begin-send",
                 {"task_id": task.task_id, "run_id": run_id})
        ev2 = dict(ev, message_visible=True,
                   session_title=f"会话 {task.task_id}",
                   session_id=f"sess-{task.task_id[:8]}")
        self.cli("dispatch-confirm",
                 {"task_id": task.task_id, "run_id": run_id, "evidence": ev2})
        return SimpleNamespace(task=task, plan=ready.plan,
                               dispatch_request_id=req_id, dispatch_run_id=run_id,
                               session_marker=c["context"]["session_marker"],
                               evidence=ev2)

    def make_in_review(self, **kw):
        """running → monitor claim → advance-task in-review → report → in_review。"""
        running = self.make_running(**kw)
        task = running.task
        req = self.active_request(task.task_id, kind="monitor")
        c = self.claim("monitor", request_id=req["request_id"])
        assert c["claimed"] is True, f"monitor claim 失败：{c}"
        self.cli("advance-task", {
            "task_id": task.task_id, "run_id": c["run_id"],
            "target": "in-review", "expected_current": "in-progress",
        })
        self.cli("report", {"request_id": c["request_id"], "run_id": c["run_id"],
                            "outcome": "succeeded", "note": "执行完成"})
        return SimpleNamespace(task=task, plan=running.plan,
                               dispatch_request_id=running.dispatch_request_id,
                               monitor_request_id=c["request_id"])

    def make_needs_user(self, send_state="not_sent", **kw):
        """ready → dispatch claim → dispatch-fail → needs_user。"""
        ready = self.make_ready(**kw)
        task = ready.task
        self.cli("enqueue", {"task_id": task.task_id})
        req = self.find_request(task.task_id, "dispatch")
        c = self.claim("dispatch", request_id=req["request_id"])
        assert c["claimed"] is True
        resp = self.cli("dispatch-fail", {
            "task_id": task.task_id, "run_id": c["run_id"],
            "send_state": send_state, "reason": "UI 测试失败",
        })
        return SimpleNamespace(task=task, plan=ready.plan,
                               dispatch_request_id=c["request_id"],
                               dispatch_run_id=c["run_id"], fail=resp)

    # -- 查询 ---------------------------------------------------------------

    def get_task(self, task_id):
        return self.cli("get-task", {"task_id": task_id})["task"]

    def active_request(self, task_id, kind=None):
        t = self.get_task(task_id)
        if t.get("active_request") and (
                kind is None or t["active_request"]["kind"] == kind):
            return t["active_request"]
        # 不依赖 "active" 过滤器的语义（契约未定义其边界），本地过滤
        reqs = self.cli("list-requests", {})["requests"]
        for r in reqs:
            if (r["task_id"] == task_id and r["status"] in ("queued", "claimed")
                    and (kind is None or r["kind"] == kind)):
                return r
        return None

    def find_request(self, task_id, kind, statuses=("queued",)):
        reqs = self.cli("list-requests", {})["requests"]
        for r in reqs:
            if (r["task_id"] == task_id and r["kind"] == kind
                    and r["status"] in statuses):
                return r
        return None


@pytest.fixture
def factory(cli, tmp_env, fake_kaneo):
    return TaskFactory(cli, tmp_env, fake_kaneo)


# ---------------------------------------------------------------------------
# 旧状态夹具（legacy_state）——state-model §6
# ---------------------------------------------------------------------------


LEGACY_MANIFEST_IDS = {
    "ready": "legacytaskready0001aa",
    "running": "legacytaskrunning01aa",
    "unknown": "legacytaskunknown01aa",
    "dispatching": "legacytaskdispatchaa",
    "in_review": "legacytaskinreviewaa",
    "reviewed_done": "legacytaskreviewedaa",
    "failed": "legacytaskfailed01aa",
    "done": "legacytaskdone0001aa",
}


@pytest.fixture
def legacy_state(tmp_env):
    """构造旧三 manifest + 锁 + 旧 Plan 目录样本。schema 为测试约定（集成核对点）。

    覆盖：ready / running / needs_user+unknown / dispatching 残留 /
    in_review 待评审 / reviewer 已结论且远端 done / execution_failed / done /
    attempt_history / 锁 owner.json / 孤儿计划。
    """
    ids = LEGACY_MANIFEST_IDS
    plan_root = tmp_env.plan_root
    repo = tmp_env.repos_root / "StudyTool"
    pid = PROJECTS["StudyTool"]

    def plan_for(tid, title):
        p = write_plan_file(
            plan_root, task_id=tid, project_id=pid, project_name="StudyTool",
            repository=repo, source_sha256="0" * 64,
            requirements_sha256="1" * 64, title=title)
        return p

    plans = {k: plan_for(v, f"旧任务 {k}") for k, v in ids.items()}
    # 孤儿计划：无 manifest 对应 → 应出现在 warnings
    orphan = write_plan_file(
        plan_root, task_id="legacyorphan000000aa", project_id=pid,
        project_name="StudyTool", repository=repo,
        source_sha256="0" * 64, requirements_sha256="1" * 64, title="孤儿计划")

    base = {"project_id": pid, "project_name": "StudyTool", "repository": str(repo)}
    executor_tasks = {
        ids["ready"]: {
            **base, "title": "旧任务 ready", "status": "ready",
            "plan_path": str(plans["ready"].path),
            "plan_sha256": plans["ready"].sha256,
            "requirements_sha256": "1" * 64, "source_sha256": "0" * 64,
            "send_state": "not_sent", "sent": False,
            "attempt_history": [
                {"at": iso_past(7200), "run_id": "old-run-1",
                 "outcome": "failed", "note": "旧失败一"},
                {"at": iso_past(3600), "run_id": "old-run-2",
                 "outcome": "failed", "note": "旧失败二"},
            ],
        },
        ids["running"]: {
            **base, "title": "旧任务 running", "status": "running",
            "plan_path": str(plans["running"].path),
            "plan_sha256": plans["running"].sha256,
            "requirements_sha256": "1" * 64, "source_sha256": "0" * 64,
            "send_state": "sent", "sent": True,
            "run_id": "old-dispatch-run", "session_marker": f"{ids['running']}/old-dispatch-run",
            "kaneo_status": "in-progress",
            "attempt_history": [
                {"at": iso_past(9000), "run_id": "old-run-a",
                 "outcome": "claimed", "note": "旧派发"},
            ],
        },
        ids["unknown"]: {
            **base, "title": "旧任务 unknown", "status": "needs_user",
            "plan_path": str(plans["unknown"].path),
            "plan_sha256": plans["unknown"].sha256,
            "requirements_sha256": "1" * 64, "source_sha256": "0" * 64,
            "send_state": "unknown", "sent": False,
            "run_id": "old-lost-run", "failure_code": "send_unknown",
            "failure_reason": "发送后锁屏，结果不明",
            "lock_retained": True, "manual_recovery_required": True,
        },
        ids["dispatching"]: {
            **base, "title": "旧任务 dispatching", "status": "dispatching",
            "plan_path": str(plans["dispatching"].path),
            "plan_sha256": plans["dispatching"].sha256,
            "requirements_sha256": "1" * 64, "source_sha256": "0" * 64,
            "send_state": "not_sent", "sent": False,
            "run_id": "old-mid-dispatch",
            "ui_evidence": {"note": "半截 UI 证据"},
        },
        ids["in_review"]: {
            **base, "title": "旧任务 in_review", "status": "in_review",
            "plan_path": str(plans["in_review"].path),
            "plan_sha256": plans["in_review"].sha256,
            "requirements_sha256": "1" * 64, "source_sha256": "0" * 64,
            "send_state": "sent", "sent": True, "kaneo_status": "in-review",
        },
        ids["reviewed_done"]: {
            **base, "title": "旧任务 reviewed", "status": "in_review",
            "plan_path": str(plans["reviewed_done"].path),
            "plan_sha256": plans["reviewed_done"].sha256,
            "requirements_sha256": "1" * 64, "source_sha256": "0" * 64,
            "send_state": "sent", "sent": True, "kaneo_status": "done",
        },
        ids["failed"]: {
            **base, "title": "旧任务 failed", "status": "execution_failed",
            "plan_path": str(plans["failed"].path),
            "plan_sha256": plans["failed"].sha256,
            "requirements_sha256": "1" * 64, "source_sha256": "0" * 64,
            "send_state": "sent", "sent": True,
            "failure_reason": "Devin 执行失败",
        },
        ids["done"]: {
            **base, "title": "旧任务 done", "status": "done",
            "plan_path": str(plans["done"].path),
            "plan_sha256": plans["done"].sha256,
            "requirements_sha256": "1" * 64, "source_sha256": "0" * 64,
            "send_state": "sent", "sent": True, "kaneo_status": "done",
        },
    }
    for tid, t in executor_tasks.items():
        t.setdefault("task_id", tid)
        t.setdefault("updated_at", iso_past(3600))

    planner_tasks = {
        tid: {
            "task_id": tid, "plan_path": t["plan_path"],
            "plan_sha256": t["plan_sha256"], "source_path": "",
            "source_sha256": t["source_sha256"],
            "requirements_sha256": t["requirements_sha256"],
            "generated_at": iso_past(86400), "planner_model": "gpt-6-astra",
            "reasoning_effort": "medium", "project_name": "StudyTool",
            "repository": str(repo),
        }
        for tid, t in executor_tasks.items()
    }

    reviewer_report = write_review_report(
        tmp_env.app_support, task_id=ids["reviewed_done"], conclusion="pass")
    reviewer_tasks = {
        ids["reviewed_done"]: {
            "task_id": ids["reviewed_done"], "status": "completed",
            "conclusion": "pass", "report_path": str(reviewer_report.path),
            "report_sha256": reviewer_report.sha256,
            "kaneo_status": "done", "review_key": "rev-legacy-1",
        }
    }

    def dump(app, name, obj):
        d = tmp_env.legacy_root / app
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    dump("KaneoExecutor", "manifest.json",
         {"version": 1, "tasks": executor_tasks})
    dump("KaneoPlanner", "manifest.json",
         {"version": 1, "tasks": planner_tasks})
    dump("KaneoReviewer", "manifest.json",
         {"version": 1, "tasks": reviewer_tasks})

    # 活动锁目录（owner.json）——只记 extra_json.legacy_locks，不复活
    lock_dir = tmp_env.legacy_root / "KaneoExecutor" / "locks" / ids["unknown"]
    lock_dir.mkdir(parents=True)
    (lock_dir / "owner.json").write_text(json.dumps({
        "run_id": "old-lost-run", "pid": 4242, "at": iso_past(86400)}),
        encoding="utf-8")

    return SimpleNamespace(
        ids=ids, plans=plans, orphan_plan=orphan,
        executor_manifest=tmp_env.legacy_root / "KaneoExecutor" / "manifest.json",
        planner_manifest=tmp_env.legacy_root / "KaneoPlanner" / "manifest.json",
        reviewer_manifest=tmp_env.legacy_root / "KaneoReviewer" / "manifest.json",
        lock_owner=lock_dir / "owner.json",
        reviewer_report=reviewer_report,
    )
