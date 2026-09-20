# KaneoPilot 固定命令协议 v1

本文件是桌面端、Codex 执行端与调度核心之间唯一的接口约定。任何改动只能由主 agent 发布新版本；实现者不得自行增删字段或语义。

## 1. 调用方式

核心是本地 CLI：`kanpilot`（实质为 `python3 <core>/kanpilot.py`）。所有命令：

```
kanpilot <command> [--json '<payload>'] [--db <path>]
```

- 无 `--json` 时从 stdin 读 JSON payload（可为空对象 `{}`）。
- 成功：stdout 输出**单个 JSON 对象**，exit 0。
- 失败：stdout 输出 `{"error": "<人类可读原因>", "code": "<稳定错误码>"}`，exit 1。stderr 可含诊断，不得含凭据。
- 所有时间一律 ISO 8601 UTC（`YYYY-MM-DDTHH:MM:SS.ffffff+00:00`）；展示层自转时区。
- DB 路径解析顺序：`--db` > `KANPILOT_DB` 环境变量 > 默认 `~/Library/Application Support/KaneoPilot/kanpilot.db`。
- 未知命令、payload 缺必填字段、含未知字段均报 `bad_request`。
- 每个命令是独立进程调用；调用方不得假设进程存活等于操作存活（见心跳/失联语义）。

## 2. 概念模型

- **task**：一个被跟踪的 Kaneo 任务。`task_id` 是 Kaneo 任务 ID。
- **request**：发给执行端的一次性工作项。`stage ∈ plan|dispatch|monitor|review`。同一 `idempotency_key` 只允许一个活动请求（`queued|claimed`）。
- **attempt**：一次领取执行记录（含证据）。request 被 claim 即产生 attempt。
- **event**：不可变时间线条目。
- **stage**：`plan`（规划）、`dispatch`（派发）、`monitor`（执行监控）、`review`（评审）。

### task.status（生命周期，单一字段）

| status | 含义 |
|---|---|
| `needs_plan` | 已发现/需求已变，无有效计划 |
| `planning` | plan 请求已被领取，计划制作中 |
| `ready` | 计划已发布，可派发（含尚无请求与请求排队中两种显示态） |
| `dispatching` | dispatch 请求已领取，UI 准备中 |
| `prepared` | UI 证据已核验，待触发发送 |
| `submitting` | Kaneo 已转 in-progress，待确认消息发出 |
| `running` | 已确认发送，Devin 执行中 |
| `in_review` | 已进入评审阶段 |
| `needs_user` | 阻塞，需人工决定 |
| `done` | 已归档完成 |

### request.status

`queued` → `claimed` → `succeeded | failed | cancelled`。`claimed` 且 `lease_expires_at < now` 在读取层派生为 **lost（执行端失联）**——状态仍保持 `claimed`，不自动释放、不自动重发。

### 显示态（派生，服务端在 `list-tasks`/`get-task` 计算）

`display_status` ∈ `待规划|规划中|就绪|已排队|派发中|执行中|评审中|需人工|执行端失联|已归档`：

- task `ready` 且有 `queued` dispatch 请求 → `已排队`
- task `ready` 无活动请求 → `就绪`
- task `dispatching|prepared|submitting` → `派发中`；其 claimed 请求失联 → `执行端失联`
- task `running|in_review|planning` 同理叠加失联显示
- task `needs_user` → `需人工`（附 `failure_reason`/`blocked_summary`）
- 其余直译

## 3. 命令总表

| 命令 | 使用方 | 副作用 |
|---|---|---|
| `doctor` | 桌面/运维 | 只读诊断（`--online` 时才访问网络） |
| `tick` | 执行端/桌面 | 按业务周期补建到期请求 |
| `overview` | 桌面 | 只读汇总 |
| `list-tasks` | 桌面 | 只读 |
| `get-task` | 桌面 | 只读 |
| `list-requests` | 桌面 | 只读（含失联派生态） |
| `list-events` | 桌面 | 只读 |
| `enqueue` | 桌面 | 建请求（幂等） |
| `set-priority` | 桌面 | 改排队顺序 |
| `set-paused` | 桌面 | 暂停/恢复领取 |
| `resolve` | 桌面 | 人工解除阻塞/释放失联 |
| `recheck` | 桌面/执行端 | 重新核验远端+本地指纹 |
| `sync-tasks` | plan 执行端 | 批量登记远端发现 |
| `claim` | 执行端 | 事务领取（含 tick） |
| `heartbeat` | 执行端 | 续租 |
| `report` | 执行端 | 记录请求结果 |
| `publish-plan` | plan 执行端 | 登记已落盘计划 |
| `register-review` | review 执行端 | 登记评审结论与交付 |
| `create-review-fix` | review 执行端 | 受控创建修复任务（远端写） |
| `advance-task` | monitor/review 执行端 | 受控 Kaneo 状态迁移（远端写） |
| `dispatch-prepare` | dispatch 执行端 | 核验 UI 证据 → prepared |
| `dispatch-begin-send` | dispatch 执行端 | 远端核验+转 in-progress → submitting |
| `dispatch-confirm` | dispatch 执行端 | 核验发送证据 → running |
| `dispatch-fail` | dispatch 执行端 | 失败记录 → needs_user |
| `migrate` | 主 agent/运维 | 备份并导入旧状态（幂等） |

## 4. 通用行为

### 4.1 幂等与去重

- `enqueue` 默认 `idempotency_key = "<kind>:<task_id>"`；`plan-scan` 为 `"plan-scan:workspace"`。
- 已存在同 key 的 `queued|claimed` 请求时返回既有请求并带 `"deduplicated": true`，**不新建**——重复点击不重复派发。
- 每 task+kind 同时只允许一个活动请求。

### 4.2 领取（claim）事务

`claim` 在单个 `BEGIN IMMEDIATE` 事务内完成：

1. 先执行 `tick` 内联逻辑（见 §5.2）。
2. 若 stage 或全局暂停 → `{"claimed": false, "reason": "paused"}`。
3. `dispatch` 阶段额外要求当前无其他 `claimed` dispatch 请求（桌面 UI 单操作者）；有则 `{"claimed": false, "reason": "stage_busy"}`。
4. 取该 stage 下 `status='queued'` 且 `not_before <= now` 的请求，按 `priority ASC, created_at ASC` 取首条（或按 `request_id` 指定，指定时仍校验 stage/queued，但跳过 `not_before`——冷却期只约束自动取单；指定请求已 `claimed`（并发竞争失败）→ `{"claimed": false, "reason": "already_claimed"}`，其他非 queued 状态仍报错）。
5. 领取守卫（见 §4.4）失败 → 该请求标记 `failed`（attempt 记录原因），返回 `{"claimed": false, "reason": "guard_failed", "request_id": ..., "detail": ...}`；调用方可再次 claim 拿下一条。
6. 成功：置 `claimed`，写 `run_id`（新 UUID）、`claimed_by`、`claimed_at`、`heartbeat_at`、`lease_expires_at = now + lease_seconds`，attempt+event 落库，提交。
7. 返回请求及阶段上下文（dispatch 见 §4.5）。

并发两个 claim 只有一个成功；失败者得到 `claimed:false` 或下一条——由事务保证，不依赖文件锁。

### 4.3 租约与失联

- `lease_seconds` 默认 1200（settings 可改）。`heartbeat` 刷新 `heartbeat_at` 并把 `lease_expires_at` 顺延 `now + lease_seconds`。
- `heartbeat`/`report`/dispatch 子步骤均校验 `request_id + run_id` 匹配且 status=`claimed`；不符报 `stale_run`。
- 失联只读派生：`claimed` 且租约过期 → `display_status=执行端失联`。**不**因 PID、CLI 退出或时间流逝自动释放/重发；只有 `resolve` 的 `release_lost` 动作能解除。

### 4.4 领取守卫

按 kind 校验 task 当前状态，不符即守卫失败：

| kind | 允许 task.status | 附加校验 |
|---|---|---|
| `plan` | `needs_plan` | — |
| `dispatch` | `ready` | §4.5 全量校验（含远端） |
| `monitor` | `running` `in_review` | — |
| `review` | `in_review` | — |
| `plan-scan` | —（无 task） | — |

### 4.5 dispatch claim 校验与上下文

`claim` 对 dispatch 请求在事务外**先**执行与旧 `kaneo_dispatch.py check+claim` 等价的校验，全部通过才提交领取；任一失败按 §4.2-5 处理（task 转 `needs_user`，`failure_code=validation_failed|remote_unavailable` 区分确定性与暂时性）：

1. 计划文件存在、在 Plan 根内、无符号链接；frontmatter 含 `task_id` 匹配、`implementation_status=未开始`、`project_name` 在项目白名单、`repository` 等于注册仓库且目录存在、`source_sha256`；`plan_sha256` 重算等于登记值。
2. 来源快照（`tasks.source_path`）存在；规范化重算 `source_sha256` 等于计划值；`task.id`/`projectId` 一致。
3. `requirements_sha256` 由快照重算等于计划值（无快照或不可判 → `needs_user`）。
4. 远端 `get-task`：`projectId` 一致、`status ∈ {to-do, todo}`、`requirements_sha256` 一致、列含当前状态与 `in-progress`。远端不可达 → `remote_unavailable`（task 保持原状，不算阻塞）。
5. `plan_kind=review_fix` 追加：reviewer 登记 `fix_task_id/review_key/repair_plan_path/repair_plan_sha256` 全匹配、远端描述含 `<!-- codex-kaneo-review:KEY -->`；`standard` 要求远端描述**不**含该标记。
6. 无冲突发送证据（`send_state ∈ {unknown, sent}` 或 `sent=true` → `needs_user`，须走 `resolve`）。

返回上下文（与原 claim 同构）：

```json
{"claimed": true, "request_id": "...", "run_id": "...",
 "context": {"task_id": "...", "plan_path": "...",
   "attachment_name": "<plan 文件名>", "repository": "...",
   "project_id": "...", "plan_kind": "standard|review_fix",
   "session_marker": "<task_id>/<run_id>",
   "instruction": "任务标识：<task_id>/<run_id>。读取并完整执行所附计划，保留现有改动，按计划使用 subagent 和独立 worktree，完成验证并报告结果。",
   "original_todo_slug": "to-do", "requirements_sha256": "...",
   "source_sha256": "...", "lease_seconds": 1200}}
```

claim 成功后 task → `dispatching`（记录 `run_id`、`session_marker`、`sent=false`、`send_state='not_sent'`、`dispatch_lease_expires_at`）。

### 4.6 发送证据（evidence JSON）

dispatch-prepare/confirm 校验，字段全部来自 UI 观察，不得推测：

```json
{"session_marker": "<task>/<run>", "repository": "<abs>",
 "mode": "Agent", "model": "SWE-2 Max", "execution_mode": "Local",
 "attachment_name": "<exact file>", "attachment_visible": true,
 "instruction_visible": true, "message_visible": false,
 "session_title": null, "session_id": null}
```

校验规则同旧实现：marker/repository/mode/model/execution_mode/attachment 全等；prepare 要求 `instruction_visible`；confirm 额外要求 `message_visible === true` 且重算计划哈希未变。

### 4.7 报告（report）

`report {request_id, run_id, outcome, note?, send_state?, evidence?, progress?, kaneo_status?}`

- `outcome ∈ succeeded|failed|needs_user`。
- `succeeded`：请求→succeeded；按 kind 更新 task（monitor 带 `progress` 则更新 `last_progress`；`kaneo_status` 为观察值则记录）。
- `failed`：请求→failed，记录 reason；task 保持原状态（由 `needs_user` 或 resolve 决定后续）。
- `needs_user`：请求→failed，task→`needs_user`，写 `failure_reason`/`failure_code`（payload 可给）/`blocked_at`。
- 任何 report 都写 attempt（outcome、evidence、note）与 event。

### 4.8 tick（业务周期调度）

`tick` 幂等，在 `claim` 内联执行，也可单独调用：

- `plan-scan`：距上次该请求完成 ≥ `plan_scan_interval_hours`（默认 24）且无活动同 key 请求 → 建 `plan-scan`。
- `monitor`：每个 `running|in_review`（仅当 review 监控 pending?——in_review 由 review 请求覆盖，monitor 只针对 `running`）且无活动 monitor 请求的 task → 建 `monitor`（key `monitor:<task_id>`）。
- `review`：每个 `in_review` 且无活动 review 请求、无未消费的 review 结论的 task → 建 `review`（key `review:<task_id>`）。
- `queued` 请求的 `not_before` 语义：monitor 连续观察由 `not_before = 上次 report + monitor_interval_seconds`（默认 900，对齐 15 分钟周期）控制，避免同一 task 每 tick 重复建。tick 只建 `not_before <= now` 不限制的请求——即 tick 创建时立即生效的是 not_before 已过期的；新 cycle 请求由 report 时写 not_before。实现上 tick 对 running task 建 monitor 请求的条件：无活动 monitor 请求 **且**（无历史 monitor 完成记录 或 最近一次完成时间 + interval <= now）。

### 4.9 暂停

`set-paused {stage: "dispatch"|"plan"|"monitor"|"review"|null, paused: bool}`；`null` = 全局。claim 只检查领取时刻的暂停位——**只阻止新领取，不影响已 claimed/running**。

### 4.10 人工解决（resolve）

`resolve {task_id, action, note?}`，action 只允许与当前事实相容的项（核心校验，非法 → `bad_request`）：

| action | 前置条件 | 效果 |
|---|---|---|
| `retry` | task `needs_user` 且 `send_state='not_sent'`（或非 dispatch 阻塞） | task→`ready`（dispatch 类）或 `needs_plan`（plan 类）；清阻塞字段；按原 kind 建新请求 |
| `restore` | `needs_user` 且 `send_state='unknown'` | 认定未发送：`sent=false, send_state='not_sent'`，task→`ready` 并建 dispatch 请求 |
| `accept_duplicate` | `needs_user` 且 `send_state='unknown'` | 接受可能重复执行：同 restore 建 dispatch 请求，`resolution` 记录用户知情 |
| `mark_sent` | `needs_user` 且 `send_state='unknown'` | 人工确认已发出：`sent=true, send_state='sent'`，task→`running`，建 monitor 请求 |
| `replan` | `needs_user`（任意来源） | task→`needs_plan`，建 plan 请求 |
| `release_lost` | 存在 claimed 且租约过期请求 | 该请求→`cancelled`；task→阻塞前可安全态（dispatch 中途→`needs_user` send_state 保持；monitor/plan/review 中途→回原 status）并建相应新请求 |
| `mark_done` | `in_review`/`needs_user`/`running`（非 dispatch 中途） | task→`done` |
| `dismiss` | `needs_user` | 清阻塞字段，task→`ready`（不自动建请求） |

所有 resolve 写 `event kind=resolved`（actor、action、note）。

### 4.11 错误码（稳定值）

`bad_request` `not_found` `conflict` `stale_run` `busy` `paused` `guard_failed` `validation_failed` `remote_unavailable` `remote_conflict` `evidence_invalid` `send_conflict` `already_done` `migration_needed` `internal`。

## 5. 命令详约

### 5.1 doctor

`doctor {online?: bool}` →

```json
{"ok": true, "checks": [
  {"name": "python", "ok": true, "detail": "3.10.10"},
  {"name": "db_writable", "ok": true, "detail": "<db path>"},
  {"name": "credentials", "ok": true, "detail": "present, baseUrl https://…"},
  {"name": "plan_root", "ok": true, "detail": "<path>"},
  {"name": "repos_root", "ok": true, "detail": "<path>"},
  {"name": "state_root", "ok": true, "detail": "<app support dir>"},
  {"name": "kaneo_ping", "ok": true, "detail": "HTTP 200", "skipped": false}]}
```

`online` 缺省 false → `kaneo_ping` `skipped:true`。凭据检查只验证文件可读、含 baseUrl+token，**不输出 token**。

### 5.2 tick / overview / list / get

- `tick {}` → `{"created": [{"request_id","kind","task_id"}], "skipped": [...]}`
- `overview {}` → `{"paused": {"global": false, "stages": ["dispatch"]}, "counts": {"tasks": {"ready": 3,...}, "requests": {"queued": 2, "claimed": 1, "lost": 0}}, "last_tick_at": ..., "schema_version": 1, "settings": {"lease_seconds": 1200, "monitor_interval_seconds": 900, "plan_scan_interval_hours": 24, "plan_scan_anchor": "09:00 Asia/Shanghai", "migration": {...}|null}, "paths": {"db": "...", "plan_root": "...", "repos_root": "...", "app_support": "...", "core": "..."}}`
- `list-tasks {stage?, status?, needs_attention?: bool}` → `{"tasks": [TaskSummary]}`，`TaskSummary = {task_id, project_id, project_name, title, stage, status, display_status, last_progress, blocked_reason, next_claim_at, active_request: {request_id, kind, status, priority, claimed_by, heartbeat_at, lease_expires_at, not_before}|null, kaneo_status, kaneo_checked_at, priority, plan_path, updated_at}`
- `get-task {task_id}` → `{"task": TaskDetail}`，`TaskDetail = TaskSummary + {repository, plan_sha256, plan_kind, requirements_sha256, source_sha256, source_path, review_report_path, review_report_sha256, review_key, review_source_task_id, fix_task_id, session_id, session_title, session_marker, send_state, sent, run_id, failure_code, failure_reason, failure_stage, manual_recovery_required, allowed_actions: [...], attempts: [Attempt], links: {kaneo_task_url, plan_file, report_file}}`；`Attempt = {request_id, run_id, stage, kind, started_at, finished_at, outcome, send_state, note, evidence}`
- `list-requests {status?: queued|claimed|lost|failed|succeeded|cancelled|active, limit?: 200}` → `{"requests": [Request]}`，`Request = {request_id, idempotency_key, task_id, stage, kind, status, display_status, priority,`（`display_status` 用 §2 同一中文显示词汇；`status` 为库内原值，失联派生只在 `display_status` 标 `执行端失联`） not_before, requested_by, run_id, claimed_by, claimed_at, heartbeat_at, lease_expires_at, result, attempt_no, created_at, updated_at}`
- `list-events {task_id?, limit?: 200}` → `{"events": [{id, at, task_id, request_id, kind, actor, detail}]}` 倒序。

### 5.3 enqueue / set-priority / set-paused

- `enqueue {task_id, kind?, note?}`：kind 缺省按 task.status 推断（`needs_plan`→plan，`ready`→dispatch，`running`→monitor，`in_review`→review）；不允许的推断 → `bad_request`。→ `{"request": Request, "deduplicated": bool}`
- `set-priority {request_id? task_id?, priority: int}`：request_id 直接改 queued 请求；task_id 改其活动 queued 请求；无 → `not_found`。
- `set-paused {stage|null, paused}` → `{"paused": {...}}`

### 5.4 sync-tasks

`sync-tasks {executor: str, projects: [{project_id, project_name, repository?, tasks: [{task_id, title, kaneo_status, requirements_sha256, source_sha256, source_path}]}]}`

- upsert task；新 task → `needs_plan` + `plan` 请求。
- `requirements_sha256` 变化 → `needs_plan` + `plan` 请求（保留人工编辑由 planner 侧处理；核心只管状态）。
- `kaneo_status` 变化：→ done 则 task `done`；→ in-review 且 task `running` → `in_review`。
- **不得覆盖** `dispatching|prepared|submitting` 的 task 状态/指纹（只更新 `kaneo_status/kaneo_checked_at` 观察值）。
- → `{"upserted": n, "created_requests": [...], "changed": [...]}`

### 5.5 claim / heartbeat / report

- `claim {stage, executor, request_id?}` → §4.2/§4.5。非 dispatch 返回 `{"claimed": true, "request_id", "run_id", "context": {"task_id"?, "kind", "payload", "lease_seconds", "task": TaskSummary?}}`。
- `heartbeat {request_id, run_id, progress?}` → `{"ok": true, "lease_expires_at": ...}`；`progress` 写 task.last_progress。
- `report` 见 §4.7 → `{"ok": true, "request_status": ..., "task_status": ...}`。

### 5.6 publish-plan

`publish-plan {task_id, plan_path, plan_kind: standard|review_fix, source_sha256, requirements_sha256, generated_at, planner_model, reasoning_effort, run_id?}`

- 校验：文件在 Plan 根内存在、frontmatter `task_id` 匹配、`implementation_status=未开始`、project 白名单与 repository 一致、重算 `plan_sha256`；`source_sha256`/`requirements_sha256` 与 task 当前指纹一致（task 有基线时）。
- task 处于 `dispatching|prepared|submitting|running` → `conflict`（不得覆盖派发中输入）。
- 成功：task→`ready`，写 plan_* 字段，event `plan_published`；若 task 有失败 plan 请求可同 report 闭环（调用方顺序：publish-plan → report succeeded）。
- → `{"ok": true, "task_status": "ready", "plan_sha256": "..."}`

### 5.7 register-review

`register-review {task_id, run_id?, report_path, conclusion: pass|changes_requested|blocked, review_key?, fix_task_id?, fix_task_source_sha256?, repair_plan_path?, kaneo_status?}`

- 校验 report 文件在 Reviewer reports 根内存在、重算 `report_sha256`；fix/repair 字段互相一致（changes_requested 需 fix_task_id + review_key；给 repair_plan_path 则需存在且重算 sha）。
- 写 task.review_*；conclusion=pass → task 可 `done`（由 advance-task 或本命令带 `kaneo_status='done'` 共同确认时）；`blocked` → task `needs_user`。
- → `{"ok": true, "report_sha256": "..."}`

### 5.8 create-review-fix

`create-review-fix {source_task_id, title, description, priority, dedupe_key}` → 经内嵌 bridge 幂等创建（dedupe marker `<!-- codex-kaneo-review:KEY -->`），登记 fix task 行（`needs_plan`，plan_kind=review_fix，review_source_task_id，review_key）→ `{"ok": true, "fix_task_id": "...", "created": bool, "source_sha256": "..."}`。重复调用同 dedupe_key 返回已存在任务，`created:false`。

### 5.9 advance-task

`advance-task {task_id, run_id?, target: "in-review"|"done", expected_current: "in-progress"|"in-review", evidence_note?}`

- 核心经 bridge：`get-task` 验证 projectId/status/requirements → `update-status` → 再 `get-task` 验证目标态与指纹。
- 成功：`in-review` → task `in_review` + 建 review 请求；`done` → task `done`。event `kaneo_transition`。
- 远端冲突 → `remote_conflict`，task 记 `kaneo_status` 观察值并 `needs_user`（`failure_code=remote_conflict`）。
- → `{"ok": true, "kaneo_status": "...", "task_status": "..."}`

### 5.10 dispatch-prepare / begin-send / confirm / fail

均要求 `{task_id, run_id}` 匹配 task.run_id 且该 task 有 `claimed` dispatch 请求（同一 run）。

- `dispatch-prepare {..., evidence}` → §4.6 校验 → task `prepared`，存 `ui_evidence` → `{"status":"prepared"}`
- `dispatch-begin-send {task_id, run_id}` → 要求 `prepared`；`lifecycle` 串行位（settings 内布尔，事务内检查）下：`get-task` 验 projectId/status=original_todo_slug/requirements → task `submitting` + `transition_started_at` → bridge `update-status in-progress` → `get-task` 复验 → 存 `transition_verified_at` → `{"status":"submitting","action":"click visible Send once; verify message and attachment"}`。远端冲突 → `remote_conflict`（task→`needs_user`，`failure_stage` 记录）。
- `dispatch-confirm {..., evidence}` → §4.6 全校验（`message_visible`）+ `transition_verified_at` 存在 → task `running`、`sent=true`、`send_state='sent'`、`sent_at`、session_*；请求→succeeded；建 monitor 请求；释放 dispatch 串行位 → `{"status":"running"}`
- `dispatch-fail {..., send_state: not_sent|unknown, reason}` → task `needs_user`，`send_state`/`sent`/`failure_reason`/`failure_stage` 记录；请求→failed；`not_sent` 释放占用，`unknown` 保留占用标记（`lock_retained`）；释放 dispatch 串行位 → `{"status":"needs_user","send_state":...}`

### 5.11 recheck

`recheck {task_id}` → 重算计划/来源哈希 + 远端 `get-task`（可缺省 `offline:true` 只验本地）→ `{"task_id", "checks": [{name, ok, detail}], "kaneo_status"?, "requirements_sha256"?, "ok": bool}`；写 `kaneo_checked_at` 与观察 `kaneo_status`。

### 5.12 migrate

`migrate {legacy_root?, plan_root?, backup_root?, dry_run?}` →

- 备份 `KaneoPlanner/KaneoExecutor/KaneoReviewer` 状态目录与 Plan 目录到 `backup_root/migration-<ts>/`（默认 app support 下），**原文件不动**。
- 导入三 manifest + 锁证据 → tasks/requests/attempts/events（映射见 state-model.md §6）。已 `running` 的 task 建 monitor 请求；`needs_user` 保持；`dispatching|prepared|submitting` 历史残留 → `needs_user` `interrupted`。
- 幂等：`settings.migration` 记录 manifest 哈希集合；重跑跳过已导入。
- → `{"ok": true, "backup_path": "...", "imported": {"tasks": n, "requests": n, "events": n}, "warnings": [...]}`

## 5.13 规范补充裁决（v1.1）

1. **哈希口径**：`source_sha256`/`requirements_sha256` 的「规范化重算」就是内嵌 bridge 的实现：`stable()`（dict/list 递归排序）→ `json.dumps(sort_keys=True, separators=(",",":"), ensure_ascii=False)` → sha256。`source_sha256 = add_hash(stable({task,comments,relations,activity}))`；`requirements_sha256 = requirements_hash(source)`（requirements-v1 schema，字段集见 bridge 实现）。与旧 manifest 既有值必须一致。
2. **settings 本版只读**：`lease_seconds`/`monitor_interval_seconds`/`plan_scan_interval_hours`/`plan_scan_anchor` 只有默认值，无 set-settings 命令（UI 只读展示）。测试改租约直接改库属测试设施。
3. **REVIEW_REPORTS_ROOT** = `~/Library/Application Support/Codex/KaneoReviewer/reports`（沿用旧根）。
4. **claim 空队列**：`{"claimed": false, "reason": "empty"}`（另有 `paused`/`stage_busy`/`guard_failed`）。
5. **`migrate.plan_root`** = 用户可见 Plan 根（备份对象+计划校验根），`legacy_root` = `~/Library/Application Support/Codex`。
6. **`advance-task.run_id`**：可选；提供时必须等于该 task 当前 `claimed` 请求的 `run_id`（即调用方本轮领取身份），与历史 dispatch run 无关。
7. **dispatch-confirm 校验失败**：`evidence_invalid`，task 停留 `submitting`（由 dispatch-fail 显式处置）。
8. **`list-requests status=active`** = `queued OR claimed`（含失联派生项，`display_status` 标 lost）。
9. **`sent` API 类型**：JSON `true|false|null`（库内 0/1/NULL 为内部表示）。
10. **resolve/set-priority 返回**：`{"ok": true, "task": TaskSummary}` / `{"ok": true, "request": Request}`。
11. **Kaneo 响应形状**以 bridge 解析口径为准（`as_list`/`unwrap_data` 宽容解析）；fake/stub 按真实服务形状对齐。
12. **`doctor` 凭据**：输出不含 token 值及 `accessToken`/`Bearer` 字样。

## 6. 常量

- `PROJECTS`：`Music→example-project-01`、`Library→example-project-02`、`StudyTool→example-project-03`、`Comic→example-project-04`、`Feedback→example-project-05`；仓库根 `~/Documents/Code/Project/Personal Project/<项目名>`。
- `WORKSPACE_ID = example-record-id`
- `PLAN_ROOT = ~/Documents/Code/Plan`
- `LEGACY_STATE_ROOT = ~/Library/Application Support/Codex`（KaneoPlanner/KaneoExecutor/KaneoReviewer）
- `APP_SUPPORT = ~/Library/Application Support/KaneoPilot`（DB、备份、报告缓存）
- `TODO_SLUGS = {to-do, todo}`；`REVIEW_SLUGS = {in-review, review}`；`DONE_SLUGS = {done}`。
- 计划 frontmatter 必填：`task_id project_id project_name repository source_sha256 requirements_sha256 planner_model reasoning_effort implementation_status generated_at`；standard 要求 `planner_model=gpt-6-astra, reasoning_effort=medium`；review_fix 要求 `planner_model=gpt-5.6-sol, reasoning_effort=xhigh` 且含 `review_report_path review_report_sha256 review_source_task_id review_key`。
- 已知限制：Kaneo 服务端无 CAS，桥接的先读后写有远端并发窗口；本地事务只序列化本地写入，不声称阻止人工远端竞争。
