# KaneoPilot 状态模型 v1.1（冻结）

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
  extra_json TEXT,                   -- 前向兼容杂项（last_observed、recovery_history 等）
  save_state TEXT NOT NULL DEFAULT 'active',   -- v1.3: active|archived|trashed; v2: +purged（终态，§5.20）
  saved_at TEXT,                     -- v1.3: 归档/移入回收站时间
  auto_advance_paused INTEGER NOT NULL DEFAULT 0  -- v1.3: 恢复后暂停自动推进
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

-- v1.3: 会话读取位点（只读适配层的本地游标，断线补齐用）。
-- 消息正文以本机运行记录（events.jsonl）为准，本地只记录读取位点，不缓存全文。
CREATE TABLE IF NOT EXISTS session_reads(
  session_key TEXT PRIMARY KEY,     -- "<task_id>/<run_id>"（= session_marker）
  task_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  cursor TEXT,                      -- 最后读取游标
  last_seq INTEGER,                 -- 最后消息序号
  last_revision INTEGER,            -- 最后消息修订版本
  state TEXT NOT NULL DEFAULT 'pending',  -- associated|pending|unavailable
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS session_reads_task ON session_reads(task_id, run_id);

-- v1.5（protocol.md §5.16）：执行轮次索引。task_id+run_id 唯一定位一个执行
-- 轮次；会话保存状态（save_state）与任务保存状态正交；删除会话只改本表，
-- 不删文件、不停进程、不改任务生命周期。
CREATE TABLE IF NOT EXISTS execution_rounds(
  task_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  backend TEXT NOT NULL DEFAULT 'headless',   -- pi|codex|pi_desktop（历史行可为 dsh|headless，读取已弃用；v3 起任务执行轮次可为 pi_desktop，protocol §5.23.3）
  session_id TEXT,
  session_title TEXT,
  run_status TEXT NOT NULL DEFAULT 'unknown', -- pending|running|finished|failed|unknown
  config_json TEXT,                           -- claim 时固化的执行配置快照（pi 四字段或 pi_desktop 五字段，protocol §5.23.3）
  worktree_path TEXT,                         -- 该轮独立 worktree 绝对路径
  started_at TEXT,                            -- 启动意图落盘时间（非空即禁止再创建进程）
  pid INTEGER,                                -- worker 进程号（诊断用，不作存活依据）
  save_state TEXT NOT NULL DEFAULT 'active',  -- active|trashed|purged（v2 终态）
  saved_at TEXT,                              -- 移入回收站时间
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (task_id, run_id)
);
CREATE INDEX IF NOT EXISTS execution_rounds_save ON execution_rounds(save_state, updated_at);
```

settings 键：`paused_global` `paused_stages`(JSON 数组) `lease_seconds`(默认 1200) `monitor_interval_seconds`(默认 900) `plan_scan_interval_hours`(默认 24) `plan_scan_anchor`(默认每日 09:00 Asia/Shanghai) `last_plan_scan_at` `migration`(JSON) `workspace_id` `dispatch_serial`(派发串行位持有者 run_id，空=空闲) `session_poll_seconds`(默认 2，订阅不可用时的增量检查间隔) `planner_inputs_root`（v2：规划材料根目录，缺省 `<app_support>/planner_inputs`） `codex_timeout_seconds`（v2：Codex 规划单轮上限，缺省回退 `executor_timeout_seconds`=1800）。

v1.5 新增键：`exec_config`(JSON，protocol.md §5.17 执行配置；v1.6 缺省=初始默认 `CloseAI/deepseek-flash/xhigh/default`；v3 起允许 `backend`/`pi_desktop` 扩展字段，protocol §5.23.3——缺 `backend` 按 `pi` 解释)、`worktrees_root`(独立 worktree 根目录路径字符串；空=未设置，取 `<app_support>/worktrees`)、`dsh_round_timeout_seconds`(默认 14400)。v1.6 新增键：`pi_round_timeout_seconds`(默认 14400，缺省回退读旧键 dsh_round_timeout_seconds)、`executor_timeout_seconds`(默认 1800，执行端单轮上限)、`executor_runs`(JSON，四 stage 最近一次运行记录 pid/started_at/log/exit_code/finished_at)、`planner_config`(JSON，v2.1 Codex 规划配置 {standard, review_fix} 各含 planner_model/reasoning_effort + updated_at；v3 起加 `default_method` ∈ builtin|spec_plan，缺省 builtin，protocol §5.23.2；独立于 exec_config，仅影响新规划请求)。v3 新增键：`review_config`(JSON，protocol §5.23.4 Review 环境 {mode:"inherit"|"custom", provider?, model?, reasoning_effort?, updated_at}；未保存/inherit 保持现状=继承本机 PI 默认会话配置)。**不写入任何 pi 全局配置（`~/.pi/agent/*` 等）**；凭据只查 `pi auth check` 退出码或 PI-Desktop 连接文件 token 的内存就绪性，不存值。

## 1.1 升级与备份（v1.3）

- `schema_meta.version` 1 → 2：`ALTER TABLE tasks ADD COLUMN save_state/saved_at/auto_advance_paused` + 新建 `session_reads`。
- **升级前备份**：复制 DB 文件到 `<app_support>/backups/schema-<ts>-v<from>-to-<to>/`（WAL 场景先 checkpoint）；备份失败 → 中止升级，报 `internal`。
- 旧数据默认 `save_state='active'`、`auto_advance_paused=0`；升级幂等（已存在列则跳过）。

## 1.2 升级与回填（v1.5，执行轮次索引）

- `schema_meta.version` 2 → 3：新建 `execution_rounds`（CREATE TABLE IF NOT EXISTS，幂等）+ 设置键默认值补种；升级前同样整库备份。
- **历史回填**：不迁移数据行；由读取层（`list-sessions`/`get-session`/`trash-session`/`restore-session`）按 runs 根目录 `job.json` 身份核验后补入（protocol.md §5.16.1）。回填 upsert **永不改写已存在行的 `save_state`/`saved_at`**——重复导入不得复活已删除会话；`save_state='trashed'` 的行在重复回填后保持 trashed。
- 已有 `tasks.run_id`/`session_marker` 历史轮次无索引行时按 `job.json` 的 `backend` 回填（旧值保留原样；v1.6 起读取层对非 `pi` 一律返回 unavailable「旧后端会话已弃用」）。

-- v2（protocol.md §5.22）：手动规划输入的不可变材料登记。源快照、补充说明、
-- 附件 manifest 按 input_id 落盘为 planner_inputs/<input_id>/，本表只登记索引
-- 与哈希；请求 payload 存 manifest 副本，不新增附件表。
CREATE TABLE IF NOT EXISTS planner_inputs(
  input_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  input_sha256 TEXT NOT NULL,          -- 对规范化 manifest 重算的输入指纹
  requirements_sha256 TEXT,            -- 本轮确认的远端需求基线
  source_sha256 TEXT,                  -- 本轮来源快照指纹
  manifest_json TEXT NOT NULL,         -- {files:[{name,source,media_type,sha256,size}],supplement,excluded_remote_ids,task:{...}}
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS planner_inputs_task ON planner_inputs(task_id, created_at);
```

### 1.3 升级与回填（v2，schema v4）

- `schema_meta.version` 3 → 4：新建 `planner_inputs`（幂等）+ `tasks.save_state`/`execution_rounds.save_state` 词表扩为含 `purged`（无 DDL，仅读取层词表）+ 设置键默认值补种；升级前整库备份（同 §1.1）。
- 历史数据不迁移、不重写：旧 `queued` plan 请求保持未授权（`payload.manual_authorized` 缺失即未授权，等待确认材料）；purged 不作用于任何既有行。
- 升级幂等；失败升级不推进版本。旧版核心打开 v4 库：schema 版本检查拒绝（version 4 > 其支持上限）。
- **版本保护**：schema v4 为语义兼容边界——旧版应用会把 `purged` 当可恢复记录（不识别该值），因此不得与新版同时访问同一库；切换由主 agent 按 automation-contract §4 执行。

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

## 2.1 保存状态转换（save_state，v1.3，与生命周期正交）

| 当前 | 命令 | 目标 |
|---|---|---|
| active | archive-task | archived（取消 queued 请求；进行中/评审/规划/派发/失联未解除/发送不明 → `conflict`） |
| active | trash-task | trashed（同上前置） |
| archived/trashed | restore-task | active（`auto_advance_paused=1`，除非 `resume_auto=true`） |
| trashed | purge-task / purge-session | purged（普通入口不可恢复，历史与文件保留；任务显式重新接入例外如下） |
| archived/trashed/purged 任务 | reconnect-task（用户显式确认） | active + needs_plan/plan（保留旧轮次保存状态；自动推进暂停；契约见 protocol 手动重新接入章节） |
| 任意（幂等） | archive/trash/restore 到同一状态 | 原状态（`deduplicated: true`） |
| purged | purge-task / purge-session | purged（`deduplicated: true`） |

除显式 `reconnect-task` 同时将任务转为 needs_plan/plan 并记录 `task_reconnected` 外，`save_state` 不改写 `status`/`stage`，不产生 `status_change` 事件；每次真实转换写 `task_archived`/`task_trashed`/`task_restored` 事件（actor、原因）。

## 2.2 看板归列（board_column / lifecycle_column / exception，v1.3）

- `board_column` 是读取层派生（同 display_status 同一计算点），不落库、不改转换表。规则见 protocol.md §5.15.4：**异常统一进第六列 `failed`**（needs_user、评审未通过·待修复、失联），正常列只放正常卡片；`lifecycle_column` 保留纯生命周期位置（needs_user 按 `failure_stage` 归列）。
- `exception` 为结构化异常信息（kind/label/severity/detail/failure_code/fix_task_id），无异常为 null；桌面按 `board_column` 分列、`board_substatus`/`exception.label` 标子状态、`exception.severity` 选红/橙徽标。
- 观测异常（`observation_blocked`）在监控恢复（monitor report succeeded）时自动清除并回 `running`；发送不明/人工阻塞/失联只经 resolve 流程解除，会话日志不解除异常。
- 归档/回收站任务不进看板：`list-tasks` 默认 `save_state='active'`；`sync-tasks` 跳过非 active 任务（防重导入）。

## 3. request 转换

- `enqueue`/tick → `queued`（受 idempotency_key 唯一约束）。
- `claim` 事务 → `claimed`（写 run_id/claimed_by/claimed_at/heartbeat_at/lease_expires_at，attempt_no+1，落 attempt outcome=claimed）。
- `heartbeat` → 仅更新 heartbeat_at/lease_expires_at。
- `report` → `succeeded|failed`（写 result_json/finished_at，attempt 补 outcome）。
- `resolve release_lost` → `cancelled`。
- 读取层：`claimed` 且 `lease_expires_at < now` → 派生 `lost`（执行端失联），库内状态不变。

## 4. 事件 kind（固定枚举）

`task_created` `task_updated` `requirements_changed` `kaneo_observed` `plan_published` `enqueued` `claimed` `heartbeat` `status_change` `dispatch_prepared` `kaneo_transition` `dispatch_sent` `dispatch_failed` `reported` `review_registered` `fix_task_created` `resolved` `paused` `unpaused` `priority_changed` `migrated` `rechecked` `request_cancelled` `task_archived` `task_trashed` `task_restored` `session_trashed`(v1.5 会话移入回收站) `session_restored`(v1.5 会话恢复) `round_started`(v1.5 轮次启动意图落盘) `round_start_failed`（轮次启动失败） `round_observe`(v3 观察者续接) `exec_config_updated`(执行配置保存) `planner_config_updated`(v2.1 规划配置保存) `review_config_updated`(v3 Review 环境保存) `task_reconnected`（用户显式重新接入待规划） `task_purged` `session_purged`（v2 永久从列表移除） `plan_input_prepared` `plan_started`（v2 手动规划） `plan_ready` `plan_needs_input`（v2 Codex 结果） `goal_report_evaluated`(v3 Goal 报告完成判定留痕) `lost_detected`(仅展示层概念，不落库——以租约派生为准)。

本清单必须与 `core/kanpilot/derive.py` 的 `_EVENT_KIND_CN` 以及桌面端
`Labels.eventKind` 逐项一致；三者不一致时以核心**实际写入**的 kind 为准，并由
`core/tests/test_event_kind_labels.py` 钉住。

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

### T3 Plus R1 补充（2026-09-26）

本次无表结构或 schema 版本变化。pi_desktop 继续复用现有轮次、快照和 send_state。能力复核使用该轮 config_json；未知发送或停止未证实保留 unknown，不因配置切换重派。Plus 字符串消息 ID/修订仅用于事件与会话投影，不把它当 SQLite 行序或分页坐标。

### T6 补充（2026-09-30）

T6 不新增表、不改 schema 版本：只在现有 `settings` JSON 键上扩展字段——`planner_config` 增 `backend` + `pi_desktop` 块，`exec_config` 增 `controller_backend`，`review_config` 增 `backend` + `pi_desktop` 块。缺字段一律按旧语义解释（`codex`/`pi`），**零迁移**。

- `execution_rounds.config_json.backend` 仍只解释**任务执行**后端；控制端 `pi_desktop` 不得伪装成执行轮次进入轮次索引——控制轮以 `executor_runs[stage].invocation_id` 标识，产物落 `<APP_SUPPORT>/executor-sessions/<stage>/<invocation_id>/`。
- 规划请求 `payload_json` 增 `planner_backend` 与 `runtime_config`（五字段**脱敏**快照，无 token）；旧请求缺字段按 `codex` 读，不受后来改设置影响。
- `register-review` 的身份字段 `request_id`/`expected_execution_run_id` 只存在于请求 payload 与校验路径，不改表结构；`review_report_path` 仍由核心重算哈希。
- 历史轮次、历史请求与既有 settings 一律按原语义读取；禁止为开发夹具重写历史或对正式库做迁移。

### T1/T2 补充（2026-10-02 项目仓库配置与 UI 简化）

本次无表结构或 schema 版本变化（保持 v5）：
- **settings.project_mappings**：保存项目与本地仓库映射 `{version: 1, revision: <int>, workspace_id: "<id>", items: {<project_id>: {project_name, repository, enabled, updated_at}}}`。
- **快照固化**：`planner_inputs.manifest.task` 与 `requests.payload_json.project_snapshot` 固化 `project_id`, `project_name`, `repository`, `project_mapping_revision`，保证派发和执行对后续设置修改具备不可变性隔离。
- **计划与运行记录路径**：新计划文件写入 `<plan_root>/projects/<project_id>/`；默认运行记录根动态解析 `constants.app_support()/"runs"`。


## 全自动流水线 v5 增量

以 protocol §5.27 为权威契约，schema 不升版。in_review → needs_plan 为同任务修复轮；needs_user 可经 resume 回到 running/in_review/needs_plan，accept_verification 仅回到 running；PR 任务 in_review → done 只在 PR 已合并或 Kaneo 已 done 的被动同步时成立。新增事件和 failure_stage 映射见 §5.27.13；嵌套 automation/delivery 对象在同事务整体读改写，不能覆盖其他 extra。

## Desktop 正式队列补充（2026-10-08）

Desktop不另建任务状态机：plan/review正式requests仍使用queued→claimed→succeeded/failed/lost，只有领到且回报心跳才算开始。Desktop队列暂停只停止新的claim，不代表原生自动任务已暂停。领取收据、后端配置、执行轮次和计划哈希固化于请求；设置变更不重写旧请求。Desktop Review独立run索引不替换task的执行run_id。

规划ready沿publish-plan进入ready并保持执行hold；needs_input沿needs_user。Review沿既有三结论及修复流程，手工解除失联仍为明确边界。native配置存在、mock通过与真实Desktop领取/结算分别记证据。


### 执行失败的手动恢复（2026-10-09）

当前执行轮次经核验失败、任务 needs_user/execution_failed、stage=monitor（兼容 failure_stage=running）时，用户确认 resume 并携带匹配的 expected_execution_run_id，可沿原确认计划转为 ready/dispatch 并排队新请求。原 failed 轮次及改动保留；本轮发送字段和会话指针清除，新轮次由执行端领取后建立。资格、远端需求与状态、计划和工作树指纹不匹配或有活动请求时拒绝恢复，不改变原失败任务。


### verification_unconfirmed 恢复

当前活跃 needs_user/monitor/verification_unconfirmed 经用户 reverify 且本轮全部可信验证通过时，转回 running、排队同一执行轮次的 monitor；不创建新执行轮次、不修改原计划、工作树或配置，不写人工验证 override。真实失败、不完整或身份变化保持原阻塞。
# 规划问题及答案保存（2026-10-09）

需要补充材料的规划请求结束后，任务保留 `needs_user`，问题绑定原 request/run 与材料版本。
答案草稿经核心存储到 SQLite，不启动规划。完整提交原子校验当前问题和任务资格，登记用户决定，
保留原请求/草稿/材料，创建新的规划输入及请求，并转入规划队列；不复活旧领取或写入 harness 会话。
持久化提交 operation_id 和内容指纹，防止重复排队及同 ID 异内容重放。
原需求、材料或问题版本变化时拒绝旧答案；错误不解除阻塞、不丢失已保存草稿。
新的 ready 计划继续使用现有执行确认门控。此功能无数据库破坏性迁移。

## 新任务分支工作区（2026-10-09）

首次规划登记 `extra.delivery.workspace_version=1`、mode=manual 与固定任务 branch/worktree/base/head；
自动化 PR 任务继续沿用已有登记。创建和 Git 校验在 SQLite 写事务外执行，状态只经核心登记。
补充回答和同任务修复保留登记，不创建第二任务分支；旧请求无标记按其原快照处理。
执行结束且验证通过后，核心按当前执行 run 做幂等本地提交并更新 head_sha，随后才允许分支模式 Review。
Review 固定基线与已登记提交，工作区未提交、身份不符或 SHA 漂移均不能登记 pass。
本地提交不改变手动任务为自动 PR 交付，不新增推送、合并或远端发布授权。

## Review blocked 处置（2026-10-09）

只对当前有效 needs_user/review_blocked、已登记 blocked 报告和结束的执行轮次提供处置。
显式准备修复冻结失败 Review 请求、报告/计划哈希及执行成果，登记独立 review_remediation provenance，
保持人工接管和执行确认闸；原报告/计划/轮次留存，状态转 needs_plan，无模型调用或自动执行。
历史 detached 成果显式恢复到核心拥有的专用分支，保留原树，未提交成果完整进入新规划输入。
原远端 in-review 仅在该次有明确恢复授权和身份核验时允许重新规划/派发，不能泛化放宽其他任务。
补充证据后在同执行轮次排队新 Review，保留原 blocked 记录；用户证据标注来源且不可写为验证通过。
失效报告、活动请求、材料漂移或创建/校验失败拒绝，不解除阻塞、不丢用户成果。

### 独立验证检查回执（2026-10-10）

`verification_unconfirmed` 的显式检查重跑不产生新模型执行轮次。核心保存操作归属与状态；检查运行时原任务保持阻塞并拒绝竞争写入。失败/未知/中断均不清除阻塞。全部检查独立通过且任务、计划、执行、工作树身份未漂移后，原子登记可信回执，清除阻塞并重新排队 monitor。回执不能跨计划或执行轮次复用，也不能替代原计划原生验收或 Review。
 
 ## 7. phase × condition 状态模型
 
 | phase | condition | 说明 | 典型来源 |
 |---|---|---|---|
 | intake | idle | 无自动化任务待接收 | scheduler |
 | intake | pending | 已触发自动接收，等待 fetch/prepare/plan | auto_intake |
 | plan | wanted | 需求已变或首次规划，等待材料确认 | sync-tasks / review_fix |
 | plan | drafting | 规划请求已 claimed，runner 制作计划 | planner_runner |
 | plan | ready | 计划已发布，等待执行确认 | publish-plan |
 | dispatch | confirmed | 已确认执行，等待派发 | confirm_dispatch |
 | dispatch | submitting | 远端已转 in-progress，等待发送确认 | dispatch-begin-send |
 | observe | running | 已确认发送，worker 执行中 | dispatch-confirm |
 | observe | observing | 观察者缺席，只读续接中 | round-observe |
 | verdict | checking | 执行终态已到，等待验证证据 | monitor |
 | verdict | unconfirmed | 验证缺失或上次失败 | verification_unconfirmed |
 | commit | committing | PR 交付任务等待提交工作树 | delivery-commit |
 | review | reviewing | 等待评审结论 | review request |
 | review | repairing | 同任务修复轮次中（≤2 轮） | register-review changes_requested |
 | delivery | syncing | PR 已开，等待合并同步 | delivery-sync |
 | done | merged | PR 已合并或 Kaneo 已 done | merge-sync / advance-task |
 | needs-you | blocked | 人工阻塞，等待用户动作 | resolve / attention-list |
 
 **phase 与 task.status 的映射关系：**
 - `needs_plan` → phase=plan, condition=wanted
 - `planning` → phase=plan, condition=drafting
 - `ready` → phase=plan, condition=ready
 - `dispatching/prepared/submitting` → phase=dispatch, condition=submitting/confirmed
 - `running` → phase=observe, condition=running
 - `in_review` → phase=review, condition=reviewing/repairing
 - `needs_user` → phase=needs-you, condition=blocked（按 failure_code 细分）
 - `done` → phase=done, condition=merged
 
 ## 8. 失败阶梯
 
 | 阶梯 | 类型 | 处置 | 自动重试 | 升级 |
 |---|---|---|---|---|
 | 1 | transient（瞬时） | 退避重试一次（指数退避，上限 2 次/阶段） | ✅ 同阶段 | 仍失败 → 阶梯 2 |
 | 2 | environmental（环境） | 等待人工环境修复，不升级失败 | ❌ 不自动重试 | 用户修复环境后手动 resume |
 | 3 | quality（质量） | 有界补救，≤2 轮同分支修复 | ✅ 同分支 fix_round+1 | 超出 → 阶梯 4 |
 | 4 | needs-you（需人工） | 收件箱单一来源，用户决定 | ❌ 不自动 | 用户 resolve 后回到对应 phase |
 
 **阶梯规则：**
 - 瞬时失败（network timeout、git_transient、remote_unavailable）归入阶梯 1，按退避重试。
 - live 核心验证的 `verification_environment_failure` 属环境阶梯：环境修复后用户 resume 只恢复原轮 monitor，不重新 dispatch，不增加 execution_rounds 或 engine_fix_rounds。
 - 环境失败（repository_missing、not_git_repo、origin_not_github、gh_missing、exec_permission_not_auto）归入阶梯 2，明确原因并等待用户。
 - 质量失败（verification_unconfirmed、review_blocked、delivery_commit_failed、branch_diverged）归入阶梯 3，受 max_fix_rounds ≤2 约束。
 - 其余 needs_user 归入阶梯 4，统一进入 attention inbox，由用户单点处置。
 
 ## 9. 黄金路径
 
 Kaneo To Do → intake → plan → confirm → dispatch → observe → verdict → commit → review ⇄ fix(≤2 同分支) → PR → merge-sync → done
 
 各阶段只向下一阶段推进，或向 needs-you 升级；live 的绑定质量失败可由引擎以 actor=automation 经 resolve 开启有界补救，其余由用户处置。
 
 - intake：只接收开启且就绪项目的 To Do，保留人工筛选前提；自动确认由 tick 执行。
 - plan：手动规划由面板授权；live 自动接收/修复由核心以 automation 经同一 prepare-plan-input + start-plans 生成请求；publish-plan 后保留 plan_execution_hold。
 - confirm：手动任务由用户「确认并安排执行」解除 hold；live 自动任务遵循策略自动确认。
 - dispatch：派发请求 claimed → dispatch-prepare → dispatch-begin-send → dispatch-start → dispatch-confirm → running。
 - observe：worker 执行中；观察者缺席时只读续接（round-observe），不重发 prompt。
 - verdict：live monitor 读取可信终态，排除 `.pi/**` 与构建产物后核心亲跑 verify；legacy 保留转录匹配，shadow 在隔离副本比较、不改旧判定。
 - commit：受 PR 交付管理的任务先 delivery-commit，再进入 review。
 - review：评审通过只登记，PR 交付继续等待合并；评审未通过 → changes_requested → fix_request 同任务修复（≤2 轮，与引擎质量补救分别记账）。
 - delivery：PR 已开 → delivery-sync 轮询合并状态；合并后 advance-task done。
 - done：任务归档，保留全部历史与文件。
 
 ## 10. 8 个用户动作清单
 
 以下 8 个动作是用户可从 attention inbox 或任务详情触发的核心人工干预：
 
 | 动作 | 触发条件 | 效果 | 对应 resolve action |
 |---|---|---|---|
 | take_over | 自动化任务出现阶梯 3/4 失败 | 转为人工处理（manual_takeover=true） | dismiss |
 | resume_auto | 已转人工任务，环境已修复 | 恢复自动推进，继续原 phase | resume |
 | dismiss | 任意 needs_user | 清除阻塞，回到 ready/needs_plan | dismiss |
 | retry | dispatch 类未发送/执行失败 | 创建新 dispatch 请求 | retry / resume（执行失败） |
 | replan | 需求变化或修复轮已用完 | 重新规划 | replan |
 | resolve | 失联/发送不明/观察异常 | 释放失联/确认发送/恢复观察 | release_lost / mark_sent / restore |
 | accept_verification | verification_unconfirmed | 人工接受验证结果，回到 running | accept_verification |
 | review_recovery | review_blocked 历史误退 | 恢复评审阶段，重新排队 review | recover_review |
 
 **动作约束：**
 - 所有动作经核心 `resolve` 或专用命令校验身份与任务状态，不绕过 allowed_actions。
 - 危险操作（retry、replan、mark_done）须二次确认。
 - 动作结果写入 event，桌面即时刷新。
