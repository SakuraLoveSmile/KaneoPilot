# KaneoPilot 执行端契约 v5（全自动反馈流水线）

现行冻结契约以 protocol §5.27 为准。App 每 20 秒读取 tick/status，intake 每 300 秒；dispatch/monitor/delivery 均为确定性 Python，不调用控制模型。controller_backend 只作旧配置展示，不作为保存或运行门。

## v5 现行路由与结算

本节覆盖上文 v1–v4 控制会话描述；完整接口、载荷和失败语义见 protocol §5.27。dispatch/monitor 不再调用模型，不看 controller_backend，经 cli.run 驱动确定性流程；monitor 使用 task.run_id 观察执行，advance-task 不传 request_id，被动同步 in-review/in-review 与 done/done 均允许。验证缺失或最后一次失败进入 verification_unconfirmed。

intake 每 300 秒，只接收开启且就绪项目的 To Do，保留人工筛选前提；自动确认由 tick 执行。Review 统一结算：手动任务保留跨任务修复，自动化任务 changes_requested 在同任务分支最多修复 2 轮并受段预算约束，pass 只登记。delivery 提交后 Review，pass 后推送/开 PR/评论，合并才 done，不自动 merge。Plus 会话 create/configure/prompt，不挂接 task grant；状态全部由 runner 经核心命令完成。


---

以下 v4 材料只描述保留的旧控制器入口，**不得用于现行路由、自动化任务修复或完成判定**。

# 历史 v4 角色契约（仅供旧入口兼容与故障回看）

> Goal 报告驱动的完成判定与监控分支见 §6 和 protocol.md §5.23.10。

执行端每 15 分钟检查所属阶段，错开起始分钟；无请求立即结束。承载方式自 v1.6 起为 **KaneoPilot App 内置 ExecutorScheduler**（App 存活期间定时拉起 `python3 -m kanpilot.executor_runner --stage <stage>`，runner 渲染 `core/executor_prompts/<stage>.md` 后以 `pi -p --mode json` 执行一轮领取-执行-汇报循环）。业务周期（评审）由核心 settings 单独配置——领取频率不等于反复执行全部任务。
**v4（T6）起四个模型调用点各自可选后端**（protocol §5.24）：计划生成 `planner_runner`（payload `planner_backend=codex|pi_desktop`）；dispatch/monitor 控制端 `executor_runner --stage dispatch|monitor`（`exec_config.controller_backend`）；review 控制端 `executor_runner --stage review`（`review_config.backend`）；任务执行仍由 `exec_config.backend` 决定 worker。选 `pi_desktop` 时该轮经 Plus 本机 MCP 控制会话（计划/Review 为 `mode=plan`，控制端为 `mode=agent`）运行，**不再**拉起 `codex exec` 或 `pi -p`；队列、租约、审批与状态写入仍全在 KaneoPilot，人工启动规划、确认计划后执行、Review 后完成/修复的流程不变。

**v2 起 plan 阶段分流**：规划请求由独立 **规划适配器**（`python3 -m kanpilot.planner_runner`；`kanpilot.codex_planner`，v4 另加 `kanpilot.pi_desktop_planner`）执行，不走 15 分钟定时表。规划请求只在用户经规划面板（`fetch-todos`→`prepare-plan-input`→`start-plans`）明确授权后存在；`start-plans` 成功即唤起 runner，App 启动/恢复时 runner 补领**已授权**的排队规划请求。dispatch/monitor/review 仍为 ExecutorScheduler 定时表。**v3 起规划请求携带 `planner_method`**（`builtin`/`spec_plan`，protocol §5.23.2）；**v4 起另携带 `planner_backend`**（`codex`/`pi_desktop`，protocol §5.24.3）——领取时按 payload 快照选择执行路径，不自行改方式或后端；Plus 分支只支持 `builtin`。

## 1. 执行端与调度

| 执行端 | stage | 触发分钟（每小时） | executor id |
|---|---|---|---|
| dispatch | dispatch | 4, 19, 34, 49 | `kaneo-dispatch` |
| monitor | monitor | 8, 23, 38, 53 | `kaneo-monitor` |
| review | review | 12, 27, 42, 57 | `kaneo-review` |
| plan（v2 起不在定时表） | plan | 手动触发：`start-plans` 成功后立即唤起；App 启动时补领已授权队列 | `kaneo-planner`（Codex 适配器） |

- **调度位点**：App 内 DispatchSource 定时器；**常驻菜单栏，关窗继续，退出停止**（可配置登录项）。启用开关在设置页「执行端调度」。
- **单飞**：触发前先 `executor-status`；该 stage `running` 非空（pid 存活）→ 跳过本轮。
- **运行记录**：runner 写 `settings.executor_runs[stage]`（pi 分支 `pid/started_at/log/exit_code/finished_at`；v4 的 `pi_desktop` 控制轮另写 `backend/session_id/turn_id/invocation_id/config/send_state`）；事件流落 `<app_support>/executor-logs/<stage>-<ts>.log`；pi 会话落 `<app_support>/executor-sessions/<stage>/`，Plus 控制轮落 `<app_support>/executor-sessions/<stage>/<invocation_id>/`（脱敏 job/events）；控制 `invocation_id` 与任务执行 `run_id` 不可混用。
- 每轮开始：`kanpilot claim --stage <stage> --executor <id>`；`claimed:false` → 报告原因并结束。`claimed:true` → 按 kind 流程执行；长操作每 < lease_seconds 调 `heartbeat`；结束调 `report`；然后继续 `claim` 直到无请求。
- runner 拉起参数：`pi -p --mode json --session-dir … --no-extensions --no-context-files --no-skills`；超时 `settings.executor_timeout_seconds`（缺省 1800s）→ 终止进程组。

## 2. 各 stage 流程

### plan（v2：Codex 规划适配器，executor id `kaneo-planner`）

- **不再有 `plan-scan`**：不自动枚举看板、不自动建请求。规划请求只来自规划面板手动授权（`payload_json.manual_authorized=true` + `input_id` + `planner_input_sha256`）；未授权请求 claim 一律拒绝（`unauthorized_plan`）。
- runner 触发：`start-plans` 成功后立即唤起；App 启动/恢复时检查已授权 plan 队列，有则唤起。同一时间只跑一个规划轮次（核心 claim 串行保证）。
- 每轮：`claim --stage plan --executor kaneo-planner` → `claimed:false` 结束；`claimed:true` → 按 payload 的 `planner_backend` + `planner_method` 分流：`codex`+`builtin` 按 §5.22.3 调 `codex exec`（`-s read-only`、`--json`、`-i <图片>`、`--output-schema`、`--ephemeral`、`--ignore-user-config` 等）；`codex`+`spec_plan` 按 §5.23.2（不带 `--ignore-user-config`，`$spec-plan` 引用技能）；`pi_desktop` 按 §5.24.3 用本轮固化五字段建 `mode=plan` 会话（发送前落 `send_state=unknown`，读 `kind=plan` proposal 后本机 `reject` 关闭，不发 approve、不生成执行 turn、不调 `pi_project_open`），**不**拉起 `codex exec`/`pi`。cwd=任务 repository（只读）或规划 worktree，材料目录只读挂载；心跳 ≤ `min(lease_seconds/3, 60)`s。
- 输出契约（schema 强制）：`ready`（非空 `plan_markdown` 且无未决 questions）→ 核心落盘计划文件 + `publish-plan`（request_id/run_id/planner_input_sha256 复核）→ task `ready` + `plan_execution_hold`；`needs_input`（questions 非空/草稿）→ 保存草稿与问题，`report needs_user` + `failure_code=reported`；进程失败/超时/空输出/不合规 → 保存 JSONL+stderr 证据，`report failed`（或 `needs_user`+`plan_failed`），**不自动重试**。
- 元数据（task_id/plan_sha256/planner_model/输入哈希）由核心按真实输入生成，不信模型自报；`plan_markdown` 经计划规范校验（frontmatter 必填、中文自包含）。
- review_fix 计划：只处理核心登记的 fix task（payload 含 review_key 等）；模型 `gpt-5.6-sol/xhigh`，其余同 standard；publish 成功时核心把修复计划写回原评审任务 `extra_json.repair_plan_*`。
- 规划与派发分离：publish 成功后 task 处于 `待确认执行`（hold）；runner 不再做 dispatch，执行授权由桌面「确认并安排执行」完成（protocol §5.22.4）。

### dispatch（executor id `kaneo-dispatch`）

- **v3 起任务执行后端由 `exec_config.backend` 决定**：`pi`（默认）或 `pi_desktop`（protocol §5.23.3）。**v4 起 dispatch/monitor 控制端来源 = `exec_config.controller_backend`**（protocol §5.24.4）：`pi`（缺省）继续继承本机 PI 默认会话设置并以 `pi -p --mode json` 运行；`pi_desktop` 用 `exec_config.pi_desktop` 五字段建独立 `mode=agent` 控制会话，宿主环境经 runner 生成的固定包装器**显式**传入（`KANPILOT_DB`/`KANPILOT_APP_SUPPORT`/`KANPILOT_PLAN_ROOT`/`KANPILOT_REPOS_ROOT`/`KANPILOT_LEGACY_ROOT`/`KANEO_CREDENTIALS_PATH` 等），不依赖继承环境。不用 Computer Use、不截图、不核验附件/发送按钮。任务执行配置由调度台设置页管理，claim 时固化进轮次快照。
- 流程：
  1. `claim --stage dispatch` → context 含 `backend`、`exec_config` 快照、plan_path/repository/session_marker/instruction/original_todo_slug；核心完成计划哈希、来源快照、远端状态、review 交付与**所选后端可用性门**（§5.17.3 pi 分支 / §5.23.3 pi_desktop 分支）校验，失败 `validation_failed` 原文转述、不自行换模型/agent/后端。context 无 backend 字段（历史遗留）→ 简报交人工结束。
  2. `dispatch-prepare`（证据按后端分支：pi 为 session_marker/repository/backend="pi"/config=快照；pi_desktop 为 session_marker/repository/backend="pi_desktop"/config=快照）。
  3. `dispatch-begin-send`（远端 To Do→In Progress，核心复验；remote_conflict/unavailable → dispatch-fail unknown）。
  4. `dispatch-start`：按轮次固化配置而非当前设置复核可用性 → 启动意图落盘 → worktree → 拉起该轮 worker（backend=pi 拉起 `kanpilot.pi_worker`；backend=pi_desktop 拉起 `kanpilot.pi_desktop_worker`，protocol §5.23.3）→ 等 session 事件。`deduplicated:true` 绝不重复启动；仅有启动意图且无启动证明时为 conflict，交人工处理，不声称已开始；`status:"starting"` 轮询 get-session ≤5 分钟取 session_id。pi_desktop 轮次另需 turn_id（持久终态查询）。
  5. `dispatch-confirm`（证据按后端分支：pi 为 session_id/protocol_seen/config；pi_desktop 为 session_id/turn_id/config）：核心核验 events.jsonl session 事件与快照 → running；成功即结束本项，**不等执行完成**。
  6. 异常 `dispatch-fail --send-state not_sent|unknown --reason <短原因>`：进程从未拉起且远端未迁移 → not_sent；已 dispatch-start 或迁移结果不明 → unknown（保留启动意图与恢复信息）。
  7. 每 ≤10 分钟 `heartbeat`；单操作者仍由核心 dispatch 串行位保证（`stage_busy` 结束）。

### monitor（executor id `kaneo-monitor`）

- **观察经 kanpilot 会话读取（get-session + session-messages），不做桌面 UI 操控**；被观察轮次恒取 **task 当前执行轮次**（`get-task` → `task.run_id`），**不是**本端 monitor 请求的 run_id。旧后端（dsh/headless/ATIF）轮次不可读——`get-session` 返回 `unavailable` + detail「旧后端会话已弃用」，按读不到处理（report failed / observation_blocked），不报执行失败。
- `kind=monitor`：
  1. `recheck` 只读核验远端：已 done/in-review → `advance-task` 被动同步对（v1.2）→ `report succeeded`；**不得** in-progress→done 直推（无评审 pass 核心拒绝）。
  2. 仍 in-progress → 读 `get-session`（`run_id=task.run_id`；round_detail：terminal/exit_code/run_status/observe_pid/observer_alive/detail）+ `session-messages`（消息与工具输出），轮次完成判定三证据**同时满足**（§5.18.4；pi_desktop 轮次的 terminal 由 worker 经 `pi_turn_get` 持久终态+记录完整性写入，§5.23.3）——(a) 终止事件 outcome=completed、(b) exit_code=0 且 run_status=finished、(c) 验证证据（计划 verify 块每条命令在本轮最后一次工具结果的整数退出码为 0；子 agent 结束/进程退出/模型自称完成任一单独不算）→ `advance-task --target in-review` → `report succeeded`。仍在跑 → heartbeat + report succeeded（progress 记摘要）。明确异常（failed/非 0 终止/超时/截断/身份不符/send_state=unknown）→ `report needs_user` + `failure_code=execution_failed`（附 terminal/detail），**不自动重新提交、不重建轮次、不重复 agent_prompt**。
  2a. **观察者缺席先续接**（§5.23.7；pi_desktop 轮次）：`round_detail.backend=="pi_desktop"` 且（`round_detail.observer_alive==false` 或 `round_detail.detail` 以 `heartbeat_failed:` 开头）→ `round-observe {task_id, run_id=task.run_id, executor:"kaneo-monitor"}` 只读续接观察进程（绝不重发 prompt/新建会话，取证靠 session 事件，不靠 `send_state`），再重新读取；返回 `deferred|exited|starting` 时按第 3 条处置（report failed + observation_blocked），下轮再试。`observer_alive==true` 不得重复拉起。
  2b-2. **Goal 契约审批门（§5.23.9）**：pi_desktop 轮次以 Goal（契约）模式派发——规划轮次先协商目标契约，worker 本机代批（留痕 `job.goal_approval` + `system` 事件）后宿主在同一会话开启执行轮次。审批门期间轮次保持 running：monitor **不得**把 Plus 侧规划轮次的 completed 当作执行完成，也不得重发 prompt/重建会话；终态仅认 `round_detail.terminal`。终态 detail 含 `goal_contract_missing`/`goal_approval_failed`/`goal_execution_unstarted` 时按 `report needs_user` + `failure_code=execution_failed` 交人工，不自动重试。
  2b. **后续轮次接续与解除误报（§5.23.8）**：PI-Desktop 在瞬时模型错误（如 `EMPTY_MODEL_RESPONSE`）后会在**同一会话**自动重开新 turn，观察进程只读接续（`round_detail.turn_adopted` 记审计，**绝不重发 prompt**）；续接返回 `reopened:true` 表示核心已解除由该误报产生的 `execution_failed` 阻塞（任务回 `running`）——此时按第 2 条「仍在跑」分支继续判定，不得沿用旧 failure 文案重复 report needs_user。
  3. 会话读不到 → `report failed`（note 记 observation_blocked 与 detail），不改状态。
  4. 远端已 done 的任务由 `sync-tasks` 观察同步归档；monitor 不经 advance-task 直接 done。

### review（executor id `kaneo-review`）

- `kind=review`：评审仓库实现 vs 计划与需求 → 报告写 `<APP_SUPPORT>/reports/<task_id>/<run_id>.md`（v4；旧 `KaneoReviewer/reports/...` 路径仅历史） → `register-review` →
  - `pass`：`advance-task --target done`（`expected_current=in-review`）→ `report succeeded`。
  - `changes_requested`：**先** `create-review-fix`（dedupe）取得 `fix_task_id`/`source_sha256`，**再** `register-review(changes_requested, review_key, fix_task_id, fix_task_source_sha256)` → `report succeeded`；fix 任务停在 `needs_plan`，等用户 `prepare-plan-input`/`start-plans`，**不**自动规划。
  - `blocked`：`register-review blocked` → `report needs_user` + `failure_code=review_blocked`（核心自动记 review_blocked，可省略）。
- **登记身份与周期保护（v4，protocol §5.24.5）**：新自动 Review 调用必须带 `request_id` + `run_id` + `expected_execution_run_id`；核心在事务内核对 stage/task_id/status=claimed/run_id/未过期 lease 与评审周期，失配 → `stale_run`/`conflict`，不写报告登记或 pass。无身份的旧手动调用保留兼容，但不作为新自动链路入口；`create-review-fix`/`advance-task` 同样传身份。部分成功恢复先读 `get-task`/`get-request` 核对已确认检查点，`report` 响应丢失不重放非幂等调用。
- **Review 环境（v3/v4，protocol §5.23.4/§5.24.5）**：来源 = `review_config.backend`——`pi` 分支按 `review_config.mode`（`inherit` 继承本机 PI 默认会话设置；`custom` 把 `{provider, model, reasoning_effort}` 固化为本轮 pi 参数）；`pi_desktop` 分支用 `review_config.pi_desktop` 五字段建 `mode=plan` + `permissionMode=ask` 控制会话，只在 `completed` + 完整转录后严格解析 `{conclusion, report_markdown, fix}` JSON（`priority` ∈ `no-priority|low|medium|high|urgent`），意外 proposal 只 `reject` 关闭并记 `review_blocked`，不按其 markdown 登记 pass。保存只影响**下一次** review 启动；固定提示词仍来自 `core/executor_prompts/review.md`，不复用任务执行 `agent_preset`；评审修复计划属规划环境。

## 3. 不变量（执行端必须遵守）

- 一切状态读写只经 kanpilot 命令；不直接改 DB、不复活旧文件锁、不写替代管理脚本。
- 无请求立即结束；不轮询刷屏。
- 远端 Kaneo 写只能经核心命令（dispatch-begin-send、advance-task、create-review-fix）；批量远端读取经 bridge。
- 派发证据字段全部来自 context/协议事件观察；不推测、不修饰。
- 关闭/中断不意味着操作结束：已 claimed 请求由租约派生失联，人工 resolve 才释放。
- 暂停只影响新领取；执行端不得因全局暂停中止已 running 的轮次。
- **归档/回收站是调度台本地行为（protocol v1.3）**：执行端无感——`sync-tasks` 整体跳过 `save_state != active` 的任务（不重新导入看板），`tick`/`claim` 不为恢复后暂停（`auto_advance_paused`）的任务建/发请求；观测异常（`observation_blocked`）在 monitor 观测成功时由核心自动清除，执行端无需额外动作。
- **`purged` 终态（v2）**：永久从列表移除，历史与文件保留；`sync-tasks` 不得复活 purged 任务，所有写入口对 purged 目标拒绝。执行端对 purged 无特殊处理（不会领到 purged 相关请求）。
- **规划手动门控（v2）**：执行端不创建/不授权 plan 请求；planner runner 只处理 `manual_authorized` 请求；`plan_execution_hold` 未解除前 dispatch 请求不可领取——不绕过、不替用户确认。

## 4. 切换步骤（主 agent 执行；v1.6 pi 接入）

1. **先备份**：`kanpilot doctor` 全绿 → `kanpilot migrate`（自动整库备份 → 导入 → 校验计数；逐级备份在 `<app_support>/backups/`）。
2. **确认执行环境**：`kanpilot get-exec-config` / `exec-capabilities` 四项 checks 全 ok（pi 可执行、models.json 可读、agent.md 预设可用、凭据就绪）；规划端另查 Codex CLI 可执行 + `~/.codex/auth.json` 登录态就绪（只查就绪，不读凭据值）。不满足先补环境，**不改模型/agent 凑合**。
3. 停旧流程新领取：Codex automation 四条旧定时任务 `status → DISABLED`（历史存档见 automation/cutover.md）；**不取消**任何已 running 轮次、不重发 send_state=unknown 的任务。
4. 启用 App 内置调度：设置页「执行端调度」打开总开关；确认 `executor-status` 返回四 stage 结构。
5. 验证执行端：各 stage 手动「立即运行」，确认无请求即退/有请求按新流程正确处理；派发首项走完 prepare→begin-send→start→confirm 全链。v3：backend=pi_desktop 时另验证连接文件就绪、`pi_turn_get` 持久终态与 `send_state=unknown` 不重复发送。
6. 恢复自动推进：确认首轮回合无异常后视为接入完成。
7. 旧 skill 与旧 automation 定义原样保留只读备查；旧 dsh/devin 后端代码已从 core 移除，历史轮次会话读取返回「旧后端会话已弃用」。

## 5. 验收检查点

- 四个执行端按错开分钟触发、无请求即退；`executor-status` 显示最近一次运行与退出码。
- 桌面「安排下一步」→「已排队」；执行端领取+心跳 →「已开始/派发中」；窗口关闭重开状态一致。
- 发送不明任务在桌面呈现证据与三个专用按钮；任一决议后才生成对应请求。
- 新派发轮次 backend=pi：dispatch-start 幂等（同轮次不创建第二个进程），monitor 三证据判定完成；旧后端轮次显示弃用占位不崩溃。
- 会话回收站：删除/恢复闭环，禁止条件（执行中/失联/发送不明）给出中文原因，重启后状态保持。
- running 任务迁移后由 monitor 请求接续观察，不靠旧锁。
- v2 追加：规划面板三步流闭环（fetch-todos 只读 → prepare-plan-input 材料目录 → start-plans 入队 → Codex runner 领取 → publish-plan → `待确认执行` → 确认并安排执行）；未授权 plan 请求不可领取；`purged` 终态（任务/会话仅从回收站永久移除、列表消失、历史与文件保留、不复活）；`get-request`/`list-events` 支撑待处理详情与运行记录翻页搜索。
- v4 追加：`planner_backend=pi_desktop` 的规划轮在 Plus 侧生成计划正文、由核心 `publish-plan` 置 hold，Plus 侧 proposal 被本机 `reject` 关闭且不产生执行 turn；`controller_backend=pi_desktop` 时 dispatch/monitor 无 `pi -p` 子进程，只在 Plus 控制会话中调用核心命令；`review_config.backend=pi_desktop` 的 Review 三结论各自登记正确、`changes_requested` 先 create 后 register；每一步的实际 backend/session/turn/invocation 在设置页与详情可见；连接缺失/工具缺失/型号不在本地目录时明确阻断，不回退 codex/pi。

### v4 历史控制轮的命名 MCP（protocol §5.25）

v4 历史 Plus dispatch/monitor 使用 `executor_prompts/dispatch_mcp.md` / `monitor_mcp.md`，经会话 task grant 领取、心跳、读取、派发和汇报；不得提示模型调用 shell/CLI 包装器。Pi CLI 保留原角色 prompt；历史控制轮只读恢复不重发。规划/Review/执行各自在新会话发送前挂接对应角色工具，只由宿主核心登记最终结果。项目 MCP 或 Plus 会话挂接接口缺失时，发前明确失败而非降级执行。

### 项目映射与快照固化（T1，protocol §5.26）

- 执行端派发及 runner 取决于请求固化的 `project_snapshot`（包含 `project_id`, `project_name`, `repository`, `project_mapping_revision`），不依赖运行时临时查询当前设置，确保已有排队和执行中任务免受外部映射修改干扰。
- 新计划文件按 `projects/<project_id>/` 安全目录写入，并在发布和派发前进行一致性快照核验。


## 6. Goal 报告完成判定与派发命名（v3 补丁，T5/T6）

> v5 新 Plus 派发轮次（自动化或手动）固化 `completion_policy=command_verification`，按 protocol §5.27 的有效 monitor claim、终态、计划哈希和 verify 证据/本轮人工接受记录收口，不调用模型判定。以下报告完成判定只适用于旧 `goal_report` 策略或缺省的历史 spec/job，命名约定继续适用；策略随轮次固化，不读取后来改变的项目策略或人工接管设置。

本节是 protocol.md §5.23.10 在执行端视角的落地口径；字段、身份核对与失败语义以 §5.23.10 为准。

### 6.1 monitor 分支口径（T5）

- **被观察轮次恒取 `task.run_id`**；`execution_run_id`（被观察轮次）与 `monitor_run_id`（本端 monitor 请求自己的 run_id）不可互换。
- `round_detail.backend` 分支：
  - `pi`：维持 §5.18.4 三证据（terminal outcome=completed + exit_code=0/finished + 语义验证证据）不变。
  - `pi_desktop`：**不再**以「发现一条退出码 0 的命令」或会话最后一句作为放行条件。完成判定以 **Plus Host 结构化 Goal 报告**为主材料：
    1. 终态 `round_detail.terminal` 出现且执行终态为 `completed`（`finished` + `exit_code=0`）；
    2. `get-goal-report {task_id, run_id=task.run_id}` 取回经身份/完整性校验的报告；
    3. 报告须 `status=ready` 且 `integrity.kind=structured`（`ready + fallback` **等同没有完成报告**）；执行终态落地后给 Host **最多 60 秒**完成 outbox/报告发布，期间只读查询、**不重发 prompt**；
    4. monitor AI 把报告当作**不可信数据**，逐项比对当前已批准 KaneoPilot 计划验收、Plus Goal 契约、报告的 criteria/steps/checks/evidences、limitations 与 nextSteps，输出 `decision ∈ {complete,incomplete,uncertain}` 与 `criterion_reviews`；
    5. `decision=complete` → `record-goal-evaluation` 登记判定 → `advance-task --target in-review`（带 monitor claim 与 evaluation event ID）→ `report succeeded`。
- **缺报告/兜底报告/报告不完整** → `report needs_user` + `failure_code=execution_failed`（原因写明：无结构化报告 / `integrity=fallback` / AI 判 `incomplete` / 报告自称 `partial|blocked`），**不自动重新派发、不重发 prompt、不重建会话**。
- **报告不可读/损坏或 AI `uncertain`** → 不进入 Review；最多**一次有界重读**后同样交人工，原因须区别于「已验证失败」。临时读取失败**不伪称**没有报告。
- **旧轮次**：升级时仍 `running` 的旧 Plus 轮次若无报告，同样按缺报告规则交人工，**不走旧消息三证据放行**；已在 `in_review`/`done`/`needs_user` 的历史任务不回填、不降级、不改判。
- `goal_report_evaluated` 幂等：同 `task_id`/`execution_run_id`/`plan_sha256`/`report_sha256` 已有判定 → 复用原事件，不重复调用 AI。
- **采集修补**：`pi_desktop_worker` 的 shell 工具名集合须同时接受 `Bash`/`bash`（大小写归一），`exitCode` 保真透出；该修补服务于报告证据与 Review 查阅，不单独构成放行条件。
- 提示词 `core/executor_prompts/monitor.md` 必须同时写明 `pi` 与 `pi_desktop` 两个分支，不得把 `backend != pi` 称作旧后端。

### 6.2 dispatch 命名（T6）

- KaneoPilot 在 `dispatch-start` 前复核 Plus 能力时必须确认 `pi_session_create` 工具 schema 暴露 `projectName`（protocol §5.23.10 T6 兼容门）；旧 Plus → 明确阻断显示「版本缺少工作区命名能力」，**不静默**派发 UUID 名称。
- 新轮次 `spec.json` 固化展示快照 `workspace_display_name`/`session_display_name`（来自 `task.project_name`/`task.title`）；worker 在 `pi_session_create` 携带 `{projectPath, projectName, title, mode:"goal", …}`。
- 显示名只用于展示：派发身份仍以 `session_marker`/`task_id`/`run_id`/`session_id`/`turn_id`/计划哈希为准；不得用于去重、路径生成、权限判断或完成判定。
- PI CLI 后端、已派发的 spec/job/session、Plus 用户手工重命名与历史轮次不受影响。
