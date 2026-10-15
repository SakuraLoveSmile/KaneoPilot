"""schema v1 → v2 升级测试（state-model §1.1）。

覆盖契约条款：state-model §1.1（`schema_meta.version` 1→2：`ALTER TABLE tasks
ADD COLUMN save_state/saved_at/auto_advance_paused` + 新建 `session_reads`；
升级前备份到 `<app_support>/backups/schema-<ts>-v<from>-to-<to>/`，WAL 场景先
checkpoint；旧数据默认 `save_state='active'`、`auto_advance_paused=0`；升级幂等）、
protocol §1（错误信封）、§4.11（`migration_needed` 稳定错误码）。

v1 库由本文件按 v1 冻结结构直接创建（不 import 核心代码），再经任意 CLI 命令
触发 `db.connect()` 内的升级路径；升级前后用 sqlite 只读检查冻结 schema。
"""

import sqlite3

try:
    from conftest import db_exec
except ImportError:  # unittest discover 包上下文
    from tests.conftest import db_exec


# state-model v1.0 冻结结构（= 现 schema 去掉 v1.3 三列与 session_reads）。
V1_SCHEMA = """
CREATE TABLE schema_meta(version INTEGER NOT NULL);

CREATE TABLE tasks(
  task_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  project_name TEXT NOT NULL,
  title TEXT,
  repository TEXT,
  stage TEXT NOT NULL,
  status TEXT NOT NULL,
  kaneo_status TEXT,
  kaneo_checked_at TEXT,
  requirements_sha256 TEXT,
  source_sha256 TEXT,
  source_path TEXT,
  plan_path TEXT, plan_sha256 TEXT,
  plan_kind TEXT,
  plan_generated_at TEXT,
  planner_model TEXT, reasoning_effort TEXT,
  send_state TEXT,
  sent INTEGER,
  run_id TEXT,
  session_marker TEXT, session_id TEXT, session_title TEXT,
  dispatch_lease_expires_at TEXT,
  original_todo_slug TEXT,
  transition_started_at TEXT, transition_verified_at TEXT, sent_at TEXT,
  ui_evidence TEXT, send_evidence TEXT,
  review_report_path TEXT, review_report_sha256 TEXT,
  review_key TEXT, review_source_task_id TEXT, fix_task_id TEXT,
  failure_code TEXT, failure_reason TEXT, failure_stage TEXT,
  blocked_at TEXT, blocked_summary TEXT,
  lock_retained INTEGER DEFAULT 0,
  manual_recovery_required INTEGER DEFAULT 0,
  last_progress TEXT,
  priority INTEGER DEFAULT 0,
  resolution TEXT,
  migrated_from TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  extra_json TEXT
);

CREATE TABLE requests(
  request_id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL,
  task_id TEXT REFERENCES tasks(task_id),
  stage TEXT NOT NULL,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 100,
  not_before TEXT,
  payload_json TEXT,
  requested_by TEXT,
  run_id TEXT, claimed_by TEXT, claimed_at TEXT,
  heartbeat_at TEXT, lease_expires_at TEXT,
  result_json TEXT, finished_at TEXT,
  attempt_no INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX requests_active_key ON requests(idempotency_key)
  WHERE status IN ('queued','claimed');
CREATE INDEX requests_poll ON requests(stage, status, priority, created_at);

CREATE TABLE attempts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL, task_id TEXT, run_id TEXT,
  stage TEXT, kind TEXT,
  started_at TEXT, finished_at TEXT,
  outcome TEXT, send_state TEXT, evidence_json TEXT, note TEXT
);
CREATE INDEX attempts_task ON attempts(task_id, id);

CREATE TABLE events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL, task_id TEXT, request_id TEXT,
  kind TEXT NOT NULL, actor TEXT, detail_json TEXT
);
CREATE INDEX events_task ON events(task_id, id);

CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

V1_TASK_ID = "v1task00000000000000000a"

SESSION_READS_COLUMNS = {
    "session_key", "task_id", "run_id", "cursor", "last_seq",
    "last_revision", "state", "updated_at",
}

# state-model v1.5 冻结结构（protocol §5.16.1）。
EXECUTION_ROUNDS_COLUMNS = {
    "task_id", "run_id", "backend", "session_id", "session_title",
    "run_status", "config_json", "worktree_path", "started_at", "pid",
    "save_state", "saved_at", "created_at", "updated_at",
}

# state-model v1.3（schema v4，protocol §5.22.1）。
PLANNER_INPUTS_COLUMNS = {
    "input_id", "task_id", "input_sha256", "requirements_sha256",
    "source_sha256", "manifest_json", "created_at",
}
# state-model v5（schema v5，protocol §5.25）。
RUNTIME_OPERATIONS_COLUMNS = {
    "scope_id", "operation_id", "harness_id", "tool", "request_sha256",
    "status", "session_id", "turn_id", "proposal_id", "result_json",
    "created_at", "updated_at",
}

def _create_v1_db(path):
    """按 v1 冻结结构建库并写一行任务/请求（升级后必须原样保留）。"""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(V1_SCHEMA)
        conn.execute("INSERT INTO schema_meta(version) VALUES (1)")
        conn.execute(
            "INSERT INTO tasks(task_id, project_id, project_name, title, "
            "stage, status, send_state, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (V1_TASK_ID, "example-project-02", "Library", "v1 旧任务",
             "dispatch", "ready", "not_sent",
             "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"))
        conn.execute(
            "INSERT INTO requests(request_id, idempotency_key, task_id, stage,"
            " kind, status, requested_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("req-v1-1", f"dispatch:{V1_TASK_ID}", V1_TASK_ID, "dispatch",
             "dispatch", "queued", "user",
             "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"))
        conn.commit()
    finally:
        conn.close()


def _table_columns(path, table):
    conn = sqlite3.connect(str(path))
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _schema_version(path):
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("SELECT version FROM schema_meta").fetchone()[0]
    finally:
        conn.close()


def _backup_dirs(tmp_env):
    root = tmp_env.app_support / "backups"
    return sorted(root.iterdir()) if root.exists() else []


# ---------------------------------------------------------------------------
# v1 → v2 升级
# ---------------------------------------------------------------------------


class TestUpgradeV1ToV2:
    def test_fresh_db_is_v2(self, cli, tmp_env):
        """新库直接建为 v6（含 session_reads + execution_rounds +
        planner_inputs + runtime_operations + worktree_slots），无升级备份。"""
        cli("overview", {})
        assert _schema_version(tmp_env.db) == 6
        assert _table_columns(tmp_env.db, "session_reads") == SESSION_READS_COLUMNS
        assert _table_columns(tmp_env.db, "execution_rounds") == EXECUTION_ROUNDS_COLUMNS
        assert _table_columns(tmp_env.db, "planner_inputs") == PLANNER_INPUTS_COLUMNS
        assert _table_columns(tmp_env.db, "runtime_operations") == RUNTIME_OPERATIONS_COLUMNS
        assert _backup_dirs(tmp_env) == [], "新库不应产生升级备份"

    def test_v1_db_requires_migration_then_upgraded_via_migrate(self, cli, tmp_env):
        """v1 库经普通命令拒绝并报 migration_needed；经 migrate 命令升级至 v6。"""
        _create_v1_db(tmp_env.db)
        assert _schema_version(tmp_env.db) == 1
        assert "save_state" not in _table_columns(tmp_env.db, "tasks")

        # 普通命令拒绝隐式升级
        out = cli("overview", {}, expect=1)
        assert out["code"] == "migration_needed"

        # 显式 migrate 触发升级
        mig_out = cli("migrate", {})
        assert mig_out["ok"] is True

        assert _schema_version(tmp_env.db) == 6
        columns = _table_columns(tmp_env.db, "tasks")
        assert {"save_state", "saved_at", "auto_advance_paused"} <= columns
        assert _table_columns(tmp_env.db, "session_reads") == SESSION_READS_COLUMNS
        assert _table_columns(tmp_env.db, "execution_rounds") == EXECUTION_ROUNDS_COLUMNS
        assert _table_columns(tmp_env.db, "planner_inputs") == PLANNER_INPUTS_COLUMNS
        assert _table_columns(tmp_env.db, "runtime_operations") == RUNTIME_OPERATIONS_COLUMNS

    def test_old_rows_get_defaults(self, cli, tmp_env):
        """旧数据默认 save_state='active'、saved_at=NULL、auto_advance_paused=0
        （state-model §1.1）；行业务数据原样保留。"""
        _create_v1_db(tmp_env.db)
        cli("migrate", {})
        rows = db_exec(
            tmp_env.db,
            "SELECT save_state, saved_at, auto_advance_paused, status, title "
            "FROM tasks WHERE task_id=?",
            (V1_TASK_ID,))
        assert len(rows) == 1
        save_state, saved_at, auto_paused, status, title = rows[0]
        assert save_state == "active"
        assert saved_at is None
        assert auto_paused == 0
        assert status == "ready" and title == "v1 旧任务"
        # 旧请求行仍在（升级不改业务数据）
        reqs = db_exec(tmp_env.db, "SELECT status FROM requests")
        assert [r[0] for r in reqs] == ["queued"]

    def test_upgraded_task_reads_as_active(self, cli, tmp_env):
        """升级后的旧任务经 CLI 读取：save_state=active、auto_advance_paused=0，
        仍出现在默认 list-tasks（§5.15.3 缺省 active）。"""
        _create_v1_db(tmp_env.db)
        cli("migrate", {})
        out = cli("list-tasks", {})
        row = next((t for t in out["tasks"] if t["task_id"] == V1_TASK_ID), None)
        assert row is not None, "升级后的旧任务应出现在默认列表"
        assert row["save_state"] == "active"
        assert row["auto_advance_paused"] in (0, False)

    def test_backup_written_before_upgrade(self, cli, tmp_env):
        """升级前逐级备份 DB（v1-to-v2、v2-to-v3、v3-to-v4、v4-to-v5、v5-to-v6，
        state-model §1.1/§1.2/§1.3/§1.4）。"""
        _create_v1_db(tmp_env.db)
        cli("migrate", {})
        dirs = _backup_dirs(tmp_env)
        assert len(dirs) == 5, f"应恰好五个逐级升级备份目录：{dirs}"
        names = [d.name for d in dirs]
        assert all(n.startswith("schema-") for n in names), names
        assert any("v1-to-v2" in n for n in names), names
        assert any("v2-to-v3" in n for n in names), names
        assert any("v3-to-v4" in n for n in names), names
        assert any("v4-to-v5" in n for n in names), names
        assert any("v5-to-v6" in n for n in names), names
        # v1→v2 备份内含升级前的 v1 库（版本仍为 1）
        first = next(d for d in dirs if "v1-to-v2" in d.name)
        copied = list(first.iterdir())
        assert copied, "备份目录不应为空"
        db_copy = next(p for p in copied if p.name == tmp_env.db.name)
        assert _schema_version(db_copy) == 1

    def test_upgrade_idempotent(self, cli, tmp_env):
        """重复命令不重复升级、不重复备份（§1.1 幂等）。"""
        _create_v1_db(tmp_env.db)
        cli("migrate", {})
        cli("overview", {})
        cli("list-tasks", {})
        assert _schema_version(tmp_env.db) == 6
        assert len(_backup_dirs(tmp_env)) == 5, "二次命令不应再新增备份"
        rows = db_exec(tmp_env.db, "SELECT save_state FROM tasks")
        assert [r[0] for r in rows] == ["active"]
class TestSchemaTooNew:
    def test_newer_version_rejected(self, cli, tmp_env):
        """schema 版本高于核心支持 → migration_needed（§4.11），不改库。"""
        conn = sqlite3.connect(str(tmp_env.db))
        try:
            conn.executescript(V1_SCHEMA)
            conn.execute("INSERT INTO schema_meta(version) VALUES (7)")
            conn.commit()
        finally:
            conn.close()
        out = cli("overview", {}, expect=1)
        assert out["code"] == "migration_needed"
        assert out["error"]
        # 库未被改写（仍为过新的 7）
        assert _schema_version(tmp_env.db) == 7


# ---------------------------------------------------------------------------
# unittest discover 收集入口（本机无 pytest；见 tests/unittest_compat.py）
# ---------------------------------------------------------------------------


def load_tests(loader, standard_tests, pattern):
    import sys

    try:
        import unittest_compat
    except ImportError:
        from tests import unittest_compat
    return unittest_compat.collect_module(sys.modules[__name__])
