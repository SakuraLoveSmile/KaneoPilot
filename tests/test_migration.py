"""旧状态迁移测试。

覆盖契约条款：protocol §5.12（migrate）；state-model §6（旧状态导入映射）。

legacy manifest schema 为测试约定（契约未冻结文件格式）：
<KANPILOT_LEGACY_ROOT>/{KaneoPlanner,KaneoExecutor,KaneoReviewer}/state/manifest.json，
均为 {"version":1,"tasks":{<task_id>:{...}}}；锁为 KaneoExecutor/locks/<id>/owner.json。
集成时若实现对旧格式的读取口径不同，只需改 conftest.legacy_state 夹具。
"""

import hashlib
import json
from pathlib import Path

import pytest

try:
    from conftest import LEGACY_MANIFEST_IDS as IDS
except ImportError:  # unittest discover 包上下文
    from tests.conftest import LEGACY_MANIFEST_IDS as IDS


def _migrate(cli, tmp_env, **kw):
    payload = {
        "legacy_root": str(tmp_env.legacy_root),
        "plan_root": str(tmp_env.plan_root),
        "backup_root": str(tmp_env.root / "backups"),
    }
    payload.update(kw)
    return cli("migrate", payload)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class TestMigrateBasics:
    def test_backup_created_and_originals_untouched(self, cli, tmp_env,
                                                    legacy_state):
        """先整体备份，原文件不动（§5.12 / state-model §6）。"""
        before = {p: _sha(p) for p in (
            legacy_state.executor_manifest,
            legacy_state.planner_manifest,
            legacy_state.reviewer_manifest,
            legacy_state.lock_owner)}
        out = _migrate(cli, tmp_env)
        assert out["ok"] is True
        backup = Path(out["backup_path"])
        assert backup.exists()
        assert "migration-" in backup.name or "migration" in str(backup)
        # 备份内容含 manifest
        copied = list(backup.rglob("manifest.json"))
        assert len(copied) >= 3, f"备份未包含三 manifest：{list(backup.rglob('*'))}"
        # 原文件不动
        for p, h in before.items():
            assert _sha(p) == h, f"原文件被改动：{p}"

    def test_imported_counts(self, cli, tmp_env, legacy_state):
        out = _migrate(cli, tmp_env)
        imp = out["imported"]
        assert imp["tasks"] == 8           # 八个旧任务全部导入
        assert imp["requests"] >= 2        # running→monitor、in_review→review
        assert imp["events"] >= 8
        assert "warnings" in out

    def test_orphan_plan_warning(self, cli, tmp_env, legacy_state):
        """孤儿计划（无 manifest 对应）→ warnings，不自动建 task。"""
        out = _migrate(cli, tmp_env)
        warns = " ".join(str(w) for w in out["warnings"])
        assert warns.strip(), "应有孤儿计划警告"
        # 孤儿计划的 task_id 不应入库
        tasks = cli("list-tasks", {})["tasks"]
        assert not any(t["task_id"] == "legacyorphan000000aa" for t in tasks)


class TestStatusMapping:
    """Executor manifest status → task.status（state-model §6 映射表）。"""

    @pytest.fixture(autouse=True)
    def _migrated(self, cli, tmp_env, legacy_state):
        self.cli = cli
        self.out = _migrate(cli, tmp_env)
        assert self.out["ok"] is True
        self.factory_get = lambda tid: cli("get-task", {"task_id": tid})["task"]

    def test_ready_mapped(self):
        t = self.factory_get(IDS["ready"])
        assert t["status"] == "ready"
        assert t["send_state"] == "not_sent"
        assert t["plan_path"] and t["plan_sha256"]

    def test_running_gets_monitor_request(self):
        t = self.factory_get(IDS["running"])
        assert t["status"] == "running"
        reqs = self.cli("list-requests", {})["requests"]
        m = [r for r in reqs if r["task_id"] == IDS["running"]
             and r["kind"] == "monitor" and r["status"] == "queued"]
        assert m, "running 任务应自动建 monitor 请求"
        assert m[0]["idempotency_key"] == f"monitor:{IDS['running']}"
        assert m[0]["requested_by"] == "migration"

    def test_needs_user_unknown_preserved(self):
        """needs_user + send_state=unknown 全字段保留，绝不重发。"""
        t = self.factory_get(IDS["unknown"])
        assert t["status"] == "needs_user"
        assert t["send_state"] == "unknown"
        assert t["failure_code"] == "send_unknown"
        # 没有为它建 dispatch 请求（不重发 unknown）
        reqs = self.cli("list-requests", {})["requests"]
        assert not [r for r in reqs if r["task_id"] == IDS["unknown"]
                    and r["kind"] == "dispatch"
                    and r["status"] in ("queued", "claimed")]
        # allowed_actions：三专用键，无 retry
        actions = set(t["allowed_actions"])
        assert {"restore", "accept_duplicate", "mark_sent"} <= actions
        assert "retry" not in actions

    def test_interrupted_dispatch_mapped(self):
        """dispatching/prepared/submitting 残留 → needs_user interrupted。"""
        t = self.factory_get(IDS["dispatching"])
        assert t["status"] == "needs_user"
        assert t["failure_code"] == "interrupted_dispatch"
        assert t["run_id"] == "old-mid-dispatch"  # 证据保留

    def test_in_review_pending_gets_review_request(self):
        t = self.factory_get(IDS["in_review"])
        assert t["status"] == "in_review"
        reqs = self.cli("list-requests", {})["requests"]
        assert any(r["task_id"] == IDS["in_review"] and r["kind"] == "review"
                   and r["status"] == "queued" for r in reqs)

    def test_reviewed_done_mapped(self):
        """Reviewer status=completed 且 kaneo_status=done → done + review_*。"""
        t = self.factory_get(IDS["reviewed_done"])
        assert t["status"] == "done"
        assert t["review_report_path"]
        assert t["review_key"] == "rev-legacy-1"

    def test_execution_failed_mapped(self):
        t = self.factory_get(IDS["failed"])
        assert t["status"] == "needs_user"
        assert t["failure_code"] == "execution_failed"

    def test_done_mapped(self):
        t = self.factory_get(IDS["done"])
        assert t["status"] == "done"

    def test_attempt_history_imported(self):
        """attempt_history[] → attempts 行。"""
        t = self.factory_get(IDS["ready"])
        notes = [a.get("note") for a in t["attempts"]]
        assert "旧失败一" in notes and "旧失败二" in notes

    def test_migrated_event(self):
        evs = self.cli("list-events", {})["events"]
        assert any(e["kind"] == "migrated" for e in evs)


class TestMigrateIdempotent:
    def test_rerun_skips_imported(self, cli, tmp_env, legacy_state):
        """settings.migration 记 manifest 哈希；重跑跳过（§5.12）。"""
        out1 = _migrate(cli, tmp_env)
        assert out1["ok"] is True
        tasks1 = cli("list-tasks", {})["tasks"]
        reqs1 = cli("list-requests", {})["requests"]
        out2 = _migrate(cli, tmp_env)
        assert out2["ok"] is True
        # 重跑不重复导入
        tasks2 = cli("list-tasks", {})["tasks"]
        reqs2 = cli("list-requests", {})["requests"]
        assert len(tasks2) == len(tasks1)
        assert len(reqs2) == len(reqs1)
        # 无重复 task_id（幂等）
        ids = [t["task_id"] for t in tasks2]
        assert len(ids) == len(set(ids))

    def test_dry_run_no_import(self, cli, tmp_env, legacy_state):
        out = _migrate(cli, tmp_env, dry_run=True)
        assert out["ok"] is True
        # dry_run 不落库（若实现语义为预览）
        tasks = cli("list-tasks", {})["tasks"]
        assert len(tasks) == 0, "dry_run 不应导入任务"
