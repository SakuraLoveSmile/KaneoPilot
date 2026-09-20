# KaneoPilot 状态模型 v1（冻结）

SQLite 是队列与运行状态的唯一写入来源。所有写经 protocol.md 命令完成；桌面与执行端不得直接改库或文件锁。

## 1. 物理模型

```sql
PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000; PRAGMA foreign_keys=ON;

CREATE TABLE schema_meta(version INTEGER NOT NULL);

CREATE TABLE tasks(
  task_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  project_name TEXT NOT NULL,
  title TEXT,
  repository TEXT,
  stage TEXT NOT NULL,               -- plan|dispatch|monitor|review|done
  status TEXT NOT NULL,              -- §2 生命周期
  kaneo_status TEXT,                 -- 最近观察的远端列 slug
  kaneo_checked_at TEXT,
  requirements_sha256 TEXT,          -- 当前登记的远端需求指纹
  source_sha256 TEXT,
  source_path TEXT,
  plan_path TEXT, plan_sha256 TEXT,
  plan_kind TEXT,                    -- standard|review_fix
  plan_generated_at TEXT,
  planner_model TEXT, reasoning_effort TEXT,
  send_state TEXT,                   -- not_sent|unknown|sent
  sent INTEGER,                      -- 0|1|NULL
  run_id TEXT,                       -- 当前 dispatch run
  session_marker TEXT, session_id TEXT, session_title TEXT,
  dispatch_lease_expires_at TEXT,
  original_todo_slug TEXT,
  transition_started_at TEXT, transition_verified_at TEXT, sent_at TEXT,
  ui_evidence TEXT, send_evidence TEXT,     -- JSON
  review_report_path TEXT, review_report_sha256 TEXT,
  review_key TEXT, review_source_task_id TEXT, fix_task_id TEXT,
  failure_code TEXT, failure_reason TEXT, failure_stage TEXT,
  blocked_at TEXT, blocked_summary TEXT,
  lock_retained INTEGER DEFAULT 0,
  manual_recovery_required INTEGER DEFAULT 0,
  last_progress TEXT,
  priority INTEGER DEFAULT 0,
  resolution TEXT,                   -- 最近一次 resolve 记录 JSON
  migrated_from TEXT,                -- 迁移来源标识（审计）
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  extra_json TEXT                    -- 前向兼容杂项（last_observed、recovery_history 等）
);

CREATE TABLE requests(
  request_id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL,
  task_id TEXT REFERENCES tasks(task_id),
  stage TEXT NOT NULL,
  kind TEXT NOT NULL,                -- plan-scan|plan|dispatch|monitor|review
  status TEXT NOT NULL,              -- queued|claimed|succeeded|failed|cancelled
  priority INTEGER NOT NULL DEFAULT 100,
  not_before TEXT,
  payload_json TEXT,
  requested_by TEXT,                 -- user|scheduler|<executor id>|migration
  run_id TEXT, claimed_by TEXT, claimed_at TEXT,
  heartbeat_at TEXT, lease_expires_at TEXT,
  result_json TEXT, finished_at TEXT,
  attempt_no INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
-- 同一幂等键只允许一个活动请求：重复点击不重复派发
CREATE UNIQUE INDEX requests_active_key ON requests(idempotency_key)
  WHERE status IN ('queued','claimed');
CREATE INDEX requests_poll ON requests(stage, status, priority, created_at);

CREATE TABLE attempts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL, task_id TEXT, run_id TEXT,
  stage TEXT, kind TEXT,
  started_at TEXT, finished_at TEXT,
  outcome TEXT,                      -- claimed|succeeded|failed|needs_user|cancelled
  send_state TEXT, evidence_json TEXT, note TEXT
);
CREATE INDEX attempts_task ON attempts(task_id, id);

CREATE TABLE events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL, task_id TEXT, request_id TEXT,
  kind TEXT NOT NULL, actor TEXT, detail_json TEXT
);
CREATE INDEX events_task ON events(task_id, id);

CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
```

settings 键：`paused_global` `paused_stages`(JSON 数组) `lease_seconds`(默认 1200) `monitor_interval_seconds`(默认 900) `plan_scan_interval_hours`(默认 24) `plan_scan_anchor`(默认每日 09:00 Asia/Shanghai) `last_plan_scan_at` `migration`(JSON) `workspace_id` `dispatch_serial`(派发串行位持有者 run_id，空=空闲)。

## 2. task.status 转换表

| 当前 | 触发 | 目标 |
|---|---|---|
| —（新建） | sync-tasks 新任务 | needs_plan |
| needs_plan | claim(plan) | planning |
| planning | publish-plan | ready |
| planning | report needs_user / resolve | needs_user / needs_plan |
| ready | claim(dispatch) | dispatching |
| dispatching | dispatch-prepare | prepared |
| prepared | dispatch-begin-send | submitting |
| submitting | dispatch-confirm | running |
| dispatching/prepared/submitting | dispatch-fail | needs_user |
| ready/planning | dispatch claim 校验失败 | needs_user(确定性) 或保持原状(remote_unavailable) |
| running | monitor report kaneo_status=in-review（经 advance-task） | in_review |
| running | advance-task done / 远端观察 done | done |
| running | report needs_user / dispatch 类阻塞 | needs_user |
| in_review | advance-task done | done |
| in_review | register-review blocked | needs_user |
| needs_user | resolve retry/restore/accept_duplicate | ready + dispatch 请求 |
| needs_user | resolve mark_sent | running + monitor 请求 |
| needs_user | resolve replan | needs_plan + plan 请求 |
| needs_user | resolve mark_done | done |
| needs_user | resolve dismiss | ready（不建请求） |
| 任意 | sync-tasks kaneo_status=done | done |
| 任意 | sync-tasks requirements 变化（非派发中） | needs_plan |

非法转换一律 `conflict`/`bad_request`，不静默跳过。每次转换写 `events(kind=status_change, detail={from,to,actor})`。

## 3. request 转换

- `enqueue`/tick → `queued`（受 idempotency_key 唯一约束）。
- `claim` 事务 → `claimed`（写 run_id/claimed_by/claimed_at/heartbeat_at/lease_expires_at，attempt_no+1，落 attempt outcome=claimed）。
- `heartbeat` → 仅更新 heartbeat_at/lease_expires_at。
- `report` → `succeeded|failed`（写 result_json/finished_at，attempt 补 outcome）。
- `resolve release_lost` → `cancelled`。
- 读取层：`claimed` 且 `lease_expires_at < now` → 派生 `lost`（执行端失联），库内状态不变。

## 4. 事件 kind（固定枚举）

`task_created` `task_updated` `requirements_changed` `kaneo_observed` `plan_published` `enqueued` `claimed` `heartbeat` `status_change` `dispatch_prepared` `kaneo_transition` `dispatch_sent` `dispatch_failed` `reported` `review_registered` `fix_task_created` `resolved` `paused` `unpaused` `priority_changed` `migrated` `rechecked` `request_cancelled` `lost_detected`(仅展示层概念，不落库——以租约派生为准)。

## 5. 串行位

- **dispatch 串行位**：同一时间至多一个 `claimed` dispatch 请求（§4.2-3）。由 claim 事务内 `SELECT count(*) FROM requests WHERE stage='dispatch' AND status='claimed'` 保证；`settings.dispatch_serial` 冗余记录持有者供诊断。
- **lifecycle 串行**：远端状态写（dispatch-begin-send、advance-task、create-review-fix）在事务内检查 `settings.lifecycle_owner`；同一时刻只允许一个远端写操作。持有时长短，失败必须归还。
- 这些位是**持久操作租约**，不是进程锁；进程退出不代表操作结束，不凭 PID 判断。

## 6. 旧状态导入映射（migrate）

来源：`~/Library/Application Support/Codex` 下 KaneoPlanner/KaneoExecutor/KaneoReviewer's manifest + locks + Plan 目录。

| Executor manifest status | → task.status / 附带 |
|---|---|
| ready / retry_ready | `ready`；保留 plan_*、requirements、send_state |
| dispatching / prepared / submitting | `needs_user`，`failure_code=interrupted_dispatch`，保留证据与 run_id |
| running | `running` + monitor 请求（key `monitor:<task_id>`，requested_by=migration） |
| in_review | `in_review` + review 请求（若 Reviewer 无结论）；有结论则登记 review_* |
| needs_user | `needs_user`，原 failure_*/send_state/lock_retained/manual_recovery_required 全保留——**绝不重发 unknown** |
| execution_failed | `needs_user`，`failure_code=execution_failed` |
| done / completed | `done` |

- Planner manifest → 合并 plan 注册信息、source_path、requirements 基线、evidence 目录引用。
- Reviewer manifest → review_* 字段、fix_task_id/review_key/repair_plan_*、dedupe_key；`status=completed` 且 `kaneo_status=done` → task `done`。
- `attempt_history[]` → attempts 行（outcome=failed/历史）。
- 活动锁目录（owner.json 存在）→ 只把 `lock_retained` 与 owner_run_id 记到 task.extra_json['legacy_locks']，**不**在新库复活文件锁。
- Plan 目录扫描发现的孤儿计划（无 manifest 对应）→ 警告列表，不自动建 task。
- `settings.migration = {at, manifests_sha256, backup_path}`；重复执行读哈希跳过，幂等。
- 迁移前先整体备份（manifests + locks + Plan），原文件保留供审计；新旧流程不得同时写两套状态——切换步骤见 automation-contract.md。
