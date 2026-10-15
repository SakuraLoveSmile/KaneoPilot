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
| `report-execution-question` | worker | 登记/关闭当前轮次问题与等待区间（§5.28） |
| `save-execution-answers` | 桌面 | 保存当前问题草稿，不触达 Plus（§5.28） |
| `submit-execution-answers` | 桌面 | 收据先行，回答同一 Plus 待答请求（§5.28） |
| `reconcile-execution-question` | 桌面/核心 | 只读 Plus、核心结算送达与问题状态（§5.28） |
| `stop-execution` | 桌面/核心 | 收据先行停止当前轮次，观察终态再结算（§5.28） |

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

`tick {project_ids?: string[]}` 幂等，有三个入口：`claim` 内联（全部项目，不传 preflight、不做自动确认）、CLI 手动调用、引擎每循环一次（仅有效 live 项目，见 §5.27.15）。过滤缺省为全部项目；传入时 monitor、review、托管处理及 ready preflight 都限于列出的项目。列表须非空，元素为非空且非纯空白字符串，去重；不合法时返回 `bad_request`。

- `plan-scan`：距上次该请求完成 ≥ `plan_scan_interval_hours`（默认 24）且无活动同 key 请求 → 建 `plan-scan`。
- `monitor`：每个 `running` 且无活动 monitor 请求的 task → 建 `monitor`（key `monitor:<task_id>`）。
- `review`：每个 `in_review` 且无活动 review 请求、无未消费的 review 结论的 task → 建 `review`（key `review:<task_id>`）。
- `queued` 请求的 `not_before` 语义：monitor 连续观察由 `not_before = 上次 report + monitor_interval_seconds`（有效默认 120 秒；旧存储值 900 按 120 解释，不重写 DB；其他已设值保持）控制，避免同一 task 每 tick 重复建。tick 只建 `not_before <= now` 不限制的请求——即 tick 创建时立即生效的是 not_before 已过期的；新 cycle 请求由 report 时写 not_before。实现上 tick 对 running task 建 monitor 请求的条件：无活动 monitor 请求 **且**（无历史 monitor 完成记录 或 最近一次完成时间 + interval <= now）。

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

- `tick {project_ids?: string[]}` → `{"created": [{"request_id","kind","task_id"}], "skipped": [...]}`；缺省为全部项目，空列表或元素不是非空字符串时返回 `bad_request`。
- `overview {}` → `{"paused": {"global": false, "stages": ["dispatch"]}, "counts": {"tasks": {"ready": 3,...}, "board": {"needs_plan": 1, "todo": 2, "in_progress": 1, "in_review": 0, "done": 5, "failed": 0}, "requests": {"queued": 2, "claimed": 1, "lost": 0}}, "last_tick_at": ..., "schema_version": 1, "settings": {"lease_seconds": 1200, "monitor_interval_seconds": 120, "plan_scan_interval_hours": 24, "plan_scan_anchor": "09:00 Asia/Shanghai", "migration": {...}|null}, "paths": {"db": "...", "plan_root": "...", "repos_root": "...", "app_support": "...", "core": "..."}}`
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

`archive`（可归档时）、`trash`（可移入回收站时）、`restore`（`save_state != active` 时）、`resume-auto`（`auto_advance_paused=1` **或** `automation.manual_takeover` 为真时）。禁用原因由 `blocked_reason`/命令错误原文提供。

> `resume-auto` 的第二个条件不可省：`resolve {action:"dismiss"}` 只在 review 阻塞时置 `auto_advance_paused=1`，非 review 路径的接管落在 `ready` 且不暂停——若只按暂停露出，那些任务的「交回自动化」入口在桌面端永不出现，D4 的单向门只关了一半。

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
- `run_status` 只是最近观察值，不作收尾信号；轮次是否已收尾以任务是否离开 observe/running 为准（§5.27.15）。
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
- 执行问答扩展 `execution_question_max_wait_seconds`（非负整数，缺省86400，0不限）；在 `set-exec-config` 顶层接收并合并保存，派发快照冻结。`pi_desktop` 五字段授权配置形状不变，旧配置/快照缺省值见 §5.28.5。
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
- 每轮启动**独立 pi worker 进程**（`python3 -m kanpilot.pi_worker --spec <spec.json>`），并使用**独立 worktree**：执行分支持续使用 `worktrees_root/<task_id>/branch`（历史非分支执行回退仍为 `<task_id>/<run_id>`）；规划与评审使用 §5.29 的仓库槽位池，自 `task.repository` 创建；会话删除不删除 worktree。
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
- **legacy/shadow 的 Goal 验证证据窗口**：有 Goal 审批标记时，只认同执行 turn 的消息或可信的执行起点之后的消息；保留消息原始 turn 身份与时间缺失标记，不以采集时间冒充执行开始。缺可信边界显式返回 `whole_round` / `unscoped_goal_window`，monitor 转 `verification_unconfirmed`；没有 Goal 标记的普通轮次沿用整轮证据。live 继续由核心亲跑 verify，不据转录完成判定。
- **新生成 Goal 材料的交付排除**：仅 HEAD 原本未跟踪、普通真实目录中的新 `.pi/goal/*.md` 普通文件可排除；不追随符号链接，不排除已有 tracked 文件的修改/删除或其他 `.pi` 文件。安全 Goal 材料不计作任务改动、不阻止干净检查、不进 legacy/live 交付提交；用户已暂存的 Goal 材料保留。核心源码/索引指纹及 live 提交的 hook、恢复与索引并发核验仍按 §5.27.11。
- **完成判定须同时具备**：(a) 主会话 `terminal` 事件 `outcome=completed`；(b) 进程结果 `job.json.status=finished` 且 `exit_code=0`；(c) 验证证据（本轮工具事件中，计划 verify 块的每条命令最后一次匹配的整数退出码为 0；缺失转 verification_unconfirmed，用户可接受验证结果）。三者齐 → `advance-task --target in-review` → `report succeeded`（既有流程不变，Review 仍是 Done 的前置）。
- **明确异常**（→ `run_status=failed`，detail 记原因；monitor `report needs_user` + `failure_code=execution_failed`）：崩溃（进程非 0 退出/信号）、超时（超过 `pi_round_timeout_seconds`，默认 14400，settings 兼容旧键 `dsh_round_timeout_seconds` 回退）、日志截断（events.jsonl 尾部不完整且无 terminal）、身份不符（job.json/事件身份与目录不符）。**不自动重新提交、不自动重建轮次**。
 - **单独条件不得推动完成**：子 agent 结束、进程正常退出、模型自称完成——任一单独出现都不满足 (a)(b)(c)，任务保持 `running` 或按异常处置。

### 5.18.5 worktree 与评审衔接

- context / `get-task.execution_round` 提供 `worktree_path` 时，monitor 验证与 review 评审在该 worktree 内进行（`git -C <worktree>` 读改动）；主仓库检出不被改动。
- Done 后 worktree 与运行记录保留；受 PR 交付管理的任务由调度台提交，Review 通过后推送并开 PR，合并后完成；调度台不 merge；普通手动任务沿旧流程。

### 5.18.6 引擎 verdict（v6，A10 核心亲判）

 新引擎（`engine_mode=live`）的 verdict 由**核心亲判**，不依赖 Plus 转录：

 - **有效 diff**：以计划声明的执行工作树为范围取改动，**排除 `.pi/**` 前缀**后必须非空；为空即空转，归阶梯 3（quality）按空转重试一次处理。
 - **核心亲跑 verify 命令**：取计划 verify 块的每条命令，由核心以 **login shell** 执行并注入项目 `verify_env`，超时可配（`KANPILOT_VERIFY_TIMEOUT`，默认 600s）。
 - **login shell 口径（A4 spike 实测）**：默认用 `$SHELL -l -c`（读 `os.environ["SHELL"]`，本机为 `/bin/zsh`）；`/bin/bash -l -c` 仅作 `$SHELL` 缺失时的兜底；`KANPILOT_VERIFY_SHELL` 可整体覆盖。**不得**默认 `/bin/bash -l -c`——本机 `~/.bash_profile` 不含 homebrew/pyenv PATH，会导致 `gh`/`pytest` 全部 `command not found`（rc=127），而 `~/.zprofile` 有。详见 `docs/evidence/spike-verify-env-20261010.md`。
 - **命令不存在（rc=127）归环境类失败（阶梯 2）**，不得与「验证未通过（阶梯 3）」混为一谈。
 - **判定**：有效 diff 非空 **且** 每条 verify 命令退出码为 0 → verdict 通过；否则 `verification_unconfirmed`，进入阶梯 3 同分支有界补救（≤`max_fix_rounds` 轮）。
 - **`verify_env` 形态**：项目映射 `project_mappings.items[project_id].verify_env`，为字符串→字符串的 dict（缺省 `{}`），在 login shell 内作为环境变量注入；不得包含凭据/token（凭据另走凭据文件，不进入 verify_env）。
 - **证据**：逐命令的退出码 / 是否超时 / 输出尾部由核心落库（`extra.engine_verdict` 与既有 verification 回执并存，不互相覆写）；Plus 转录仅用于 UI 按需展示，**不参与判定、不整段持久化**。
 - **命令面**：`engine-verdict {task_id, expected_execution_run_id, expected_plan_sha256, operation_id?}`（幂等，`operation_id` 去重）。

## 5.19 执行端调度（App 内置，v1.6）

本节为历史记录；已由 §5.27 的 EngineSupervisor + `engine run` 取代。当前周期 tick 见 §5.27.15，不再由 ExecutorScheduler 调用。

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
- 描述/评论链接下载的文件（如无扩展名的 Kaneo `/api/asset/<id>`）按文件头魔数识别图片：PNG（`89 50 4E 47 0D 0A 1A 0A`）、JPEG（`FF D8 FF`）、WebP（`RIFF????WEBP`）。命中时 `media_type` 记 `image/png|image/jpeg|image/webp`；原名不带该类型扩展名时在原名后追加 `.png|.jpg|.webp`（不改原名其余部分、不改文件内容），使 Codex/Claude（按扩展名）与 Plus（按 media_type/扩展名）都把它作为图片附件。未命中保持原名与 `media_type=null`。只看文件内容，不信远端 `Content-Type`。
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
- 命令：`round-observe {task_id, run_id, executor?, continue_goal?, continue_goal_only?}`。`run_id` 必须是该任务**当前**执行轮次；任务须处于 `running|in_review|needs_user`；轮次 `backend=pi_desktop`；`job.json` 身份核验通过、`session_id`/`turn_id` 齐备、`send_state ∈ {sent, unknown}`，且 **events.jsonl 已有 session 事件**（「已确认发送」的硬证据，由发送成功 + 启动证明共同产生；`send_state` 只作粗筛）；`status=finished` → 返回 `{"status":"finished"}`（无需续接）。
- **只读续接**：仅以既有 `spec.json` 拉起 `python3 -m kanpilot.pi_desktop_worker --spec <spec.json> --resume`（独立会话，stdout/stderr 追加 `worker.log`）。`--resume` 分支**不建会话、不下发 `pi_session_configure`、绝不 `pi_agent_prompt`**；接管成功写 `job.observation_resumed_at`/`observe_pid`/`resume_started_at`，把轮次恢复为 `running`，并把 `send_state` 修回 `sent`（旧版假失败会写成 `unknown`）；首次续接前的收尾现场（status/detail/ended_at/exit_code/send_state）存入 `job.resume_previous` 供审计。
- **幂等**：原 worker 或上一次续接的进程仍存活（轮次行 `pid` 或 `job.observe_pid`）→ 返回 `{"status":"observing","deduplicated":true}`，绝不拉起第二个观察者。
- **剩余预算**：续接按 `job.started_at + 轮次时限` 计算剩余时间；执行问答的当前/历史等待按 §5.28.5 扣除核心区间并集（不累加 job 缓存）；有开放问题时先检测并接管等待。已超时 → 不续接、也不动手停止仍在跑的会话，返回 `deferred`（`round_timeout_elapsed`），交人工判定终态。
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
- **续接观察同样适用**：`round-observe`（§5.23.7）传 `continue_goal=true` 时，经下述恢复授权校验后接管停在审批门的轮次，继续同一审批门（批准属本轮职责，非重复发送）；普通续接仍只读。已批准过（`job.goal_approval.status=="approved"`）则按可信执行轮次收尾。
- **边界**：Goal 契约的批准权限取自**本轮快照**的 `permission_mode`，不读 Plus 当前设置、不改为 `inherit`；`ask` 语义仍是「Plus 每次工具确认需人工」，KaneoPilot 只代批契约本身，不代批工具权限。

### 5.23.10 Goal 报告完成判定与派发命名（v3 补丁，T5/T6）

> v5 兼容边界：报告完成判定适用于 `completion_policy=goal_report` 的历史轮次；旧 spec/job 未声明策略时按此解释。所有 v5 新 Plus 派发轮次（包括手动任务）固化 `completion_policy=command_verification`，按 §5.27 的确定性验证门推进，不要求 AI 判定事件；报告仍可只读查阅，派发命名规则继续适用。完成策略随轮次固化，不因项目策略、人工接管或运行时设置变化而改变。

本节冻结 KaneoPilot 与 PI-Desktop-Plus 之间的两项共享契约：**Goal 报告驱动的完成判定**（T5）与**新派发轮次的展示命名**（T6）。两仓按本节实现，任何一侧都不能单方面改变字段名、身份核对口径或失败语义。

#### T5-A 报告读取（Plus 侧只读 MCP）

- Plus 本机 MCP 新增两个**只读**具名工具，由既有 Host `goalReports.list/get` 提供，不开放 `submitDraft`/`retry`/`finalize`/`invalidate` 等写操作：
  - `pi_goal_report_list {sessionId}` → `{reports:[…GoalReportSummary]}`；
  - `pi_goal_report_get {sessionId, executionId}`（`reportId` 亦可）→ 完整报告 JSON，附 `report_sha256`。
- 正式返回形状为 `{state, report, report_sha256, ...}`：`state` 属于 Host 读取信封，报告正文不要求重复 `status`。核心只在信封明确 `ready` 且哈希、报告 schema 与本轮身份全部有效时放行；旧直传报告形态继续按其 `status` 校验，不能把缺失状态推断为 ready。
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
  3. `decision=complete` 时还须**确定性**核对 Host 读取状态 `state=ready`（旧直传形态为 `status=ready`）、`integrity.kind=structured`、`execution.status=completed`、`verdict=met`；传入硬闸的读取状态须来自本次经哈希与身份校验的核心读取结果，不能来自用户参数，也不向报告正文补造 `status`。`criterion_reviews` 非空、ID 不重复、结果**全为 `met`**；引用的 criterion/check/evidence ID **确实存在于报告**。任一不满足 → 拒绝记录完成判定。
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
     - **无人值守硬策略**：规划会话的 create/configure 显式下发 `interactionMode:"unattended"`；权限快照仍独立保持，新请求 `auto`、旧 `ask` 不提升。发送前核对 Plus 实际回执及 `session_get` 读回的会话身份、`interactionMode:"unattended"` 和严格整数 `interactionPolicyVersion:1`。缺字段、不支持、配置失败或读回不符时明确阻断，绝不退回提示词禁令或发送 prompt。恢复已有未结算轮次只读核验，不改活动会话策略、不重发 prompt；已经结算的历史结果不要求重新连接 Plus。
     - Plus 在模型完成响应、任何同批工具执行之前拦截 `asktool`，不产生待答请求、不执行该批其他工具、不继续模型调用。持久 turn 为 `status:"error",errorCode:"INTERACTION_REQUIRED"`，`errorDetails` 为 `{kind:"interaction_required",toolName:"asktool",toolCallId,questions:[{question,options,multiSelect?}]}`。该核心控制结果优先于模型最终文本/proposal；核心把合法问题转为现有 `needs_input`/`needs_user` 路径，保存原始回执和问题证据，不发布计划、不批准契约。非法问题（空问题或 `invalidArguments:true`）明确失败，不制造用户答案。
     - 单选沿现有结构化问题选项；多选沿现有文本回答框，说明中保留全部选项/描述并明确可填写多个选项，不伪造推荐项。正常模型自行输出的 `needs_input` 仍走原路径。角色指令保留无人值守说明及禁令帮助模型选择，但它不是执行保障；保障由上述策略及 Plus 执行层落实。
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
- Plus 分支：`claim(stage=review)` 后核对请求 `run_id`、任务本轮执行 worktree、计划哈希、执行证据与关联来源（**不得**只审主仓库）；runner 用只读 `git status/diff` 生成改动材料，并把计划/报告/执行证据复制到**专属 Review worktree** 的材料目录（该 worktree 是审查输入，不复用仍活动的执行会话）；`pi_session_create(mode:"plan", interactionMode:"unattended", …)` + `permissionMode:"auto"`，并按 §5.24.3 核验实际硬策略及版本，未确认时不发送。指令只要求审查并输出 JSON，不 `SubmitPlan`、不 `SubmitGoal`、不代批任何契约。Plus 的 `INTERACTION_REQUIRED` 持久回执直接生成 `blocked` 报告，保留问题/选项，经现有 `review_blocked` 结算交回用户，不要求模型自行再整理、不登记 pass、不推进远端任务。已登记结论的确定性恢复不要求重新连接；无效拦截回执明确受阻，不伪造审查通过。
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

- `PROJECTS`：`Music→e8magole7hbivl8103wevkss`、`Library→nosjbbex7hpyaiweh6cdidwr`、`StudyTool→jb22vodykf5ct64ng1acje5o`、`Comic→ms42fpv4oqdompsw3l0a7j4k`、`Feedback→ryryo1reph1xfemqtwkjyiiz`；仓库根 `~/Documents/Code/Project/Personal Project/<项目名>`。
- `WORKSPACE_ID = ujhqmhAOywzRnoqy0UUH3KaZajQriUI1`
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

- 执行问答增量增加 execution role 的 kp_runtime_input_pending/read 与 kp_runtime_input_respond/write，DTO、必需工具门、确认与送达规则见 §5.28；只读 worker 不获得 respond 权限。

- 项目服务为 `kaneopilot`，HTTP 只绑定 loopback，协商 `2025-06-18` / `2025-03-26`；stdio 是同一服务的逐行 JSON-RPC 桥，不建立第二份任务状态。
- 每次 HTTP 请求必须验证 Bearer、精确 Host、存在时的 Origin；transport session 绑定同一凭据、scope、协议与来源。撤销、过期、重启后的旧 grant 无效。管理凭据仅用于 `/admin/scopes` 与 initialize/initialized/ping 就绪检查，不可列出或调用模型工具。
- runtime/task grant 分离。角色 scope 是稳定 invocation 或 task/run 身份、安全文件名、固定项目/五字段运行配置与有效期；文件 owner-only，凭据不进模型、argv、数据库或日志。有效 scope 同 ID 只能复用完整快照，续期不得超出初始观察边界；过期后由本机管理端重新注册相同身份，新 token 替代旧 token。
- 新 Plus 轮次先检查项目 MCP 服务就绪，再经 `kp_runtime_session_create → session_configure → prompt`。不挂接 task grant，不要求 pi_session_attach_mcp，不向会话注入 kp_task 工具；runner 经核心命令完成状态转移。新规划固定 `plan/auto`，Review 固定 `plan/auto`，旧规划保留原权限快照，执行固定 `goal/auto`；dispatch/monitor/delivery 为确定性 Python 流程。历史轮次保留原只读恢复路径。
- `kp_runtime_session_create` 可选接收 `title` / `project_name`，分别透传 Plus `title` / `projectName`；返回 `session_id` 以及 Plus 实际返回的展示名，不以请求值补造成功回显。展示名不是角色配置或授权身份，不改变 scope 的项目路径/模型/模式校验；worker 按 §5.23.10 核对真实回显，不符时不发送 prompt。
- `kp_runtime_session_create/configure` 增加 `interaction_mode`，透传 Plus `interactionMode`，保留真实策略回执及 `interactionPolicyVersion`；runtime messages 读回保留这些字段，turn_get 保留 `errorDetails`。planner/review scope 在会话创建、配置和 prompt 门要求 `unattended`，prompt 门再次核验当前实际策略，并强制携带 `expected_interaction_mode:"unattended"`（Plus 为 `expectedInteractionMode`）；Plus 在 Host turn admission 原子比较，`INTERACTION_POLICY_MISMATCH` 明确记为 `not_sent` / `interaction_policy_unverified`，不从旧 turn 恢复。执行 scope 保持原配置，不借本修复提升权限。所有写操作继续先登记 `runtime_operations`，不绕过核心运行时域。
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


 ### 5.27 引擎与用户动作（v5）
 
 本节覆盖长驻引擎、待处理列表、会话总览与诊断命令。旧版自动化流水线详细契约已移至 `docs/automation-contract.md`；本节仅保留与引擎命令和用户动作相关的接口。
 
#### 5.27.1 engine run
 
 长驻进程：DB 级单实例租约 + 心跳 + 退避重启。
 
 ```json
 engine run {stage?: string, executor_id?: string, lease_seconds?: int, mode?: legacy|shadow|live, intake?: bool, spawn?: bool, tick?: bool, force?: bool}
 ```
 
 - `stage` 缺省时读取 `settings.executor_runs` 当前未暂停且可领取的阶段。
 - **单实例租约**：在 `BEGIN IMMEDIATE` 事务内写入 `settings.executor_instances`（`instance_id, started_at, pid, stage, lease_expires_at`）；重复调用同一 `instance_id` 幂等续租。条目同时记录持有者进程启动时间（birth，`started_seconds`/`started_microseconds`，与 worker 登记同口径）。
 - 每循环在 intake 之后、spawn 之前执行业务 tick（§5.27.15）；`tick` 缺省同 `spawn`，`intake`/`spawn` 缺省 true。进程 `mode` 缺省 legacy，项目配置覆盖进程默认；`force=true` 时一律采用进程模式。租约附记 `mode`/`force` 供独立 doctor 重建有效模式；旧条目缺省 legacy/false，不新增 setting 或 schema。
 - **失联持有者接管**：另一引擎条目除「未过期」外，还须「持有进程存活」才阻止第二个长驻引擎。持有条目的 pid 经 birth 核验已不存在（进程退出且启动时间无法匹配）→ 该条目失联，`acquire_or_renew_lease` 放行接管，并把失联条目标记 `state=stopped`（附 `stopped_reason=holder_dead`），不得仅因 `lease_expires_at` 未到就无限期拒绝恢复。pid 复用由 birth 核验防混淆；`engine status` 与活动实例计数同口径（pid 已死不计为活动）。
 - **心跳**：启动后每 `min(lease_seconds/3, 60)` 秒刷新 `lease_expires_at`；租约过期前 60 秒主动续约。
 - **退避重启**：非零退出或信号终止时，按指数退避（1s → 2s → 4s → … → 上限 60s）重试；连续失败 5 次后进入 backoff 状态，由 `engine status` 展示。
 - **失联判定**：`lease_expires_at < now` 且无心跳 → 派生 `lost`；不自动释放、不自动重启。持有 pid 经 birth 核验已死 → 同样派生失联（可按上条接管），不自动释放、不自动重启。
 - 环境变量 `KANPILOT_EXECUTOR_INSTANCE_ID` 由 App 启动时生成 UUID 并传入 runner/worker；worker 启动后经 `executor-instance` 的 `register-worker` 登记真实 PID 与启动时间。
 - **旧 App 调度器互斥**：`executor-instance start` 首次登记时，App 的直接 Python CLI 调用经 `KANPILOT_SCHEDULER_PID` 与核心父 PID 一致核验后，由核心读取父进程 birth，保存为 `scheduler_pid`、`scheduler_started_seconds`、`scheduler_started_microseconds`，不接受调用者提供的进程身份，重复 start 不替换身份。引擎只在旧实例仍为 active 且调度进程经 birth 核验存活（或尚有存活归属 worker）时避让；已证明退出或 PID 被复用且无存活 worker 的旧实例不阻塞排队请求。birth 不可读但 PID 仍存活时保持避让；未标识直接调用、可执行 shim 及缺完整进程身份的历史 active 登记保持保守避让，核实失效后只经 `executor-instance stop` 关闭；不凭时间或 worker 为空自动停用。
 
#### 5.27.2 engine status
 
 只读：返回当前引擎实例与存活 worker。
 
 ```json
 engine status {instance_id?: string}
 ```
 
 - 返回形状：
   ```json
   {"instances": [{"instance_id", "stage", "pid", "started_at", "lease_expires_at", "owned_workers": [{"pid", "started_seconds", "task_id", "run_id", "spec_path"}]}], "business_tick": {"last_tick_at": null, "age_seconds": null}}
   ```
 - 缺 `instance_id` 时返回全部实例。
 - `owned_workers` 仅返回该实例登记且启动时间仍匹配的进程；历史实例可在新登记时清除。
 - `business_tick` 读自 `settings.last_tick_at`，`age_seconds` 为距该时刻的非负秒数；时间缺失或无法解析时 age 为 null。该时间也会由手动/claim tick 刷新，不独立证明引擎 tick 成功。
 
#### 5.27.3 attention-list
 
 只读：聚合当前需要用户关注的任务与请求。
 
 ```json
 attention-list {save_state?: string, include_historical?: bool}
 ```
 
 - 聚合源：`needs_user` 任务 + 活动失败请求（`failed`/`lost`）+ 最近失败请求。
 - 同一任务不重复出现；按阶梯优先级排序（阶梯 4 > 阶梯 3 > 阶梯 2 > 阶梯 1）。
 - 返回形状（字段名单一来源；`reason_code` 等 6 个字段为 v6 新增，与既有字段并存，不破坏既有消费方）：
   ```json
   {"items": [{"attention_id", "task_id", "project_name", "title",
               "reason_code", "failure_code", "failure_stage", "failure_reason",
               "evidence", "blocked_summary",
               "recommended", "alternatives", "allowed_actions": [...],
               "attention_state": "actionable|historical|informational", "since", "question_preview": null}]}
   ```
 - `save_state` 缺省 `active`；`include_historical=true` 时包含已归档/回收站中的历史阻塞。

 **v6 新增字段语义（A15 契约）：**

 - `attention_id`：开放项稳定 ID。确定性派生为 `attn_<task_id>_<reason_code>`；同一任务同一 `reason_code` 复用同一 ID，重启后不变。任务至多一个开放项，因此 `attention_id` 在任务维度唯一。
 - `reason_code`：从 `failure_code` + 失败阶梯类别派生的稳定枚举，是 attention 的**分类主键**（`failure_code` 为原始失败码，保留原值）。派生规则：

   | reason_code | 来源 failure_code | 阶梯 | 含义 |
   |---|---|---|---|
   | `needs_plan_input` | `planner_failed`、`plan_missing` | 3 | 规划材料不足或规划失败，需重新规划 |
   | `execution_question` | `execution_input_required` | 3 | 执行中待答问题，轮次仍在运行（§5.28） |
   | `verify_unconfirmed` | `verification_unconfirmed` | 3 | 核心亲跑 verify 未通过或证据缺失 |
   | `review_changes_exhausted` | `review_blocked`、`review_fix_exhausted` | 3→4 | 同分支修复轮已达上限 |
   | `delivery_blocked` | `delivery_commit_failed`、`branch_diverged` | 3 | 提交/推送/PR 受阻 |
   | `remote_unavailable` | `remote_unavailable`、`remote_task_missing` | 1→2 | Kaneo 不可达或任务在远端已不存在 |
   | `plus_unreachable` | `plus_unreachable`、`plus_timeout` | 2 | Plus 控制面不可达/超时 |
   | `plus_send_unknown` | `send_state_unknown` | 2 | prompt 发送结果未知，禁止自动重发 |
   | `gh_environment` | `gh_missing`、`gh_not_logged_in`、`gh_timeout` | 2 | gh 未安装/未登录/超时 |
   | `push_rejected` | `push_rejected` | 3 | 非强制推送被拒（non-fast-forward） |
   | `pr_closed_unmerged` | `pr_closed_unmerged` | 3 | PR 已关闭未合并，不得标记 done |
   | `budget_exceeded` | `auto_budget_exceeded` | 4 | 成本或时限触顶 |
   | `concurrency_stalled` | `lease_expired`、`request_lost` | 1→2 | 租约过期/请求失联，需重新入队 |
   | `needs_user_other` | 其余 needs_user | 4 | 未归类的人工阻塞兜底 |

   `failure_code` 为空时按任务 `status`/`failure_stage` 归类；仍无法归类落 `needs_user_other`。`reason_code` 表由 `core/kanpilot/engine/attention.py` 的 `reason_code_for()` 实现，单测逐行覆盖。
 - `evidence`：结构化证据数组（非字符串拼接），每项含 `kind`（`event`/`request`/`probe`/`runtime_operation`）、`id`、`at`、`detail`。至少包含：触发本次阻塞的事件 id、关联失败请求 id（若有）、最近一次世界探测结果（若有）。不含 token、连接文件全文或整段 Plus 转录。
 - `recommended`：单个推荐动作，从 `allowed_actions` 按 `reason_code` 优先级派生（不引入第二张表）；`allowed_actions` 为空时为 `null`。
 - `alternatives`：其余可选动作数组（`allowed_actions` 去掉 `recommended` 后的有序列表）。
 - `since`：该开放项首次出现的 ISO-8601 时间戳（UTC）。同一任务同一 `reason_code` 复用首次出现时间，不随重复观测刷新；用于「等待多久」展示与排序稳定性。
 
#### 5.27.4 用户动作
 
 以下动作由 `resolve` 或专用命令实现，核心在事务内校验身份与任务状态，桌面只渲染 `allowed_actions`。
 
 | 动作 | 命令/action | 前置条件 | 效果 |
 |---|---|---|---|
 | take_over | resolve {action:"dismiss"} | automation=true，failure_code ∈ 阶梯 3/4 | manual_takeover=true，delivery.mode="manual"，回到 ready/needs_plan |
 | resume_auto | restore-task {resume_auto:true} | save_state=active，auto_advance_paused=1 | 清除暂停，继续原 phase |
 | dismiss | resolve {action:"dismiss"} | needs_user | 清除阻塞字段，回到对应 phase（自动化任务额外置 manual_takeover） |
 | retry | resolve {action:"retry"} | send_state=not_sent 或非 dispatch 阻塞 | 创建新 dispatch/plan/monitor/review 请求 |
 | replan | resolve {action:"replan"} | 任意 needs_user | needs_plan + plan_wanted，开新段 |
 | resolve | resolve {action:"release_lost"/"mark_sent"/"restore"/"accept_duplicate"} | 失联/发送不明/unknown | 释放失联/确认发送/恢复原任务 |
 | accept_verification | resolve {action:"accept_verification"} | verification_unconfirmed | 写入 verification_override，回到 running + monitor |
 | review_recovery | resolve {action:"recover_review"} | review_blocked 历史误退 | 恢复 in_review + 新 review 请求 |
| answer_execution_questions | submit-execution-answers | §5.28 当前可答请求 | 回答同一 Plus 工具调用 |
| skip_execution_questions | submit-execution-answers {skip_all:true} | §5.28 当前可答请求 | 全部跳过，继续同一 turn |
| stop_execution | stop-execution | §5.28 当前轮次；界面二次确认 | 停止本轮，由观察者收尾 |
 
 - 所有动作写 `event kind=resolved`（actor、action、note）。
 - 危险操作须二次确认；动作结果写入 `resolution` JSON。
- `execution_input_required` 只允许上述三个执行问答动作。通用 `resolve`、归档、回收、重新规划或新派发不能绕过 §5.28 的活跃轮次守卫。
 
#### 5.27.5 session-view
 
 只读：任务/执行轮次的会话状态总览。
 
 ```json
 session-view {task_id?, run_id?, status_only?: bool}
 ```
 
 - `status_only=true` 时不请求 `session-messages`，仅返回状态摘要。
 - 返回形状：
   ```json
   {"session": {"task_id", "run_id", "backend", "run_status", "session_id", "turn_id", "save_state", "state", "connection": {"state": "ok|degraded|unavailable", "detail"}, "allowed_actions": [...], "blocked_reason"?, "execution_question": null}}
   ```
 - `run_status` ∈ `pending|running|finished|failed|unknown`。
 - 状态页刷新沿用既有 `session_poll_seconds`（默认 2s）；连接失败保留上一份有效状态并标「可能已过期」。
 - 旧后端（dsh/headless/ATIF）返回 `unavailable` + detail「旧后端会话已弃用」。
 
#### 5.27.6 doctor --live
 
 在线诊断：`doctor {online?: bool, live?: bool}`；`--live` 与 `online=true` 等价（CLI 同时接受 `--live` 与 `--online`，任一为真即进入在线模式）。
 
 ```json
 doctor --live {}
 ```
 
 - 返回：
   ```json
   {"ok": true, "checks": [
     {"name": "python", "ok": true, "detail": "3.10.10"},
     {"name": "db_writable", "ok": true, "detail": "<db path>"},
     {"name": "credentials", "ok": true, "detail": "present, baseUrl https://…"},
     {"name": "plan_root", "ok": true, "detail": "<path>"},
     {"name": "repos_root", "ok": true, "detail": "<path>"},
     {"name": "state_root", "ok": true, "detail": "<app support dir>"},
     {"name": "kaneo_ping", "ok": true, "detail": "HTTP 200", "skipped": false},
     {"name": "engine_lease", "ok": true, "detail": "active instances: 2"},
     {"name": "attention_backlog", "ok": true, "detail": "actionable: 3, historical: 1"}
   ]}
   ```
 - `online` 缺省 false → `kaneo_ping` `skipped:true`；`--live`/`--online` 任一强制包含连通性检查。
 - 凭据检查只验证文件可读、含 baseUrl+token，**不输出 token**。
 - 新增 `engine_lease`（当前活跃实例数与健康状态）与 `attention_backlog`（attention-list 聚合计数）检查项。
 - `business_tick_fresh`：按引擎租约、state、holder birth 的共同口径判断活动实例，且仅检查 `save_state=active` 的 running/in_review 任务。存在该实例有效 live 项目的待观察任务时，`last_tick_at` 缺失、无效或 age >300 秒则失败，detail 给出时间与检查 business_tick actions/重启引擎的提示；否则通过。没有活动引擎或无需观察的 live 任务时通过并说明原因。有效模式包含租约的进程默认与 force（旧条目 legacy/false），不使用带 enabled/preflight 门的 intake 范围。

 **探针超时与失败区分（A8）：**

 - 每个探针有独立可配置超时（缺省：`kaneo_ping` 8s、`plus` 5s、`gh` 5s、`git` 5s；`KANPILOT_DOCTOR_TIMEOUT` 可整体覆盖）。git 探针不得使用短于 1s 的超时——历史实现用 70ms 量级超时导致把慢但正常的 git 调用误报为「非 git 仓库」，已修正为 5s 且对 `timeout` 与 `command not found` 分别归因。
 - 超时与真实失败必须区分：超时返回 `{"ok": false, "timeout": true, "detail": "… 超过 Ns"}`；真实失败（非零退出、缺工具、缺凭据）返回 `{"ok": false, "timeout": false, ...}`。二者不得合并为同一个 `ok:false`。
 - 探针失败时 `detail` 必须给出**明确可执行的修复项**（缺什么、去哪装、跑什么命令），不得只写「失败」。
 - 全部检查通过时顶层 `ok: true`；任一必需检查失败时 `ok: false`，且不得因单个可选探针失败而掩盖其余结果。


 **`plus_fallback_model` 检查项（仅在线模式；spike-fallback-model-id-20261010.md「失效与刷新」）：**

 - 扫各项目映射里配置的 `fallback_model`（条目层 `items[pid].fallback_model`，去重后逐个核对），
   用与引擎同一只读探针（`providers/list` + `providers/listModels {source:"cache"}`）确认
   provider 存在且模型在本地 cache 目录中。
 - 未配置 → `skipped:true` 且 `ok:true`（缺省不配置 = 不换模型，无需核对）；
   无 Plus 连接文件 → 同样 `skipped:true`（与 `plus` 检查同口径，不伪造失败）。
 - 配置且存在 → `ok:true`，detail 列出已核对的模型；配置但不存在 → `ok:false`，
   detail 必须给出**明确可执行的修复项**（改用同 provider 备选或刷新目录后重跑），
   并声明引擎不会静默换模型（届时按 `validation_failed` 阻塞）。
 - 该检查**只读**，绝不输出 token / 连接文件全文；detail 中模型 id 可原样出现（非凭据）。
 - 目录是 `source=cache` 的本地快照，可能随 Plus 升级或账号变更过期，因此需要可复跑；
   本项就是该复跑入口，不替代引擎侧探针。

#### 5.27.7 set-project-engine-mode（A12 按项目三态）

 ```json
 set-project-engine-mode {project_id, engine_mode?: "legacy"|"shadow"|"live", expected_revision?: int}
 ```

 - 写入 `project_mappings.items[project_id].engine_mode`（**映射条目层**，不是 `automation` 内），带 revision CAS（`expected_revision` 提供时校验，冲突 → `conflict`）。
 - **为什么在条目层**：`set-project-automation` 会用固定键白名单重建 `automation` 子字典（`validate_automation`），存在它里面的 `engine_mode` 会被下一次 automation 写入静默丢弃（已在生产库实测复现）。条目层键同时经受 `set-project-mapping`（它展开 `existing_entry`）与 `set-project-automation`（它只替换 `automation` 键）。
 - 配套 `set-project-verify-env {project_id, verify_env, expected_revision?}` 同样写在条目层（`items[pid].verify_env`），供 `engine-verdict` 的 login-shell 注入读取；`verify_env` 必须是扁平字符串→字符串字典，**凭据类键（`TOKEN`/`SECRET`/`AUTHORIZATION`/`KANEO_TOKEN`/`PI_DESKTOP_TOKEN` 等）一律拒绝**——凭据走凭据文件，不进 `verify_env`。
 - `engine_mode` 缺省或省略 → `legacy`（strangler 安全默认：旧项目行为不变，引擎不驱动）。
 - 非法值 → `bad_request`；命令独立于 `set-project-automation`（后者不改动、不校验该键，读侧 `automation_policy.policy_for` 原样透出）。
 - 不新增 schema 列、不升版本；旧数据（无该键）可回退。

 **按项目解析规则（`engine run` 的 Reconciler）：**

 | 项目 `engine_mode` | 引擎行为 |
 |---|---|
 | `legacy` | 该项目的任务候选在 decide 之前被过滤，引擎完全不驱动；由旧调度链路处理 |
 | `shadow` | 只算决策 + 计划命令 + `explain()`，全部落 `summary.actions` 日志；**零写入**（不下发命令、不 intake、不 spawn） |
 | `live` | 照常执行前向命令 + intake（只采纳 live 项目）+ spawn（只处理 live 项目的 queued 请求） |

 - 一个 `engine run` 进程可同时服务三种模式的项目；三态并存是 strangler 的核心，不得互相干扰。
 - `engine run {mode}` 改为**进程默认模式**（缺省 `legacy`）：项目显式配置优先，未配置回落进程默认；`force=true` 时所有项目强制使用进程 `mode`（演练/调试用）。
 - `engine-mode` 决策与旧调度的差异在 shadow 模式下全部落日志且带 `explain()` 解释（A12）。

#### 5.27.8 set-project-fallback-model（场景 5 备用执行模型）

 ```json
 set-project-fallback-model {project_id, fallback_model?: {provider_id, model_id}, expected_revision?: int}
 ```

 - 写入 `project_mappings.items[project_id].fallback_model`（**映射条目层**，同 `engine_mode`/`verify_env` 的理由：`validate_automation` 的白名单会静默丢弃 `automation` 内的未知键）。
 - **缺省不配置 = 不换模型**。配置项只是「允许换」的前置条件，不是「一定换」——换不换由探针结果决定。
 - 触发条件：空 diff（`empty_diff`，阶梯 3 质量失败）进入同计划有界补救轮次时（空 diff 同计划至多一次；任务总补救 ≤2 轮，跨 replan 累计）。
 - **探针先行**：模型必须存在于 Plus 本地 cache 目录（只读 `providers/list` + `providers/listModels {source:"cache"}`，复用 `pi_desktop_backend.probe_connection`，不返回 token/连接文件全文）。探针失败 → `validation_failed`（阶梯 2 环境类，block + attention），**不静默换模型**。
 - 探针通过 → 记事件 `model`（`detail.model="fallback"`、`model_source="custom"`、provider/model），并用该模型快照重跑：`model_source="custom"` + `provider_id`/`model_id`，**沿用主模型的 `connection_file`/`thinking_level`/`permission_mode`**（不新增第二套凭据）。未配置 → 记 `model`（`detail.model="primary"`）+ 事件 `model_hint`（「建议配置备用模型」，带推荐值）。
 - 快照经一次性覆盖 `extra.engine_model_override` 落盘，dispatch claim 冻结轮次快照时应用（`exec_config.snapshot_with_model_override`）：只在计划身份（`plan_sha256`/`plan_generated_at`）一致时应用，**不写回** `settings.exec_config`（设置变更仍只影响新轮次），新执行轮次开始后由引擎清除，不套用到无关轮次。
 - 重跑授权见 §5.27.12：引擎以 `actor=automation` 经 execution_retry 或 resolve/replan 开启新轮次，保留既有轮次不得重发闸门。
 - 主执行后端不是 `pi_desktop` 时 fallback 不适用：按主模型重试并记 `reason="unsupported_backend"`，不换模型。
 - 推荐缺省值（**仅建议值，绝不隐式套用**）：`{"provider_id": "707a4c67-4ee0-484d-b635-730372501eaf", "model_id": "google/gemini-3.7-flash"}`，理由见 `docs/evidence/spike-fallback-model-id-20261010.md`（同 provider 复用鉴权、跨厂商 flash 档、目录内确认存在）。
 - 非法形状（缺 `provider_id`/`model_id`、非字符串、空串）→ `bad_request`；省略 `fallback_model` 或传 null → 清除配置，回到「不换模型」。
 - 不新增 schema 列、不升版本；旧数据（无该键）可回退。

#### 5.27.9 spawn 与 lifecycle 串行位（A14 并发派发）

 `lifecycle_owner` 是「同一时刻只允许一次 Kaneo 远端写」的全局串行位（state-model §5）。
 持锁的写入方：`dispatch-begin-send`（owner `begin-send:<run_id>`）、`advance-task`、
 `create-review-fix`；`recheck` 与 `register-review` **不**持锁。

 - 因此会被 spawn 的 worker 分成两类：`dispatch` / `monitor` / `delivery` 的 worker 会持锁
   （monitor 与 delivery 都经 `advance-task` 收尾），`plan` / `review` 不持锁。
 - **引擎在 `lifecycle_owner` 非空时不起前一类 worker**（`loop.plan_spawns` 的
   `lifecycle_busy`）：抢锁失败会被记成「已发生」的确定结果——dispatch 记
   `send_state=unknown`（begin-send 之后一律 unknown）并把任务推进 needs_user，
   而这一次发送根本没有发生。让行是安全的：请求保持 queued，下一 tick 重试。
 - 本轮让行的 stage 记在 `spawn` action 的 `deferred={stages:[...], reason:"lifecycle_serial_busy"}`，
   便于观测「为什么这个 tick 没起 worker」。已有 live runner 的 stage 不算让行。
 - `lifecycle_owner` 是无时间戳的普通 setting：worker 在 acquire 与 `finally` release
   之间硬崩会泄漏它，使这三类 stage 无限期让行（请求不丢、可恢复，但不再推进）。
   回收器需要协议对「无心跳串行位的陈旧判定」给出定义，属后续事项。

#### 5.27.10 `actor` 入参（A14 无人值守事件归属）

 A14 要求无人值守运行时 `actor=user` 的事件**仅限任务创建本身**。以下命令新增显式
 `actor`（`"user"` 缺省保持兼容，`"automation"` 表示无人值守调用方；非法值 → `bad_request`），
 与 `prepare-plan-input` 同一对词表：

 | 命令 | 事件 | 无人值守调用方 |
 |---|---|---|
 | `enqueue {task_id, kind?, actor?}` | `enqueued`（+ `confirm_plan` 时的 `resolved`） | 引擎 Reconciler 的每一次入队 |
 | `resolve {task_id, action, actor?}` | `resolved` | 引擎补救与用户动作；所有子事件继承显式 actor |
 | `recheck {task_id, offline?, actor?}` | `rechecked` | monitor stage 的无人值守复校 |

 - `enqueue` 的 `actor="automation"` 同时把 `requests.requested_by` 写为 `automation`
   （该列即 `enqueued` 事件的 actor）。历史调用方只设 `not_before` 时仍写 `engine`，
   不静默改变既有语义。
 - 其余 `requested_by` 取值不变：`scheduler`（legacy 自动推进）、`migration`、
   `user`（规划问答/评审决议等真正的用户动作）。


#### 5.27.11 live monitor 核心判定（A1）

- legacy 保持转录匹配；shadow 保持旧结果，同时记录核心 verdict 对比回执；live 完成点调用 engine-verdict，转录仅用于展示。回执绑定 task/run/plan，验证前后复校身份，禁止陈旧结果覆盖新轮次。
- 核心读取冻结且哈希匹配的顶层 verify 围栏（1–5 条），沿用 parse_verify_block 的命令限制；cwd 为本轮已验证的任务工作树，环境继承并注入项目 verify_env，输出和回执脱敏。缺少可信声明或工作树不能确认时 fail closed，不能退回仓库根目录执行。
- 每条命令上限 600 秒，整组含 PATH 探测总时限不超过 monitor 租约 80%，同时受 runner 剩余时限约束；命令之间续心跳，超时终止进程组，禁止遗留验证子进程。
- 有效 diff 非空且全部通过 → delivery-commit、advance in-review、成本、report succeeded。环境失败 → needs_user，failure_code=verification_environment_failure，环境阶梯 waiting_env；修复 verify_env/PATH 后用户 resume 将原 run/plan/worktree 排回 monitor，不新增 dispatch、执行轮次或补救计数；空 diff / verify 失败 → 质量阶梯；缺失声明、身份变化等不可确认结果进入 attention，不误判成功。构建产物沿交付谓词排除，不因 pyc 形成有效交付。
- live 派发提示允许执行端自检，由核心判通过；不改变发送已知/未知不重发守卫。

#### 5.27.12 自动补救闭环（A2）

- START_FIX_ROUND 必须真正执行新轮次，事件 actor=automation。空 diff 沿 execution_retry 同计划重跑一次（可用已配置且探针通过的备用模型）；verify 失败沿 resolve replan 同分支修复，失败输出尾部进入 planner 输入。
- execution_retry 保留远端 In Progress、计划哈希、工作树身份与 expected_execution_run_id 守卫；只放行已绑定核心空 diff 回执的自动补救，绝不重发旧已发送轮次。
- engine_fix_rounds 按任务跨 replan 累计至多 2；同一失败 execution_run_id 至多启动一次。领取/排队失败不可产生虚假成功回执；崩溃后可幂等继续。任务成本/时间上限继续生效；超限进入 attention。评审修复单独记账。
- shadow 只计算决策、不下补救命令、不写补救计数；legacy 不接管。

#### 5.27.13 评审与探针（A3）

- 评审快照仍要求 detached HEAD 且 HEAD==冻结 head_sha；git status 中只有 git_delivery.is_build_artifact 命中的路径（包括 tracked 修改）可忽略。源文件、重命名源与目标中任一非产物变化仍阻塞。CLI 与 Plus 同守卫。
- doctor --live 增加 Claude CLI 可用性探针；不调用付费模型，不输出凭据。后端与模型切换仅经核心配置命令，真实库切换需对应授权。


#### 5.27.14 原生读侧共享字段（B4/B5）

get-project-mappings.items 增加 engine_mode（legacy缺省）、verify_env（字典）、fallback_model（{provider_id,model_id}|null），从映射条目层回读，不改变既有 automation 投影。get-task.task 与 list-tasks.tasks 各项增加 phase/condition（state-model §7；Review通过或PR存在为delivery/syncing）以及 attention（当前active需人工项，形状与attention-list同一项逐字段相等，否则null）。纯读取，无新schema或状态写入。

engine-verdict 可选 request_id/run_id（monitor租约身份）、budget_seconds:int（正数）；resolve 可选 engine_fix:dict 为引擎事务内补救参数，必须核验 automation actor 与绑定失败回执，不允许借其绕过发送守卫。


A1 交付补充：live 的判定回执只保存命令摘要，不保存原始命令中的凭据；交付提交使用私有 Git index 与冻结源码/tree 对照，保留仓库 hooks。hook 或并发变化导致最终提交 tree 与通过验证的 tree 不同则阻塞，不登记交付、不进入Review、不发布；异常本地提交保留以便恢复，不自动reset工作树。shadow验证使用隔离快照，失败、异常和验证产物不得改变旧monitor判定或旧工作树。


三态与手动规划兼容：engine-owned worker 携带进程默认mode/force与实例身份，核心在候选选择、定点领取及dispatch验证后的事务再次校验当前项目模式与活动引擎租约；自动请求仅可领live项目，旧CLI/App runner不套用此限制。唯一手动例外是用户通过start-plans明确授权的plan请求（requested_by=user、manual_authorized=true），可由引擎代原App拉起，领取后仍走核心单飞和原后端守卫；这不授权该项目后续自动派发，也不改变legacy/shadow设置。


Live 手动处置边界：核心验证不读取人工verification_override，因此live不得提供无效的accept_verification入口；allowed_actions隐藏该动作，直接resolve同样在任何写入前明确拒绝。可重新运行验证检查、修复/重规划，或使用其他核心允许的处置；legacy/shadow保留原人工接受行为。此约束不改变发送恢复或手动重新执行。

#### 5.27.15 运行中轮次的补给与收尾兜底

- 引擎对 `save_state=active AND status!=done` 任务所在项目取有效模式（项目配置优先进程默认，force 时采用进程模式），只为 live 项目执行业务 tick；不要求 enabled/preflight，以免已运行任务因临时环境失败而失去观察。legacy/shadow 不调用 tick、不新增请求。
- `Reconciler(enable_tick=False)` 默认关闭；`engine run` 的 `tick` 缺省同 spawn。开启时每循环在 intake 后、spawn 前，经核心 `tick {project_ids: sorted(pids)}` 补给，同循环可拉起新 monitor/review worker。空范围记录 `business_tick` / `executed=false` / `reason=no_live_projects`。
- 每循环 `min(lease_seconds/3,60)` 秒（默认 60 秒）；monitor 仍遵守有效默认 120 秒的间隔门，通常约 2–3 分钟观察一次。action 记录 created/skipped 计数和 created 明细；范围查询、命令或结果处理异常均以脱敏错误记 action/日志，继续 spawn 与续租，不持有包裹核心命令的外层写事务。
- 人工接管只停止自动化决策，不停止观察、收尾与评审：tick 的 monitor/review 覆盖普通及接管任务；托管的丢失租约释放、自动确认与 PR 交付补建仍跳过接管任务。
- `_round_settle` 仅对仍在 observe/running、无活动请求的托管任务生效：job.json 身份可信且含 ended_at/exit_code 即可补 monitor，不因 run_status 已为 finished/failed 跳过。每轮最多 2 次 settle，shadow 只计算不写；run_status/terminal_recorded 仅诊断。任务离开 observe/running 即不再 settle。
- settle 在业务 tick 前入队；两者共用 `monitor:<task>` 活动 key 去重。2 次预算只限制 settle，不限制 tick 按间隔继续观察。观察者恢复与本节收尾职责分离。

## 5.28 执行期提问转交（P0 冻结，2026-10-14）

本节为执行期问答的目标契约。当前状态为实现中，接口尚未实现；源码、测试和候选实测齐备前不得宣称可用。执行保持 `goal/auto`、`interactive`，覆盖目标契约轮及执行轮；规划、Review、Team 托管输入、permission 确认不属于本节。部署顺序为先 Plus、后 KaneoPilot，不含手机端、远程访问、付费模型验证或正式安装。

### 5.28.1 发现、身份与存储

- Plus `pi_agent_status` 的摘要位于 `status.pendingInputs={count,requestIds}`；Main 合并，runtime `getStatus()` 不变。摘要和详情用同一过滤条件：排除 `teamManaged`/`teamPending`，`native-pi:` 返回空。`pi_asks_pending {sessionId}` 返回 `{sessionId,asks:[{requestId,toolCallId,turnId?,createdAt?,questions}]}`，不得沿 Renderer 的最多八项裁剪冒充完整列表。
- worker 每两秒先读摘要，发现新 requestId 再读详情。详情必须匹配当前 session 及 job.turn_id；缺省 turnId 时以同次观察的 status.currentTurnId 核验。不匹配不接受、不作答，不得用“当前唯一问题”猜身份。turn 终态需结合持久工具结果，不能仅因 completed 判过期。
- 外部身份为 `(session_id,request_id)`，核心还绑定 `task_id/run_id/turn_id/tool_call_id`。存放在 task.extra 的 execution_questions 集合，不新建第二状态源。worker/job 只是缓存，核心命令重新校验当前轮次及实际 job 归属。
- 请求首次 open 时间取核心 UTC 时间，不信任 Plus createdAt。重复同一身份、同一指纹幂等，不能重开已关闭记录或覆盖指纹不同的请求。问题上限为20题、每题100项、问题 UTF-8 JSON 64 KiB、身份字段512 UTF-8字节。非法/超限仍阻塞，但 `answerable=false`，保留安全摘要与拒绝原因，提示在 Plus 回答；不截断问题后伪装可答。
- 同任务多请求按 opened_at/request_id 稳定先进先出。主 DTO 只显示一份，`queued` 为其余开放请求数；关闭后选下一份，直到所有开放请求关闭才解除阻塞。历史问题和草稿保留。

### 5.28.2 稳定 DTO 与答案

`get-task.task`（任务详情）、`session-view.session` 增加 `execution_question`，无记录时为 null；有开放请求返回最早一份，否则返回当前 run 最近的关闭记录。attention-list 条目增量增加 `question_preview:{count,first_title,request_id}|null`，沿用同一 DTO，不由 Swift 再派生另一套状态。

```json
{
  "request_id":"…", "run_id":"…", "session_id":"…", "turn_id":"…", "tool_call_id":"…",
  "source":"plus_asktool", "questions_sha256":"…", "answerable":true,
  "state":"open", "delivery":"none", "answered_by":null,
  "opened_at":"…", "closed_at":null, "wait_seconds":0,
  "observer_alive":true, "queued":0,
  "questions":[{"id":"q0","title":"…","description":"","multi_select":false,
    "allow_custom":true,"required":false,
    "options":[{"id":"o0","label":"…","description":"…"}]}],
  "draft":[{"question_id":"q0","option_ids":["o0"],"text":"","skipped":false}],
  "submitted":null
}
```

- state 为 `open|answered|expired|stopped`；delivery 为 `none|submitting|accepted|not_pending|unknown|not_applied`；answered_by 为 `kaneopilot|plus|unknown|null`。submitted 非空时为 `{answers,operation_id,at,by}`，answers 是冻结的核心草稿形状，by 为调用端标签，不是权限来源。
- `questions_sha256` 是核心对规范化问题的规范 JSON（UTF-8、排序对象键、无多余空白）计算的 SHA256；不传给 Plus。选项原始 label 保留，显示时不得改写回送值。新增 multi_select 只用于执行 DTO，规划 DTO 不变。
- 每题身份为 q<i>，选项为 o<j>。answers 必须逐题按原顺序对应，拒绝未知、重复、遗漏 question_id/option_id 和超限文本。skipped=true 必须无选项与文本，回送 null；否则单选的自定义文本与选项互斥，多选去重后保持原选项顺序，可追加一项非空自定义文本。save可保存未完成条目（option_ids=[]、text=""、skipped=false）；submit对非跳过但没有任何有效值明确拒绝，不隐式代答。草稿同样校验身份、形状、互斥与上限。
- 回送 Plus 的 answers 是 `(null|string[])[]`。不超过20题，每题最多100个非空字符串（含多选的自定义项）。规范化答案 JSON UTF-8 上限1 MiB，并额外校验完整 MCP 编码请求不超过2 MiB，包含转义、身份和 JSON-RPC 信封；超限明确拒绝，不截断。skip_all 生成每题 null，不可答请求不走此入口。

### 5.28.3 五个核心命令

命令沿固定 JSON 信封，只能写 SQLite；远端副作用只经 §5.25 runtime 域。业务校验失败不改变已有草稿、请求或任务。
成功 data 共同字段为 `{task_id,run_id,execution_question}`，execution_question 沿 §5.28.2，可为null；
命令特定status/observer字段只作补充。桌面写后经get-task重读权威DTO，不从返回字符串推断任务状态。

| 命令与字段 | 行为 |
|---|---|
| `report-execution-question {task_id,run_id,session_id,turn_id,request_id,tool_call_id,questions?,state:open|closed|expired,detail?}` | worker 经 cli.run 调用。open 仅允许当前 running，或替换当前 run 的 observation_blocked/已有执行提问阻塞；核心规范化并登记。closed 结算身份一致、turn仍运行时已消失的请求，或 completed 且具有下述真实回答持久证据的请求。expired 在 turn 终止/丢失 waiter 的证据下结算。重复回报幂等。 |
| `save-execution-answers {task_id,request_id,questions_sha256,answers}` | 核验当前 run 的开放可答请求与指纹，保存完整草稿；不触达 Plus。submitting/unknown 的冻结答案不得修改。 |
| `submit-execution-answers {task_id,run_id,request_id,questions_sha256,answers|skip_all,operation_id}` | 短事务校验、预留并冻结答案，事务外调用 runtime.input_respond，再在短事务按当前 run/request/指纹/operation_id 条件结算；成功后确保有观察者。 |
| `reconcile-execution-question {task_id,run_id}` | 只读 Plus status/turn/pending，再写核心结算。结果未知可自动调用一次；无外部写、无自动重发，读取失败保留 unknown 与原草稿。 |
| `stop-execution {task_id,run_id,operation_id,reason?,abort?}` | 二次确认由 UI 承担；核心核验活动轮次后，经 runtime stop 收据先行。接受停止请求不等于已终止；确保观察者，按持久 turn 终态完成 stopped/失败恢复。未知结果不重发。worker可传reason=execution_question_expired；abort=true仅同run已有核心stop_requested_at且满10秒，使用新operation_id，不重新计时。UI不传内部字段。 |

- 同 operation_id、同冻结参数返回现有结果，不同参数冲突；本机并发提交只能一个预留成功，不持 SQLite 写锁等待 Plus。未答期间通用 resolve、重新派发、归档、回收等核心入口在外部 I/O 前及写事务内拒绝，不仅隐藏 UI 按钮。
- open：`running→needs_user`，failure_code=`execution_input_required`，reason_code=`execution_question`（阶梯3），blocked_summary=`执行中提问：<首题>（共 N 题）`。轮次仍 running，engine decide 对任务保持 BLOCK。
- 全部答完：仅当前 run 的 execution_input_required 清阻塞并 `needs_user→running`；外部其它阻塞原样保留，不触发重新规划或新派发。`round-observe` 的 §5.23.8 解除逻辑仍只作用 execution_failed，不得提前清除提问阻塞。
- 终止/过期：保留草稿和回答历史，核心同事务改为既有 execution_failed 恢复流程；不能保留已无开放问题的 execution_input_required，不能仅靠 worker.finalize 修改 job。最长等待原因 detail=`execution_question_expired`，显式停止保留 stopped 来源。
- 事件 kind 增加 `execution_question_opened/answered/closed`，中文分别为“执行中提问”“执行问题已回答”“执行问题已关闭”；同步 derive、Swift Labels 和固定枚举测试。只记录 operation_id/指纹/答案摘要/端来源，不记 token、请求头或连接文件全文。

### 5.28.4 送达、竞争与未知结果

- `kp_runtime_input_respond` 先 reserve_downstream，再调用 `pi_asks_resolve {sessionId,requestId,expectedToolCallId,answers,confirm:true}`，最后 complete_operation。Plus 由 runtime waiter 删除保证最多一次，桌面与 MCP 谁先答谁生效；本地锁不能阻止 Plus UI 竞争。
- `answered`：delivery=accepted，结算 answered_by=kaneopilot。`not_pending`：先读取持久 turn/status；turn 已终止时按持久证据规则结算，身份一致仍运行且问题消失可结算 answered。没有被接受的本地送达时来源为 plus，有未知本地送达时为 unknown；不能将未知送达冒充 Plus 端已答。
- 传输失败、断连或进程中断的应用结果不明：delivery=unknown，保持阻塞，核心答案冻结。只读 pending 仍在只说明该次读取尚待答，**不能证明原调用未生效**：原网络调用可能仍在途。原计划由 pending 直接推断 not_applied 的规则在此废止。
- not_applied 仅在原调用明确拒绝且可证明没有后续执行时使用。不得以 worker 退出、pending 快照、无回答或找不到回执单独证明未应用。
- unknown 后不自动重发。用户显式重新提交只能使用原冻结答案和新 operation_id；本机仍在 submitting 时拒绝重复提交，unknown 时允许同答案重试并保留各次收据。不同答案拒绝直到原结果确定，防止旧答案晚到覆盖用户新选择。重复最多得到 not_pending，不声称未知的旧调用被取消。
- 问题消失但 turn 不可读或身份无法核实：保持 unknown/阻塞，不猜结算。durable running 但 status.currentTurnId 缺省、pending为空时不得猜已回答；sidecar 重启造成 waiter 丢失不代表工具已答。晚到完成回执不能重新打开已关闭请求或覆盖更新的 run/operation；条件更新失败只保留审计回执。

- 快速完成：Plus 在现有 asktool toolResult.details 持久化 resolutionKind=`answered|stopped|aborted`。普通回答及 Decline all 为 answered，graceful-stop 取消为 stopped，abort 取消为 aborted。completed 时必须读取完整 transcript，匹配同 session/turn/toolCallId 且 resolutionKind=answered，才按 answered 正常收尾；错误、aborted、stopped 或缺少真实回答证据仍按 expired 处理。Plus session.get 的 message.turnId 必须来自同session消息ID对应的持久messages索引及同session turn归属，不按当前turn/时间猜测；索引未知保留缺省。input.resolved.by 仅事件可见，不作为持久来源证据，不新增表或历史 API。

### 5.28.5 等待、时限与恢复

观察者恢复只保证观察，不负责收尾；执行轮结束后的 monitor/review 补给与收尾见 §5.27.15。

- 最长等待默认86400秒（24小时）；执行配置增加非负整数 execution_question_max_wait_seconds，0为不限，派发冻结到该轮快照。旧快照缺省86400，不读当前设置覆盖旧轮次。成本预算不变。
- 核心是等待时间唯一权威。run 待答集合 `0→1` 开启等待区间，`1→0` 关闭；并发问题的等待取**区间并集**，不能将各请求重叠时长相加。开放区间计算到本次核心时间，历史区间重报/恢复不重复累计。
- 轮次剩余预算=`时限−max(0,now−started_at−等待并集时长)`；自动化预算扣除等待区间与本段 `[segment_started_at,now]` 的交集并集。新段采用新起点，不清除历史问答。job.input_wait_seconds 仅缓存；resume 从核心重建，不使用旧缓存再加一次。
- pending 检测先于 deadline 判断，包括 resume 第一轮与 Goal 契约轮。有开放问题不得因普通时限 stop/abort。最长等待达到后，经核心停止操作收据执行 stop；十秒后未见终态才 abort，原因 execution_question_expired，不扩大只读 client 的写权限。
- Plus graceful stop 预期关闭 ordinary waiter 并 completed；abort 是 aborted。P1 必须先红验证 spec17 的 stop 跳过规则（当前 runtime graceful stop 的 waiter 关闭尚缺实现），修复时保留 Team 与 preserveRunningTools 边界。worker 不得用 runtime idle 冒充成功完成。
- 观察续接保持同 task/run/session；只读接管活跃 ask，不传 continue_goal_only。continue_goal 状态门可接受该 run 的 execution_input_required，但不绕过 Goal 精确审批授权，也不获得回答/停止写权限。
- 提交后、App 后台恢复入口及任务详情“重新连接”汇入同一只读 observer ensure。engine 可发恢复意图，但 legacy/shadow 或 engine关闭时仍有已有 recover-execution-goals 本机入口；不依赖 desktop_queue 领取。该 run 有问答历史、任务 running 且观察者缺席时，后台恢复继续检查；开放问题走普通只读观察，已答契约轮完成后仅沿既有精确 continue_goal_only 审批门续接。多路恢复必须有原子 launch reservation，PID检查只作存活核实，不能单靠检查后spawn宣称并发去重。
- scope 到期按 §5.25 同身份重新注册，新token替代旧token，accepted create收据仍证明session归属。只对可确认的授权过期恢复，不把全部forbidden作为可重试；只读重连允许身份一致且正在等答的running turn，保留原有历史恢复错误门。

### 5.28.6 runtime 工具、能力门与验收

- execution role 增加 `kp_runtime_input_pending {scope_id,session_id}`（read）、`kp_runtime_input_respond {scope_id,session_id,request_id,expected_tool_call_id,answers,operation_id}`（write）。guard校验有效scope、execution角色及accepted create的session归属，连接只取scope冻结值。其他角色拒绝。respond仅核心提交使用非只读client，不能放宽worker的通用_write门。
- PI_DESKTOP_INPUT_TOOLS 包含 pi_asks_pending/pi_asks_resolve，并入新派发必需工具列表。旧Plus明确unsupported_tools/能力tools_ok=false，发prompt前阻断；旧轮次只读恢复保留原能力，不凭新增工具门停止旧轮次。
- 隔离测试覆盖原计划全部验收：10秒内收件箱/通知；同turn原tool续跑；Plus先答与双端竞争；unknown不自动重发且同答案重试；重叠等待/六小时假时钟/最长等待；scope到期/observer崩溃及并发恢复；终态/重启草稿保留；engine关闭仍可回答；直接CLI非法动作拒绝；日志与DB不泄漏token；历史恢复不变。
- 单测/mock通过不能替代真实Rust Host、生产sidecar、loopback确定性SSE联合演练及原生问答卡/系统通知核验。候选记录base/head/源码与产物指纹；各未运行项明确NOT RUN。适用检查齐备后为待体验，只有用户确认该新功能才标已验收。


## 5.29 规划/评审槽位池与历史目录回收（schema 6）

### 5.29.1 范围与租约

规划与评审（Plus、Claude/Codex CLI，包括 plan-scan、干净交付快照及脏状态快照）在 `<worktrees_root>/pool/<repo_key>/slot-<k>` 执行，不在真实仓库执行。执行 `<task>/branch`、恢复分支及历史执行 UUID 不池化。repo_key 取规范 git common dir 的 SHA256 前16位；同一仓库多工作树共池。`set-config {worktree_pool_size:N}` 配置每仓库上限，默认4，取1–32整数；隔离槽位计入上限，缩小配置不删除已有槽位，不再增建直至数量低于上限。容量不足返回等待，保留可重试请求，不转为业务失败；attention-list 暴露等待/隔离。

核心在 BEGIN IMMEDIATE 内分配 free 槽位，登记 stage、holder_id（请求run_id）、lease_token（每次租用新代次）、目标sha、心跳和到期时间；Git操作在事务外执行。创建失败也保留隔离行，防止并发超过上限。所有创建/检出/移除/恢复先登记 runtime_operations intent，完成后写回执。路径须精确属于本池，实际顶层与git common dir须匹配，且为detached；不跟随伪造路径或符号链接。检出前工作区必须干净；用 `git checkout --no-overwrite-ignore --detach <sha>`，不覆盖忽略文件，不使用force/reset/clean。

租约时限至少为配置最长阶段运行时限加300秒（默认下限86400秒）。心跳不改变持有者；恢复必须匹配slot_id、holder_id与lease_token，旧代次不能释放新代次。超时不是停止证据：只有请求终态、持有进程确认已结束且Plus turn已确认终态才能自动释放；未知Plus状态保留租约并进入待处理，不在活动会话下清理。Codex Desktop原生consumer不以MCP/claim短进程退出或submit成功作为停止证明，未知时同样保留租约。槽位自身`worktree-slot:<slot_id>`、holder或session关联的runtime_operations仍为prepared/unknown时，不得自动或直接释放，也不得经解除隔离绕过；保留现场并暴露operation_unconfirmed。手动解除隔离经核心命令，要求confirm=true且重新通过同一守卫；不提供强制丢弃接口。

### 5.29.2 产物与脏快照

规划正文、proposal、转录与 `.pi/plan/*.md` 等证据先保存到plan_root、runs或executor-sessions，重规划继续读取持久任务分支并冻结到planner_inputs，不依赖槽位旧路径。manual_branch已有交付目录规则不改变；需要隔离的脏规划仍从被冻结分支状态构造槽位。

正常干净释放只删除未跟踪的系统产物 `.kaneopilot-plan-material/` 与 `.pi/plan/*.md`；不得删除tracked文件、符号链接或目录逃逸。删除后 `git status --porcelain` 为空才置free，否则quarantined，不清未知改动。脏快照是已知输入而非未知改动：populate前后持久登记完整冻结基线（review_snapshot材料与哈希）；释放前删除上述产物后，以verify_root核对HEAD、diff、原始字节/模式、精确新增文件；完全匹配才对该冻结diff执行反向apply并仅移除冻结新增文件，恢复到原HEAD的干净树。恢复失败隔离，保留现场；不得使用reset/clean。Review仍满足HEAD=snapshot.head，快照路径不同于执行路径，源分支后续移动不影响已租槽位。

### 5.29.3 只读审计与确认回收

`worktree-audit {}` 报告目录类别、引用/未知原因、脏状态、进程/活动状态与分配大小；只读。`worktree-gc {dry_run?:true, retention_days?:7}` 默认只预览。执行要求 `dry_run:false, confirm:true, paths:[...], expected_audit_sha256:<预览指纹>`，每批至多10项；重新审计后候选身份、HEAD、状态、引用与交付状态必须仍匹配，不扩大确认范围。每批删除前用SQLite backup API备份一致的DB（含WAL内容），核验integrity_check并记录备份路径与哈希；逐项先intent，再git worktree remove（无force），再prune并更新回执。

请求终态为succeeded/failed/cancelled；其它状态含未知均视作引用。save_state active/trashed/purged仅是显示/保存状态，均不证明目录可删。GC结合requests、execution_rounds、tasks与经task_id/run_id/backend身份验证的runs/job.json、payload/持久计划材料路径；不得以requests/tasks行缺失认定无引用。历史读取仍依赖目录或无法证明终态时只报告并保留。历史Codex Desktop也须同run的可信owner停止证明；只有请求终态和短CLI进程消失不足以回收，已知为Desktop却缺job时仍保护。执行branch还须当前执行run的终态请求链、同run可信job停止、终态execution_round与交付commit的run/SHA一致，以及交付已合并、来源分支已清理；历史执行UUID默认保留，待独立证据核实。不能归类、非git、runs/executor-sessions/planner_inputs只报告。脏存量只在迁移或可恢复备份后再考虑；不在GC中强制删除。

### 5.29.4 项目记忆登记（M4）

核心仅写SQLite，`<app_support>/worktree-slots.json` 是派生只读投影，以临时文件+原子替换在状态变化后重建，失败明确报告且不回滚伪造成功。格式：`{schema_version:1,updated_at,worktrees_root,slots:[{slot_id,path,repository,git_common_dir,repo_key,state}]}`。repository为该common-dir所属真实主仓库根；登记路径须为pool下精确slot路径。Plus另案读取此投影，验证实际Git common-dir一致后，仅解析记忆owner到真实项目，保持执行cwd与工作范围在槽位；启动与每轮刷新一致，plan/goal禁止记忆写入。不能把真实主根作为槽位会话额外工作根注入提示词。KaneoPilot不写Plus SQLite、不改分组、不调用projects.remove。Plus实现/重装与真实模型验证另行关卡，不因生成投影声称记忆已接通。
