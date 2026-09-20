"""Shared test harness: isolated tempdir roots + in-process CLI calls."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

CORE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CORE))
sys.path.insert(0, str(CORE / "_vendor"))

import yaml  # noqa: E402
from kanpilot import bridge, cli, remote, util  # noqa: E402


class FakeKaneoClient:
    """In-memory Kaneo API stub implementing the endpoints the bridge uses."""

    def __init__(self):
        self.tasks = {}          # task_id -> task dict
        self.columns = [
            {"slug": "to-do"}, {"slug": "in-progress"},
            {"slug": "in-review"}, {"slug": "done"},
        ]
        self._seq = 0
        self.fail_next = None    # set to an exception to fail the next request
        self.requests_log = []

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
        # /api/task/tasks/<project> GET
        if parts[:3] == ["api", "task", "tasks"] and method == "GET":
            project_id = parts[3]
            tasks = [
                dict(task) for task in self.tasks.values()
                if task["projectId"] == project_id
            ]
            return {
                "data": {"columns": [{"tasks": tasks}]},
                "pagination": {"totalPages": 1},
            }
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
        self.reports_root = self.legacy_root / "KaneoReviewer" / "reports"
        self.sources_root = (
            self.legacy_root / "KaneoPlanner" / "state" / "sources"
        )
        for path in (
            self.plan_root, self.repos_root, self.legacy_root,
            self.app_support, self.reports_root, self.sources_root,
        ):
            path.mkdir(parents=True, exist_ok=True)
        self._env = {
            "KANPILOT_PLAN_ROOT": str(self.plan_root),
            "KANPILOT_REPOS_ROOT": str(self.repos_root),
            "KANPILOT_LEGACY_ROOT": str(self.legacy_root),
            "KANPILOT_APP_SUPPORT": str(self.app_support),
            "KANPILOT_REPORTS_ROOT": str(self.reports_root),
        }
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
        project_id="example-project-05",
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
            self.call(
                "publish-plan",
                {
                    "task_id": task_id, "plan_path": str(plan_path),
                    "plan_kind": overrides.get("plan_kind", "standard"),
                    "source_sha256": source_sha,
                    "requirements_sha256": req_sha,
                    "generated_at": "2026-09-20T00:00:00+00:00",
                    "planner_model": overrides.get(
                        "planner_model", "gpt-6-astra"
                    ),
                    "reasoning_effort": overrides.get(
                        "reasoning_effort", "medium"
                    ),
                },
            )
        return {
            "remote": remote_task, "plan_path": plan_path,
            "source_sha256": source_sha, "requirements_sha256": req_sha,
            "source_path": source_path,
        }

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
