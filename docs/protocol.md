# KaneoPilot 固定命令协议 v1

本文件是桌面端、pi 执行端与调度核心之间唯一的接口约定。任何改动只能由主 agent 发布新版本；实现者不得自行增删字段或语义。

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
| `running` | 已确认发送，pi worker 执行中 |
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

### 看板列（board_column，派生，桌面唯一分列依据）

`board_column` ∈ `needs_plan|todo|in_progress|in_review|done|failed`，由核心在 `list-tasks`/`get-task` 统一计算；桌面不得按中文标签、计划文件或历史失败记录自行归列。每个任务恰出现一次。

非 `needs_user` 状态直映射：

| task.status | board_column |
|---|---|
| `needs_plan` `planning` | `needs_plan` |
| `ready` `dispatching` `prepared` `submitting` | `todo` |
| `running` | `in_progress` |
| `in_review` | `in_review` |
| `done` | `done` |

`needs_user` 按**当前未解决的失败证据**分类（历史失败不覆盖恢复后状态）：

1. `send_state='unknown'`（发送不明）→ 不算失败，按 `failure_stage` 归列并附阻塞摘要；
2. `failure_code ∈ {validation_failed, dispatch_failed, execution_failed, plan_failed}` → `failed`（有明确证据的规划/派发/执行失败）；
3. 其余 `needs_user`（含 `remote_unavailable` `remote_conflict` `send_conflict` `send_unknown` `executor_lost` `interrupted_dispatch` `review_blocked` `observation_blocked` `reported` 及未识别码）→ 不算失败，按 `failure_stage` 归列并附阻塞摘要。

> **v1.3 起本节「看板列」以 §5.15.4 为准**：异常（needs_user、评审未通过、失联）统一进第六列 `failed`，由 `lifecycle_column` 保留生命周期位置、`exception` 给出结构化异常信息。v1.2 的 `failure_stage` 归列口径仅用于计算 `lifecycle_column`。

`failure_stage → board_column`：`needs_plan|planning|plan → needs_plan`；`ready|dispatching|prepared|submitting|dispatch → todo`；`running|monitor → in_progress`；`in_review|review → in_review`；`done → done`；缺失或不在表内（如历史 `needs_user` 值）时按 `task.stage` 同口径回退。

**失联不是失败**：任何列内 `claimed` 请求租约过期 → 保留本列，`blocked_reason` 标 `执行端失联`，`display_status` 照旧派生 `执行端失联`。

伴随字段（均在 TaskSummary/TaskDetail）：

- `board_substatus`（中文词表，列内当前子状态）：
  - `needs_plan`：`待规划`|`规划中`（planning）|`规划受阻`（阻塞归列）
  - `todo`：`就绪`|`已排队`（有 queued dispatch）|`派发中`（dispatching/prepared/submitting）|`派发受阻`（阻塞归列，含发送不明）
  - `in_progress`：`执行中`|`执行受阻`（阻塞归列）
  - `in_review`：`评审中`|`待修复`（当前周期结论 `changes_requested`，关联 `fix_task_id`）|`评审受阻`（阻塞归列）
  - `done`：`已完成`
  - `failed`：`规划失败`|`派发失败`|`执行失败`（按 failure_code；兜底 `失败`）
- `blocked_reason`：任一阻塞（needs_user 或活动请求失联）非空；失联时含失联请求 kind；失败列给具体原因。
- `last_checked_at`：最近一次执行观测时间 = max（最近 monitor/review 尝试 finished_at、活动请求 heartbeat_at、kaneo_checked_at），无则 null。
- `review_summary`：null 或 `{conclusion, registered_at, fix_task_id, review_key, report_path}`（当前周期；新一轮 in_review 起始即清空，下同）。
## 3. 命令总表

| 命令 | 使用方 | 副作用 |
|---|---|---|
| `doctor` | 桌面/运维 | 只读诊断（`--online` 时才访问网络） |
| `tick` | 执行端/桌面 | 按业务周期补建到期请求（v2 起不再建 plan-scan） |
| `overview` | 桌面 | 只读汇总 |
| `list-tasks` | 桌面 | 只读（默认排除 purged） |
| `get-task` | 桌面 | 只读（purged 返回只读审计详情） |
| `list-requests` | 桌面 | 只读（含失联派生态；`include_removed` 默认 false） |
| `get-request` | 桌面 | 只读：请求详情+attempts+合法动作+attention_state（v2） |
| `list-events` | 桌面 | 只读：标题/阶段/结果/中文摘要+游标翻页+搜索筛选（v2 扩展） |
| `enqueue` | 桌面 | 建请求（幂等；dispatch 支持 confirm_plan+expected_plan_sha256） |
| `set-priority` | 桌面 | 改排队顺序 |
| `set-paused` | 桌面 | 暂停/恢复领取 |
| `resolve` | 桌面 | 人工解除阻塞/释放失联（v2 起不再自动启动规划） |
| `recheck` | 桌面/执行端 | 重新核验远端+本地指纹 |
| `sync-tasks` | 桌面（手动获取） | 批量登记远端发现（v2 起不自动建 plan 请求） |
| `fetch-todos` | 桌面 | 只读：分页枚举远端 To Do+附件与不可选原因（v2） |
| `prepare-plan-input` | 桌面 | 建不可变规划材料目录（v2；不入队不调模型） |
| `start-plans` | 桌面 | 逐任务提交手动规划（v2；仅已授权项入队） |
| `claim` | 执行端 | 事务领取（含 tick；拒绝未授权规划请求） |
| `heartbeat` | 执行端 | 续租 |
| `report` | 执行端 | 记录请求结果 |
| `publish-plan` | 规划 runner | 登记已落盘计划（v2 新协议：request_id/run_id/planner_input_sha256） |
| `register-review` | review 执行端 | 登记评审结论与交付 |
| `create-review-fix` | review 执行端 | 受控创建修复任务（远端写；规划请求不再自动启动） |
| `advance-task` | monitor/review 执行端 | 受控 Kaneo 状态迁移（远端写） |
| `dispatch-prepare` | dispatch 执行端 | 核验 UI 证据 → prepared |
| `dispatch-begin-send` | dispatch 执行端 | 远端核验+转 in-progress → submitting |
| `dispatch-confirm` | dispatch 执行端 | 核验发送证据 → running |
| `dispatch-fail` | dispatch 执行端 | 失败记录 → needs_user |
| `migrate` | 主 agent/运维 | 备份并导入旧状态（幂等） |
| `archive-task` | 桌面 | 归档（移出活跃看板，可恢复） |
| `trash-task` | 桌面 | 移入本地回收站（可恢复） |
| `restore-task` | 桌面 | 从归档/回收站恢复（默认暂停自动推进） |
| `purge-task` | 桌面 | 仅从回收站永久从列表移除（v2；保留历史与文件） |
| `list-sessions` | 桌面 | 只读：按任务/执行轮次列出会话（含保存状态筛选） |
| `get-session` | 桌面 | 只读：会话详情与关联状态 |
| `session-messages` | 桌面 | 只读：按游标读取会话消息 |
| `trash-session` | 桌面 | 会话移入调度台回收站（幂等，不停进程不删文件） |
| `restore-session` | 桌面 | 会话从回收站恢复（幂等） |
| `purge-session` | 桌面 | 仅从回收站永久从列表移除该轮次（v2） |
| `get-exec-config` | 桌面 | 只读：PI 执行配置 |
| `set-exec-config` | 桌面 | 写 settings.exec_config（经能力校验，不改 pi 全局配置） |
| `exec-capabilities` | 桌面/执行端 | 只读：当前安装 pi 的能力与环境检查 |
| `pi-desktop-catalog` | 桌面 | 只读：获取 Plus provider/model 候选与默认值（T3.5，§5.23.6） |
| `dispatch-start` | dispatch 执行端 | 落盘启动意图并拉起该轮 pi worker（幂等） |
| `executor-status` | 桌面 | 只读：四个执行端 stage 的在途/最近一次运行状态（§5.19） |
| `get-plan` | 桌面 | 只读：已发布计划的核验正文（v3，§5.23.1） |
| `get-review-config` | 桌面 | 只读：Review 环境配置（v3，§5.23.4） |
| `set-review-config` | 桌面 | 写 settings.review_config（v3，§5.23.4；仅影响新 review 轮次） |
| `get-project-mappings` | 桌面 | 只读：项目与本地仓库映射列表及状态（T1，§5.26） |
| `set-project-mapping` | 桌面 | 写 settings.project_mappings（T1，§5.26；保存/停用/解除仓库映射） |
| `set-project-automation` | 桌面 | 更新项目自动处理策略与 revision（§5.27） |
| `set-automation-global` | 桌面 | 更新全局自动任务并发上限（§5.27） |
| `record-cost` | 角色 runner | 幂等登记该请求/轮次/阶段的成本（§5.27） |
| `delivery-commit` | monitor | 核验执行轮次并提交任务工作树（§5.27） |
| `delivery-publish` | delivery runner | 非强制推送、复用或创建 PR、写交付评论（§5.27） |
| `delivery-sync` | delivery runner | 读取 PR 状态并登记同步结果（§5.27） |
| `executor-instance` | App 调度器 | 登记/关闭本次 App 进程归属；不取消 Plus 会话或任务（§5.27） |
| `round-observe` | monitor 执行端 | 只读续接缺席的观察进程（幂等；不改任务状态，§5.23.7） |
| `get-goal-report` | monitor 执行端 | 只读：取回经身份/完整性校验的 Plus Goal 报告（T5，§5.23.10） |
| `record-goal-evaluation` | monitor 执行端 | 登记 AI 完成判定（幂等；校验 monitor claim 与报告底线，§5.23.10） |

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
| `delivery` | `in_review` | 受 PR 交付管理；Review 已通过；身份、租约与工作树核验（§5.27） |
| `plan-scan` | —（无 task） | — |

### 4.5 dispatch claim 校验与上下文

`claim` 对 dispatch 请求在事务外**先**执行与旧 `kaneo_dispatch.py check+claim` 等价的校验，全部通过才提交领取；任一失败按 §4.2-5 处理（task 转 `needs_user`，`failure_code=validation_failed|remote_unavailable` 区分确定性与暂时性）：

1. 计划文件存在、在 Plan 根内、无符号链接；frontmatter 含 `task_id` 匹配、`implementation_status=未开始`、`project_name` 在项目白名单、`repository` 等于注册仓库且目录存在、`source_sha256`；`plan_sha256` 重算等于登记值。
2. 来源快照（`tasks.source_path`）存在；规范化重算 `source_sha256` 等于计划值；`task.id`/`projectId` 一致。
3. `requirements_sha256` 由快照重算等于计划值（无快照或不可判 → `needs_user`）。
4. 远端 `get-task`：`projectId` 一致、普通任务 `status ∈ {to-do, todo}`、`requirements_sha256` 一致、列含当前状态与 `in-progress`。同任务修复轮及用户明确重新规划的自动交付周期按 §5.27 校验已接受的运行/Review 列；不放宽普通任务。远端不可达 → `remote_unavailable`（task 保持原状，不算阻塞）。
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
- `needs_user`：请求→failed，task→`needs_user`，写 `failure_reason`/`failure_code`（payload 可给）/`blocked_at`。**task 已是 `needs_user` 时**：保留既有 `failure_stage` 与 `failure_code`（除非 payload 显式给了 `failure_code`），只更新 `failure_reason`/`blocked_at`——不让后到的通用 report 抹掉阻塞发生点与分类（v1.2）。
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
- `overview {}` → `{"paused": {"global": false, "stages": ["dispatch"]}, "counts": {"tasks": {"ready": 3,...}, "board": {"needs_plan": 1, "todo": 2, "in_progress": 1, "in_review": 0, "done": 5, "failed": 0}, "requests": {"queued": 2, "claimed": 1, "lost": 0}}, "last_tick_at": ..., "schema_version": 1, "settings": {"lease_seconds": 1200, "monitor_interval_seconds": 900, "plan_scan_interval_hours": 24, "plan_scan_anchor": "09:00 Asia/Shanghai", "migration": {...}|null}, "paths": {"db": "...", "plan_root": "...", "repos_root": "...", "app_support": "...", "core": "..."}}`
- `list-tasks {stage?, status?, board_column?, needs_attention?: bool, save_state?: active|archived|trashed|all}` → `{"tasks": [TaskSummary]}`，`TaskSummary = {task_id, project_id, project_name, title, stage, status, display_status, board_column, lifecycle_column, exception, board_substatus, last_checked_at, review_summary, last_progress, blocked_reason, next_claim_at, active_request: {request_id, kind, status, priority, claimed_by, heartbeat_at, lease_expires_at, not_before}|null, kaneo_status, kaneo_checked_at, priority, plan_path, updated_at, save_state, saved_at, auto_advance_paused, move_out_eligibility: {allowed: bool, reason: string|null}}`。`board_column` 过滤参数按 §2/§5.15.4 归列结果过滤（派生后过滤）；`save_state` 缺省 `active`。
- `get-task {task_id}` → `{"task": TaskDetail}`，`TaskDetail = TaskSummary + {repository, plan_sha256, plan_kind, requirements_sha256, source_sha256, source_path, review_report_path, review_report_sha256, review_key, review_source_task_id, fix_task_id, session_id, session_title, session_marker, send_state, sent, run_id, failure_code, failure_reason, failure_stage, manual_recovery_required, allowed_actions: [...], attempts: [Attempt], links: {kaneo_task_url, plan_file, report_file}}`；`Attempt = {request_id, run_id, stage, kind, started_at, finished_at, outcome, send_state, note, evidence}`- `list-requests {status?: queued|claimed|lost|failed|succeeded|cancelled|active, limit?: 200}` → `{"requests": [Request]}`，`Request = {request_id, idempotency_key, task_id, stage, kind, status, display_status, priority,`（`display_status` 用 §2 同一中文显示词汇；`status` 为库内原值，失联派生只在 `display_status` 标 `执行端失联`） not_before, requested_by, run_id, claimed_by, claimed_at, heartbeat_at, lease_expires_at, result, attempt_no, created_at, updated_at}`
- `list-events {task_id?, limit?: 200}` → `{"events": [{id, at, task_id, request_id, kind, actor, detail}]}` 倒序。

### 5.3 enqueue / set-priority / set-paused

- `enqueue {task_id, kind?, note?, confirm_plan?: bool, expected_plan_sha256?: str}`：kind 缺省按 task.status 推断（`needs_plan`→plan，`ready`→dispatch，`running`→monitor，`in_review`→review）；不允许的推断 → `bad_request`。→ `{"request": Request, "deduplicated": bool}`
  - **计划确认闸（v2）**：`kind="dispatch"` 且任务 `extra_json.plan_execution_hold` 为真时，必须 `confirm_plan:true` 且 `expected_plan_sha256` 等于任务当前 `plan_sha256`——同一事务内完成「哈希复核 + 建 dispatch 请求 + 解除 hold」；哈希不符或未确认 → `conflict`（不建请求、不解 hold）。`plan_execution_hold` 为假/无且 `confirm_plan` 未给时行为同旧版（直接排队）。自动领取与定点 `claim` 均不绕过 hold：hold 未解除前 dispatch 请求不得被领取。
  - `kind="plan"` 不再由本命令直接排队（v2 手动门控）：任务 `needs_plan` 时 enqueue 推断为 plan → `bad_request`（原因：规划需在规划面板确认材料后经 `start-plans` 提交）；其余 kind 照旧。
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

`publish-plan {task_id, plan_path, plan_kind: standard|review_fix, source_sha256, requirements_sha256, generated_at, planner_model, reasoning_effort, request_id, run_id, planner_input_sha256}`

v2 起 `request_id`/`run_id`/`planner_input_sha256` 为必填；旧三字段形态的调用按 `bad_request` 拒绝。

- **事务外核验**（不占写锁）：计划文件在 Plan 根内存在、无符号链接、frontmatter `task_id` 匹配、`implementation_status=未开始`、project 白名单与 repository 一致、重算 `plan_sha256`；`planner_input_sha256` 等于本轮规划输入快照哈希（`planner_inputs` 表 `input_sha256`，与请求 payload 登记一致）；远端 `get-task` 复验任务仍为 To Do 且 `requirements_sha256` 未变——远端不可达 → `remote_unavailable`（保留产物并阻塞发布，不写本地状态）。
- **事务内复核**（单 `BEGIN IMMEDIATE`）：任务活跃（`save_state='active'` 且非 `purged`）、请求为本任务当前 `claimed` 规划请求且 `run_id` 匹配、租约未过期、`planner_input_sha256` 一致；任一不符 → `conflict`/`stale_run`。
- **同一事务**：登记计划字段（plan_path/plan_sha256/plan_kind/generated_at/planner_model/reasoning_effort/source_sha256/requirements_sha256/repository），task→`ready`，置 `extra_json.plan_execution_hold=true`（安排执行前的手动确认闸），结束请求为 `succeeded` 并闭合 attempt；写 `plan_published` event。成功路径不再额外调用 `report succeeded`。
- **幂等**：同 `run_id`、同计划哈希的重复提交返回 `{"ok": true, "deduplicated": true, ...}`；发布事务提交后 runner 崩溃不得回退为失联或失败。
- **拒绝矩阵**：错误 `run_id`/`request_id`、过期租约、输入哈希不符、远端需求已变、远端不可达均拒绝；task `dispatching|prepared|submitting|running` → `conflict`（不得覆盖派发中输入）。
- → `{"ok": true, "task_status": "ready", "plan_sha256": "..."}`（或 deduplicated 幂等形）。

### 5.7 register-review

`register-review {task_id, run_id?, report_path, conclusion: pass|changes_requested|blocked, review_key?, fix_task_id?, fix_task_source_sha256?, repair_plan_path?, kaneo_status?}`

- 校验 report 文件在 Reviewer reports 根内存在、重算 `report_sha256`；fix/repair 字段互相一致（changes_requested 需 fix_task_id + review_key；给 repair_plan_path 则需存在且重算 sha）。
- 写 task.review_*；conclusion=pass → task 可 `done`（由 advance-task 或本命令带 `kaneo_status='done'` 共同确认时）；`blocked` → task `needs_user`。
- → `{"ok": true, "report_sha256": "..."}`

### 5.8 create-review-fix

`create-review-fix {source_task_id, title, description, priority, dedupe_key}` → 经内嵌 bridge 幂等创建（dedupe marker `<!-- codex-kaneo-review:KEY -->`），登记 fix task 行（`needs_plan`，plan_kind=review_fix，review_source_task_id，review_key）→ `{"ok": true, "fix_task_id": "...", "created": bool, "source_sha256": "..."}`。重复调用同 dedupe_key 返回已存在任务，`created:false`。

### 5.9 advance-task

`advance-task {task_id, run_id?, target: "in-review"|"done", expected_current: "in-progress"|"in-review"|"done", evidence_note?}`

- 允许的状态对：(in-progress→in-review)、(in-review→in-review)、(in-progress→done)、(in-review→done)、(done→done)。
- 核心经 bridge：`get-task` 验证 projectId/status/requirements → `update-status` → 再 `get-task` 验证目标态与指纹。**`expected==target` 为被动同步对**（`(in-review,in-review)`、`(done,done)`）：远端已在目标态时跳过 `update-status` 写，只做核验——用于 monitor 同步「远端被他人推进」的任务。
- **Review 前置守卫（v1.2）**：`target=done` 且 `expected_current="in-progress"` 时，要求 task 当前周期已登记 `review_conclusion=pass`；否则 `conflict`（执行完成不得跳过 Review 直接完成）。`expected_current="in-review"|"done"` 不受此限；`resolve mark_done` 人工登记不受此限；`sync-tasks` 观察到远端 `done` 属被动同步亦不受此限。
- 成功：`in-review` → task `in_review` + 建 review 请求（新一轮评审：清 `review_report_path`/`review_report_sha256` 与上一周期 `review_conclusion`/`review_registered_at`，保留 `fix_task_id`/`review_key` 历史关联）；`done` → task `done`。event `kaneo_transition`。
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

## 5.14 看板补充裁决（v1.2）

1. **failure_code 词表**（report `needs_user`/dispatch-fail/migrate 使用，供 §2 归列判定）：
   - 失败类（→ `failed`）：`plan_failed`（规划明确失败）、`validation_failed`（派发确定性校验）、`dispatch_failed`（派发执行失败且 send_state=not_sent）、`execution_failed`（执行明确失败）。
   - 阻塞类（按 failure_stage 归列）：`reported`（通用需人工）、`observation_blocked`（暂时无法查看进度/锁屏）、`remote_unavailable`、`remote_conflict`、`send_conflict`、`send_unknown`、`executor_lost`、`interrupted_dispatch`、`review_blocked`（评审受阻）。
   - 未识别/缺失码一律按阻塞类处理——失败列只认明确证据；`send_state='unknown'` 优先于一切 code 判阻塞。
2. **`board_column` 一致性**：`list-tasks` 与 `get-task` 同任务同列；`overview.counts` 增加 `board` 键：`{needs_plan,todo,in_progress,in_review,done,failed}` 各列计数（含 needs_user 归列结果）。
3. **待修复**：`in_review` 且当前周期 `review_conclusion='changes_requested'` → `board_substatus='待修复'`，`review_summary.fix_task_id` 关联修复任务；修复任务自身走 needs_plan→…→done 流程，不占原任务的列。
4. **恢复即恢复**：resolve 后阻塞字段清除，归列回到生命周期直映射；`extra_json` 中历史 `failure` 记录不影响归列。
5. **`sync-tasks` 远端 done**：观察到 `done` → task `done`（board_column=done），与 Review 守卫不冲突（被动同步非自动推进）。
6. **评审受阻**（`register-review blocked` 或 review report needs_user）：`failure_code=review_blocked` → 留 `in_review` 列，`board_substatus='评审受阻'`。
7. **桌面信任边界**：桌面只按 `board_column` 分列、`board_substatus` 标子状态、`blocked_reason` 显阻塞；不得用 `display_status`/plan_path 存在性/attempts 历史二次推断列归属。
8. **`list-tasks board_column` 非法值** → `bad_request`（与 §1 未知参数同口径）。

## 5.15 保存状态、异常归列与会话读取补充裁决（v1.3）

### 5.15.1 save_state（任务保存状态，独立于生命周期）

`save_state` 是任务在调度台的**保存状态**，与 `task.status` 生命周期正交，不复用生命周期值：

| save_state | 含义 |
|---|---|
| `active` | 活跃，出现在看板（默认） |
| `archived` | 已归档：移出活跃看板，保留全部详情/会话/计划/报告/执行记录与关联，可恢复 |
| `trashed` | 已移入本地回收站，可恢复 |
| `purged` | 永久从列表移除的终态（v2）：不可恢复、不排队、不修改；历史与文件全部保留，仅退出常规列表与统计 |

- 库内列：`tasks.save_state TEXT NOT NULL DEFAULT 'active'`、`tasks.saved_at TEXT`（移出时间）、`tasks.auto_advance_paused INTEGER NOT NULL DEFAULT 0`（恢复后默认暂停自动推进）。
- 归档/回收站**只影响调度台**：不删除 Kaneo 任务、计划文件、报告或执行端会话； Kaneo 状态不被本地异常分类或归档动作修改。
- **已完成（board_column=done）与已归档（save_state=archived）是两个概念**：归档不等于标记完成，完成任务不自动归档。

### 5.15.2 archive-task / trash-task / restore-task

| 命令 | payload | 效果 |
|---|---|---|
| `archive-task` | `{task_id, expected_save_state?: string}` | save_state→`archived`，写 `saved_at`；同事务取消该任务 `queued` 请求；event `task_archived` |
| `trash-task` | `{task_id, expected_save_state?: string}` | save_state→`trashed`，写 `saved_at`；同事务取消 `queued` 请求；event `task_trashed` |
| `restore-task` | `{task_id, resume_auto?: bool=false}` | save_state→`active`、清 `saved_at`；`auto_advance_paused = resume_auto ? 0 : 1`；本地重算计划/来源哈希并尝试远端 `get-task` 观察（远端不可达→warning 不阻断）；按核验后状态重新归列；event `task_restored` |

- **禁用条件**（`conflict` + 中文原因，UI 展示）：`task.status ∈ {planning, dispatching, prepared, submitting, running, in_review}`（正在规划/派发/执行/评审）、存在**未解除失联**请求（`claimed` 且租约过期）、`send_state='unknown'`（发送不明），或存在 `claimed` 请求（已被执行端领取）。其余状态（含 `needs_user` 其他阻塞、`needs_plan`、`ready`、`done`）允许移出。核心统一在任务摘要输出 `move_out_eligibility: {allowed, reason}`。
- **排队取消与领取竞争**：取消在单个 `BEGIN IMMEDIATE` 事务内对该任务 `status='queued'` 的请求执行 `cancelled`；事务内复核状态，若已被 `claimed`（执行端已领取）→ 中止并返回 `conflict`（原因：任务已被执行端领取，暂不能移出看板），不取消、不改保存状态。
- **保存状态冲突核验**：`archive-task` 与 `trash-task` 支持可选 `expected_save_state`；已处于目标状态按幂等成功处理；若当前保存状态与 `expected_save_state` 不符（且不是目标状态）则返回 `conflict`。
- **幂等**：对已处于目标 save_state 的任务返回 `{"ok": true, "deduplicated": true}`，不重复写事件。
- `restore-task` 对 `active` 任务同样幂等（仍执行状态核验并返回任务）。

### 5.15.3 调度入口排除（归档/回收站/恢复暂停）

- `list-tasks` 默认只返回 `save_state='active'`；新增过滤参数 `save_state` ∈ `active|archived|trashed|all`（非法值 `bad_request`）。`overview.counts` 只统计 active。
- `tick`：不为 `save_state != 'active'` 或 `auto_advance_paused=1` 的任务建 plan/monitor/review 请求（workspace 级 `plan-scan` 不受影响）。
- `claim`：候选请求所属任务 `save_state != 'active'` 或 `auto_advance_paused=1` → 跳过（自动取单时视不存在）；按 `request_id` 指定领取 → `{"claimed": false, "reason": "task_archived"|"task_paused"}`。
- `enqueue`：目标任务 `save_state != 'active'` → `conflict`；`auto_advance_paused=1` → `conflict`（原因：任务已暂停自动推进，请先恢复自动推进）。
- `sync-tasks`：`save_state != 'active'` 的任务**整体跳过**（不 upsert、不改状态/指纹、不建请求）——后续同步不得把归档/回收站任务重新导入看板。

### 5.15.4 看板归列 v1.3（board_column / lifecycle_column / exception）

读取层统一输出三个字段（TaskSummary/TaskDetail 均有）：

- **`lifecycle_column`**（纯生命周期位置，无异常逻辑）：`needs_plan|planning→needs_plan`；`ready|dispatching|prepared|submitting→todo`；`running→in_progress`；`in_review→in_review`；`done→done`；`needs_user` 按 `failure_stage` 归列（缺失回退 `task.stage`，同 v1.2 口径）。
- **`board_column`**（六列唯一分列依据；异常一律进第六列，正常列不再混入异常卡片）：
  1. `status='needs_user'` → `failed`；
  2. `in_review` 且当前周期 `review_conclusion='changes_requested'` → `failed`（评审未通过·待修复）；
  3. 存在活动 `claimed` 请求且租约过期（失联）→ `failed`；
  4. 其余按 status 直映射（同 lifecycle_column 的非 needs_user 部分）。
- **`exception`**（结构化异常信息；无异常为 `null`）：

```json
{"kind": "failed|send_unknown|manual_block|executor_lost|review_blocked|observation_blocked|review_rejected",
 "label": "规划失败|派发失败|执行失败|失败|发送不明|需人工处理|执行端失联|评审受阻|监控异常|评审未通过·待修复",
 "severity": "failed|blocked",
 "detail": "<原因文本，同 blocked_reason>",
 "failure_code": "<原始 failure_code 或 null>",
 "fix_task_id": "<review_rejected 时关联的修复任务，否则 null>"}
```

kind 判定：失联（规则 3）→ `executor_lost`；`changes_requested`（规则 2）→ `review_rejected`；`needs_user` 时 `send_state='unknown'` → `send_unknown`（优先于一切 code），`failure_code=observation_blocked` → `observation_blocked`，`failure_code=review_blocked` → `review_blocked`，`failure_code ∈ {validation_failed, dispatch_failed, execution_failed, plan_failed}` → `failed`，其余 → `manual_block`。`severity`：仅 `failed` 为 `failed`（红），其余为 `blocked`（橙）。

- `board_substatus` 在第六列等于 `exception.label`；前五列维持 v1.2 词表。
- **历史错误不影响已恢复任务**：resolve 清阻塞字段后归列回 lifecycle 直映射；`extra_json` 历史记录不参与归列。
- **观测异常自动清除**：`report outcome=succeeded` 且 `kind=monitor` 时，若任务处于 `needs_user` 且 `failure_code='observation_blocked'` → 清阻塞字段、任务回 `running`（监控连接恢复即自动清除对应观测异常）。
- **不自动解除的异常**：发送不明、人工阻塞、失联租约只经现有恢复流程（resolve `restore`/`accept_duplicate`/`mark_sent`/`retry`/`replan`/`dismiss`/`release_lost`）处理；**收到新会话日志不解除任何异常**。
- `list-tasks` 的 `board_column` 过滤参数沿用 v1.2 语义（派生后过滤），第六列值为 `failed`。

### 5.15.5 会话适配（本机运行记录）与会话命令

- 会话页 Plus 控制入口（2026-10-07）：`session-control-status {task_id,run_id}` 只读返回 `{task_id,run_id,request_id?,backend?,state,detail,connected,session_id?,turn_id?,can_start,blocked_reason?,start_label}`；按已核验 job 中固化的连接读取 Plus 状态，不返回凭据、原始状态或权限请求正文。`state` 为 `unavailable|not_started|running|waiting_permission|idle|disconnected|executor_lost|unsupported`，错误原因脱敏。
- `session-request-start {task_id,run_id,request_id}` 仅恢复同一已授权的 Plus 规划请求。活跃、未过期领取与存活原进程禁止重排；身份、当前轮次、活跃保存状态、暂停与材料授权必须再次核验。仅旧进程已退出且租约过期、尚未发送的轮次（无 session/turn/send_state）可将**同一 request**重新排队，保留原 payload 与 attempts，恢复领取前任务状态；队列中的重复请求幂等。已发送或发送未知不重发、不创建第二会话，按钮提供原因。返回 `{request_id,stage:"plan",queued:true,deduplicated:bool}`，App唤起原有单飞规划调度器；点击才启动，不因读取状态自动开始。该入口不直接调用 Plus prompt，不修改历史 job。
- Plus规划进程捕获意外内部异常时，对仍持有有效请求/轮次/租约的自身现场记录明确失败和terminal；失去归属不覆盖新领取。异常输出仅固定说明与异常类型，不持久化可能含凭据的原始异常文本，不自动重试或重发。

- 正常等待事件日志（文件尚未生成或为空）返回 `session.state=pending`，不将等待说明写入表示读取错误的 `connection.detail`；界面按状态展示等待说明。真实读取/解析失败即使无快照而回退为 `pending`，仍保留 `connection.detail`；调用方不得仅凭 `pending` 隐藏错误。

- 数据源为**本机运行记录**（v1.6 起后端为 pi；v2 起规划轮次 backend=codex，见 §5.22.3；v3 起任务执行轮次可为 backend=pi_desktop，见 §5.23.3）：运行记录根目录取 settings `session_runs_root`，缺省 `~/Library/Application Support/KaneoPilot/runs`。按 `<root>/<task_id>/<run_id>/` 定位执行目录：先核验 `job.json` 的 `task_id`/`run_id` 与目录名一致且 `backend` 字段存在；`backend` 不在 `(pi, codex, pi_desktop)` 的历史轮次 → `unavailable` + detail「旧后端会话已弃用」，再读取同目录 `events.jsonl`。**不按标题猜测关联。**
- **路径安全**：task_id/run_id 仅允许 `[A-Za-z0-9._-]`；解析后的执行目录必须仍位于根目录内（拒绝 `..` 与符号链接逃逸）；身份不符或越界 → 读取失败（`state=unavailable` + detail 说明），不得读取他目录。
- **会话关联**：以 `task_id + run_id` 绑定确切会话（`session_marker = "<task_id>/<run_id>"`）。
- 会话状态 `state` ∈ `associated|pending|unavailable`：`associated` = 身份核验通过且 `events.jsonl` 成功解析；`pending` = 执行目录与 `job.json` 有效但 `events.jsonl` 尚未导出（UI 显示「等待会话记录」，UI 端继续轮询）；`unavailable` = 执行目录/`job.json` 缺失、身份不符、越界或旧后端弃用（UI 显示原因 + 重试）。连接状态 `connection.state` ∈ `ok|degraded|unavailable`：本机记录无推送订阅 → 恒 `degraded`（UI 按 `session_poll_seconds`，默认 2 秒增量检查导出更新）；根目录不存在等机制性失败 → `unavailable`。读取失败（如 JSON 半写入）时 `connection` 仍 `degraded`，`detail` 携带原因，**保留上一份有效内容，不当作空会话**。
- **半写入容忍**：`events.jsonl` 正在写入导致解析失败时，本轮返回上一份有效快照（进程内缓存）并在 `connection.detail` 说明；无上一份时按 `pending` 处理，不报「暂无消息」。
- 消息结构（稳定标识、顺序、类型、内容、修订版本、来源时间）：

```json
{"id": "<源稳定 id>", "seq": 12, "run_id": "...", "type": "user|assistant|tool_call|tool_result|command_output|system",
 "content": "<完整文本>", "revision": 3, "source_at": "ISO8601",
 "tool": {"name": "...", "input": "...", "output": "...", "exit_code": 0}}
```

`tool` 仅 tool_call/tool_result/command_output 类型出现。读取按 `seq` 排序、按 `id`+`revision` 去重/更新，游标推进不回退。

| 命令 | payload | 返回 |
|---|---|---|
| `list-sessions` | `{task_id?}` | `{"sessions": [SessionInfo]}`；缺省返回全部有关联任务的会话（active 任务优先） |
| `get-session` | `{task_id, run_id}` | `{"session": SessionInfo, "connection": {...}}` |
| `session-messages` | `{task_id, run_id?, cursor?, limit?, source_version?, force_reload?}` | `{"session": SessionInfo, "messages": [Message], "next_cursor": "...", "has_more": bool, "connection": {...}}` |

- `run_id` 缺省取该任务当前/最近一个带 `session_marker` 的执行轮次；无会话 → `state=pending`，`messages=[]`。
- 首次加载（无 cursor）返回完整历史；增量读取传 `next_cursor`；连接中断后重连由**调用方带最后 cursor** 补齐缺失（本地 `session_reads` 记录每会话读取位点，仅作位点记录，服务端**不得**在 payload 无 cursor 时隐式套用已存位点——那会让重开阅读器只拿到增量而丢历史）。半写入保留的上一份有效快照须**跨进程持久**（CLI 每次调用是新进程），持久位在 `session_reads`。
- **会话输出只作为进度展示**：完成/失败/进入评审仍由受控生命周期命令确认；原有定时监控保留为流程保障。
- 卡片分别显示「会话更新时间」（`session.updated_at`）与「最近状态检查」（`last_checked_at`），不把心跳时间当作进度更新时间。

#### 执行会话读取优化（2026-10-06）

**跨进程的小元数据快路径**

CLI 每次是新进程，不使用 Python 全局缓存解决轮询；也不为本次引入常驻读取 daemon 或数据库迁移。

新增内部键 `settings["session_snapshot_meta:<task_id>/<run_id>"]`，仅由核心命令写。字段为 `cache_schema:1`、`source_signature`、`source_version`、`record`、`max_seq`、`full_snapshot_available`。`record` 仅保存现有 SessionInfo 的来源摘要，不复制消息数组或完整 job 内容。签名包含规范化 runs 根、task/run/backend，以及 job/events 的 `dev/ino/size/mtime_ns/ctime_ns`；版本为规范化签名的 SHA-256。读取前后签名一致且解析新鲜成功，才保存该版本。保持现有路径越界、job 身份、后端和符号链接规则。

公共增补只涉及 `session-messages`：

| 方向 | 字段 | 行为 |
| --- | --- | --- |
| 请求 | `source_version?: string` | 调用方已读完该版本；未知或不匹配版本走正常读取 |
| 请求 | `force_reload?: bool`，默认 false | 强制读取来源，仍使用显式 cursor 做消息补齐，不清空 UI |
| 响应 | `source_version: string|null` | 只在稳定、成功的来源读取或已确认快路径返回有效值；失败/兜底/pending/unavailable 返回 null |

只有当前身份核验成功、前后签名稳定且与 meta 相符、请求版本与 meta 相同、显式 cursor 已到达 `meta.max_seq`、未要求强制刷新时，才返回 `messages=[] / has_more=false / 原 cursor`。此时不读 events 正文、不解码完整快照、不转换/排序消息、不序列化全文，也不解码大 revision map；仅更新读取位点的状态检查时间，并按当前 job/索引/时钟派生 SessionInfo 与允许操作。用 `session_store` 中的小型 touch 入口更新原字段，桌面仍不直接写库。

UI **仅在当前来源版本的分页全部读取完成（`has_more=false`）后**保存可确认版本；切轮次/重新打开清空，手动刷新传 `force_reload:true`。串行分页时不把未读完页的版本用于快返。旧客户端缺少字段继续正常读取，旧快照缺少 meta 时冷解析后升级内部缓存。首次无 cursor 必须仍读完整历史，不能拿数据库位点跳过正文。

超过现有 2 MB 全文快照上限时，成功解析后仍可保存小 meta，标记 `full_snapshot_available:false`；已读完的 UI 可使用无变化快路径。源变化、重新打开或强制刷新仍读来源。meta 不能代替最后有效全文：超限会话仍依赖 App 保留已加载正文；已有完整旧快照仍沿原失败兜底，不能声称已保证最新大文件跨进程恢复。

全文快照只在源版本变化且新鲜读取成功时尝试保存。文件追加、同尺寸改写、替换、截断、root/身份变化、半写失败必须使快路径失效；签名变化期间不重复盲读，不确认该版本，下次正常定时检查再读取。`get-session` 内部复用本次 `_associate` 已解析的事件生成 `round_detail/startup_turn_id`；引用内部 `_events`，不将它加入公共 JSON 或快照。

**本次维持既有 `seq:<n>` 游标、revision 映射与监控分页协议。** 已发现仅旧消息修订时响应游标可回退、首页 revision map 收入未来消息，以及 UI/monitor 共享修订位点的相邻问题；这些不是本次快路径的实现前提。本计划不顺带改自动监控。T1 的兼容检查须证明末端确认、重新打开、修订与分页没有因快路径被跳过；发现新增回归必须修正快路径，不靠修改监控完成判定消除失败。

源确实变化时仍走现有全量解析，因此本计划承诺降低无变化轮询成本和界面重复工作，不将其描述为完整的日志增量索引。


### 5.15.6 allowed_actions 新增

`archive`（可归档时）、`trash`（可移入回收站时）、`restore`（`save_state != active` 时）、`resume-auto`（`auto_advance_paused=1` 时）。禁用原因由 `blocked_reason`/命令错误原文提供。

## 5.16 执行轮次索引与会话回收站（v1.5）

### 5.16.1 轮次索引 `execution_rounds`

以 `task_id + run_id` 唯一定位一个执行轮次（= session_marker 的两段），调度台本地表：

| 列 | 语义 |
|---|---|
| `task_id`, `run_id` | 复合主键；`session_marker = "<task_id>/<run_id>"` |
| `backend` | `pi`（历史行可为 `dsh`/`headless`，读取层已弃用为 unavailable） |
| `session_id`, `session_title` | 会话关联（pi 轮次由 worker 写入 job.json，读取层回填） |
| `run_status` | `pending\|running\|finished\|failed\|unknown`（最近观察值） |
| `config_json` | dispatch claim 时固化的 PI 执行配置快照（见 §5.17），此后设置变更不影响该轮 |
| `worktree_path` | 该轮独立 worktree 绝对路径（见 §5.18.1） |
| `started_at` | **启动意图落盘时间**（dispatch-start 事务写入；非空即禁止再创建进程） |
| `pid` | 已启动 worker 进程号（诊断/展示用，不作为存活或重启依据） |
| `save_state` | `active\|trashed`（会话保存状态，与任务 save_state 正交） |
| `saved_at`, `created_at`, `updated_at` | ISO 8601 UTC |

- **写入点**：① dispatch claim 时创建（backend/config 快照固化；v3 起 backend ∈ `pi|pi_desktop`，快照形状见 §5.23.3）；② 读取层回填——`list-sessions`/`get-session`/`trash-session`/`restore-session` 按 runs 根目录 `job.json` 身份核验（同 §5.15.5）后补入缺失行。
- **回填永不改写 `save_state`/`saved_at`**：已存在行只刷新 `backend/session_id/session_title/run_status/worktree_path/updated_at`——重复导入不得复活已删除会话。
- `run_status` 观察：pi 轮次以 `job.json.status` 为准并在读取时刷新索引；无 job.json/无 status 字段保持已存值（无则 `unknown`）。
- 删除会话只改本表 `save_state`；不终止进程、不改任务生命周期、不删事件日志/运行记录/工作目录。

### 5.16.2 `trash-session` / `restore-session`

| 命令 | payload | 效果 |
|---|---|---|
| `trash-session` | `{task_id, run_id, expected_save_state?}` | `save_state→trashed`、写 `saved_at`；event `session_trashed` |
| `restore-session` | `{task_id, run_id}` | `save_state→active`、清 `saved_at`；event `session_restored` |

- 返回 `{"ok": true, "deduplicated": bool, "session": SessionInfo}`；已处于目标状态 → `deduplicated: true`，不重复写事件；`expected_save_state` 与当前不符（且非目标态）→ `conflict`。轮次不存在且无法经身份核验回填 → `not_found`。
- **禁止删除**（`conflict` + 中文原因；`list-sessions` 同轮返回 `blocked_reason` 同文案），任一成立即禁：
  1. **仍在执行/评审**：索引 `run_status='running'`，或该轮为任务当前轮次（task.session_marker 匹配）且 `task.status ∈ {planning, dispatching, prepared, submitting, running, in_review}`；
  2. **失联待处理**：存在 `run_id` 匹配且租约过期的 `claimed` dispatch 请求（未经常 `resolve release_lost` 解除）；
  3. **发送状态不明**：该轮为任务当前轮次且 `task.send_state='unknown'`。
- 删除**不停止进程**、不修改任务生命周期、不删除任何文件；`restore-session` 无禁止条件。两侧均可重复调用。
- 历史轮次（非当前轮次、run_status 非 running、无失联）允许删除。

### 5.16.3 `list-sessions` / `get-session` 扩展

- `list-sessions` payload 增加 `save_state ∈ active|trashed|all`，缺省 `active`；非法值 `bad_request`。
- `SessionInfo` 增加字段（`get-session`/`session-messages` 返回同扩展）：

```json
{"backend": "pi|dsh|headless", "run_status": "pending|running|finished|failed|unknown",
 "save_state": "active|trashed", "saved_at": "ISO|null",
 "allowed_actions": ["trash-session"] | ["restore-session"] | [],
 "blocked_reason": "<中文原因|null>"}
```

- `allowed_actions`：active 且非禁止 → `["trash-session"]`；trashed → `["restore-session"]`；active 且禁止 → `[]` + `blocked_reason`。

## 5.17 PI 执行配置与能力查询（v1.6）

### 5.17.1 配置对象

settings 键 `exec_config`（JSON；v3 起允许 `backend`/`pi_desktop` 扩展字段，见 §5.23.3——缺省按 `backend:"pi"` 解释）：

```json
{"provider": "CloseAI", "model": "deepseek-flash",
 "reasoning_effort": "xhigh", "agent_preset": "default",
 "updated_at": "ISO"}
```

- **初始默认**（未保存时生效值）：`CloseAI / deepseek-flash / xhigh / default`。
- `agent_preset` 对应 `agents_dir`（缺省 `<app_support>/agents`，`KANPILOT_AGENTS_DIR` 覆盖）下的 `<name>.md` 文件；worker 经 `pi --system-prompt <file>` 注入。
- 凭据只查就绪态：`pi auth check --provider <p> --model <m>` 的退出码；任何命令、界面、日志、数据库**不得**输出或持久化凭据值，也不读取 `~/.pi` 之外的 pi-desktop 内部库。
- 调度台改配置只写本库 `settings.exec_config`；不写 `~/.pi/agent/*`、不动 pi 全局配置。

### 5.17.2 命令

- `get-exec-config {}` → `{"config": {...}|null, "defaults": {...}}`；`config=null` 表示从未保存，`defaults` 恒为初始默认。
- `set-exec-config {provider?, model?, reasoning_effort?, agent_preset?}`（至少一个字段；未知字段 `bad_request`）：
  - 对照 `exec-capabilities` 校验：`provider`/`model` 必须存在；`reasoning_effort` 必须是**该模型支持值之一**（禁止显示/接受模型不支持的等级）；`agent_preset` 必须在 roster（`<name>.md` 存在）且无 `broken`。
  - 失败 → `bad_request` + 具体原因，**不静默降级、不自动换模型或 agent**；成功 → 写 `exec_config` 并返回 `{"config": {...}}`。合并更新（只给部分字段时保留其余已存值；从未保存时其余字段取默认）。
- `exec-capabilities {}` →

```json
{"ok": true,
 "pi": {"path": "/path/to/pi|null", "version": "0.85.1|null",
        "home": "/Users/x/.pi"},
 "providers": [{"id": "CloseAI", "display_name": "CloseAI",
   "models": [{"id": "deepseek-flash", "name": "...",
               "reasoning_efforts": ["minimal","low","medium","xhigh"]}]}],
 "presets": [{"id": "default", "name": "default",
              "description": null, "broken": null,
              "path": "<agents_dir>/default.md"}],
 "checks": [{"name": "pi_binary", "ok": true, "detail": "..."}]}
```

  - 数据源：`PI_HOME`（缺省 `~/.pi`，`KANPILOT_PI_HOME` 覆盖）下 `agent/models.json` 的 `providers.<id>.models`；`reasoning_efforts` 由模型 `reasoning` + `thinkingLevelMap` 推导（reasoning=false → 空集）。preset roster = `agents_dir` 下全部 `*.md`（文件名 stem 为 id）。
  - `pi` 可执行：`KANPILOT_PI_BIN` 覆盖 → PATH 解析 → 回退 `~/.npm-global/bin/pi`（Finder 启动的 app PATH 不含用户 bin 目录）；运行 `<bin> --version` 取首个 `x.y.z` 形态行（超时 10s）。
  - `checks` 至少含：`pi_binary`、`models`（models.json 可读且含 providers）、`agent_preset`（agents_dir 下存在 *.md）、`credential`（当前配置 provider/model 的 `pi auth check` 退出码；detail 只写就绪/未就绪摘要，不写密钥值）。
  - 任一关键项失败 → `ok:false`，具体项 `ok:false` + detail（仍返回已解析到的部分）。

### 5.17.3 配置快照与派发阻断

- **快照固化**：dispatch claim 时把当前生效配置复制进该轮 `execution_rounds.config_json`；此后 `set-exec-config` 只影响新轮次。
- **派发阻断**：dispatch claim 校验链（§4.5）追加第 7 步 `exec-capabilities` 可用性检查（`ok==true`，即 pi 可执行、models.json 可读、当前配置的 provider/model/effort/preset 有效、凭据就绪）。不满足 → 按 §4.5 确定性失败处理（`validation_failed`，task → `needs_user`），**不自动换模型或 agent**；`dispatch-start` 落盘前复核同一条件（失败 → `validation_failed`，不启动进程）。
- 能力检查反映**当前进程环境**（pi auth 状态即执行端环境）；执行端环境缺凭据即阻止派发。

## 5.18 PI 执行接入（backend=pi，v1.6）

### 5.18.1 后端、进程与 worktree

- **新 dispatch claim 按 `exec_config.backend` 固化**：缺省 `backend:"pi"`（claim context 增加 `backend` 与 `exec_config` 快照 `{provider, model, reasoning_effort, agent_preset}`）；v3 起 `backend:"pi_desktop"` 时走 §5.23.3 分支（快照 `{backend:"pi_desktop", connection_file, provider_id, model_id, thinking_level, permission_mode}`）。**历史轮次**（rounds.backend ∈ `dsh|headless` 等旧值）：DB 行保留，读取层返回 `unavailable` + detail「旧后端会话已弃用（仅 pi 可读）」，不取消、不重发。
- 每轮启动**独立 pi worker 进程**（`python3 -m kanpilot.pi_worker --spec <spec.json>`），并使用**独立 worktree**：`worktree_path = worktrees_root/<task_id>/<run_id>`（settings `worktrees_root`，缺省 `<app_support>/worktrees`），自 `task.repository` 创建；会话删除不删除 worktree。
- worker 拉起 `pi -p --mode json --session-dir <run_dir>/pi-sessions --model <provider>/<model> --thinking <level> --system-prompt <agent.md>`（`agent.md` 为空则不传该参数），`cwd=spec.cwd`，`start_new_session`，env 透传 + `PI_HOME`（`KANPILOT_PI_HOME` 优先）。stdout 逐行消费 JSONL：OSC 序列与非 JSON 行丢弃。
- 运行记录目录沿用 `<session_runs_root>/<task_id>/<run_id>/`（缺省 `~/Library/Application Support/KaneoPilot/runs`）：`job.json`（`backend:"pi"|"codex"|"pi_desktop"`、`session_id`/`session_title`、config 快照、pid、`status ∈ running|finished|failed`、起止时间、`exit_code`、`detail`；pi_desktop 轮次另含 §5.23.3 的 `turn_id`/`send_state`）+ `events.jsonl`。`job.json` 身份核验（task_id/run_id/backend）同 §5.15.5；身份不符 → 读取 `unavailable` + `run_status` 记 `failed`（detail=identity_mismatch），不读他目录。

### 5.18.2 pi 事件流 → events.jsonl（事件契约不变）

worker 把 `pi --mode json` 的 JSONL 帧映射为 `events.jsonl`，每行 `{"v":1, "seq":<int 单调递增>, "ts":"ISO", "type":...,...}`；**半写行容忍**（尾行截断丢弃，保留上一份有效内容），**首事件恒为 `session`**，收尾写序恒「job 先、session 补齐、terminal 最后」。pi → events 映射：

| pi 帧 | events `type` | Message.type | 说明 |
|---|---|---|---|
| `session`（id/name/cwd） | `session` | `system` | **首事件**，dispatch-confirm 核验凭据 |
| `message_end`（user） | `user` | `user` | 提示词/指令 |
| `message_end`（assistant，text+thinking 块拼接） | `assistant_message` | `assistant` | 同 seq 内容更新 → revision 变化 |
| `tool_execution_start` | `tool_call` | `tool_call` | `tool.name`/`tool.input` |
| `tool_execution_end` / `toolResult message_end` | `tool_result` 或 `command_output` | 同名 | bash 类工具 → `command_output`，exitCode 入 `tool.exit_code` |
| `turn_end`/`agent_end` | （驱动 job，不发消息） | — | `agent_end`+exit 0 → finished/completed |
| 进程终了 | `terminal` | `system` | `outcome`/`reason`，驱动 `run_status` |

- 稳定标识 `id = "round:<seq>"`、`seq` 排序、`id+revision` 去重/更新、`revision` 为内容 sha 派生整数——全部沿用 §5.15.5 口径；`run_id` 由命令层注入。
- `extension_ui_request` 等 UI 交互帧与 OSC 噪声**不进入** events.jsonl。
- 覆盖要求：正文、工具调用、命令输出、子 agent 事件均须透出到阅读器消息流。

### 5.18.3 派发状态机（pi 分支）

顺序：`claim(→dispatching, 固化快照)` → `dispatch-prepare` → `dispatch-begin-send`（远端迁移，语义不变）→ `dispatch-start` → `dispatch-confirm`（→running）；失败仍走 `dispatch-fail`。

- **claim context（v1.6）**：context 增加 `backend: "pi"` 与 `exec_config` 快照；`instruction` 为无附件形态——`任务标识：<task_id>/<run_id>。读取并完整执行计划文件 <plan_path>，保留现有改动，按计划使用 subagent 和独立 worktree，完成验证并报告结果。`
- **`dispatch-prepare` evidence**：`{"session_marker","repository","backend":"pi","config":{provider,model,reasoning_effort,agent_preset}}`。核心校验：marker/repository 等于任务；`backend` 与该轮一致；`config` 全等于该轮 `config_json` 快照；计划哈希自 claim 未变。通过 → `prepared`（存 `ui_evidence`）。
- **`dispatch-start {task_id, run_id}`**：要求 task `submitting`（即 `dispatch-begin-send` 已完成）、该轮有 claimed dispatch 请求、轮次 `backend=pi`。
  1. **可用性复核先行**：§5.17.3 门 → 失败 `validation_failed`（不落意图、不启动进程；任务交 dispatch-fail 处置）。
  2. **意图先落盘**：单事务写入启动意图 `started_at`；已存在启动意图 → 返回 `{"status":"started","deduplicated":true,...}`，**绝不创建第二个进程**（同一轮次只允许一次真实启动，不凭 PID 判断存活去重启）。
  3. 创建 worktree（已存在同路径则复用；`task.repository` 非 git 仓库时退化为普通目录并在 spec 记 `worktree_mode:"plain"`）→ 写 `spec.json`（含 `pi_home`/`agent_md`/`timeout_seconds` 等）→ 拉起 `kanpilot.pi_worker` 进程（独立会话，stdout/stderr 重定向 `worker.log`）→ 事务内记录 `pid`/`worktree_path` → event `round_started`；拉起失败 → `internal`（意图保留供取证）。
  4. 轮询 `events.jsonl` 首个 `session` 事件至多 `pi_start_wait_seconds`（默认 30，env `KANPILOT_PI_START_WAIT` 秒可覆盖）。
  - 返回 `{"status":"starting"|"started", "pid": int|null, "session_id": str|null, "worktree_path": "...", "deduplicated": bool}`；超时未见 session 事件 → `status:"starting"`、`session_id:null`（执行端经 `get-session` 轮询到 `session_id` 后再 confirm）。
- **`dispatch-confirm` evidence**：`{"session_marker","repository","backend":"pi","session_id":"...","protocol_seen":true,"config":{...}}`。核心校验：该轮启动意图已落盘且 `started_at` 非空；`events.jsonl` 已出现 session 事件且 `session_id` 匹配；`config` 等于快照 → 走既有 running 迁移（sent/send_state/monitor 请求/释放串行位不变）。session 事件缺失 → `evidence_invalid`，task 停留 `submitting`（同 §5.13-7 口径）。
- **`dispatch-fail` 沿用**；`send_state=unknown` 时保留启动意图（pid/session_id/config）供恢复与取证——发送结果不明不得清空恢复信息。

### 5.18.4 监控、完成判定与异常

- monitor 读取轮次状态与执行证据：`get-session` 返回的 `SessionInfo` 含 `run_status`，并返回 `round_detail`：

```json
{"round_detail": {"backend":"pi","run_status":"running","worktree_path":"...",
  "pid": 123, "started_at":"ISO|null","ended_at":"ISO|null","exit_code":0|null,
  "terminal":{"outcome":"completed|failed","reason":"..."}|null,
  "config":{...}}}
```

  执行证据 = `events.jsonl` 中的工具/命令输出与终止事件（经 `session-messages` 读取）。
- **完成判定须同时具备**：(a) 主会话 `terminal` 事件 `outcome=completed`；(b) 进程结果 `job.json.status=finished` 且 `exit_code=0`；(c) 验证证据（本轮工具事件中，计划 verify 块的每条命令最后一次匹配的整数退出码为 0；缺失转 verification_unconfirmed，用户可接受验证结果）。三者齐 → `advance-task --target in-review` → `report succeeded`（既有流程不变，Review 仍是 Done 的前置）。
- **明确异常**（→ `run_status=failed`，detail 记原因；monitor `report needs_user` + `failure_code=execution_failed`）：崩溃（进程非 0 退出/信号）、超时（超过 `pi_round_timeout_seconds`，默认 14400，settings 兼容旧键 `dsh_round_timeout_seconds` 回退）、日志截断（events.jsonl 尾部不完整且无 terminal）、身份不符（job.json/事件身份与目录不符）。**不自动重新提交、不自动重建轮次**。
- **单独条件不得推动完成**：子 agent 结束、进程正常退出、模型自称完成——任一单独出现都不满足 (a)(b)(c)，任务保持 `running` 或按异常处置。

### 5.18.5 worktree 与评审衔接

- context / `get-task.execution_round` 提供 `worktree_path` 时，monitor 验证与 review 评审在该 worktree 内进行（`git -C <worktree>` 读改动）；主仓库检出不被改动。
- Done 后 worktree 与运行记录保留；受 PR 交付管理的任务由调度台提交，Review 通过后推送并开 PR，合并后完成；调度台不 merge；普通手动任务沿旧流程。

## 5.19 执行端调度（App 内置，v1.6）

- 四个执行端（plan/dispatch/monitor/review）由 **KaneoPilot App 内置 ExecutorScheduler** 拉起：定时器按错开分钟触发（plan 0/15/30/45、dispatch 4/19/34/49、monitor 8/23/38/53、review 12/27/42/57 分，与旧 automation 契约一致）。
- 每次触发：`executor-status` 查单飞（该 stage `running` 非空则跳过）→ `python3 -m kanpilot.executor_runner --stage <stage>`（core 根为 cwd；PATH 注入 `~/.npm-global/bin`）。
- **常驻菜单栏**：关窗后继续调度，登录项可启用；退出 App 停止调度与本实例 MCP 服务，但不结束 Plus 会话。详见 §5.27。
- executor_runner 职责：渲染 `core/executor_prompts/<stage>.md`（`{PREAMBLE}` 展开、路径占位符替换）→ 拉起 `pi -p --mode json --session-dir <app_support>/executor-sessions/<stage>/ --no-extensions --no-context-files --no-skills` → 事件流落 `<app_support>/executor-logs/<stage>-<ts>.log` → 超时（settings `executor_timeout_seconds`，缺省 1800s）终止进程组。
- runner 自己把运行记录写 `settings.executor_runs[stage]`（`pid/started_at/log/exit_code/finished_at`）——`executor-status` 读取的唯一数据源。
- `executor-status {}` → `{"stages": {"<stage>": {"running": <entry>|null, "last": <entry>|null}}}`；`running` = entry.pid 存活探测通过。
- **本地网络/代理**：macOS 本地网络隐私会拦截未授权 app 对 LAN（如模型端 `192.168.0.0/16`）的直连，pi 报 `Connection error`。调度器拉起子进程时探测系统 HTTP 代理（`CFNetworkCopySystemProxySettings`）并注入 `HTTP(S)_PROXY`（`NO_PROXY` 仅 localhost/127.0.0.1/::1），让 pi 经 loopback 代理转发到 LAN；模型在公网/localhost 时同样无害。

## 5.20 保存状态终态与永久从列表移除（v2，T1/T4）

### 5.20.1 `purged` 终态

`save_state` 增加第四个值 `purged`（终态）。语义：**永久从列表移除**——任务/轮次不再出现在任何常规列表与统计中，但数据库实体、requests/attempts/events、计划/报告/会话文件与 worktree **全部保留**，不物理删除、不释放磁盘空间。

- 只能从 `trashed` 发起；活跃或仅归档记录不得直接 purged。
- purged 任务/轮次不可经普通恢复入口恢复、不可排队、不可修改（任务仅允许下述用户显式 `reconnect-task` 例外；轮次仍不可恢复）、不被 `sync-tasks`/目录回填复活。所有写入口（enqueue/resolve/claim/report/publish-plan/dispatch-*/advance-task/register-review/create-review-fix/trash-*/restore-*/archive-*/set-priority）对 purged 目标一律拒绝（`conflict`），不只隐藏按钮。
- 明确 ID 的 `get-task`/`get-session`/`get-request` 仍可读取审计详情：返回 `read_only: true`、`allowed_actions: []`、附原因字段；不得报「不存在」。
- 运行记录（events）仍可打开只读详情；`list-events` 不受 purged 影响（历史照常可翻页查看，标题经 §5.21.3 规则回填）。

### 5.20.2 `purge-task` / `purge-session`

| 命令 | payload | 返回与语义 |
|---|---|---|
| `purge-task` | `{task_id, expected_save_state: "trashed"}` | `{ok, deduplicated}`；已 `purged` 为幂等成功。前置：`save_state` 必须为 `trashed`（否则 `conflict` 中文原因），且满足安全条件——无 `queued`/`claimed` 请求、无未解除失联、`send_state != 'unknown'`。同事务复核；条件不满足 → `conflict` 逐项给原因。 |
| `purge-session` | `{task_id, run_id, expected_save_state: "trashed"}` | 同上，精确到轮次（`execution_rounds.save_state`）。轮次不存在 → `not_found`。 |

- 永久移除任务后：其关联会话（`execution_rounds`）与请求退出常规列表及统计；`list-sessions`/`list-requests`/`list-tasks` 默认排除（`list-requests`/`list-sessions` 支持 `include_removed`/对任务 save_state 的排除由 `save_state='purged'` 天然过滤）。
- 每项独立事务，不回滚已成功项；「清空当前分类」由界面固定本次确认时的对象集合逐项调用，不把随后新进入回收站的对象一起清理。
- 确认文案（UI）：「永久从列表移除；保留历史记录与文件，不释放磁盘空间。」

## 5.21 可处理请求详情与可读事件（v2，T2/T3）

### 5.21.1 `get-request`

`get-request {request_id}` →

```json
{"request": {"request_id","idempotency_key","task_id","stage","kind","status",
 "display_status","priority","not_before","requested_by","run_id","claimed_by",
 "claimed_at","heartbeat_at","lease_expires_at","result","attempt_no",
 "created_at","updated_at"},
 "task": {TaskSummary}|null,
 "attempts": [Attempt],
 "allowed_actions": ["view","set-priority","release_lost","retry","replan","prepare_plan","fetch_todos", ...],
 "blocked_reason": "<中文原因|null>",
 "read_only": false,
 "log_path": "<该请求关联轮次的 events.jsonl 绝对路径|null>",
 "attention_state": "actionable|historical|informational"}
```

- `task` 为关联任务摘要（TaskSummary）；无任务（plan-scan）或任务已 purged 时为 `null`（purged 任务仍可经 `read_only:true` 的 `get-task` 查看）。
- `attempts` 按 `id` 升序；含结构化结果（result_json）、失败说明（note/failure）、`send_state`、`evidence`。
- `allowed_actions` 由核心按**当前任务状态与失败阶段**派生，UI 只渲染、不自判：
  - `queued`：`view`、`set-priority`；旧未确认规划（kind=plan 且 payload 无 `manual_authorized`）追加 `prepare_plan`（「准备规划」，打开材料面板）。
  - `claimed`：`view`（进度/日志）；失联（租约过期）追加 `release_lost`；不显示普通重试。
  - `failed`：按当前任务状态与失败阶段给合法动作——`retry`（send_state=not_sent/非 dispatch 阻塞）、`replan`（needs_user）、`release_lost`（失联遗留）、`prepare_plan`（规划类失败重试先打开材料面板）；发送结果不明（send_state=unknown）不出现普通重试。
  - `succeeded`/`cancelled`：`view`（只读）。
  - 无任务的旧 `plan-scan`：`view`、`fetch_todos`（「重新获取 To Do」进入规划面板；不恢复自动扫描）。
- `read_only`：purged 关联、或 `succeeded`/`cancelled`/`failed` 终态且无可执行动作时为 `true`。
- `attention_state`：`actionable`（queued/claimed/lost/当前失败需处理）｜`historical`（旧失败已有后续成功、任务已归档/回收/移除、或终态无待办）｜`informational`（succeeded/心跳等纯记录）。历史失败不冒充当前异常：同一任务的旧 failed 请求在已有更新成功请求或任务已离开该阶段时归入 `historical`/`informational`。

### 5.21.2 `list-events` 扩展

```text
输入：task_id?、stage?、result?、query?、before_id?、limit?
输出：{"events": [...], "next_before_id": <id|null>}
```

- 每页默认 `limit=100`，上限 `200`（>200 按 200 处理，非法值 `bad_request`）；`before_id` 为严格小于该 id 的游标（`id < before_id`），无则取最新页。排序恒 `id DESC`；翻页期间新追加事件（id 更大）不会使已翻页结果重复/错位。
- 每条事件在原字段（`id/at/task_id/request_id/kind/actor/detail`）上补充：`task_title`、`project_name`、`stage`、`result`、`summary`。
- **标题回填**：优先取仍保留的任务实体 `title`；任务已 purged/删除则取可读取的来源快照标题（`source_path` 快照 `task.title`）；确无标题 → `task_title="未保留标题 · <短id>"`（短 id 取前 8 位），不编造名称。`project_name` 同口径。
- **stage/result/summary 由核心统一派生**（`stage` 取事件关联请求/任务的阶段，`result` 取 request 终态或事件语义结论，`summary` 为中文一句话摘要）；未知事件 kind 显示中性摘要+原始类型，不猜成成功。
- **搜索**：`query` 在核心对**全部历史**执行（匹配 task_title/project_name/task_id/request_id/中文摘要/detail 原文），先过滤再按 `id DESC` 分页——不是只搜当前页。`stage`/`result` 同样先过滤后分页。
- 详情字段（原始 JSON `detail`、完整 `request_id`/`task_id`、`detail` 内哈希、actor）全部保留，供 UI 展开复制；不因无 task_id 而无法查看。

## 5.22 手动规划链路（v2，T5）

### 5.22.1 获取与准备

| 命令 | payload | 返回与副作用 |
|---|---|---|
| `fetch-todos` | `{}` | `{fetched_at, items[]}`；复用 bridge 分页读取每个 To Do 的描述/评论/关系/活动。每项含 `task_id/title/project_id/project_name/requirements_sha256/source_sha256/kaneo_status/repository/local_status/attachments[]/selectable/blocked_reason`。不导入任务、不创建请求。 |
| `prepare-plan-input` | `{task_id, expected_requirements_sha256, local_paths[], supplement, excluded_remote_ids[]}` | 重新获取该任务（bridge `get-task` 核验需求哈希）→ 建不可变材料目录 → `{input_id, input_sha256, files[], issues[], can_start}`。不入队、不调用模型。 |
| `start-plans` | `{items: [{input_id, input_sha256, expected_plan_sha256?, planner_method?}]}`（v3：`planner_method` ∈ `builtin|spec_plan`，缺省 `builtin`，见 §5.23.2） | 每任务独立提交 → `{results: [{task_id, request_id?, deduplicated?, code?, error?, waiting_reason?}]}`。只有成功项进入规划队列。 |

规则：

- 沿用现有项目白名单与仓库映射；未映射项目仍展示但 `selectable=false` + `blocked_reason` 说明；不新增项目配置系统。
- 每个 To Do 独立规划、独立产出；同一时间只领取一个规划任务。
- 附件：图片 PNG/JPEG/WebP、日志 UTF-8 文本；单文件 ≤20 MiB、每任务材料合计 ≤100 MiB，超限明确报错（`validation_failed`），不静默截断。
- 识别任务附件中的可下载 URL、描述与评论中的 Markdown 图片/文件链接及 HTML 图片；未知附件结构/格式/失效链接列入 `issues[]` 且 `can_start=false`，须补齐或 `excluded_remote_ids` 明确排除后才能开始。
- 下载只向 Kaneo 同源地址发凭据；跨域/重定向不携带 `Authorization`；不输出凭据或带认证参数的 URL。
- 本地文件与已下载文件复制到应用管理的材料目录 `planner_inputs/<input_id>/`，记录 `name/source/media_type/sha256/size`；不依赖临时剪贴板路径长期存在。
- `source_sha256`/`requirements_sha256` 保持原口径；补充说明、附件内容、排除项进入独立 `planner_input_sha256`（= `input_sha256`，对 manifest 规范化重算）。
- 源快照与附件按 `input_id` 存为不可变产物（`planner_inputs` 表 + 目录）；复用请求 `payload_json` 保存 manifest，不新增附件数据库表。

### 5.22.2 排队、去重与手动门控

- `kind=plan`、幂等键 `plan:<task_id>` 不变；`payload_json` 增加 `manual_authorized: true`、`input_id`、`planner_input_sha256`、材料 manifest、`model_config`（planner_model/reasoning_effort）、`confirmed_at`。
- `tick` 不再生成 `plan-scan`；`sync-tasks`、create-review-fix 登记、resolve retry/release_lost 不再自动创建/启动 plan 请求。需求变化仍更新状态与证据（task→needs_plan），但等待用户经 `prepare-plan-input`+`start-plans` 手动提交。
- 旧未授权 queued plan 请求（payload 无 `manual_authorized`）保留，UI 显示「等待确认材料」；用户提交时复用该请求（写回已确认输入 payload），不新建。
- 已授权 queued 且 `input_sha256` 相同 → 幂等返回原请求（`deduplicated:true`）；输入不同 → `conflict`（不偷偷替换材料）。`claimed` 请求不可覆盖。
- `start-plans` 前置拒绝：存在非规划活动请求、任务不活跃（非 active/purged）、`send_state=unknown`、或 ready 任务 `expected_plan_sha256` 与当前 `plan_sha256` 不符（重新规划需先确认现行计划）→ 逐项 `code`/`error`/`waiting_reason`。
- 自动领取与指定 `request_id` 的 `claim` 都检查 `manual_authorized`：未授权 plan 请求 → 自动取单跳过、定点 claim 返回 `claimed:false, reason:"unauthorized_plan"`；不增加 attempt、不改状态。
- 点击「开始规划」后立即唤起规划 runner（见 §5.22.3）；App 运行期间及重启后只继续领取**已确认**的队列，不自动发现新任务。
- 全局/阶段 `paused` 与任务 `auto_advance_paused` 始终生效，不因手动绕过；已排队但暂停时 UI 显示原因。

### 5.22.3 Codex 规划适配（planner backend）

规划执行从 pi 分流到独立 Codex 适配器（`kanpilot.codex_planner` / `planner_runner`）；dispatch/monitor/review 执行路径保留 pi。

- 调用 `codex exec`：`-s read-only`（只读 sandbox）、无交互审批、`--json`（JSONL 进度流）、`-i <图片>`（图片参数）、`--output-schema <file>`（结构化最终输出）、`-o <file>`（最后消息落盘）。经 `--ignore-user-config`/`--skip-git-repo-check`/`--ephemeral` 提供只读本次配置：不继承第三方执行插件/MCP/用户规则，不修改全局 Codex 设置。
- **服务商解析（v2.1，T1）**：`--ignore-user-config` 会同时跳过 `config.toml` 的服务商定义，因此 runner 启动时必须从 `constants.codex_home()/config.toml` 解析当前 `model_provider`（`kanpilot.codex_provider.resolve_provider`），把服务商必要连接配置以 `-c` 覆写显式传回：`model_provider`、`model_providers.<id>.name`、`.base_url`、`.wire_api`（`responses`）、`.requires_openai_auth`、`.env_key`；顶层 `model_catalog_json` 存在时原样沿用。配置损坏、`model_provider` 指向不存在的服务商、或所需认证缺失 → 明确失败（`validation_failed`），**禁止静默退回官方服务商**；未配置自定义服务商 → 允许 Codex 默认认证路径（`auth.json`）。内联 `experimental_bearer_token` **只在内存读取**，转为子进程专用环境变量并经 `-c model_providers.<id>.env_key` 引用；密钥不得进入 argv、SQLite、任务快照、日志或 UI。运行记录只保存脱敏摘要（provider_id/name/base_url/wire_api/auth 就绪说明/模型目录来源）。
- 模型：默认 `standard` 计划 `gpt-6-astra / medium`；`review_fix` 计划 `gpt-5.6-sol / xhigh`（可经 §5.22.6 规划配置调整；`start-plans` 把生效快照写入 `payload.model_config`，请求内不再变化）。不可用即报告，不替换模型。
- 工作目录 `cwd` 为任务 `repository`（可读）；业务源码不可写（sandbox 只读）；runner 指示模型阅读项目 `AGENTS.md` 及相关入口。任务描述、日志、附件作为材料输入，不覆盖「只规划」约束。
- **输出契约**（`--output-schema` 强制）：

```json
{"status": "ready|needs_input",
 "plan_markdown": "<完整计划 markdown 字符串>",
 "questions": ["..."],
 "evidence": ["<引用的材料/文件>"]}
```

- `ready`：必须有非空 `plan_markdown` 且无未决 `questions`，满足项目计划规范；计划元数据（task_id/plan_sha256/planner_model/input 哈希等）由核心根据本轮真实输入生成，**不**信模型自报的 ID/模型名/哈希。
- `needs_input`：保存 `questions` 与 `plan_markdown` 草稿，task→`needs_user`、`failure_code=reported`；UI 允许补充说明/附件后重新提交，不发布为可执行计划。
- 模型退出失败/超时/空输出/不合规输出：保存证据（JSONL 日志+stderr），进入规划失败（`report failed`/`needs_user`），**不自动重试**。超时沿用 `executor_timeout_seconds`（默认 1800s）。
- runner 心跳间隔 ≤ `min(lease_seconds/3, 60)` 秒；租约失效或所有权丢失（`stale_run`）时终止自己的子进程组，禁止发布。
- 日志展示结构化进度与 stderr；未知 JSONL 事件保留原始记录；不把进程退出码 0 单独视为成功（须 schema 校验输出）。

### 5.22.4 发布与执行分离

发布（`publish-plan`，§5.6）与「安排执行」（`enqueue` dispatch + `confirm_plan`，§5.3）是两个独立动作：

- `publish-plan` 成功后 task `ready` 且 `plan_execution_hold=true`：计划已登记但**尚未授权派发**；UI 显示「待确认执行」。
- 「安排执行」= `enqueue {task_id, kind:"dispatch", confirm_plan:true, expected_plan_sha256}`：同事务复核计划哈希 → 建 dispatch 请求 → 解除 `plan_execution_hold`。旧计划哈希/并发修改/其他失败 → `conflict`，hold 保留。
- `plan_execution_hold` 为真时，dispatch 请求即便存在也不得被 claim（自动与指定 `request_id` 领取都拒绝 `reason:"plan_hold"`）——确认前不派发。
- 其他任务（无 hold）的既有派发行为不变。

### 5.22.5 review_fix 计划衔接
修复计划继续核验 `review_key`、原报告及关联任务：publish-plan 成功时把修复计划路径/哈希写回**原评审任务**的 `extra_json.repair_plan_path`/`repair_plan_sha256`，保证 §4.5-5 派发校验（fix_task_id/review_key/repair_plan 匹配 + 远端 marker）通过。`plan_kind=review_fix` 的 frontmatter 必填项沿用 §6。

### 5.22.6 规划配置与失败语义（v2.1）

**规划配置**（`settings.planner_config`，独立于 pi 的 `exec_config`，不加表）：

| 命令 | payload | 返回与副作用 |
|---|---|---|
| `get-planner-config` | `{}` | `{config: {"standard","review_fix","default_method"}|null, defaults, updated_at}`；config=null = 从未保存（生效值=defaults，`default_method` 缺省 `builtin`）。v3：`default_method` 见 §5.23.2。 |
| `set-planner-config` | `{standard?, review_fix?, default_method?}`（每组完整 `{planner_model, reasoning_effort}`；`default_method` ∈ `builtin|spec_plan`） | 逐组校验（模型 ID 去空白/拒空/控制字符；目录已知模型按 `reasoning_efforts` 校验思考等级；目录缺失/模型未登记 → 仅提示「能力未验证」）→ 原子保存 → `{config, updated_at}`；失败 `bad_request`，旧值不变。**只影响之后创建的规划请求**；幂等命中的已排队请求不回写快照。v3：`default_method` 为规划面板的默认方式预选（§5.23.2），不改变既有请求的已固化方式。 |
| `planner-capabilities` | `{}` | `{codex:{path,version}, provider: 脱敏摘要|null, auth:{ready,detail}, models:[{id,name,reasoning_efforts}], models_source, models_verified, planner_methods:{builtin:{available:bool,reason:str|null}, spec_plan:{available:bool,reason:str|null,plugin_id?,version?}}}`；不发模型请求、不返回凭据。v3：`planner_methods` 见 §5.23.2（`spec_plan` 须经 `codex plugin list --json --marketplace personal` 结构化结果确认插件已安装、启用并核对 manifest/SKILL.md）。「配置就绪」不得表述为「真实服务验证成功」。 |

- `start-plans` 创建请求时把 `planner_config.snapshot(plan_kind)` 写入 `payload.model_config`；`publish-plan`/`dispatch` 校验一律匹配请求快照（§5.6/§4.5），不再用当前默认值重新校验；旧记录缺快照时沿用历史固定规则。
- **失败原因分类（`failure_reason`/terminal `reason`，不改变看板状态机）**：`model_unavailable`（模型不被账号/服务商支持）、`auth_failed`（认证失败）、`request_rejected`（HTTP 4xx 拒绝，正文保留）、`timeout`、`process_exit`（未知非零退出，注明退出码）、`crash`（信号/系统级终止）。相同错误在会话内只显示一条主要原因，完整脱敏日志保留在 `codex.jsonl`；事件/日志落盘前经 `util.redact` 处理（HTTP 状态、模型名、错误正文保留）。
- 读取层把 codex JSONL 的顶层 `error`、`turn.failed.error`、`item.completed` 错误项映射为 `system` 事件（`_EVENT_TO_MESSAGE` 含 `system`）；历史轮次同样可见，不重写旧日志。

## 5.23 计划阅读与角色环境（v3，T1–T5）

本节冻结 T1（计划页）、T2（施工规划方式）、T3（PI-Desktop 任务执行后端）、T4（详情分页的协议面）、T5（角色配置/Review 环境）。界面度量与交互细节见 ui-spec.md §4.8/§4.9；执行端编排细节见 automation-contract.md。

### 5.23.1 `get-plan`：已发布计划正文读取（T1）

`get-plan {task_id, expected_plan_sha256?}` →

```json
{"task_id": "...", "title": "...", "plan_kind": "standard|review_fix",
 "generated_at": "ISO", "plan_sha256": "...",
 "markdown": "<去除核心 frontmatter 后的正文>"}
```

- **定位**：以任务登记的 `plan_path` 为唯一入口，复用 `planfile.load_plan` 的 Plan 根边界/无符号链接/frontmatter `task_id` 身份校验；不接受调用方另传路径。
- **哈希核验**：重算**整个文件**的 SHA-256 并与 `tasks.plan_sha256` 比较；调用方给的 `expected_plan_sha256`（当前视图快照）不符 → `conflict`。文件哈希与登记值不符（被篡改/重写过）→ `conflict`。
- **错误矩阵**：任务无 `plan_path`/`plan_sha256` 登记 → `not_found`；文件缺失、越出 Plan 根、符号链接、frontmatter `task_id` 不符 → `validation_failed`；任务为 `purged` 仍允许只读审计（同 get-task 口径，返回 `read_only: true`）。
- **正文边界**：`markdown` 为去除 YAML frontmatter 后的原始字节解码（UTF-8），核心不做重写/裁剪；不执行 HTML/脚本、不代取远端媒体（渲染约束在 UI 层，见 ui-spec §4.8）。
- 本命令只读：不改 task/request/事件；失败不伪造正文返回。

### 5.23.2 施工规划方式 `planner_method`（T2）

- **取值**：`builtin`（默认，现有内置规划链路）｜`spec_plan`（Codex `spec-planner@personal` 插件技能）。当前版本仅这两项，不开放任意插件 ID 与市场操作。
- **入口**：`start-plans.items[].planner_method`（缺省 `builtin`）；`planner_config.default_method`（`builtin|spec_plan`，缺省 `builtin`）为规划面板的默认预选——`default_method` 是 UI 预选初值，**item 未给 `planner_method` 时仍按 `builtin` 解释**，不回写已排队请求。
- **请求固化**：`payload_json` 在 §5.22.2 字段基础上增加 `planner_method`；`spec_plan` 时额外固化 `plugin_id="spec-planner@personal"` 与 `plugin_version`（`codex plugin list` 的实际版本号）。`model_config` 快照、`planner_input_sha256` 规则不变。
- **幂等**：`planner_method` 属于请求身份的一部分——已授权 queued 且 `input_sha256` 相同但 `planner_method` 不同 → `conflict`（不暗换方式）；全同 → `deduplicated`。缺字段的旧请求按 `builtin` 解释。
- **能力门**：`planner-capabilities.planner_methods`（§5.22.6）。`builtin` 可用性 = 现有 codex 检查；`spec_plan` 需 `codex plugin list --json --marketplace personal` 确认 `spec-planner@personal` 已安装且启用，并在其安装源核对 manifest 与 `skills/spec-plan/SKILL.md` 存在；缺失 → `available:false` + `reason`（中文）。排队后插件被移除/禁用 → 该请求**明确失败**（`report failed`，材料保留），**不静默改用 builtin**。
- **builtin 执行路径**不变（§5.22.3：`--ignore-user-config -s read-only --output-schema` 等）。
- **spec_plan 执行路径**（用户选中后）：新开 `codex exec` 会话，**不带 `--ignore-user-config`**（该标志会隔离插件），prompt 以 `$spec-plan` 显式引用已安装技能；保留 `-s read-only`、`--ephemeral`、`--json`、`-o`、`--output-schema`、模型快照与超时/心跳；provider 解析沿用 config.toml 用户配置（不做 v2.1 的 `-c` 覆写，因为 user config 未被忽略）；本轮指令明确「本次不写文件，完整 Markdown 放入 `plan_markdown`」。输出仍走 §5.22.3 的 `ready|needs_input`/`questions`/frontmatter/哈希/远端复验/`publish-plan` 契约；模型自述「已用插件」不算启用证据——以 `tools/list`/argv 审计为准。
- `job.json` 记录 `planner_method` 与 `plugin_version`（有则），不写凭据。

### 5.23.3 PI-Desktop 任务执行后端 `pi_desktop`（T3）

**范围**：只改变任务执行轮次的后端；dispatch/monitor 控制端与 review 控制端仍按各自环境运行（§5.23.5）。
- **`exec_config` 扩展**（同一 settings JSON，向后兼容）：

```json
{"provider": "...", "model": "...", "reasoning_effort": "...",
 "agent_preset": "...", "backend": "pi|pi_desktop",
 "pi_desktop": {"connection_file": "<abs>",
                "provider_id": "...", "model_id": "...",
                "thinking_level": "off|minimal|low|medium|high|xhigh|max",
                "permission_mode": "ask|accept-edits|auto"},
 "updated_at": "ISO"}
```

  - 缺 `backend` 一律按 `pi` 解释（旧数据零迁移）。`pi` 分支沿用四字段；`pi_desktop` 分支五字段中 `permission_mode` **必须经用户显式保存**——`set-exec-config` 不允许后端切换时自动补 `auto`。
  - `set-exec-config` 对**当前 backend** 的必填组合整体校验、原子保存；切换后端不删除另一组已存字段（`pi` 四字段与 `pi_desktop` 块可共存，`agent_preset` 不映射 PI-Desktop 全局配置）。
- **轮次快照**：dispatch claim 把所选分支固化进 `execution_rounds.config_json`：pi → `{backend:"pi",provider,model,reasoning_effort,agent_preset}`；pi_desktop → `{backend:"pi_desktop",connection_file,provider_id,model_id,thinking_level,permission_mode}`。旧轮次缺 `backend` 按 `pi` 读。`ROUND_BACKENDS` 扩为 `(pi, codex, pi_desktop)`；`job.json`、`get-session`、dispatch-prepare/confirm 同样接受 `pi_desktop`。
- **`exec-capabilities` 扩展**：桌面分支为 `{available,detail,tools_ok,version,session_naming}`，只检测已保存配置。`detail` 为脱敏原因；顶层 `ok/checks` 仍为 PI CLI 分支检查，不能阻止桌面配置保存或冒充控制端模型已验证。连接文件要求 owner-only、active=true、PID 存活、loopback `/mcp`、Bearer 握手；PID 存活不单独证明服务身份。必需工具为 `pi_turn_get/pi_session_create/pi_session_configure/pi_agent_prompt/pi_agent_status/pi_session_get/pi_agent_stop/pi_agent_abort/pi_desktop_invoke` + Goal 契约审批的 `pi_plans_pending/pi_plans_resolve`（缺任一即 `tools_ok=false`，阻止派发，见 §5.23.9）。`session_naming` 为 T6 兼容门布尔值：仅当 `pi_session_create.inputSchema.properties.projectName` 存在时为 `true`；旧 Plus 为 `false`（见 §5.23.10），供 `dispatch-start` 在任何 `session.create` 之前判定，不能替代 `tools_ok`。型号通过通用 `providers/list`（args=[]）确认 provider 存在且未禁用后，再调用 `providers/listModels`（args=[{providerId,source:"cache"}]）精确匹配 modelId；unwrap `{operation,result}`，允许已配置模型的 cache/fallback，禁止 refresh/远端探测。该检查只证明本地配置存在，不证明凭据或远端可用。保存为结构校验，服务离线仍可保存；缺工具/服务/型号阻止派发，不自动切回 pi。token/完整连接文件/Authorization 永不进入 DB、argv、UI、日志。
- **能力前置（子任务 E）**：`pi_turn_get` 是 PI-Desktop 本机 MCP 控制面的只读增补——Host RPC `session.getTurn {sessionId, turnId}` → Electron Main 只读 `turnGet` IPC → MCP 同名工具，统一返回 `{sessionId, turnId, status:"running"|"completed"|"error"|"aborted", errorCode?, endedAt?}`（ISO8601）。只按本机 `turns` 表**同 session+turn 双键**查询；不存在或跨 session → `not_found`；不返回提示词/转录/凭据。不新增端口/认证/表/写权限。自动完成判定**只认**该持久终态，不得以 `pi_agent_status.isRunning=false` 代替。
- **派发状态机（pi_desktop 分支）**：claim（backend=pi_desktop 快照）→ `dispatch-prepare` → `dispatch-begin-send`（远端迁移不变）→ `dispatch-start` → `dispatch-confirm`；`dispatch-fail` 不变。
  - `dispatch-prepare` evidence：`{session_marker, repository, backend:"pi_desktop", config:{...}}`；marker/repository/backend/config 快照全等校验同 pi 分支，计划哈希自 claim 未变。
  - `dispatch-start`：以该轮 claim 固化快照复核可用性（连接文件/token/tools/provider/model，不能改用当前全局配置）→ 意图落盘（同一轮次只允许一次真实启动，mark_intent 语义不变）→ 创建独立 worktree（同 §5.18.1）→ 重算计划哈希核验 → 拉起 `python3 -m kanpilot.pi_desktop_worker --spec <spec.json>`。worker：经连接文件 token 完成 MCP `initialize`→ `pi_session_create(projectPath=worktree, mode="goal", providerId/modelId/thinkingLevel)` → 必要时 `pi_session_configure(id, mode="goal", confirm:true, permissionMode=已保存值)` → 计划正文超出 MCP 2 MiB 请求限制 → 发送前报验证错误（不入队）→ `pi_agent_prompt(sessionId, content=任务标识+计划哈希+完整已核验计划正文)`，返回 `{turnId}` 记 `job.send_state="sent"`；**不用** `pi_project_open` 全局切换。会话固定以 Goal（契约）模式创建：宿主先协商目标契约并停在审批门，由 worker 本机代批后在同一会话开启执行轮次（`§5.23.9`）——KaneoPilot 的计划审批仍只在本机完成，Plus 侧契约只是执行形式，不把审批权交给 Plus 界面、也不向用户二次索批。
  - 首个 `session` 事件写入 events.jsonl 的条件：`pi_turn_get` 确认同一 session/turn 为 `running` 且 worker 已 `heartbeat` 回报核心；极短任务已为 `completed` 时，以同一持久 turn 记录（status=completed + endedAt）证明实际启动后再写 session 事件、heartbeat 与 confirm。**符合「已排队≠已开始」**。
  - `dispatch-confirm` evidence：`{session_marker, repository, backend:"pi_desktop", session_id, turn_id, config:{...}}`；核心核验启动意图、events.jsonl session 事件、turn_id 匹配、config 快照 → running 迁移不变（sent/send_state/monitor 请求/释放串行位）。
- **观察与完成**：Plus `messages[].id` 为字符串身份；分页使用物理 `messageStart/messageEnd`，向前读取至 hasMoreBefore=false，不按 ID 数值比较或消息数组长度推算。页大小自适应：控制面把大页判为 `MCP_RESULT_LIMIT` 截断时按半数缩小消息数重试（下限 5），仍截断才算链不完整——长会话的大页不得把仍在跑的轮次判成失败；首轮/续接自索引 0 核验整段链后记「已核验前缀」，后续轮询只读到与新页**重叠段指纹逐一相符**即收敛（不必整段重读）；重叠段指纹不符（历史被改写）→ 安全回退为整段重读。截断/读失败按**每次轮询**派生（一次瞬时截断不作废整轮）。工具读取 toolStatus/toolArgs/toolResult/toolCompletedAt/isError，保持 toolCallId；MCP_RESULT_LIMIT、缺页、未知消息结构或最终空转录均不能完成。worker 以 `pi_agent_status` + 分页 `pi_session_get` 采集进度/记录，按消息 ID/修订去重映射到 `events.jsonl`/会话阅读器；分页不完整/截断保留证据并阻止成功判定。`job.json` 保存 `session_id`/`turn_id`/`pi_desktop_pid`/status/脱敏 config（不含 token）。**执行会话标 finished 的条件**：`pi_turn_get.status=completed` 且同轮会话记录已持久读全；任务完成仍依赖现有 monitor 验证与 Review。
- **异常归因**：`pi_turn_get.status ∈ {error, aborted}` → job `failed`/`needs_user`（存 `errorCode`）。服务重启但持久 turn 已结束 → 按身份恢复读取；仍 `running`、发送响应丢失、身份不符或无法归因 → `send_state=unknown`/失联记录，**绝不自动重复 `pi_session_create` 或 `pi_agent_prompt`**；同一 `run_id` 超时/重启后不得再次发送。停止/超时先 `pi_agent_stop`，必要时 `pi_agent_abort`，并以 `pi_turn_get` 核实终态——「停止请求已接受」不等于已停止。
- **桌面消息修订投影**：事件可携带 `source_message_id:string` 与递增正整数 `source_revision`，日志 seq 仍单调。会话消息 id 为 `desktop:<source_message_id>:<event_type>`，revision 使用源修订，seq 使用该修订的最新事件序号，以保证游标能收到旧消息更新；相同工具输入/结果按事件类型区分。历史 pi/codex 事件没有这两字段时沿用 `round:<seq>` 与内容哈希修订。
- **T3 Plus R1 失败约束**：每次持久 turn 响应核对 sessionId/turnId。prompt 响应丢失保持 send_state=unknown，currentTurnId 仅作候选证据，不自动恢复发送。只读连接恢复最多三次且总计不超过 10 秒，重读同一连接文件；不重放任何创建/配置/发送操作。恢复后仍 running 或无法确认身份/终态转人工。stop/abort 接受不等于已停止，未取得持久终态须明确“可能仍在运行”。意图后 worktree/哈希/Popen 失败记录失败证据，只有意图的重复 start 返回 conflict，而非伪 started。
- **`pi_desktop` worker spec.json 键**：`{backend, task_id, run_id, session_marker, plan_sha256, config_snapshot, connection_file?, worktree_path, worktree_mode, timeout_seconds, request_id, db_path}`——`request_id`/`db_path` 供 worker 经核心命令回报 heartbeat，`plan_sha256` 供 worker 发送前重算核验；不得含 token。

### 5.23.4 Review 环境 `get/set-review-config`（T5）

- settings 键 `review_config`（JSON）：`{mode:"inherit"|"custom", provider?, model?, reasoning_effort?, updated_at}`。
- `get-review-config {}` → `{"config": {...}|null, "defaults": {"mode":"inherit"}}`；未保存或 `inherit` **保持当前实际行为**（review 控制端继承本机 PI 默认会话配置）。
- `set-review-config {mode, provider?, model?, reasoning_effort?}`：`custom` 时 provider/model/effort 必填并经 `exec-capabilities` 校验（同 §5.17.2 规则）；校验失败 `bad_request`，旧值不变；成功 → 原子保存 + event `review_config_updated`（detail 脱敏快照）。
- `custom` 生效点：`executor_runner --stage review` 启动时把 `{provider, model, reasoning_effort}` 固化为 pi 参数（本轮 review 会话专用），记录脱敏快照到运行日志；保存只影响**下一次** review 控制端启动，不改运行中进程。固定提示词仍来自 `core/executor_prompts/review.md`，不复用任务执行的 `agent_preset`。评审修复计划属规划环境，不走 review_config。

### 5.23.5 控制端配置来源（v3 明确；v4 由 §5.24.1/§5.24.4 扩展）

- `dispatch`/`monitor` 控制端来源 = `exec_config.controller_backend`（v4）：`pi`（缺省）继承 PI 默认会话设置；`pi_desktop` 使用 `exec_config.pi_desktop` 五字段快照。不再笼统声称两者恒继承 PI 默认。
- `review` 控制端来源 = `review_config.backend`（v4）：`pi` 分支按 `review_config.mode`（inherit→PI 默认；custom→固化快照）；`pi_desktop` 分支使用 `review_config.pi_desktop` 五字段。
- 任务执行轮次来源 = `exec_config.backend` 分支（§5.23.3）。四个来源独立展示，互不覆盖。

### 5.23.6 Plus 本地目录读取 `pi-desktop-catalog`（T3.5）

- 命令只读，不写 `settings`、执行配置、Plus 数据库或用户文件：

```json
pi-desktop-catalog {"connection_file":"/abs/path/mcp-control.json","provider_id":"optional-provider-id"}
```

  - 两个字段均可省略。`connection_file` 省略时解析核心进程 HOME 下 `~/.pi-desktop-plus/mcp-control.json`；显式传入须为绝对路径。仅使用确定路径，不扫描 `.pi-desktop`、开发 profile、其他 HOME 或 `PI_DESKTOP_DATA_DIR`。
  - 读取顺序：复用 `load_connection` 校验 owner-only 文件权限、`active`、PID、loopback `/mcp`、Bearer 和兼容协议；MCP `initialize`/`tools/list`；通用 invoke `providers/list`（`args:[]`）；`settings/get`（`args:[]`）；对唯一选中的启用 provider 调 `providers/listModels`（`args:[{providerId,source:"cache"}]`）。禁止 refresh/OAuth、远端 `/models`、session 操作、prompt 或任何写操作。`source` 响应只允许 `cache|fallback`，其他值作为错误拒绝。
  - 默认选择：有效的 `settings.defaultProviderId` 必须精确匹配启用 provider；仅当该字段缺省且启用 provider 恰好一个时可选唯一 provider。模型优先使用 `settings.defaultModelId`，缺省时使用所选 provider 的 `defaultModelId`；存在的默认 ID 必须精确匹配该 provider 本地目录。只有两个默认都缺省且目录恰好一个模型时才选唯一模型。无默认且多个候选时不得取数组首项。调用方传 `provider_id` 时只为该启用 provider 取目录；返回中的 `default_*` 仍是 Plus 全局默认。
  - `models[].thinking_levels` 优先取该 provider `providers/list` 中对应配置模型的非空 `thinkingLevels`；其次取目录模型的非空 `supportedThinkingLevels`；再次取 provider 的非空 `supportedThinkingLevels`；缺失则返回 `[]`，由 UI 显示全部合法枚举并标记支持范围未确认。`default_thinking_level` 仅取 provider 配置模型的同名字段。不得从 session/list 的当前活动会话推断全局默认；不得把空数组当成模型不支持思考。
  - 成功响应形状：`{connection_file,providers:[{id,name,enabled,default_model_id,models:[{id,name,thinking_levels,default_thinking_level}]}],default_provider_id,default_model_id,selected_provider_id,catalog_source}`。只有当前选中 provider 有 `models`，其余 provider 的 `models=[]`；多启用 provider 且无有效默认时 `selected_provider_id=null`、`catalog_source=null`，由用户选择后以 `provider_id` 再查。无 provider/无模型是成功空目录，不能伪装成 MCP 失败。
  - 连接、工具、设置或目录读取失败使用通用错误信封并携带脱敏原因，UI 保留草稿与上一份目录。完整 `settings/get`、provider baseUrl/凭据标志、manifest 全文、Bearer/token、请求头不得进入命令响应、UI、日志、SQLite、argv 或轮次快照。token 仅在连接和 MCP 客户端内存使用。
- 设置页仅把结果应用到未保存草稿：默认 path 只在 path 草稿为空时填充；已保存的 `pi_desktop` 配置照常播种并保持原样，只更新候选。没有已保存桌面配置时，仅在各字段仍为空时依上述默认规则填入 provider/model/thinking。`permission_mode` 不从 Plus 获取，不自动补 `auto`。读取及刷新不调用 `set-exec-config`；必须由用户显式保存后才影响新轮次。详见 `docs/ui-spec.md` §4.9 T3.5。
### 5.23.7 PI-Desktop 观察者续接 `round-observe`（v3 补丁）

**背景**：pi_desktop 轮次由本机 worker 进程观察（采集 `events.jsonl`、判定终态）。worker 退出（崩溃/被清理/机器重启）时轮次会停在中间态，而 PI-Desktop 里的会话可能仍在跑。

- **心跳被拒不等于轮次失败**：派发请求在 `dispatch-confirm` 后即 `succeeded`，worker 对它后续的 `heartbeat` 只会得到 `stale_run`/`not_found`——这是**正常收尾路径**：worker 停止续租、继续观察，**绝不**把心跳被拒写成 job `failed`/terminal。启动证明阶段的 heartbeat 失败仍按「证明未闭环」处理（不写 session 事件）。
- 命令：`round-observe {task_id, run_id, executor?}`。`run_id` 必须是该任务**当前**执行轮次；任务须处于 `running|in_review|needs_user`；轮次 `backend=pi_desktop`；`job.json` 身份核验通过、`session_id`/`turn_id` 齐备、`send_state ∈ {sent, unknown}`，且 **events.jsonl 已有 session 事件**（「已确认发送」的硬证据，由发送成功 + 启动证明共同产生；`send_state` 只作粗筛）；`status=finished` → 返回 `{"status":"finished"}`（无需续接）。
- **只读续接**：仅以既有 `spec.json` 拉起 `python3 -m kanpilot.pi_desktop_worker --spec <spec.json> --resume`（独立会话，stdout/stderr 追加 `worker.log`）。`--resume` 分支**不建会话、不下发 `pi_session_configure`、绝不 `pi_agent_prompt`**；接管成功写 `job.observation_resumed_at`/`observe_pid`/`resume_started_at`，把轮次恢复为 `running`，并把 `send_state` 修回 `sent`（旧版假失败会写成 `unknown`）；首次续接前的收尾现场（status/detail/ended_at/exit_code/send_state）存入 `job.resume_previous` 供审计。
- **幂等**：原 worker 或上一次续接的进程仍存活（轮次行 `pid` 或 `job.observe_pid`）→ 返回 `{"status":"observing","deduplicated":true}`，绝不拉起第二个观察者。
- **剩余预算**：续接按 `job.started_at + 轮次时限` 计算剩余时间；已超时 → 不续接、也不动手停止仍在跑的会话，返回 `deferred`（`round_timeout_elapsed`），交人工判定终态。
- **观察环境不成立**（连接不可用/身份不符/turn 读不到/已超时）→ worker 退出码 3，只写 `job.resume_detail`/`resume_deferred_at`/`resume_attempts`，**既有 job 状态与 terminal 事件一字不改**。
- 返回形状：`{"status": "observing"|"settled"|"finished"|"deferred"|"exited"|"starting", "pid", "detail", "run_id", "reopened"}`；`observing` = 已接管且在观察，`settled` = 接管即见持久终态（进程已退出），`finished` = 轮次已按 completed 收尾；`deferred|exited|starting` = 本轮未接管成功（监控端按「会话读不到」处置，不改任务状态）。`reopened` = 本次接管是否顺带解除了误报的执行失败阻塞（§5.23.8），只在 `observing` 时可能为 true。
- 事件：`round_observe`（detail 含 run_id/pid/status/detail/reopened）。命令不写远端、不发消息、不重复 agent_prompt；**task 状态只在 §5.23.8 的解除误报阻塞一种情形下被改写**（本任务本轮 `needs_user` + `failure_code=execution_failed` → `running`），其余情形一律不改。
- **`round_detail.observe_pid`/`observer_alive`**：读取层按本机进程存活派生（`observe_pid` 优先 job，其次轮次行 pid），**只**用于判断是否需要续接观察者；完成判定仍只认 §5.18.4 三证据，不得以进程存活/进程消失代替。
- **旧终态作废（`terminal_superseded`）**：续接接管后（job `status=running` 且已写 `observation_resumed_at`），早于接管时刻的 `terminal` 属上一次观察者收尾留下的旧终态（旧版把心跳被拒写成 `failed`），**不再作为本轮判定依据**：`round_detail.terminal` 只反映接管后新写的终态（无则为 `null`），旧终态原文转 `round_detail.terminal_superseded`（含 `outcome`/`reason`/`ts`）供审计。events.jsonl 行 append-only**一字不改**；未续接的轮次（无 `observation_resumed_at`）行为不变。

### 5.23.8 后续轮次接续与误报阻塞解除 `turn_adopted`（v3 补丁）

**背景**：PI-Desktop 在**瞬时模型错误**（当前集合：`EMPTY_MODEL_RESPONSE`）后会在**同一会话**自动重开新 turn——任务实际没有中断，旧 turn 只留下 `status=error` 的持久记录。观察进程若据此收尾，就会把仍在执行的任务写成执行失败。

- **接续判据（四条缺一不可；任一不成立即按失败收尾，不接续）**：
  1. 持久 turn `status ∈ {error, aborted}` 且 `errorCode` 属瞬时集合 `{EMPTY_MODEL_RESPONSE}`；
  2. **会话归属锚点**：转录（`pi_session_get`）里**物理下标最小的 user 消息仍是本轮派发的 prompt**（normalize 空白后逐字比较）——证明该会话确由本轮创建。用户随后在同一会话里追加指令（如「用中文回复我」）不阻止接续；锚点不符（会话被改用）则绝不接续；
  3. `pi_agent_status` 显示**同一会话仍在跑另一个 turn**（`isRunning=true` 且 `currentTurnId` ≠ 旧 turn）；
  4. 未超接续上限（每轮最多 5 次）且不重复接续同一 turn。
- **接续动作只读**：`job.turn_id` 前移为新 turn，`job.turn_adopted` 追加 `{from, to, code, at}` 审计记录，events.jsonl 追加一条 `system` 事件（`turn_adopted: <旧> → <新>（<code> 后同一会话自动重开，继续观察；未重发 prompt）`），观察进程换绑新 turn 继续轮询；**不新建会话、不下发 `pi_session_configure`、绝不 `pi_agent_prompt`**，既有 session 事件与 turn 记录一字不改。
- **适用范围**：启动证明阶段（首个 `pi_turn_get` 已是 error/aborted）与观察循环（轮询中落 error/aborted）走同一判据与同一动作。
- **`round-observe` 解除误报阻塞**：续接接管成功（`status=observing`）后，若本任务停在 `needs_user` + `failure_code=execution_failed` 且 `task.run_id` 与本轮一致 → 核心清空 block 字段（`clear_block_fields`）并把任务转回 `running`（`status_change` 事件 detail 带 `via=round-observe`、`reopened=true`；`round_observe` 事件与返回形状带 `reopened:true`）。**只**解除本轮的 `execution_failed`——其它阻塞原因（`review_failed`/`observation_blocked`/…）与其它轮次一律原样保留，`deduplicated` 幂等返回不触发解除。
- **读取层**：`round_detail.turn_adopted` 透出接续历史（`null` = 从未接续），只作审计展示；完成判定仍只认 §5.18.4 三证据 + §5.23.3 的持久 turn 终态。

### 5.23.9 Goal 模式派发与契约审批门（v3 补丁）

**背景**：PI-Desktop 的 `goal` 是**契约模式**（`plan|goal` 同为契约模式）：派发 prompt 后模型只协商并提交「目标契约」（标题/正文/验收问题），会话停在 `awaiting_approval`，**不执行**；只有批准（并给出权限模式）才会在同一会话开启**执行轮次**（新的 turnId，Agent 模式自主跑到验收达标或边界受阻）。若把规划轮次的 `completed` 当作执行完成，就会把从未执行的任务判成完成。

- **会话固定 Goal**：`pi_session_create(mode="goal")`，非 inherit 时 `pi_session_configure(mode="goal", permissionMode=已保存值)`。工具门要求控制面同时具备基础工具与 `pi_plans_pending/pi_plans_resolve`，缺任一项即 `unsupported_tools`（或能力探测 `tools_ok=false`）——不做事后半残派发。
- **审批门触发**：观察循环看到持久 turn `completed` 时，先读 `pi_agent_status.planningState`：
  - `planning|awaiting_approval`（会话仍在 Goal 契约模式）→ 进入审批门；
  - 其它/读取失败（批准后已切回 Agent 模式，或非 goal 会话）→ 按普通 `completed` 收尾。
- **审批门动作**（全程只读会话身份，绝不重发 prompt）：
  1. 轮询 `pi_plans_pending(sessionId)` 取 `kind="goal"` 且 `status="pending"` 的契约（宽限 `_GOAL_CONTRACT_WAIT_SECONDS=20s`）；
  2. 本机代批：`pi_plans_resolve(proposalId, sessionId, turnId, toolCallId, action="approve", targetPermissionMode=该轮已保存 permission_mode, version, confirm:true)`——批准是本机自动化行为，必须留痕（见下）；契约批准后会话切回 Agent 模式，宿主 dispatcher 开启执行轮次；
  3. 轮询 `pi_agent_status.currentTurnId` 直到出现**不同于规划轮**的新 turn，并以 `pi_turn_get` 核验身份与状态（宽限 `_GOAL_EXECUTION_WAIT_SECONDS=30s`）→ `job.turn_id` 前移、观察换绑该执行轮次（不新建会话、不重发 prompt）。
- **留痕**：`job.goal_approval = {proposal_id, title, status:"approved", planning_turn_id, execution_turn_id, target_permission_mode, approved_at, approved_by:"kaneopilot"}`；events.jsonl 追加 `system` 事件 `goal_contract: 目标契约待批准（…）` 与 `goal_approved: <proposalId> → 执行轮次 <turnId>（权限 <mode>；本机代批，未重发 prompt）`。契约批准**不**写入 `turn_adopted`（那是瞬时错误接续的审计位，§5.23.8）。
- **明确失败（均不重发 prompt、不新建会话）**：契约模式轮次已结束但宽限内无待批准契约 → `goal_contract_missing`；批准后契约仍 pending → `goal_approval_failed`；批准后宽限内未观察到执行轮次 → `goal_execution_unstarted`；`pi_plans_pending` 读取失败 → `goal_contract_unreadable`。已保存 `permission_mode` 非法/缺省 → `goal_approval_permission_unknown`（配置校验本应在前拦住）。
- **续接观察同样适用**：`round-observe`（§5.23.7）接管停在审批门的轮次时走同一审批门（批准属本轮职责，非重复发送）；已批准过（`job.goal_approval.status=="approved"`）则直接按执行轮次收尾。
- **边界**：Goal 契约的批准权限取自**本轮快照**的 `permission_mode`，不读 Plus 当前设置、不改为 `inherit`；`ask` 语义仍是「Plus 每次工具确认需人工」，KaneoPilot 只代批契约本身，不代批工具权限。

### 5.23.10 Goal 报告完成判定与派发命名（v3 补丁，T5/T6）

> v5 兼容边界：报告完成判定适用于 `completion_policy=goal_report` 的历史轮次；旧 spec/job 未声明策略时按此解释。所有 v5 新 Plus 派发轮次（包括手动任务）固化 `completion_policy=command_verification`，按 §5.27 的确定性验证门推进，不要求 AI 判定事件；报告仍可只读查阅，派发命名规则继续适用。完成策略随轮次固化，不因项目策略、人工接管或运行时设置变化而改变。

本节冻结 KaneoPilot 与 PI-Desktop-Plus 之间的两项共享契约：**Goal 报告驱动的完成判定**（T5）与**新派发轮次的展示命名**（T6）。两仓按本节实现，任何一侧都不能单方面改变字段名、身份核对口径或失败语义。

#### T5-A 报告读取（Plus 侧只读 MCP）

- Plus 本机 MCP 新增两个**只读**具名工具，由既有 Host `goalReports.list/get` 提供，不开放 `submitDraft`/`retry`/`finalize`/`invalidate` 等写操作：
  - `pi_goal_report_list {sessionId}` → `{reports:[…GoalReportSummary]}`；
  - `pi_goal_report_get {sessionId, executionId}`（`reportId` 亦可）→ 完整报告 JSON，附 `report_sha256`。
- Host 对 `status=ready` 的文件**重算** `file_hash`/`file_size` 并校验 schema 与身份，`report_sha256` 即重算值（不得直接回显 DB 旧值）；返回上限沿用既有 256 KiB，超限 → 明确 `truncated`，不返回截断正文。
- 分态必须互不混淆：`pending` / `draft` / `failed` / `not_found` / `corrupt`(哈希或 schema 不符) / `truncated`。`fallback` 是**成功返回的报告**，但其 `integrity.kind=fallback` 表示**没有完成报告**，不能与 `structured` 混为一谈。
- 只读工具不得返回提示词、转录或凭据；只按本机 `goal_reports` 表**同 session** 查询，跨 session → `not_found`/拒绝。

#### T5-B 身份与报告底线（KaneoPilot 核心）

- KaneoPilot 核对：`task_id`/`run_id`、`job.session_id`、`job.goal_approval.proposal_id`、已批准的执行 turn 与报告的 `sessionId`/`proposalId`/`executionId`/`turnId`。`turnId` 允许沿本轮已记录的 `turn_adopted` 链核对（§5.23.8）；**不得**只凭同一 session 名称或标题匹配。
- 计划 `plan_sha256`（KaneoPilot 发布计划文件）与 Plus Goal 契约的 `contractHash`（Plus 侧目标契约 artifact）**各管各的文件，不可直接比较成同一个哈希**。
- 无 `job.goal_approval`、报告跨轮/身份不符、或 Plus 缺只读报告工具 → **不能放行**。

#### T5-C AI 判定与核心硬闸

- monitor AI 调核心只读 `get-goal-report {task_id, run_id}` 获取经身份/完整性校验的报告，再读当前已批准 KaneoPilot 计划，把报告当作**不可信数据**逐项比对：输出结构化

```json
{"decision":"complete|incomplete|uncertain","plan_sha256":"…","report_id":"…","report_sha256":"…",
 "criterion_reviews":[{"id":"…","result":"met|missing|failed|unknown","reason":"…","evidence_refs":["…"]}],
 "reason":"…"}
```

  报告自称 `verdict=met` 是**必要但非充分**条件；`partial`/`blocked`/`unknown`、重要验收遗漏、明确失败或无法解释的证据缺口**不可**被 AI 升格为完成。可引用报告中的实际检查/证据 ID 作理由；不机械要求每类任务都有测试命令，但计划明确要求的验证必须在报告中有可审查的结果或明确说明未能验证。
- 新核心命令 `record-goal-evaluation {task_id, execution_run_id, monitor_request_id, monitor_run_id, plan_sha256, report_id, report_sha256, decision, criterion_reviews, reason}`：
  1. 先核对该 monitor 请求仍由 `kaneo-monitor` 领取且**租约有效**；
  2. 只读**重新取回同一报告**并核对当前任务/执行轮次/计划/报告 hash；
  3. `decision=complete` 时还须**确定性**核对报告 `status=ready`、`integrity.kind=structured`、`execution.status=completed`、`verdict=met`；`criterion_reviews` 非空、ID 不重复、结果**全为 `met`**；引用的 criterion/check/evidence ID **确实存在于报告**。任一不满足 → 拒绝记录完成判定。
  4. 通过后把 AI 判定、理由、引用 ID 与两种轮次身份写入 `goal_report_evaluated` 事件。
- **幂等**：同一 `task_id`/`execution_run_id`/`plan_sha256`/`report_sha256` 已有判定 → 返回原事件，**不重复调用 AI**；新的 monitor 请求可引用它，但必须自己仍是有效 claim。
- **`advance-task in-progress -> in-review` 对 `pi_desktop`** 必须带当前 monitor claim 与 evaluation event ID；核心在**远端更新前再次**复核：当前任务的执行轮次、报告身份/hash、上述确定性报告底线、事件确由有效 monitor 请求登记、`decision=complete` 与执行终态 `completed + finished/0`。绕过 monitor 直接调用、旧判定或报告变化 → 一律 `conflict`，**不触碰远端**。
- `execution_run_id`（`task.run_id`，被观察的派发轮次）与 `monitor_run_id`（monitor 请求自己的 run_id）**不可互换**。
- `pi` 后端**维持** §5.18.4 原三证据口径与旧接口；核心不把 AI 文案当身份凭据，也不解析 Canvas HTML/截图。

#### T5-D 没有可用 Canvas 报告就没有完成

- 只有 `ready + integrity=structured` 才是可供 AI 分析的报告；Plus 自动生成的 `ready + fallback` **等同「没有完成报告」**。
- 执行中/报告仍在 `pending|draft` → 继续观察；执行终态落地后给 Host **最多 60 秒**完成 outbox/报告发布，期间只读查询，**不重发 prompt**。
- 过时仍无结构化报告、报告 `fallback`、AI 判定 `incomplete` 或报告明确 `partial`/`blocked` → `report needs_user` + `failure_code=execution_failed`，原因写明具体缺口，**不自动重新派发**。
- 报告不可读/损坏或 AI `uncertain` → 不进入 Review，最多**一次有界重读**后同样交人工，并保留**区别于「已验证失败」**的原因；临时读取失败不伪称没有报告。
- 规则只作用于**后续推进动作**：已在 `in_review`/`done`/`needs_user` 的历史任务不回填、不降级、不改判；升级时仍 `running` 的旧 Plus 轮次若无报告，按缺报告规则进入人工处理，**不走旧消息三证据放行**。
- **采集修补（同一变更内）**：`Bash` 工具名大小写归一（`pi_desktop_worker` 的 shell 工具集合须同时接受 `Bash`/`bash`）与退出码保真仍须修，供报告证据与 Review 查阅；但「发现一条退出码 0 的命令」**不再**单独作为 Plus 轮次的放行条件。
- `core/executor_prompts/monitor.md` 必须把 `pi_desktop` 与 `pi` 并列写明各自的分支口径，不得再把 `backend != pi` 称作旧后端。

#### T6 派发命名（仅新建的 `pi_desktop` 轮次）

- **展示快照**：KaneoPilot 在现有计划哈希/任务身份复核后，把 `workspace_display_name = task.project_name`、`session_display_name = task.title` 固化到**新轮次** `spec.json`。两者是**展示快照**——旧 spec 缺字段仍按旧语义读取/续接，**不重新生成原 prompt**。
- **共享创建契约**：Plus 的 `pi_session_create` / Host `session.create` 增加可选 `projectName`，继续使用已有 `title`：

```json
{"projectPath":"<真实独立 worktree 绝对路径>","projectName":"Music","title":"<计划页标题>","mode":"goal", …}
```

  - Host 复用已有 `projects.name` 并在**同一次会话创建调用**里保存名称；返回 `session:{id,title,projectPath,projectName}`；`SessionSummary.projectName` 为可选字段，旧客户端未传时保留 basename 默认。
  - 工作区 `get`/`set` 对已有 path 从 `projects.name` 读回显示名；Sidebar 优先级：`projectMeta[path].name`（用户手工别名）> Host `session.projectName`/工作区名称 > 目录 basename。**工作区身份始终 keyed by canonical path**——同项目两个轮次可各有一个物理 worktree，主名称同为 `Music`，按路径分别归组，**不按名称合并**。
  - 名称创建与会话插入在 **Plus Host 同一数据库事务**内完成；失败不留下只改一半的可见会话。不得为更名调用全局 `pi_project_open`，也不得把 `projectPath` 改成原仓库路径。
  - 名称含空白控制字符、或任务登记标题/项目名缺失 → 在创建会话前给**确定性**校验错误，**不回退到 UUID**。
  - Plus 创建返回的两种显示名与请求不符 → **不下发 `agent_prompt`**，保留已创建会话 ID 供人工检查，**绝不自动再建一次**。
- **兼容门**：Plus 的 `tools/list` 必须表明 `pi_session_create.inputSchema.properties.projectName` 可用；KaneoPilot 在 `exec-capabilities` 与 `dispatch-start` 于**任何 `session.create` 之前**检查，旧 Plus 明确显示「版本缺少工作区命名能力」，**不静默**派发出 UUID 名称。
- **不受影响**：PI CLI 后端、已派发的 spec/job/session、Plus 用户手工重命名、原有项目路径、旧历史轮次（不批量改名）。
- 显示名**不得**用于去重、路径生成、权限判断或完成判定；`session_marker`、`task_id`/`run_id`、`session_id`/`turn_id`、计划哈希与事件关联仍是机器身份。

## 5.24 全流程 Pi-Desktop-Plus 接入（v4，T6）

本节冻结 T6：把普通/修复计划生成、dispatch 控制端、monitor 控制端与 review 控制端的模型调用全部交到用户自己的 PI-Desktop-Plus 本机 MCP 控制面，KaneoPilot 继续负责确定性的队列、租约、审批与状态写入。界面度量见 ui-spec §4.10，执行端编排见 automation-contract §2。本节**不**改变 §5.22 的手动规划门、§5.23.1 的计划阅读、§5.23.3 的任务执行后端、§5.23.9 的 Goal 契约代批与 monitor/Review 状态机。

### 5.24.1 角色后端与配置形状（v4）

四个模型调用点各有**独立**后端选择，互不隐式联动：

| 调用点 | 配置键 | 新增字段 | 缺省解释 |
|---|---|---|---|
| 规划（standard/review_fix） | `settings.planner_config` | `backend: "codex"|"pi_desktop"` + `pi_desktop` 五字段块 | 缺 `backend` → `codex` |
| 任务执行 | `settings.exec_config.backend` | 无（沿用 §5.23.3） | 缺 → `pi` |
| dispatch/monitor 控制端 | `settings.exec_config` | `controller_backend: "pi"|"pi_desktop"` | 缺 → `pi` |
| review 控制端 | `settings.review_config` | `backend: "pi"|"pi_desktop"` + `pi_desktop` 五字段块 | 缺 `backend` → `pi` |

- `pi_desktop` 五字段沿用 §5.23.3：`connection_file/provider_id/model_id/thinking_level/permission_mode`；`permission_mode`：新规划固定 `auto`，Review 固定 `auto`，执行/控制端仍须用户显式保存；默认连接路径仍是 `~/.pi-desktop-plus/mcp-control.json`，不扫描 `.pi-desktop` 或其他 HOME。
- Codex 规划字段（`standard`/`review_fix` 的 `planner_model`/`reasoning_effort`）与 `default_method` 保留，切回 codex 时原样生效；Plus 分支使用 `thinking_level`（Plus 枚举），**不**把 codex `reasoning_effort` 当 Plus 思考等级。
- `review_config.mode`（inherit/custom）仅对 `backend="pi"` 生效；`backend="pi_desktop"` 必须有完整五字段，`mode` 不参与生效。
- 三个配置命令（`get/set-planner-config`、`get/set-exec-config`、`get/set-review-config`）的保存语义不变——原子保存、结构/枚举校验、**服务离线仍可保存**、切换后端不删除另一分支已存字段、失败 `bad_request` 且旧值不变；只扩展字段集合与返回形状。`get-*-config` 的 `config` 含各自 `backend`，未保存 → `null`（生效值取缺省）。
- 切换后端不改变已排队请求、已固化轮次与已启动控制轮：快照按 §5.22.6/§5.23.3 固定，保存只影响之后创建的请求与控制轮。

### 5.24.2 能力查询（v4）

- `planner-capabilities` 顶层增加 `backends`：`{codex:{…沿用 §5.22.6 字段…}, pi_desktop:{available,detail,tools_ok,version}}`。`pi_desktop` 分支只检测**已保存**的 `planner_config.pi_desktop` 五字段与 Plus 连接；`available=false` 时 `detail` 给中文原因（未保存配置/连接文件缺失/`active≠true`/PID 不存活/工具缺失/型号不在本地目录）。工具门 = §5.23.3 基础工具 + `pi_plans_pending`/`pi_plans_resolve`。
- `exec-capabilities`（§5.23.3）顶层增加 `controller`：`{backend, available, detail}`。`pi` 分支沿用 PI CLI 检查；`pi_desktop` 分支与任务执行用同一探针但只要求基础工具集（控制端不建 Goal 会话）。控制器就绪态与任务执行就绪态**分开**展示，不用其一推断其二。
- 新增只读 `review-capabilities {}` → `{pi:{available,detail}, pi_desktop:{available,detail,tools_ok,version}}`：`pi` 分支 = `review_config` 的 pi 可用性（inherit，或 custom 时目标 provider/model/effort 在 PI 目录内）；`pi_desktop` 分支 = 五字段 + 连接 + 基础工具 + 契约审批工具。扩展响应时保留旧字段。
- 候选模型复用 `pi-desktop-catalog`（§5.23.6），严格 `source:"cache"`，不 refresh、不调远端 `/models`、不调用付费模型。
- 所有 `available=true` 只代表**本机配置与协议工具可用**，不证明凭据有效、模型在线或费用可用。缺配置/缺工具/型号不在目录 → 明确失败并给原因，**不**自动降级到 codex/pi，也不为 Plus 分支调用 Codex provider 解析或 PI auth check。

### 5.24.3 规划请求后端与 Plus 计划生成（v4）

- `start-plans.items[]` 增加可选 `planner_backend ∈ {codex, pi_desktop}`（缺省 `codex`；UI 总是提交明确值）。Plus 项在 `payload_json` 固化 `planner_backend` + `runtime_config`（五字段**脱敏**快照，无 token）；codex 项保留 `model_config` 快照。幂等判定包含 `planner_backend`、`planner_method` 与对应运行快照：同 `input_sha256` 但 backend 不同 → `conflict`（不暗换后端）；全同 → `deduplicated`。旧请求缺 `planner_backend` 按 `codex` 解释，不受后来改设置影响。
- Plus 分支只支持 `planner_method="builtin"`：`spec_plan` 是 Codex 插件运行方式，在 Plus 下**明确禁用**并给原因，不把技能文本复制给 Plus 后声称插件实际运行。codex 分支两方式不变（§5.23.2）。
- 执行路径（`planner_runner` 仍负责 claim/心跳/输入校验/frontmatter/哈希/`publish-plan`/hold）：
  1. 核对输入 manifest/哈希与请求快照，在**独立规划 worktree** 内按 manifest 的源码快照准备材料（涉及未提交文件时按 manifest 映出，不把旧 HEAD 当当前源码；该目录由核心管理，不清理用户仓库），且不把 SubmitPlan 的 `.pi/plan` 产物写进用户主工作区。复用同一提交的规划快照时，允许保留未跟踪的 `.pi/plan/*.md` 普通文件与原有 `.kaneopilot-plan-material` 材料；仅 `.pi/plan` 直层 Markdown 产物可豁免，`.pi` 其它新增内容、源码/已跟踪文件修改、规划产物的符号链接（含父目录与被忽略项）、特殊文件仍拒绝。HEAD中原有干净 `.pi` 配置保留，不能整树跳过其新增被忽略项；Review/交付工作树不使用此豁免。不删除旧产物、不修改 Git ignore。Git工作树错误按原分类和脱敏原因呈现，不降为 `planner_internal_error`。
  2. `pi_session_create(mode:"plan", projectPath=<规划worktree>, providerId/modelId/thinkingLevel=快照)` → 必要时 `pi_session_configure(id, permissionMode=<本轮权限快照>, confirm:true)`（新请求 `auto`，旧请求保留 `ask`）→ **发送前先落 `send_state="unknown"`** → `pi_agent_prompt(sessionId, content=任务+材料+角色指令)`；返回 `turnId` 并以持久 turn/`pi_agent_status` 取得启动证明后才登记 `sent`。复用 §5.23.3 的 transport、消息 Collector、脱敏与「只读重连」规则；**不**复用任务 worker 的 Goal 自动审批分支。
  3. `ready` 的**唯一**来源：该 session/turn 的 `kind="plan"` proposal（`pi_plans_pending(sessionId)`），必须严格匹配 `sessionId/turnId/toolCallId/proposalId/version`；runner 不从多个旧 proposal 猜选。terminal `completed` + 转录完整 + 产物身份匹配齐备才接收。
  4. 读取正文后调 `pi_plans_resolve(action:"reject", confirm:true)` 关闭该**生成阶段** proposal 并留痕「已导入 KaneoPilot；此会话不用于执行」；**不发 approve**、不生成 Plus 执行 turn。reject 失败 → 保留产物与身份、`plan_failed`，不发布。若期间该 proposal 已被批准或同会话出现执行 turn → `plan_approval_conflict`：报告冲突、禁止本地发布与二次派发，**不**声称已阻止 Plus 侧执行。reject 后须确认该 proposal 已 `rejected`（空 pending 清单不算成功）。
  5. 正文规范化为现有 `{status:"ready", plan_markdown, questions:[], evidence:[...]}` 后走现有发布逻辑；`task_id`/哈希由核心重算，不信模型自报。**frontmatter 的 `planner_model/reasoning_effort` 取本轮运行快照的**真值：codex 分支取 `model_config`，Plus 分支取 `runtime_config` 的 `model_id`/`thinking_level`（Plus 没有 codex 的 `reasoning_effort`，两者不得混用）；只有两者都缺（旧记录）才回落 §6 历史常量，并必须留痕（`model_pair_source` + 事件），不得让 Plus 计划在计划文件里声称一个从未运行的 Codex 模型。`publish-plan` 继续置 `plan_execution_hold=true`，用户确认后才 dispatch。
  6. `needs_input`：最终答复必须严格为 `{status:"needs_input", plan_markdown, questions:[...], evidence:[...]}`，**不提交 proposal**；completed + 完整最终答复经现有解析器校验后按 §5.22.3 处理（草稿保留、`report needs_user`）。不新增自动修复 JSON 的第二次模型调用。`ready` 无有效 proposal、空输出、混入 `needs_input` 与 proposal、分页不完整 → 明确失败并保留证据。
  7. 附件：`pi_agent_prompt` 支持可选 `attachments`（类型取 Plus `AgentPromptAttachment` 真实定义）。图片按 manifest 逐一校验哈希，复制到该规划 project 根下独立材料子目录后传绝对路径；不得静默省略无法解析的图片；型号/附件不支持 → 明确阻塞（`attachment_unsupported`）。超过 `PI_DESKTOP_MCP_MAX_REQUEST_BYTES`（2 MiB）在发送前拒绝，不截断正文或图片。
- 失败分类扩展 §5.22.6：`plan_proposal_missing`、`plan_proposal_ambiguous`、`plan_approval_conflict`、`plan_reject_failed`、`attachment_unsupported`、`transcript_incomplete`；均不自动重试、不静默换后端或方式。
- review_fix 计划走同一 Plus 路径，仍核验 `review_key`/原报告/关联任务并按 §5.22.5 回写；修复计划仍由用户手动 `prepare-plan-input`/`start-plans` 发起，**不**因创建修复任务自动规划。

### 5.24.4 Plus dispatch / monitor 控制端（v4）

> 本节保留 v4 控制轮历史契约；v5 新的 dispatch/monitor 调度改用 §5.27 的确定性流程，不创建模型控制会话。

- `executor_runner --stage dispatch|monitor` 按 `exec_config.controller_backend` 分流：`pi` 分支行为不变（runner 渲染 prompt 后以 `pi -p --mode json` 执行）；`pi_desktop` 分支创建独立 `mode:"agent"` 控制会话（`pi_session_create` → 必要时 `pi_session_configure(permissionMode=exec_config.pi_desktop.permission_mode, confirm:true)` → `pi_agent_prompt(渲染后的 <stage>.md)`），运行现有相应角色 prompt，模型/权限来自 `exec_config.pi_desktop`；该分支**不再**拉起 `pi -p`。调度器仍只拉 runner。
- **宿主环境必须显式传入**：Plus 的 shell 工具在宿主进程执行，不继承 runner 环境。控制轮 prompt 必须携带本控制轮固定的 `KANPILOT_DB`、`KANPILOT_APP_SUPPORT`、`KANPILOT_PLAN_ROOT`、`KANPILOT_REPOS_ROOT`、`KANPILOT_LEGACY_ROOT`、`KANEO_CREDENTIALS_PATH` 与核心根/可执行路径，经正确 shell 引号或 runner 生成的**固定本地包装器**调用既有 CLI；包装器只启动现有 CLI 并注入这些环境，不得暴露 token、绕过核心规则或变成替代状态管理脚本。工作目录使用独立控制目录，不更改 Plus 全局当前项目。
- **产物与检查点**：控制轮以 `invocation_id` 区分，产物落 `<APP_SUPPORT>/executor-sessions/<stage>/<invocation_id>/`（脱敏 job/events）；`settings.executor_runs[stage]` 增加 `backend/session_id/turn_id/invocation_id/config/send_state`，保留旧 `pid/started_at/log/exit_code/finished_at`。控制 `invocation_id` 与任务执行 `run_id` **不可混用**。
- 保留既有 dispatch/monitor 领取顺序、停止与 unknown 策略（automation-contract §2）：monitor **不**新建任务执行会话、不发执行 prompt，只经自己的控制会话调用 `get-session`/`round-observe` 观察既有轮次；Goal 契约门只由任务 worker 管理。
- Plus 控制会话 `completed` 只表示**控制模型回合结束**；请求是否成功以核心 claim/report 与请求结果为准。进程日志正常退出不得把 queued 标成 running——仍遵循「已排队≠已开始」。
- 发送与恢复：发送前持久 `send_state="unknown"`，响应不明不重发；已知 session/turn 只重连读取；恢复轮先查已保存身份，不重复 `pi_session_create`/`pi_agent_prompt`；`stale_run` 后停止该项；无已知 turn 的 unknown 保留人工阻塞，同一阶段禁止新控制轮重复发送。
- **单飞扩展**：既有 worker PID 与持久活跃/unknown 控制轮**都**能阻止重复启动；控制会话进程消失只触发只读恢复，不视为已完成。关闭/暂停 App 调度只阻止新轮，不擅停活跃 Plus turn。
- 心跳沿当前 request lease：`claim` 后每 ≤ `min(lease_seconds/3, 60)` 秒；心跳被拒（`stale_run`/`not_found`）按 §5.23.7 为正常收尾（停止续租，**不**写失败）。超时/停止复用 §5.23.3 的 `stop → abort → pi_turn_get` 有界处理与明确 unknown；权限等待不得标成功。
- 控制端连接失效/身份不符/工具缺失 → 明确失败（`pi_desktop_controller_unavailable`），**不**自动回退 pi。

### 5.24.5 Plus review 控制端（v4）

- `executor_runner --stage review` 按 `review_config.backend` 分流；`pi` 分支沿用 §5.23.4（inherit/custom 固化 pi 参数）。
- Plus 分支：`claim(stage=review)` 后核对请求 `run_id`、任务本轮执行 worktree、计划哈希、执行证据与关联来源（**不得**只审主仓库）；runner 用只读 `git status/diff` 生成改动材料，并把计划/报告/执行证据复制到**专属 Review worktree** 的材料目录（该 worktree 是审查输入，不复用仍活动的执行会话）；`pi_session_create(mode:"plan", …)` + `permissionMode:"auto"`；指令只要求审查并输出 JSON，不 `SubmitPlan`、不 `SubmitGoal`、不代批任何契约。
- 最终答复结构冻结为 `{"conclusion":"pass|changes_requested|blocked","report_markdown":"<非空>","fix":{"title","description","priority"}|null}`；`changes_requested` 必须有 `fix`；`priority ∈ no-priority|low|medium|high|urgent`（`bridge.VALID_PRIORITIES`）。Plus 无原生 outputSchema 能力：runner 在 `completed` + 完整转录后严格解析，**不**把 MCP `structuredContent` 当模型结构化输出保证。
- 意外生成 proposal：只读确认本 session/turn 身份后 `pi_plans_resolve(action:"reject", confirm:true)` 关闭并记 `review_blocked`，**不**按其 markdown 登记 pass。
- 格式错误、转录不完整、身份不符、权限等待、超时 → 不写 pass、不推进 done，保留错误与证据。
- 报告与登记：runner 写 `<APP_SUPPORT>/reports/<task_id>/<run_id>.md`，核心重算报告哈希。
  - `pass`：`register-review(pass)` → `advance-task(done, expected_current=in-review)` → `report succeeded`。
  - `blocked`：`register-review(blocked)` → `report needs_user`（保留 `review_blocked` 分类）。
  - `changes_requested`：**先** `create-review-fix` 取得 `fix_task_id`/`source_sha256`，**再** `register-review(changes_requested, review_key, fix_task_id, fix_task_source_sha256)`，最后 `report succeeded`（修正 v3 自动化契约的相反顺序）；`dedupe_key = sha256(规范 JSON(源 task_id, 本周期任务执行 run_id, 已核验报告 sha))`，`review_key = dedupe_key`。
- **登记归属与周期保护**：`register-review` 增加可选 `request_id`、`expected_execution_run_id`；新自动 Review（Plus 与更新后的 CLI prompt）**必须**传 `request_id` + `run_id` + `expected_execution_run_id`。核心在事务内核对 `request.stage=review`、`task_id`、`status=claimed`、`run_id` 与未过期 lease，以及任务仍是该次执行对应的评审周期；失配 → `stale_run`/`conflict`，**不**写报告登记或 pass。无身份的旧手动调用保留兼容，但不作为新自动链路入口。`create-review-fix`/`advance-task` 的新 Review 调用同样传身份并走该校验；`advance` 的已完成恢复场景按未完成判定只读处理，不新推进。
- **部分成功恢复**：job 持久化 `execution_run_id`、最终结果/报告 sha、`dedupe_key`、fix 结果与 `register`/`advance`/`report` 的已确认检查点。恢复先以 `get-task`/`get-request` 读核心事实：登记内容与同一报告 sha/结论/key/fix 一致 → 跳过 `register`；不一致 → `conflict`，不覆盖新周期。`pass` 已 done → 核对当前报告对应的 pass 登记与远端 recheck 证据后跳过 `advance`；仍 `in-review` 且租约有效 → 只推进剩余动作。`changes_requested` 已存在同 key fix 与登记 → 只完成 `report`。`report` 响应丢失 → 读 `get-request`：该 request/run 已以相同 outcome 结束即认定汇报完成，**不**重放非幂等的 `report`；仍 `claimed` 且租约有效才继续。lease 失效/被新 run 接管 → 停止写入，`executor-status` 显示「评审收尾受阻」，交人工恢复；不重新调用模型、不自动领第二个 review、不因已有 `review_report_path` 静默判成功。
- 服务端无 CAS：本地 guard 只序列化本地写入，不声称阻止远端竞争。
## 6. 常量

- `PROJECTS`：`Music→example-project-01`、`Library→example-project-02`、`StudyTool→example-project-03`、`Comic→example-project-04`、`Feedback→example-project-05`；仓库根 `~/Documents/Code/Project/Personal Project/<项目名>`。
- `WORKSPACE_ID = example-record-id`
- `PLAN_ROOT = ~/Documents/Code/Plan`
- `LEGACY_STATE_ROOT = ~/Library/Application Support/Codex`（KaneoPlanner/KaneoExecutor/KaneoReviewer）
- `APP_SUPPORT = ~/Library/Application Support/KaneoPilot`（DB、备份、报告缓存）
- `TODO_SLUGS = {to-do, todo}`；`REVIEW_SLUGS = {in-review, review}`；`DONE_SLUGS = {done}`。
- 计划 frontmatter 必填：`task_id project_id project_name repository source_sha256 requirements_sha256 planner_model reasoning_effort implementation_status generated_at`；planner_model/reasoning_effort 须与该请求的**运行快照**一致——codex 为 `model_config`（§5.22.6），Plus（`planner_backend="pi_desktop"`）为 `runtime_config.model_id`/`runtime_config.thinking_level`（§5.24.3）；旧记录两份快照都缺时才回落默认 `standard=gpt-6-astra/medium`、`review_fix=gpt-5.6-sol/xhigh`。review_fix 另含 `review_report_path review_report_sha256 review_source_task_id review_key`。
- v4（§5.24 T6 全流程 Plus 接入）：`PLANNER_BACKENDS=(codex,pi_desktop)`（缺省 `codex`）、`CONTROLLER_BACKENDS=(pi,pi_desktop)`（缺省 `pi`，存 `exec_config.controller_backend`）、`REVIEW_BACKENDS=(pi,pi_desktop)`（缺省 `pi`，存 `review_config.backend`）；每个后端分支各存自己的脱敏快照，切换不删除另一分支。
- v4 工具门：Plus **控制端**（dispatch/monitor）= `PI_DESKTOP_REQUIRED_TOOLS`；Plus **计划/Review** = `PI_DESKTOP_REQUIRED_TOOLS + PI_DESKTOP_GOAL_TOOLS`（`pi_plans_pending`/`pi_plans_resolve` 用于读取并安全关闭产物检点）；Plus **任务执行** = `PI_DESKTOP_DISPATCH_TOOLS`（基础 + 契约审批）。缺任一即 `tools_ok=false`，不做事后半残派发。
- v4 会话模式与权限：Plus 计划/Review 固定 `mode=plan`；新规划固定 `permission_mode=auto`，Review 固定 `auto`。Plan 模式不是 OS 只读沙箱，规划可无需工具确认运行 Bash 并修改文件；旧规划请求保留原权限快照；控制端固定 `mode=agent` 并使用用户显式选定的 `permission_mode`；任务执行固定 `mode=goal`（§5.23.9）。没有自动批准 Plus 工具权限的通道。
- v4 Review fix 优先级枚举：`no-priority|low|medium|high|urgent`；`register-review` 身份字段：`request_id`、`expected_execution_run_id`（新自动链路必传）。
- 已知限制：Kaneo 服务端无 CAS，桥接的先读后写有远端并发窗口；本地事务只序列化本地写入，不声称阻止人工远端竞争。

## 5.25 本机双向 MCP（schema 5）

- 项目服务为 `kaneopilot`，HTTP 只绑定 loopback，协商 `2025-06-18` / `2025-03-26`；stdio 是同一服务的逐行 JSON-RPC 桥，不建立第二份任务状态。
- 每次 HTTP 请求必须验证 Bearer、精确 Host、存在时的 Origin；transport session 绑定同一凭据、scope、协议与来源。撤销、过期、重启后的旧 grant 无效。管理凭据仅用于 `/admin/scopes` 与 initialize/initialized/ping 就绪检查，不可列出或调用模型工具。
- runtime/task grant 分离。角色 scope 是稳定 invocation 或 task/run 身份、安全文件名、固定项目/五字段运行配置与有效期；文件 owner-only，凭据不进模型、argv、数据库或日志。有效 scope 同 ID 只能复用完整快照，续期不得超出初始观察边界；过期后由本机管理端重新注册相同身份，新 token 替代旧 token。
- 新 Plus 轮次先检查项目 MCP 服务就绪，再经 `kp_runtime_session_create → session_configure → prompt`。不挂接 task grant，不要求 pi_session_attach_mcp，不向会话注入 kp_task 工具；runner 经核心命令完成状态转移。新规划固定 `plan/auto`，Review 固定 `plan/auto`，旧规划保留原权限快照，执行固定 `goal/auto`；dispatch/monitor/delivery 为确定性 Python 流程。历史轮次保留原只读恢复路径。
- `kp_runtime_session_create` 可选接收 `title` / `project_name`，分别透传 Plus `title` / `projectName`；返回 `session_id` 以及 Plus 实际返回的展示名，不以请求值补造成功回显。展示名不是角色配置或授权身份，不改变 scope 的项目路径/模型/模式校验；worker 按 §5.23.10 核对真实回显，不符时不发送 prompt。
- session 归属由已接受的 create 收据核对；create 的项目和配置必须等于 scope 快照。所有 runtime 写用稳定 operation_id，短事务预留 `prepared→unknown` 后释放锁再调用 Plus；成功保存 accepted 和 session/turn/proposal 身份。同 ID 同参数返回已接受结果，不同工具/参数冲突；unknown 禁止自动重发。调用 Plus 时不持有 SQLite 写锁，允许其同步回调 task 工具。
- execution runtime role 可使用只读 `kp_runtime_goal_report_list {scope_id, session_id}` 和 `kp_runtime_goal_report_get {scope_id, session_id, execution_id?, report_id?}`（get 至少提供一种报告身份）。请求须校验有效 scope 与其已接受的 session 归属，只使用 scope 固化连接；查询不创建写操作收据、不重发执行。返回 Plus 报告分态、正文与哈希，完成底线仍按 §5.23.10 核验。其他角色读取执行报告走任务核心 `get-goal-report`；无 scope 的历史轮次仅沿已有历史只读 adapter 接缝读取，不开放写入口。
- `kp_runtime_proposal_resolve` 必带 `session_id/turn_id/proposal_id/tool_call_id/version/action`，先校验待审批记录身份；可选 target_permission_mode 仅执行 Goal 且等于授权权限快照；真实 Plus DTO 使用 `proposalId/toolCallId/confirm`。消息通过 `pi_session_get(id,messageBefore,messageLimit)` 物理分页，保持原字符串消息 ID 及工具字段。
- task 工具映射固定核心命令，输入复用 CLI 字段（包括 dispatch evidence），禁止未知字段。scope 只允许自己的 task/request/run；monitor 读取执行轮次时与其领取 run 分开。task 写在获取 SQLite 写锁后再次校验当前领取、租约、角色、任务与轮次，recheck 也属于写工具。monitor 只能被动保持 done/in-review 或有证据地 in-progress→in-review，不能直接推 done。
- `mcp-claim` 在领取提交的同一事务登记结果；并发同 operation 只领一次，同 scope 最多一项活跃领取。汇报丢响应后读取同一 `get-request` 核对，不盲目重报。scope 的内存绑定不是核心状态源。
- `tools/call.structuredContent` 和 content.text 同步为 `{ok:true,data:{...}}` 或 `{ok:false,error:{code,message}}`，outputSchema 描述完整信封。业务错误 `isError=true`。输入/输出限制 2 MiB，超限明确报错而非截断。
- 普通命令和服务不隐式升级旧库，只有显式 `migrate` 允许升级并生成一致性备份；新空隔离库可初始化，缺版本或不明已有表拒绝初始化。
- App 按 profile 保存服务启用状态并启动恢复；实际 Bearer initialize/initialized/ping 成功后才展示运行。退出/关闭只停止本实例的 Process，不按 manifest PID 终止外部进程，不杀 Plus 会话。设置 `pi-desktop-probe` 只握手和读工具，分别返回控制面可连接与 `attachment_supported`，不声明真实模型闭环已验证。

## 5.26 项目与本地仓库灵活映射 (T1)

- **存储与身份**：
  - 项目权威身份全链以 `project_id` 为唯一基准，显示名仅用于展示。
  - 映射保存于 SQLite `settings.project_mappings`：`{version: 1, revision: <int>, workspace_id: "<id>", items: {<project_id>: {project_name, repository, enabled, updated_at}}}`。无需升级 SQLite schema 版本（保持 v5）。
  - 未配置键时安全提供旧五项目只读默认映射（`revision=0`）；首次配置写入持久化完整集合；停用与解除映射明确持久化状态，绝不回退至旧硬编码常量。
  - 路径支持空格、中文与 `~` 展开，core 自动规范化并校验绝对路径；Git 子目录自动定位至仓库根。
- **规划与执行链快照固化**：
  - `fetch-todos` 携带 `mapping_state`（`unmapped|available|disabled|missing|unreadable`）、`repository_kind`（`git|directory|null`）与 `blocked_reason`；未映射项目在规划列表中明确标红并阻止准备。
  - `prepare-plan-input` 提取当前有效映射的 `project_id/project_name/repository/project_mapping_revision` 写入 `manifest.task` 并固化至 `input_sha256`。
  - `start-plans` 事务内验证目标映射一致性并固化 `project_snapshot`；已有排队或执行中的请求不受后续映射修改影响。
  - `planfile.check_plan_meta` 与派发校验比对固化快照中的 ID、路径与名称；新规划文件安全写入 `<plan_root>/projects/<project_id>/...md`，校验路径位于 Plan 根内。
  - 默认运行记录根动态解析 `constants.app_support()/"runs"`，确保在隔离沙箱下运行。


### 5.27 全自动反馈流水线（v5）

本节为现行增量契约，涉及自动化管理任务时优先于 §4 与 §5.22–5.25 的旧版手动门控、跨任务修复和角色路由描述。普通手动流程保留；验证证据采用本节确定性规则。SQLite schema 仍为 5，新增字段存在 extra/settings，不迁移正式库。

新增命令：set-project-automation、set-automation-global、record-cost、delivery-commit、delivery-publish、delivery-sync。delivery 是可暂停阶段，领取守卫为 in_review；blocked 是成功命令响应中的业务阻塞 outcome，不新增 CLI error code。

#### 5.27.1 dispatch/monitor 改为确定性流程

两个新模块由 `executor_runner` 调用，入口签名与 `pi_desktop_controller.run_stage` 一致，只是去掉了 prompt；`sleep`/`clock` 可注入，便于测试：

```python
def run_stage(stage, *, conn, db_path, timeout, log_path, record_run,
              resume=False, sleep=time.sleep, clock=time.monotonic) -> int
```

实现约束：

- 只通过进程内 `cli.run(command, payload, db_path)`（cli.py:451）调用固定核心命令，捕获 `util.KanpilotError` 后按 `.code` 分支。
- 不直接读写 SQLite，不用 shell 调 CLI，不调用任何模型。
- 每一步写一行 JSON 日志到 `log_path`，内容为时间、步骤、task_id、request_id 和结果码；不写计划正文、凭据或完整远端 URL。
- `record_run` 在开始时记 pid、started_at、log，结束时记 exit_code、finished_at。
- 剩余时间不足 120 秒时不再领取新请求。
- 由 `resume` 参数接收旧调用，但不使用它。

**dispatch（执行器 `kaneo-dispatch`）：**

1. `claim {stage:"dispatch", executor:"kaneo-dispatch"}`。
   - `claimed:false`（含 reason=paused）→ 正常结束。
   - `context.backend` 不是 pi/pi_desktop → 不调用任何写命令，记日志后结束；lease 到期后按既有丢失流程处理。
2. `dispatch-prepare {task_id, run_id, evidence:{session_marker, repository, backend, config: context.exec_config}}`。任何失败 → 第 6 步，`not_sent`。
3. `dispatch-begin-send {task_id, run_id}`。任何失败 → 第 6 步，`unknown`：核心可能已迁移远端或已自行阻塞任务，dispatch-fail 对同一 run 幂等。
4. `dispatch-start {task_id, run_id}`：
   - 返回 deduplicated，或 started 且带 session_id → 第 5 步；
   - 返回 starting → 每 ≤30 秒 `get-session` 一次，最多等 5 分钟；拿到 session_id 就进第 5 步，否则进第 6 步 `unknown`，原因写「worker 会话未建立」；
   - 返回任何错误 → 第 6 步 `unknown`。
5. `dispatch-confirm {task_id, run_id, evidence:{session_marker, repository, backend, session_id, protocol_seen:true, config}}`。
6. `dispatch-fail {task_id, run_id, send_state, reason}`：
   - 只有在 begin-send 之前失败才用 `not_sent`；
   - begin-send 之后一律用 `unknown`；
   - 绝不把 unknown 改回 not_sent。
7. 整个过程（包括等待期间）每 ≤60 秒 `heartbeat` 一次。heartbeat 或任一命令返回 stale_run/not_found，说明请求已被他人接管：立即停止，不再做任何写操作。
8. 回到第 1 步继续领取，直到没有请求或剩余时间不足。

**monitor（执行器 `kaneo-monitor`）：**

1. 领取 monitor 请求，然后 `get-task` 取 `task.run_id`。这是被观察的执行轮次，**不是** monitor 请求自己的 run_id。
2. `recheck {task_id}` 读远端状态：
   - done → `advance-task {task_id, run_id:<monitor run>, target:"done", expected_current:"done"}`（被动同步对）→ report succeeded；
   - in-review，且任务不受自动化管理 → `advance-task (in-review, in-review)` 被动同步 → report succeeded；
   - in-review，且受自动化管理 → 不推进，继续第 3 步；完成时用 `expected_current="in-review"`；
   - 其他状态 → 继续第 3 步；完成时用 `expected_current="in-progress"`。

   调用 advance-task 时只传 `run_id`，**绝不传 request_id**：传了会触发 stage==review 的身份校验并失败。
3. `get-session {task_id, run_id:task.run_id}` 读 round_detail；`session-messages` 按 has_more 翻页，读完本轮全部消息，不用 preview。
4. 观察者缺席：backend=pi_desktop，且 observer_alive=false 或 detail 以 `heartbeat_failed:` 开头 → `round-observe {task_id, run_id:task.run_id, executor:"kaneo-monitor"}`。
   - 返回 observing/finished/settled → 重新读取；
   - 返回 deferred/exited/starting → report failed，note=observation_blocked；
   - 返回 `reopened:true` → 视为仍在运行。

   observer_alive=true 时绝不再拉起观察者。
新 Plus 派发 spec/job 固化 `completion_policy=command_verification`；历史缺省为 `goal_report`，其报告底线仍按 §5.23.10，缺少可用判定时交人工，不能伪造 AI 结论。确定性推进由核心在远端写入前再次核对当前有效 monitor claim、task/run、终态、计划哈希和验证证据（或只适用本轮的用户接受记录）；不得以策略字段或客户端成功声明绕过验证。

5. 只认 `round_detail.terminal`。Plus Goal 审批期间规划轮次的 completed 不算完成。terminal detail 含 goal_contract_missing、goal_approval_failed 或 goal_execution_unstarted → report needs_user（failure_code=execution_failed）。
6. 完成判定，以下三条同时满足：
   - terminal.outcome=completed；
   - exit_code=0 且 run_status=finished；
   - 验证通过（规则见第 9 节），或 `extra.verification_override.execution_run_id == task.run_id`。

   验证不通过 → report needs_user（failure_code=verification_unconfirmed），note 逐条列出缺失和失败的命令。
7. 完成后，受 PR 交付管理的任务先调用 `delivery-commit {task_id, request_id, run_id, expected_execution_run_id: task.run_id}`：
   - outcome=blocked → report failed，note=`blocked:<failure_code>`（核心已把任务转入待处理）；
   - committed/already_committed → `advance-task` 推进到 in-review → report succeeded。

   advance-task 返回 remote_conflict（核心已阻塞任务）或 remote_unavailable → report failed，下轮重试；提交是幂等的。最后 `record-cost` 记一条 Plus 执行轮次（cost_usd=null，source=pi_desktop）。
8. 仍在运行 → heartbeat + report succeeded，progress 记录摘要。`terminal_superseded` 不算失败。
9. 明确失败（failed、非 0 退出、超时、截断、身份不符、send_state=unknown）→ report needs_user（execution_failed）。不重试、不重建、不重发。
10. 旧后端（dsh/headless/ATIF）轮次永远读不到 → report needs_user（failure_code=observation_blocked，note「旧后端轮次无法观察，请人工判断」）。其他读不到的情况 → report failed（observation_blocked），下轮再试。

**路由（主控 Step 14）：** `executor_runner.run_stage` 的新分流：

- dispatch → auto_dispatch，monitor → auto_monitor，不再看 controller_backend。
- review 按 Review 后端：
  - pi_desktop → pi_desktop_review；
  - claude/codex → cli_review；
  - pi → 原 `pi -p` 路径。
- delivery → delivery_runner。

dispatch.md、monitor.md、pi_desktop_controller.py 及其测试保留，但不再路由；`controller_backend` 只用于展示。

#### 5.27.2 KaneoPilot 侧去掉 attach 前置

- runtime_client：
  - 193–194：去掉必需工具 `pi_session_attach_mcp`；
  - 287–288：去掉「未挂接会话拒绝发 prompt」。
- pi_desktop_planner:686、pi_desktop_worker:850、pi_desktop_review:543：去掉 attach 调用和其后的工具集校验。会话顺序固定为 create → configure → prompt。
- dispatch_validate：
  - 去掉 188–191 的 `attachment_supported` 门；
  - **保留** 176–187 的本机 MCP 就绪门（server.json initialize/ping）和 `exec_config.check_available`。
- 以下保留但不调用：
  - harness_plus_adapter 的 `attach_mcp`；
  - runtime_client 456 的 `_deny_write` 映射；
  - kp_runtime_attach_mcp 的工具实现。

  `attachment_supported` 只做展示，界面文案为「本版本不需要会话挂接」。
- Plus 会话不获得任何 kp_task 工具；所有状态转移由 runner 和确定性控制端经核心命令完成。规划、执行、Review 的 Plus prompt 里如有要求模型调用 kp_task_* 的文字，一并删除。

#### 5.27.3 任务分支与工作树

每个任务一条分支 `kaneo/<task_id>`；task_id 必须匹配 `^[a-z0-9]{8,64}$`（Kaneo 任务 id 由 cuid2 生成，为小写字母和数字），否则拒绝。工作树根沿用 `_ensure_worktree`（cmd_dispatch 858）的 `constants.worktrees_root(db.get_setting(conn, "worktrees_root") or None)`，可被 `KANPILOT_WORKTREES_ROOT` 覆盖，不另起新根。既有执行目录为 `<root>/<task_id>/<run_id>`，新目录与它并列：

- `<root>/<task_id>/branch`：执行工作树（检出分支）。Codex/Claude 规划器也以它为只读 cwd。
- `<root>/<task_id>/review-<sha12>`：Review 只读快照（detached HEAD）。
- `<root>/<task_id>/plan-<sha12>`：Plus 规划只读快照（detached HEAD）。

以上全部保留，不删除分支，也不删除工作树。对用户主检出只允许执行 fetch、worktree add、rev-parse、symbolic-ref、remote get-url，绝不在主检出上 checkout、reset、clean 或 stash。

**建分支工作树（`prepare_branch_worktree`）：**

1. `git -C <repo> fetch --prune origin`：超时 60 秒，环境变量带 `GIT_TERMINAL_PROMPT=0`。失败 → git_transient，intake 下轮重试。
2. 确定目标分支：
   - 优先用策略里的 `delivery_base`；
   - 否则取 `git -C <repo> symbolic-ref --short refs/remotes/origin/HEAD`，去掉 `origin/` 前缀；
   - 用 `git rev-parse --verify origin/<base>^{commit}` 取 base_sha。

   以上任一步取不到 → delivery_base_unknown。
3. 本地分支 `refs/heads/kaneo/<id>`、远端 `refs/remotes/origin/kaneo/<id>` 或目标路径，任一已经存在，且不是本任务 delivery 记录中的同一工作树 → branch_exists。不删除、不复用、不改名。
4. `git -C <repo> worktree add -b kaneo/<id> <path> <base_sha>`，不加 --force。
5. 返回 `{branch, worktree_path, base_branch, base_sha, head_sha: base_sha}`。

**核验工作树（`verify_branch_worktree`）：** 以下条件不满足时报 worktree_invalid：

- 路径存在且是目录；
- `git -C <wt> rev-parse --git-common-dir` 解析到主仓库的 common dir；
- `git -C <wt> symbolic-ref --short HEAD` 等于分支名；
- 给了 expected_head 时，`git rev-parse HEAD` 必须相等；
- require_clean 时，`git status --porcelain=v1 -z --untracked-files=all` 必须为空。

**派发指令（主控在 constants 定义 `DISPATCH_INSTRUCTION_BRANCH`）：** 原文照用；其中 `{plan_ref}` 在 pi 后端为「计划文件 {plan_path}」，在 pi_desktop 后端为「下面已核验的计划正文」。

```
任务标识：{marker}。当前目录 {worktree} 是本任务专用分支 {branch} 的工作树，直接在这里完整执行{plan_ref}。可以按计划使用 subagent，但所有改动必须落在本工作树；不要新建 worktree 或分支，不要 commit、push、reset、stash、rebase 或切换分支；提交与推送由调度台完成。按计划的 verify 命令逐条运行验证，最终答复逐条列出命令与退出码。
```

注入点：

- cmd_exec:389 的 claim 上下文（主控）；
- cmd_dispatch:472 的 pi 指令和 603–608 的 pi_desktop 内联指令（包 C）。

非交付任务仍用原 `DISPATCH_INSTRUCTION_PI`。

**各角色的工作目录：**

| 角色 | 工作目录 | 权限 |
|---|---|---|
| Codex/Claude 规划 | `<root>/<id>/branch`（请求载荷 `plan_cwd`） | 只读工具 |
| Plus 规划 | `<root>/<id>/plan-<sha12>`，sha 取载荷 `delivery_head_sha` | plan 模式 + auto（旧请求保留权限快照） |
| Plus 执行 | `<root>/<id>/branch` | auto |
| Review（三种后端） | `<root>/<id>/review-<sha12>`，sha 取 `delivery.head_sha` | 只读 |

建 Review 快照之前，先确认执行工作树 HEAD 等于 head_sha 且干净。

#### 5.27.4 项目策略与本机前置检查

策略存在项目映射设置的 `items[project_id].automation` 下，形状如下：

```json
{"enabled": false, "label_filter": [], "auto_confirm_plan": true,
 "max_fix_rounds": 2, "max_task_minutes": 240, "max_task_cost_usd": 10.0,
 "delivery_base": null, "policy_revision": 0, "updated_at": "..."}
```

字段校验：

- label_filter：≤20 个字符串，每个 ≤50 字符；去掉首尾空白后，按小写去重。
- max_fix_rounds：0..2。
- max_task_minutes：30..1440。
- max_task_cost_usd：null，或 0.5..100。
- delivery_base：null，或匹配 `^[A-Za-z0-9._/-]{1,100}$` 且不以 `-` 开头。

全局设置存在 settings `automation_global = {max_active_tasks: 2, updated_at}`，max_active_tasks 取值 1..5。

**命令：**

- `set-project-automation {project_id, automation, expected_policy_revision?}`：
  - 校验后整体替换 automation 对象，policy_revision 加 1；
  - 带了 expected_policy_revision 但不相等 → conflict。
- `set-automation-global {max_active_tasks}`。
- `get-project-mappings` 的每个条目增加 `automation`（合并默认值后的结果）和 `automation_preflight`。
- `set-project-mapping` 修改其他字段时，必须原样保留 automation。

**前置检查（`automation_preflight(conn, project_id) -> [{"code","message"}]`，空列表表示就绪）：** 只做本机 Git/配置检查，不主动发起远端探测、SSH 连接或 fetch/push，单项目耗时 <200ms。

| code | 检查 | message 示例 |
|---|---|---|
| repository_missing | 映射仓库路径存在 | 未配置仓库路径 |
| not_git_repo | `git -C <repo> rev-parse --git-dir` 成功 | 仓库不是 git 仓库 |
| origin_not_github | `git remote get-url origin` 使用与 PR/推送相同的解析规则：GitHub HTTPS/SSH 标准地址，及经本机 OpenSSH 配置确认的 SSH 别名；HTTP/Git 协议、HTTPS 别名、未知或非 GitHub 主机拒绝，URL/SSH 配置不输出、不记录 | origin 不是 GitHub 仓库 |
| gh_missing | `constants.gh_bin()` 可执行 | 未找到 gh |
| planner_unconfigured | 规划后端已配置且可用（沿用 planner_config 的检查） | 规划后端未配置 |
| review_backend_unsupported | Review 后端属于 `AUTOMATION_REVIEW_BACKENDS` | Review 后端需为 Plus/Claude/Codex |
| exec_backend_not_plus | 执行后端为 pi_desktop | 执行端需为 Plus |
| exec_permission_not_auto | exec_config.pi_desktop.permission_mode 为 auto | Plus 执行权限需为 auto |

SSH 别名限 `git@alias:owner/repo(.git)` 与 `ssh://git@alias[:port]/owner/repo(.git)`。在仓库工作目录以参数数组运行有界 `ssh -G`（禁用主机名 DNS canonicalization），读取本机可信 Host/Include/Match 配置；只有有效 hostname 为 `github.com` 或 `ssh.github.com`、user 为 `git`、port 为 22 或 443 时接受。错误、超时、非法主机或路径均拒绝，不跨检查缓存展开结果。不得把未知 HTTPS 域名交给 SSH 解析；不得解释执行仓库的 shell 传输命令。新增别名分支遇 `core.sshCommand` / `GIT_SSH_COMMAND` / `GIT_SSH` 自定义传输时，未能可靠确认则拒绝。`GIT_SSH_VARIANT` 优先于 `ssh.variant`，仅未设置、`auto` 或 `ssh` 兼容默认 OpenSSH；其余 variant 拒绝。传输配置检查与 SSH 展开共用 100ms 截止。GitHub owner/repo 身份归一化仅用于校验，实际 fetch/push 继续使用原 SSH 别名地址，以保留多账号认证。

**生效规则：**

- 实时策略：intake、自动确认、修复轮上限、成本/时长上限这类新决策，每次都读当前策略。
- 交付方式：在 intake 接收时冻结进任务的 `delivery.mode`。
- 关闭开关：
  - 停止 intake、自动确认和自动修复规划；
  - 进行中的 monitor、Review、交付照常；
  - changes_requested 照常登记，但修复请求置为 held，任务进入待处理（原因 `automation_stopped`）。

#### 5.27.5 任务 extra 字段

`db.set_extra` 只合并顶层键，因此嵌套对象必须在同一事务里整体读出、修改、写回。主控在 core 中提供访问函数：

- `automation_of(task)`、`delivery_of(task)`；
- `update_automation(conn, task, now, **changes)`、`update_delivery(conn, task, now, **changes)`。

任何包都不得自己拼 extra_json。

```json
{
  "automation": {
    "policy_revision": 3, "adopted_at": "...", "fix_round": 0,
    "segment_started_at": "...", "segment_cost_base_usd": 0.0,
    "plan_wanted": false, "plan_attempts": 0, "manual_takeover": false,
    "cost_usd": 0.0, "cost_unknown_steps": 0,
    "cost_log": [{"request_id": "...", "run_id": "...", "stage": "plan", "cost_usd": 0.42, "source": "claude", "at": "..."}],
    "last_skip": {"reason": "budget", "at": "..."}
  },
  "delivery": {
    "mode": "pr", "branch": "kaneo/<id>", "worktree_path": "...",
    "base_branch": "main", "base_sha": "...", "head_sha": "...",
    "commits": [{"execution_run_id": "...", "sha": "...", "at": "...", "files": 3, "fix_round": 0}],
    "pushed_sha": null, "pr_number": null, "pr_url": null, "pr_state": null,
    "publish_failures": 0, "kaneo_comment_at": null, "merged_at": null,
    "last_sync_at": null, "next_sync_at": null
  },
  "fix_request": {
    "fix_round": 1, "title": "...", "description": "...", "priority": "...",
    "review_key": "...", "review_run_id": "...", "execution_run_id": "...",
    "report_path": "...", "report_sha256": "...", "head_sha": "...",
    "created_at": "...", "state": "open"
  },
  "review_history": [{"review_key": "...", "conclusion": "changes_requested", "report_path": "...",
    "report_sha256": "...", "execution_run_id": "...", "head_sha": "...",
    "registered_at": "...", "fix_round": 0, "fix": {"title": "...", "priority": "..."}}],
  "plan_verify": {"plan_sha256": "...", "commands": ["..."], "error": null},
  "verification_override": {"execution_run_id": "...", "actor": "user", "at": "...", "note": "..."}
}
```

字段规则：

- **数量上限：** `cost_log` ≤50 条，`review_history` ≤20 条，超出时丢最旧的。
- **无人值守段：** `segment_*` 记录当前无人值守段的起点。时长按 `now - segment_started_at` 计，成本按 `cost_usd - segment_cost_base_usd` 计。用户执行 resume、replan 或 accept_verification 时开启新段。
- **plan_verify：** 每个任务都写，不限自动化任务。
- **delivery.mode：** 只有 `pr` 或 `manual` 两个值；转为人工处理时改为 `manual`。
- **术语：**「受自动化管理」= 有 automation 对象且 `manual_takeover=false`；「受 PR 交付管理」= `delivery.mode=="pr"`。

#### 5.27.6 自动接收（intake）

入口：`python3 -m kanpilot.auto_intake [--dry-run]`，由调度器每 300 秒或菜单「立即检查」拉起。

**一轮流程：**

1. **单飞：** `executor_runs["intake"]` 记录的 pid 仍存活 → 直接退出。
2. **暂停检查：** 全局暂停，或 paused_stages 包含 plan → 写摘要后退出。
3. **项目筛选：** 逐个读取已开启的项目；前置检查不通过的项目跳过，并记入摘要。
4. **容量：**
   - 容量 = `max_active_tasks − 占用数`；
   - 占用数 = 受自动化管理、且状态不是 done/needs_user、也不在「pass 后等待合并」的任务数。
5. **先处理 plan_wanted 任务（不受容量限制）：**
   - 条件：状态为 needs_plan、`plan_wanted=true`、没有活动的 plan 请求、未转人工；
   - 若还没有 `delivery.branch`，先补建分支工作树；
   - 然后准备材料并提交规划。
6. **新候选：**
   - 来源：`fetch-todos` 的条目。
   - 条件：本地没有该任务；或本地状态为 needs_plan、从未有过 plan 请求、且没有 automation 对象。另外要求 save_state 为 active，且不是 auto_advance_paused。
   - 顺序：按远端创建时间升序（字段缺失时按任务号）。
   - 数量：每轮最多接收 3 个，且不超过容量。
7. **标签过滤：**
   - label_filter 为空 → 不过滤。
   - 列表条目自带 labels（由包 E 确认）→ 直接比较。
   - 否则逐个调用 `remote.get_task_labels`：每轮最多 20 次；不匹配的结果缓存 30 分钟，存在 settings `automation_intake_state.label_checked`。
   - 比较方式：大小写不敏感的精确匹配，命中任一标签即可。
8. **接收单个任务：**
   1. `cli.run("prepare-plan-input", {task_id, expected_requirements_sha256: <列表里的指纹>, local_paths: []})`。
   2. `git_delivery.prepare_branch_worktree`。
   3. `cli.run("start-plans")`，条目带内部字段 `automation`：`{authorized_by:"automation", policy_revision, delivery:{...第 3 节返回值, mode:"pr"}}`。核心在同一事务里完成：
      - 写入任务的 automation（`plan_attempts=1`，开启第一段）和 delivery；
      - 建 plan 请求：`requested_by="automation"`，`manual_authorized` 仍为 true，以便既有 claim 过滤放行；
      - 载荷增加 `authorized_by`、`plan_cwd`、`delivery_mode`、`delivery_head_sha`；
      - 记录事件 `automation_adopted`。
9. **plan_wanted 任务的材料准备：**
   - prepare-plan-input 传任务当前的 `requirements_sha256`；
   - 远端需求不符 → 任务进入 needs_user（failure_code=remote_conflict，note「远端需求已变化（新增内容未经筛选）」）。
10. **失败处理：**
    - 分支失败（branch_exists/delivery_base_unknown）→ 照常导入任务行，写入 automation（`plan_wanted=false`），然后转 needs_user 并带该 failure_code。
    - 确定性失败（validation_failed、bad_request、conflict）→ 清掉 plan_wanted，在 `last_skip` 记录原因，然后转 needs_user。
    - 瞬时失败（remote_unavailable、git_transient）→ 下轮重试。
11. **摘要：** 写入 settings `automation_last_intake`，形状为 `{started_at, finished_at, projects:[{project_id, ready, preflight, adopted, skipped:[{task_id, reason}]}], errors}`。
12. **dry-run：** 只做 fetch-todos、标签读取和前置检查，输出将会接收的候选；不调用 prepare-plan-input，不写任何东西。

**提交规划时的规则（由包 D 在 `_submit_one` 实现）：**

- **plan_kind 的确定：** `fix_request.state=="open"` 时为 review_fix（same_task），否则为 standard。same_task 分支放在旧 review_fix 分支（约 737 行）和 manifest plan_kind（445–449）之前。
- **same_task 载荷：** `{plan_kind:"review_fix", fix_mode:"same_task", review_source_task_id:<self>, fix_task_id:<self>, fix_round, review_report_path, review_report_sha256, review_key}`，值取自 fix_request。
- **手动提交的已收养任务：** 用户在规划面板手动提交受自动化管理的任务时，载荷同样带 `plan_cwd`/`delivery_mode`/`delivery_head_sha`，并且不清除 automation 对象。

#### 5.27.7 计划自动确认

主控在 tick 中实现，每次 tick 扫描一遍。

**确认条件（全部满足）：**

- 任务受自动化管理，状态为 ready，且带 `plan_execution_hold`；
- 项目已开启自动处理，`auto_confirm_plan=true`，前置检查通过；
- `plan_verify.plan_sha256 == task.plan_sha256`，且 commands 非空；
- 预算未超：本段时长 < max_task_minutes；本段成本 < max_task_cost_usd，或该上限为 null；
- `db.task_schedulable(task)` 成立；
- 未处于全局暂停，也未暂停 dispatch。

**动作：** `confirm_plan_inline(conn, task, now, expected_plan_sha256=task.plan_sha256, actor="automation", note="auto")`。该函数从 `cmd_enqueue` 24–111 中抽出，行为与 `confirm_plan` 完全相同。

**跳过时：**

- 不阻塞任务，只把 `automation.last_skip` 写为 `{reason, at}`；
- 原因与上次不同时才记一次 `auto_confirm_skipped` 事件；
- reason 取值：disabled、auto_confirm_off、preflight、verify_missing、verify_mismatch、budget、paused；
- 用户仍可手动确认。

needs_input 计划在既有流程中已经进入 needs_user，不在这里处理。

#### 5.27.8 同任务修复轮

**register-review 的 same_task 分支（主控，cmd_exec 815–905）：**

- **适用范围：** 只用于受自动化管理的任务，payload 带 `fix_mode:"same_task"`、`fix:{title, description, priority}`、`review_key` 和既有的 Review 身份字段。对这类任务，旧的 `fix_task_id`/create-review-fix 路径一律返回 conflict。
- **幂等：** `fix_request.review_key` 或 `review_history[-1].review_key` 已等于本次 key → 返回 `{already_registered:true}`。
- **未超上限**（`fix_round < max_fix_rounds`、本段时长和成本未超、项目仍开启）：调用 `core.start_fix_round_inline`，在同一事务内完成：
  1. 本次 Review 归档进 review_history；
  2. `fix_round += 1`；
  3. fix_request 写入并置为 open；
  4. `plan_wanted=true`，`plan_attempts=0`；
  5. 清空列 review_report_path、review_report_sha256、review_key，删除 extra 的 review_conclusion 和 review_registered_at；
  6. **重置发送字段**：`sent=0, send_state=NULL, sent_at=NULL, lock_retained=0, manual_recovery_required=0`；以 db.py 的 tasks 列为准，主控逐列核对；
  7. in_review → needs_plan；
  8. 记录事件 `fix_round_started`。
- **超出上限：** fix_request 写入并置为 held，任务进入 needs_user。failure_code 按触发原因取：
  - 修复轮用完 → fix_rounds_exhausted；
  - 时长或成本超出 → auto_budget_exceeded；
  - 项目已关闭 → automation_stopped。
- **pass 与 blocked：** 自动化任务的 pass 和 blocked 同样追加 review_history；pass 后把 fix_request 置为 null。

**publish-plan 的 same_task 分支（主控，cmd_exec 614–812）：**

- 核验元数据：`fix_mode=="same_task"`、`review_source_task_id==task_id`、`fix_round==automation.fix_round`；review_report_path、review_report_sha256、review_key 三项都与 fix_request 一致。
- 跳过 758–776 的跨任务检查，不执行 795–802 的修复回写，也不写 Review 列。
- 629 行的远端状态检查改用 `core.expected_remote_slugs(task)`。

**远端状态口径：** `core.expected_remote_slugs(task)` 的取值：

- 默认为 `TODO_SLUGS`；
- 受 PR 交付管理且已有提交时，再加上 `REVIEW_SLUGS`（修复轮开始时，远端停在 in-review）。

publish-plan:629 和 dispatch_validate:111 都改用它。begin-send 依靠 claim 时记录的 `original_todo_slug`（即 in-review），无需改动即可把远端从 in-review 迁到 in-progress。

**dispatch_validate 第 5 步（包 C）：** 先判断 `meta.fix_mode=="same_task"`。成立时：

- 报告必须在 `constants.review_reports_root()` 内，sha 与 fix_request 和计划元数据都一致；
- `review_source_task_id==task_id`；
- `fix_round==automation.fix_round`；
- `review_key==fix_request.review_key`；
- 不做 `REVIEW_MARKER_PREFIX` 标记和修复回写检查。

旧的跨任务 review_fix 检查保持不变。

**计划元数据（包 G）：** same_task 计划的 frontmatter 从请求载荷取 review_report_path/sha256、review_key 和 review_source_task_id（取自身），并增加 `fix_mode: same_task` 和 `fix_round: N`。这些字段由 runner 写入，不由模型生成。

**sync-tasks（主控）：** 受自动化管理的任务只更新元数据、kaneo_status 和事件，不做任何状态转移，也不改 requirements_sha256。这样需求变化会由 advance-task 和 dispatch_validate 的既有指纹校验拦下，停给用户处理。

#### 5.27.9 执行验证证据

**计划格式：**

- 计划正文恰好一个 ```verify 围栏块，含 1–5 条非空命令行。
- 允许 `#` 注释行。
- 不允许：行尾续行 `\`、heredoc `<<`；以 `git commit`、`git push`、`git reset`、`git checkout`、`git switch` 开头的命令；以 `gh ` 开头的命令。
- 解析规则：比较前把连续空白压成一个空格并去掉首尾空白；命令不做任何改写。

**解析与存储：**

- 解析函数：`verify_evidence.parse_verify_block(text)`（包 A）。
- 存储：publish-plan 发布时（主控 Step 14 接入）把结果写入 `extra.plan_verify`，形状为 `{plan_sha256, commands}` 或 `{plan_sha256, error}`。

**提取命令结果：** `verify_evidence.extract_command_results(messages)` 只认工具调用事件：`raw.tool.input` 中的 JSON 字段 `command`，以及整数 `exit_code`。消息形状以下面三处为准：

- dsh_events.py:196–208；
- pi_backend.py:142；
- pi_desktop_worker.py:505。

exit_code 缺失或不是整数时记为 None。

**匹配规则：**

- 每条声明的命令，在本执行轮次的结果里找「规范化后完全相等」或「可证明成功的顶层纯 `&&` 链中某段相等」的调用；
- 子段匹配必须识别引号和转义，仅允许顶层纯 AND 链；混合 `||`、`;`、管道、换行、注释、子 shell/替换及会终止或改变当前 shell 控制流的前置命令不作子段成功证据。不能确认时要求独立 exact 工具结果；保留普通 `cd <目录> && <验证命令>` 与普通验证命令纯 AND 链。
- 取最后一次匹配，exit_code 必须为 0；
- 只统计当前执行轮次（task.run_id）的消息。

**判定：**

- 有任一声明命令缺失、最后一次失败或 exit_code=None → verification_unconfirmed。
- 用户选 `accept_verification` → 写入 `verification_override{execution_run_id: task.run_id, actor:"user", at, note}`，任务回到 running，并新建 monitor 请求；monitor 仍要核验终态和退出码。
- 手动任务同样适用此规则；计划里没有 verify 块的旧任务完成后，由用户「接受验证结果」。
- 不做任何猜测：不从最终消息文字中推断「已验证」。Plus 工具事件如果不带 exit_code，停下汇报（见 Step 4 停止条件和 Step 15-F）。

#### 5.27.10 交付：提交、推送、PR 与合并

**delivery-commit（包 C，`cmd_delivery.py`）：**

- **调用：** `{task_id, request_id, run_id, expected_execution_run_id}`，由 monitor 在完成判定之后调用。
- **身份：** 用 `core.require_stage_identity(stages=("monitor",))`，并要求 expected_execution_run_id 等于 task.run_id。
- **三段式：**
  1. 第一段（事务）：核对身份和任务状态 running。
  2. 第二段（无事务）：执行 git 操作。
  3. 第三段（事务）：复核身份并确认 head 未变，然后追加 commits、更新 head_sha，记录事件 `delivery_committed`。
- **幂等：** commits 中已有该 execution_run_id → 返回 `{outcome:"already_committed", sha}`。
- **崩溃恢复：** HEAD ≠ head_sha，但 HEAD 的父提交等于 head_sha，且提交信息带有本次 run 的 `KaneoPilot-Run` trailer → 视为已提交，直接补记。其他 HEAD 漂移 → blocked，failure_code 为 delivery_commit_failed，note「分支被执行端或他人改动」。
- **候选文件：** 来自 `git status --porcelain=v1 -z --untracked-files=all`，需正确处理 rename 的双路径。被 .gitignore 忽略的文件本来就不进入候选。
- **拦截（delivery_blocked_files）：** 候选中出现以下任一 → 不提交，列出文件名：
  - `.env` 或 `.env.*`（`.env.example` 除外）；
  - `*.pem`、`*.key`、`*.p12`、`id_rsa*`；
  - 单文件 >5MB。
- **无改动：** 候选为空 → blocked，failure_code 为 no_changes。
- **提交：**
  - `git add -A`，然后 `git commit -F <临时消息文件>`；
  - 照常执行钩子，超时 300 秒，`GIT_TERMINAL_PROMPT=0`；
  - 不设置提交身份，不加 --no-verify，不加 --no-gpg-sign；
  - 失败 → blocked（delivery_commit_failed），note 附 stderr 末尾 ≤2KB（先脱敏）。
- **提交信息：**

```
kaneo(<task_id>): <标题，截断到 72 字符>

Kaneo-Task: <task_id>
KaneoPilot-Run: <execution_run_id>
Fix-Round: <fix_round>
```

恢复已有本轮提交及新提交后，均按实际提交差异复核敏感文件与文件大小限制；本轮 trailer、单父提交或工作树干净不能替代内容检查，提交钩子新增的文件也不得绕过该门。

- **返回：** `{outcome:"committed"|"already_committed"|"blocked", sha?, files?, failure_code?, message?}`。outcome=blocked 时，核心已在第三段把任务转为 needs_user 并写好 failure_code。只有协议级错误才 raise：bad_request、not_found、stale_run、validation_failed、conflict、internal。`ERROR_CODES` 不增加。

**交付请求的补建（主控，tick）：** 以下条件全部满足时，建 `delivery:<task_id>` 请求（requested_by=scheduler）：

- 任务状态为 in_review，`review_conclusion=="pass"`，且 review_history 最后一条的 execution_run_id 等于 task.run_id；
- 任务受 PR 交付管理，且未转人工；
- `merged_at` 为空；
- 没有活动的 delivery 请求；
- `now ≥ next_sync_at`，或 next_sync_at 为空；
- `db.task_schedulable(task)` 成立。

claim 守卫为 `{in_review}`；该阶段可被暂停。

**delivery-publish（包 F，`cmd_delivery_pr.py`）：** 与 advance-task 一样走三段式，网络操作一律不在事务内。

已登记 PR 时，发布和同步在推送前即核对 PR URL/编号所属仓库与当前 origin 一致，不能先向改变后的仓库推送再发现身份失配。发布和同步入口必须核对当前执行轮次的 Review pass 与评审提交快照，不能靠 tick 排队条件代替核心硬闸；无本轮 pass 或仅有旧周期 pass 时，不推送、不创建 PR、不发评论。每个远端副作用前与最终落库事务，重新按当前时间核验 claim、租约及执行周期；慢读/推送期间租约过期或被接管后停止后续副作用，不使用命令启动时的固定时间放行。网络操作不持有 SQLite 写事务。

1. `git_delivery.push_branch`：推送前核验 `git remote get-url --push --all origin` 的真实目标，只允许单一且与交付 PR 仓库一致的 GitHub 地址；fetch 地址一致不能替代 push 地址检查。对会继续改写目标的不稳定 URL rewrite 链明确阻断。使用核验过的固定目标和 `<head_sha>:refs/heads/<branch>` 非强制推送（源 SHA 为已通过本轮 Review 的提交快照），保留已有认证、钩子和签名设置。 SSH 别名同前置检查按当前仓库和本机配置重新确认；不将推送地址改写为 github.com，也不绕过单一推送目标、仓库一致性及 URL rewrite 守卫。
   - 被拒（rejected/non-fast-forward/fetch first）→ blocked，failure_code=branch_diverged；
   - 其他失败 → `publish_failures += 1`，返回 retry。
2. 查找已有 PR：`gh pr list --repo <owner/name> --head <branch> --state all --json number,url,state,headRefOid,mergedAt,baseRefName,headRefName,isCrossRepository`。
   - 仅复用源仓库为当前 origin（非 fork）、head 分支/目标 base 分支均与交付快照相同且 headRefOid 等于本次 pushed_sha 的 OPEN/MERGED PR；已登记 PR 的编号也必须一致。身份/提交不匹配则 branch_diverged，不写评论或登记合并；缺少身份字段不能作为可信交付。
   - 只有 CLOSED 的 PR → blocked，failure_code=pr_closed。
3. 没有 PR → `gh pr create --repo <owner/name> --base <base_branch> --head <branch> --title "[Kaneo] <标题，≤100 字符>" --body-file <临时文件>`，再用 `gh pr view` 取 number 和 url。
   - 正文包括：任务标题、task_id、修复轮数、提交列表（sha12 + 标题）、verify 命令、「由 KaneoPilot 自动创建」；
   - 正文不含绝对路径、主机名或凭据。
4. Kaneo 评论：先用 `find_marker_comment(task, "[kaneopilot:delivery:<task_id>]")` 查找，不存在再 `post_comment`，内容为 `[kaneopilot:delivery:<task_id>] 已创建 PR：<url>`。
5. 第三段（事务）：写入 pushed_sha、pr_number、pr_url、pr_state、kaneo_comment_at，`publish_failures` 清零，`next_sync_at` 设为 now+5 分钟；记录事件 `delivery_pushed`、`pr_opened`、`kaneo_comment_posted`。

连续 3 次 retry → blocked，failure_code=gh_unavailable。gh 的环境变量为 `GH_PROMPT_DISABLED=1`、`GH_NO_UPDATE_NOTIFIER=1`、`NO_COLOR=1`、`GH_PAGER=`，每次调用超时 60 秒。

**delivery-sync（包 F）：**

1. 先读 Kaneo 任务（只读，无事务）：
   - 远端已 done → 返回 `{outcome:"kaneo_done"}`；
   - 否则执行 `gh pr view <number> --repo <owner/name> --json number,url,state,mergedAt,headRefOid,baseRefName,headRefName,isCrossRepository`。
2. 先核对 PR URL 所属 origin 仓库、编号、非 fork、head/base 分支以及 headRefOid=当前 pushed_sha=head_sha；不一致立即 branch_diverged，不登记合并、不推进 done。字段缺失不可作为可信证据。
3. 按 PR 状态处理：
   - MERGED → 第三段写入 merged_at 和 pr_state，记事件 `pr_merged`，返回 merged；
   - OPEN → 写入 last_sync_at 和 next_sync_at，返回 waiting。next_sync_at 的取法：距 review_registered_at 不到 1 小时为 +5 分钟，否则为 +30 分钟；
   - CLOSED → blocked，failure_code=pr_closed，记事件 `pr_closed`。

**delivery_runner（包 F）：**

1. 领取 delivery 请求。
2. 没有 PR，或 `pushed_sha≠head_sha` → 先调用 delivery-publish。
3. 调用 delivery-sync。
4. 按 sync 结果收尾：
   - merged → 调用 `advance-task {task_id, request_id, run_id, expected_execution_run_id, target:"done", expected_current:"in-review"}`；
   - kaneo_done → 同样调用，但 `expected_current:"done"`（被动）。
5. report：
   - 成功 → succeeded；
   - retry 或 waiting → succeeded，并在 progress 中说明；
   - blocked → failed，核心已转入待处理；
   - advance-task 返回 remote_conflict 或 remote_unavailable → failed。

**advance-task 的交付身份（主控 Step 3）：**

每次只读远端任务返回后、更新远端状态前，重新核验当前时间下的 claim、租约及任务执行周期；读取期间租约过期或被接管时不得继续远端写。最终本地事务仍再次核验，网络调用不持有 SQLite 写锁。

- 带 request_id 时用 `require_stage_identity(stages=("review","delivery"))`；
- 受 PR 交付管理的任务，非被动的 `target=done` 要求 `delivery.merged_at` 已记录，否则 conflict；
- 被动对 `(done,done)` 不受此限。

#### 5.27.11 Claude/Codex 后端与统一 Review 结算

**Claude CLI（包 G，`claude_cli.py`，规划与 Review 共用）：**

```
claude -p --output-format stream-json --verbose --restricted --tools Read,Grep,Glob
  --strict-mcp-config --permission-mode dontAsk --no-session-persistence
  --json-schema '<内联 JSON>' --model <M> --effort <E> --max-budget-usd <X> [--add-dir <输入目录>]
```

- **调用方式：**
  - prompt 从 stdin 传入，cwd 为 plan_cwd 或 Review 快照；
  - 绝不使用 `--bare`；
  - 施工前用 `claude --help` 逐个确认参数存在。缺参数就停下汇报，不要改用其他参数凑。
- **事件解析：**
  - `--tools` 仍仅允许 Read、Grep、Glob；init 事件的 tools 通常为其子集。传入 `--json-schema` 时，允许 CLI 额外暴露宿主内建 `StructuredOutput`（Claude 2.1.251 的真实形状），只用于生成结构化答复；无 schema 时拒绝该工具，Bash/Edit/Write/MCP 等始终拒绝。其他工具出现即终止进程并失败（「Claude 工具集不符合只读要求」）；
  - result 事件读取 subtype、is_error、structured_output、total_cost_usd、session_id。
- **费用：**
  - 每次运行都调用 `record-cost`（source=claude）；
  - `--max-budget-usd` 取 min(5, 本段剩余预算)，不低于 0.5；上限为 null 时取 5。
- **路径：** 二进制由 `constants.claude_bin()` 解析，可被 `KANPILOT_CLAUDE_BIN` 覆盖。
- **受限认证继承：** Claude 2.1.251 的 `--restricted` 会忽略用户设置；普通 CLI 已认证不代表受限调用已认证。仅从用户全局配置目录（`CLAUDE_CONFIG_DIR`，缺省/空白为 `~/.claude`，非空值展开后必须为绝对路径）的 `settings.json.env` 向子进程内存继承认证、服务地址与模型别名白名单；显式进程环境优先。白名单为 `ANTHROPIC_API_KEY`、`ANTHROPIC_AUTH_TOKEN`、`CLAUDE_CODE_OAUTH_TOKEN`、`ANTHROPIC_BASE_URL`、`ANTHROPIC_MODEL`、`ANTHROPIC_DEFAULT_HAIKU_MODEL`、`ANTHROPIC_DEFAULT_SONNET_MODEL`、`ANTHROPIC_DEFAULT_OPUS_MODEL`、`ANTHROPIC_SMALL_FAST_MODEL` 及对应 `*_MODEL_NAME`。不载入仓库配置、hooks、工具、MCP、agent teams 或任意其他 env，不修改全局配置，不把凭据写入 argv、临时文件、DB 或日志。配置不可解析时明确失败，不静默改用另一个服务；未设置认证仍按 CLI 实际错误报告。

**Claude 规划（包 G）：**

- planner_config 新增 `claude {model, effort}` 分组配置。`planner_config.BACKENDS` 直接引用 `constants.PLANNER_BACKENDS`，claude 已由主控在 Step 2 加入，G 不改 constants。
- `model_snapshot(conn, group, backend="claude")` 返回 publish-plan 期望的 `{planner_model, reasoning_effort}` 形状。
- planner_runner 529–541 新增 claude 分支，其输出走既有的 `parse_output` 和 publish 流程。
- Codex 的 `launcher_argv`（codex_planner 297）中，`cwd` 改为 `job.get("plan_cwd") or job.get("repository") or "."`。这个值就是 `-C` 工作根；只读沙箱 `-s read-only` 保持不变。
- Plus 规划在自动化任务上使用 `plan-<sha12>` 快照（第 3 节）。
- planner_prompt.md 和 pi_desktop_planner 的内联 prompt 改动：
  - 要求恰好一个 verify 块；
  - 不再指示「新建 worktree/分支/提交」；
  - 补充 same_task 修复指引：工作树已含前几轮提交，只修 Review 报告列出的问题，verify 要覆盖修复点。

**统一 Review 结算（包 H，`review_settle.py`）：**

结果 schema（所有键必填，additionalProperties=false）：

```json
{"conclusion": "pass|changes_requested|blocked", "report_markdown": "...",
 "fix": null 或 {"title": "...", "description": "...", "priority": "REVIEW_FIX_PRIORITIES 之一"}}
```

- **字段规则：** fix 在 changes_requested 时必须非空，其他结论必须为 null。fix 的可空写法先用 `"type":["object","null"]`；两种 CLI 的实际兼容性在 Step 15-F 核对，不兼容时停下汇报，不要把 fix 改成可选字段。
- **结算流程（`settle`）：**
  1. 把报告写到 `report_path(task_id, run_id)`，即沿用的 `<APP_SUPPORT>/reports/<task_id>/<run_id>.md`，并计算 sha256。
  2. 带 Review 身份（request_id、run_id、expected_execution_run_id）调用 register-review。
  3. 按任务类型分支：
     - 非自动化任务：保持原行为——pass → advance-task done；changes_requested → 先 create-review-fix，再 register；blocked → register 后 report needs_user。
     - 自动化任务：pass 只登记，不推进；changes_requested 用 same_task 方式登记；blocked 照常登记。
  4. 最后 report。
- **防重复：** `_registered_matches` 负责崩溃后重跑时避免重复登记：fix_request、review_history 或 Review 列中任一处已有同一 review_key，即视为已登记。
- **兼容：** pi_desktop_review 原有对外函数在 review_settle 中保留 re-export，旧测试照常通过。

**Claude/Codex Review（包 H，`cli_review.py`）：**

- 入口与 `run_stage` 同签名。
- 复用 claude_cli 和 codex_planner 的 `launcher_argv`/`child_env`/`map_event`；Codex 使用只读沙箱，cwd 为 Review 快照。
- 两者共用新 prompt `review_cli.md`。
- Codex 成本记为 null（source=codex），token 数只写日志。

**交付任务的 Review 材料：**

- `git diff --no-color <base_sha> <head_sha>`，以及 `git log --oneline base..head`；
- 有上一轮 Review 时，加上 `<review_history[-1].head_sha>..<head_sha>` 的增量 diff 和上一轮报告。

diff 超过 400KB 时只附 `--stat`，并注明「请在只读工作树中查看具体文件」。执行工作树 HEAD 与 head_sha 不符或不干净 → report needs_user（review_blocked，note「执行工作树与已提交版本不一致」），绝不审查未提交的内容。

**review_config（包 H）：** 新增 `claude {model, effort}`、`codex {model, reasoning_effort}`，并更新 snapshot 和 capabilities。

**Review 领取守卫（主控）：** claim(review) 领到受自动化管理的任务、但 Review 后端是 pi 时：该请求置为 failed，任务转 needs_user（review_backend_unsupported），然后继续看下一个候选。

**成本记录（包 D 实现 `record-cost`，主控注册命令）：**

- payload：`{task_id, request_id, run_id, stage, cost_usd|null, source}`。
- 以 (request_id, run_id) 去重；cost_usd 为 null 时 `cost_unknown_steps += 1`；记录事件 `cost_recorded`。
- 上限只在决策点检查：intake、自动确认、修复轮开始。pass 之后不再检查上限，也不中途打断正在运行的轮次。

#### 5.27.12 常驻调度与菜单栏

**AppRuntime（包 I）：** 单例，App 启动时（AppDelegate `applicationDidFinishLaunching`）创建，持有 `ExecutorScheduler` 和 `MCPServiceManager.shared`。

- 启动时依次执行：
  1. 恢复 MCP 服务，沿用 MCPServiceManager 现有的检查与启动入口；
  2. `LocalNetworkGate.probeAtLaunch()`（原先挂在窗口 `.task` 上）；
  3. 启动调度循环。
- 退出时沿用 `stopOwnedProcessForAppExit()`。

**调度循环（ExecutorScheduler 重写）：** 每 20 秒执行一轮，系统唤醒通知到达时立即执行一轮：

1. 调用核心 `tick`。
2. 调用 `executor-status`。
3. 交给纯函数 `SchedulerDecision.decide` 计算需要拉起的进程：
   - 对 plan/dispatch/monitor/review/delivery 中的每个阶段：未暂停、无存活进程、`claimable>0` → 拉起该阶段的进程；
   - intake：有已开启的项目、未处于全局暂停或 plan 暂停、无存活进程，且距上次 ≥300 秒或被强制触发 → 拉起。
4. 按决策拉起进程：
   - plan 用 planner_runner；
   - 其余阶段用 `executor_runner --stage <s>`；
   - intake 用 `python3 -m kanpilot.auto_intake`。

   子进程的环境变量和路径解析沿用现有实现（CoreLocator/CoreClient）。

**其他约束：**

- 有存活子进程时持有 `ProcessInfo.beginActivity(.idleSystemSleepDisabled)`，全部退出后释放。
- 保留 `runNow`、`LocalNetworkGate`、开发开关 `executorSchedulingEnabled`；为 SettingsView 保留静态的 `stages` 和 `nextFireDate(for:)` 兼容接口，直到包 J 更新设置页。
- 暂停和恢复只调用核心 `set-paused {stage:null, paused}`，不在 Swift 里另存状态。

**菜单栏（MenuBarView）：** MenuBarExtra 包含：

- 状态行：运行中/已暂停、在途任务数、待处理数、上次 intake 时间；
- 「打开 KaneoPilot」「暂停/恢复」「立即检查」「退出」。

关闭主窗口后，激活策略切到 `.accessory`；重新打开窗口时切回 `.regular`。

**登录项（LoginItem）：** 基于 `SMAppService.mainApp` 实现 register/unregister/status。状态为 `.requiresApproval` 时，界面提示用户去「系统设置 → 登录项」批准。

#### 5.27.13 待处理原因、处置与事件

| failure_code | 来源 | 自动化任务可用动作 |
|---|---|---|
| verification_unconfirmed | monitor | accept_verification（→ running + monitor）、replan、mark_done、dismiss |
| execution_failed | monitor | replan、mark_done、dismiss |
| fix_rounds_exhausted | register-review | resume（用户授权再修一轮：fix_round+1，fix_request 置为 open，plan_wanted，开新段）、replan、mark_done、dismiss |
| auto_budget_exceeded | register-review | resume（同上，在新预算段内进行）、replan、mark_done、dismiss |
| automation_stopped | register-review（项目已关闭） | resume（需先重新开启项目，同上）、replan、mark_done、dismiss |
| review_blocked | review | resume（→ in_review + 新 review 请求）、replan、mark_done、dismiss |
| review_backend_unsupported | claim(review) | resume（需 Review 后端已改为 Plus/Claude/Codex，→ in_review + review）、mark_done、dismiss |
| branch_exists、delivery_base_unknown | intake | resume（→ needs_plan + plan_wanted，重新准备分支）、dismiss |
| delivery_blocked_files、delivery_commit_failed | delivery-commit | resume（→ running + monitor，重新走完成判定与提交）、replan、mark_done、dismiss |
| no_changes | delivery-commit | replan、mark_done、dismiss |
| branch_diverged、gh_unavailable、pr_closed | delivery | resume（→ in_review + delivery，next_sync_at=now）、mark_done、dismiss |
| remote_conflict | advance-task、intake、begin-send | resume（接受远端当前需求和状态，回到被阻塞的步骤；派发中途不可用）、replan、mark_done、dismiss |
| planner_failed（自动重试一次后）、needs_input | plan | replan、dismiss；也可以在规划面板补充材料后手动提交 |
| dispatch_failed | dispatch | 沿用 retry/restore/accept_duplicate/mark_sent；另有 replan、dismiss |

**处置规则：**

- **resume / accept_verification：** 新增的 resolve 动作，只对上表列出的原因可用。核心校验 failure_code 后，按表中目标转移。
- **remote_conflict 的 resume：** 走三段式。
  1. 读取远端任务（不在事务内）。
  2. 远端状态必须是下一步所需的值：monitor 需要 in-progress、in-review 或 done；delivery 需要 in-review 或 done；plan 不限。不符 → conflict，提示「请先在 Kaneo 调整到 X 再继续」。
  3. 在事务内把 requirements_sha256 和 kaneo_status 更新为远端当前值，记录事件 `requirements_accepted`。
  4. 回到上一个失败请求对应的步骤：monitor → running + monitor；delivery → in_review + delivery；review → in_review + review；plan → needs_plan + plan_wanted；dispatch → 拒绝，改用既有的发送恢复动作。
- **replan：** 对自动化任务改为 needs_plan + plan_wanted，`plan_attempts=0`，开新段。如果远端需求有变化，用与上面相同的三段式先接受当前需求；用户点「重新规划」即视为已看过新内容。
- **dismiss：** 对自动化任务显示为「转为人工处理」：在原 dismiss 语义（needs_user → ready）之外，置 `manual_takeover=true`、`delivery.mode="manual"`，记录事件 `automation_stopped`。此后 intake、自动确认、交付补建、same_task 登记全部跳过这个任务，它按普通手动任务处理，但继续使用原分支工作树。
- **release_lost：** 释放丢失的自动化 plan 请求时，若 `plan_attempts<2` 则置 plan_wanted。
- **tick 自动释放：** 自动化任务的 monitor/delivery 丢失请求，由 tick 按 release_lost 的语义自动释放（actor=scheduler，事件 `lost_request_released`）。dispatch、plan、review 的丢失请求仍留给用户处理。
- **plan 失败：** 自动化任务第一次失败时置 plan_wanted，让 intake 自动重试一次；第二次失败转 needs_user，保留 failure_code=planner_failed。

**新事件：** automation_adopted、auto_confirm_skipped、fix_round_started、delivery_committed、delivery_pushed、pr_opened、kaneo_comment_posted、pr_merged、pr_closed、cost_recorded、requirements_accepted、automation_stopped、lost_request_released。

`BOARD_FAILURE_CODES` 不增加，新原因通过 failure_stage 归类。归类如下：

| failure_stage | failure_code |
|---|---|
| monitor | verification_unconfirmed |
| plan | branch_exists、delivery_base_unknown |
| review | fix_rounds_exhausted、auto_budget_exceeded、automation_stopped、review_backend_unsupported |
| delivery | delivery_*、no_changes、branch_diverged、gh_unavailable、pr_closed |



§5.27.1 补充（施工时核对现有 confirm 身份保护）：pi_desktop 在 dispatch-confirm 前，通过 get-session 读取同任务当前执行轮次 round_detail.turn_id 并传 evidence.turn_id。缺失不得猜测，以 unknown 收尾；不降低底层 turn 身份校验。

§5.27 恢复补充：Review resume 在保留 review_history 后清本周期报告列与 conclusion，重新建 Review；交付冲突保留 delivery 阶段。merged_at 已记录但本地仍 in_review 时继续补建 delivery 请求以恢复未完成的 done 推进，不重复合并。规划快照中固定材料目录的复用规则由快照核验实现明确，不放宽执行或 Review 工作树 clean 守卫。

§5.27.1 派发启动身份补充：get-session.round_detail.startup_turn_id 来自同轮首个 session 事件，dispatch-confirm 使用这个不可变启动证明。Goal 执行开始后 round_detail.turn_id 可变为执行轮次，不能用于启动确认。缺字段的旧核心才兼容 turn_id，字段存在但空必须阻塞。


§5.27 用户重新规划恢复补充：自动化任务 send_state=unknown 时 replan 必须返回 conflict，保留发送证据，先使用既有发送恢复裁决。已确认发送且已有轮次的任务，重新规划前经 get-session 核验旧执行终态；未结束或不能确认终态时不得自动开启新轮次。用户明确重新规划后，automation 登记 replan_requested_at、replan_remote_status、replan_worktree_sha256；仅本次恢复周期允许已接受的 in-progress/in-review 状态。首次接收仍限 To Do，自动修复轮清除该授权标记。

失败轮次有未提交改动时，准备规划材料保存该工作树状态指纹和改动文件材料；Plus 的基线快照配合材料中的当前改动规划，不能把旧提交冒充当前内容。派发前对原分支 HEAD、归属和状态指纹逐次核验；只有与已确认规划材料完全一致的用户重规划允许保留未提交改动，否则阻塞。验证通过后才提交本轮全部改动。普通自动轮次保持干净工作树守卫。

### 5.27 补充：App 实例与独立 worker 生命周期

实际派发 runner 会先于独立 execution worker 退出，仅按存活 runner 的父子树清理无法覆盖已重父进程的 worker。为满足退出无孤儿与防休眠契约，App 每次启动生成独立 UUID，通过子进程环境 `KANPILOT_EXECUTOR_INSTANCE_ID` 传给 runner/worker；该值不是凭据，不进入模型指令。

- `executor-instance {instance_id, action:"start"|"stop"|"status"|"register-worker", task_id?, run_id?, spec_path?}`：UUID 严格校验。start 幂等登记 active；stop 原子登记 stopped，返回此实例仍存活的进程身份，不发送 Plus stop/abort，不改变任务/请求/轮次状态。已 stopped 的实例不能重新 start，重启 App 使用新 UUID。status 只读；register-worker 要求 task/run/spec 字段，由核心自行取得当前调用进程真实 PID 和启动时间，原子核验实例 active 后登记，重复调用幂等。
- `executor-status {instance_id?}` 保持原字段，带 UUID 时增加 `owned_workers`，形状为 `[{pid, started_seconds, started_microseconds, task_id, run_id, spec_path}]`；只返回该实例登记且启动时间仍匹配的进程。旧调用缺字段等同旧协议。
- SQLite settings `executor_instances` 保存实例和 worker 归属，不升 schema；已停止且无存活 worker 的历史实例可在新登记时清除，任务、会话、分支和工作树不因此删除。
- 独立 worker 与恢复 observer 在任何发送/审批操作前经固定 JSON 命令（同进程 `cli.run`）登记自身真实 PID、进程启动秒/微秒、task/run/spec 身份，不自行写 SQLite；实例不存在或 stopped 时不得继续工作。无该环境值的旧手动 CLI 沿用原流程。
- App 退出先关闭实例登记，再停止自身 runner 子树，最后读取本实例 worker 身份并作有界 SIGTERM→SIGKILL。每次信号前重新核对 PID 的秒/微秒启动时间，不能扫描并终止所有 round PID、其他 App 实例或 Plus 进程。
- App 防休眠覆盖存活 runner 与本实例独立 worker；stage 退出后仍读取 worker 登记，不能随 runner 提前释放 activity。实例关闭期间禁止新 worker 注册，避免派发与退出竞态。

恢复 observer 使用单独 `observer-spec.json` 固化新的 App 归属；显式手动 CLI 可在完成原身份核验后使用无 App 归属的 observer 副本，原启动 spec 和发送证据保留。已有 owner 证据的 observer 存活判断须核对登记的 PID 启动时间；登记已清除不能仅凭 PID 被其他进程复用就拒绝恢复。无 owner 证据的历史手动轮次保持原兼容语义。

observer 续接始终只读，不发送 prompt、不批准 Goal。若停在待批准契约，保留非终态现场并提示在 Plus 中处理。若旧 worker 在远端批准成功、写入执行 turn 前退出，不能把已 completed 的规划 turn 当作执行终态；只有原 session/planning turn 对应的已批准 Goal 历史、核验通过的不同执行 turn，以及完整会话仅含本 task/run 的原始 user 锚点时，才能只读接续该执行 turn。证据缺失则 defer，不伪造完成或失败。

### 本机残留会话移除与排队规划终止（2026-10-07）

- `force-purge-session {task_id, run_id, expected_save_state?}`：确认没有实际会话记录且没有关联排队/领取或运行活动时，允许残留轮次从 active/trashed 直接进入 purged。已有 session 身份、对话或工具记录、解析失败、源不可访问时拒绝，不能把读取失败当作记录不存在。仅启动/失败诊断不是实际会话记录。写事务中再次核验，保存状态冲突为 conflict；purged 重复调用幂等成功。保留任务、文件和历史，不停止进程。SessionInfo.allowed_actions 用 `force-purge-session` 表达可用性。
- `cancel-request {request_id, task_id}`：仅终止 queued 的 plan 请求，同一写事务与 claim 竞争；已领取为 conflict，不停止运行中的规划。保留材料、请求和尝试历史，记录用户终止事件；由本命令取消的请求重复调用幂等成功。需要规划的任务进入人工处理，自动规划意图清除，防止立即自动重排；其他生命周期不因历史请求取消而回退。重新规划由用户手动发起。
- 请求详情 allowed_actions、任务详情 allowed_actions 增加 `cancel-request`；任务摘要/详情增加可选 `queued_plan_request_id`，指向该任务的排队规划请求，取消成功后为空。终态请求只读。

### Plus 新规划自动权限（2026-10-07）

用户已确认 Plus 规划采用 `auto`。`get-planner-config` 生效配置与新建请求的 `runtime_config.permission_mode` 均为 `auto`；旧 settings.planner_config 中的 `ask` 对之后创建的请求按新规则解释，无需重新保存模型配置。既有 queued/claimed 请求、执行轮次及已创建会话不改写；重新领取或手动开始旧请求仍使用其固化 `ask`。runner 和 MCP runtime 域只能按当前请求的快照配置会话，不得把旧 `ask` 升为 `auto`。新规划仍在独立规划工作树中使用 `mode=plan`，计划导入、proposal reject、用户确认执行闸保持原契约。Review 使用独立权限规则；自 2026-10-08 用户确认起，新 Review 采用 `auto`，历史轮次保留其原权限快照。

### 已结束请求的旧规划会话清理（2026-10-07）

历史规划请求已经终止、其规划执行端也已退出时，旧轮次的 `run_status=running` 或 job.status=running 不能单独阻止会话移入回收站。核心须核验该轮次与终态 plan 请求的精确绑定、无该轮次 queued/claimed 请求、可信的规划 job 身份以及所有关联执行/观察进程已退出；不能仅凭租约过期、任务不可见或 PID 未提供判断。条件满足后，已有实际会话记录同样可以经既有 `trash-session` 移入回收站、恢复或从回收站永久移出列表，文件与请求历史保留。列表能力与写命令使用相同判定，写事务再次核验。同任务其他规划轮次的活动不阻止旧轮次清理，也不因旧会话清理而改变状态；当前活动轮次继续拒绝删除。

### Plus 转录超限标记传递（2026-10-07）

`kp_runtime_messages` 的结果增加可选 `truncated: boolean`、`reason: string`。Plus `pi_session_get` 返回截断结果时，适配层保留 `session_id`、`messages: []` 和显式 `truncated: true` 及脱敏原因，不传递不可解析的 `preview`，不把超限结果转换为完整空会话。RuntimeClient 和历史只读客户端须在组装正常 session 前透传该顶层标记。采集器沿用既有同游标缩页（40→20→10→5），单页仍超限或分页链断裂时继续拒绝发布/完成；不放宽完整转录、消息身份或分页边界校验。读取修复不重新发送规划指令、不新建会话、不变更已失败请求的状态。


### 手动重新接入已移出的任务（2026-10-08）

- `fetch-todos` 保持只读；对归档、回收站和永久移除的远端 To Do 增加 `reconnectable: bool`。仅仓库映射可用且无活动请求、未解除失联或发送不明等安全阻塞时为 true，仍 `selectable=false`，不会自动复活。
- `reconnect-task {task_id, expected_save_state}` 为用户显式命令；期望状态必填且仅 archived/trashed/purged。远端只读核验任务仍为 To Do、项目身份及当前映射可用；不可达或变化拒绝，不写远端。事务内复核当前保存状态与期望一致、无 queued/claimed 请求、无未解除失联、无发送不明及进行中执行状态；曾发送执行的任务还必须核验旧轮次明确终态（run_status finished/failed，terminal.outcome completed/failed/error/aborted/timeout/truncated），缺失或不明则拒绝，不允许清执行指针绕过旧执行结束检查。列表仅查已存证据，命令可事务外核验会话，但写事务必须复核任务和轮次快照；冲突不更改任务。
- 成功后仅任务重新进入 active，状态为 needs_plan、阶段为 plan，清除当前执行/失败指针，暂停自动推进。保留旧计划、来源、历史、文件、请求和轮次；已移除轮次不会随任务复活。旧 delivery 工作树/分支信息和旧计划/来源登记写入 `task_reconnected` 事件供查阅，清除当前计划指针及 delivery 关联，旧计划不能在新规划失败或取消后重新派发；新规划以当前配置仓库为基线，不自动复用旧工作树；旧工作树文件不删除。重复提交/状态变化报 conflict，不重置已重新接入的活跃任务。
- 接入本身不排队、不运行模型。后续仍经 prepare-plan-input / start-plans 确认材料授权新规划；成功提交用户新规划时才解除该任务的领取暂停，保留人工接管与新计划执行确认门控，提交失败不解除；成功提交时将本轮确认的来源快照和需求指纹登记为当前任务基线，旧文件和审计记录不改；新计划发布后成为当前登记，旧文件及历史仍保留。普通 restore-task 和后台同步继续拒绝复活 purged。


### 状态页轻量读取（2026-10-08）

`get-session` 增加可选 `status_only: bool`（默认 false，旧读取协议不变）。状态页传 true，不请求 session-messages。该模式保留身份、路径、安全动作、超时及终态/失败审计判定，但连续未变化的源只核验小型状态与文件签名，复用不含正文的状态摘要，不每两秒扫描或转换完整会话正文。来源变化或不可用不能将旧缓存当作当前正常状态；缓存不复活已移除轮次，动作依据本次SQLite真实状态重算。界面“最近检查”采用本次成功状态读取时间，失败保留上次时间并标为可能过期；不篡改原 session_reads 位点语义。


### Plus agent/status 解析修正（2026-10-08）

适配器统一解析当前真实 `{"status":{"sessionId":...,"isRunning":bool,"currentTurnId":...,"pendingToolConfirmations":int|list}}` 与旧扁平形状，运行布尔必须存在且类型真实，响应身份不可与请求不符；pendingPermission 或非空待确认计数/列表按各自真实信号显示等待权限。只读查询不发送指令，不因idle推断任务成功。执行job未包含planning request_id时，按本地确切task+run请求/轮次确定类型；执行会话不提供规划重新开始，并给正确原因，旧规划仍依原守卫拒绝。


### 清除评审阻塞保持阶段与事故恢复（2026-10-08）

- `resolve dismiss` 清除 review 阶段的阻塞时恢复 `in_review/review`，保留当前执行 run_id、sent/send_state、会话、计划与执行文件。人工接管语义不变，自动推进暂停（auto_advance_paused=1），不创建任何请求，不自动调用模型。不得固定退回ready/dispatch。
- 历史误退的ready任务可经 `resolve {action:"recover_review", expected_execution_run_id, expected_failed_review_request_id}` 恢复。两期望ID必填。仅活跃、当前ready、最近resolution为dismiss、无活动请求/新评审结论、send_state=sent且sent=1的任务可恢复；指定失败Review须为最近该任务Review，审计/本机Review记录绑定当前执行轮次及原review_blocked。必须核验本机当前执行明确completed/finished，计划/执行身份与远端项目、需求指纹一致、远端已in-review。事务中复核身份及状态/请求/轮次；不更改远端，不重新执行。
- 恢复只将任务置in_review/review并暂停自动推进，不排队/运行Review。历史Review失败与执行成果保留，记录来源请求与执行身份；相同恢复身份重复调用仅幂等返回，不重复改变状态或创建请求。无可靠证据必须拒绝，不用stage猜测绕过。
- get-task 对合法历史事故返回可选 `review_recovery:{execution_run_id,failed_review_request_id}` 与 allowed_actions 中recover_review，供界面传确切ID。实际文件/远端核验留在命令，列表可用性只读核验既有本地审计，不启动模型。
- dispatch enqueue/计划确认对已sent任务拒绝，防止已完成执行的旧计划被重新发送。显式新规划按原协议重置发送状态后方可开始新周期。
- 用户显式恢复自动推进才允许后续Review调度：`restore-task {resume_auto:true}` 对active且暂停的任务清除任务暂停并记录事件；active普通恢复仍幂等，不默认解除暂停。


评审已有blocked报告时，阶段恢复仍保留当前登记及文件；仅用户明确开始或重试新评审时，沿原resume-review逻辑清本周期report_path/sha/key及结论，旧身份转存审计、旧报告文件与登记事件保留。避免旧blocked报告导致新Review被conclusion_pending/existing_review_report拒绝；不会在清除阻塞或事故阶段恢复时擅自重跑。


### Review 自动工具权限（2026-10-08）

用户确认新 Review 使用 `mode=plan` 与 `permission_mode=auto`。已保存旧设置对之后创建的 Review 按新规则生效；已有请求、轮次和会话保留其固化权限。运行时按请求快照配置权限，兼容历史 `ask`，不得将历史轮次自动升级。Review 仍执行审查并输出结论，不批准目标契约或重新启动任务执行。


### 待处理失败记录移除（2026-10-08）

待处理的最近失败增加单条“移除”（含右键入口）与“清空最近失败”，确认说明仅隐藏待处理历史，不影响当前任务；清空只提交当前列表加载到的未隐藏 failed 请求 ID，不匹配未来失败。核心命令 `dismiss-failed-requests {request_ids:[...]}` 原子校验 1–200 个 failed 请求，幂等写 settings.dismissed_failed_requests 的 ID/时间映射与 failed_request_dismissed 事件，不改请求终态、任务、attempts 或证据。list-requests 仍返回历史并增加 failure_dismissed_at，桌面仅在最近失败过滤该字段；get-request 和运行记录仍能回看。


### Plus 超长消息完整读取（2026-10-08）

会话物理分页遇结果大小上限时减小 messageLimit；单条仍超限则使用可选只读工具 `pi_session_get_chunk`。参数 `{id,messageBefore?,offset?,expectedSha256?}`，服务端固定单消息页且无 contentLimit 截断。响应 `{sessionId,offset,nextOffset,totalBytes,sha256,encoding:"base64",chunk}`；每片最多 64KiB 原字节，总页最多 16MiB；offset>0 必带首片哈希。每片读取同一物理页，哈希变化明确冲突。客户端验证会话、偏移、进度、总长、哈希及 UTF8/JSON/物理页边界后才纳入 transcript；不将截断内容当完整证据。旧 Plus 未提供能力时继续明确失败，不能通过放宽完整性或身份校验绕过。


### Desktop 主动领取单任务验证（2026-10-08）

替代本会话app-server诊断入口。固定验证lane仅在SQLite settings.desktop_intake_probe保存当前批次，包含plan/review两个请求，不改变正式tasks/requests或调度后端。desktop-probe-create {}创建测试批次；未完成批次幂等复用，已完成后可显式新建。desktop-probe-status {}读取当前批次，没有批次时batch_id=null、requests=[]。

专用stdio MCP命令 desktop-probe-mcp 必须提供明确--db，暴露且仅暴露kp_desktop_probe_claim、kp_desktop_probe_report、kp_desktop_probe_status。Desktop发起工具调用，核心在BEGIN IMMEDIATE事务领取；单请求状态queued/claimed/completed。plan先行，review仅在plan完成后可领取并读取上游计划。claim返回不透明claim_token，租约900秒；报告须持有匹配未过期token，非空output最多64KiB，相同报告幂等，冲突报告拒绝。status不返回token。模型提交的thread_id/model为辅助字段，执行归属使用服务端记录的真实pid/ppid核验。

响应{batch_id,requests:[{request_id,role,status,output?,thread_id?,execution_evidence?}],detail}。初始化及工具调用遵循MCP JSONL，stdout只协议数据，错误不输出凭据。验证只处理固定整数平方需求与固定缺陷函数，不给任意业务路径/核心命令入口、不连接Kaneo服务；本轮不创建持续轮询自动任务、不安装替换正式app。

## Codex Desktop 正式规划与 Review 主动领取（2026-10-08）

`codex_desktop` 是独立规划/Review后端，仅通过 `desktop-queue-mcp --db <明确DB>` 的stdio MCP领取正式requests；普通runner在候选限额前排除Desktop任务，定点领取亦不能抢单。新Review请求在真正创建时冻结review_backend、review_config、expected_execution_run_id、plan_sha256；去重不更新快照，历史无快照请求沿用切换前的legacy配置。配置切换只影响之后入队的请求。

工具仅六项：kp_desktop_status、kp_desktop_claim、kp_desktop_heartbeat、kp_desktop_submit_plan、kp_desktop_submit_review、kp_desktop_fail。claim只支持role=plan/review，operation_id是同一操作的重放键；SQLite同一领取事务固化收据与token_hash，丢响应重复原operation不取下一单。token由DB旁 `.desktop-queue/<DB路径哈希>/claim.key` HMAC派生，目录700、文件600；状态接口不返回token，备份/迁移应保留密钥目录。

心跳及提交逐次校验token、请求角色/后端/run、当前未过期租约、活跃任务和被冻结材料；过期轮次不自动复活，沿release_lost人工恢复。关闭Desktop领取仅阻止新领取，已领取结果仍可收尾；不自动取消原生定时唤醒。MCP存活不等于心跳。规划输出正文由核心写入稳定路径/元数据并publish（publish已结束请求），needs_input转已有草稿/人工路径；Review沿review_settle的结果校验及checkpoint结算，保留pass/changes_requested/blocked和修复/推进规则。相同提交可恢复，冲突正文拒绝，远端结果不明不盲目重发。

`desktop_queue_config`保存enabled、poll_minutes(1..1440)、auto_configured、planner_thread_id/review_thread_id和最多两项automation_ids。auto_configured只证明绑定配置已创建，不能证明Desktop在线或本次原生唤醒成功。模型及权限继承Desktop会话/自动任务；规划元数据 `{planner_model:"codex-desktop", reasoning_effort:"inherit"}` 表示来源，不宣称实测型号。spec_plan必须在Desktop真正调用施工规划技能，不能用CLI禁配置结果冒充。

旧desktop-probe-*仅是历史固定验证通道，不参与正式队列；正式设置界面已替换固定试验入口。
