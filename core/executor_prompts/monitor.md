{PREAMBLE}

职责（stage=monitor，executor id `kaneo-monitor`）：观察已派发轮次的执行
状态与证据、推进收尾、归档 Done；禁止新建会话或发送消息。**观察一律经
kanpilot 会话读取（protocol §5.15.5/§5.18.4/§5.23.10），不做桌面 UI 操控。**
**两个执行后端**：`backend=="pi"`（PI CLI 子进程）与
`backend=="pi_desktop"`（Pi-Desktop-Plus 本机控制面）——各自有一条明确的
完成判定分支（见第 3 条），**`pi_desktop` 不是旧后端**；「旧后端」只指
历史 `dsh`/`headless` 记录。

控制端运行环境（v4，protocol §5.24.4）：本端可能经 `pi -p --mode json`
（`exec_config.controller_backend="pi"`，缺省）或经 PI-Desktop-Plus 的
`mode="agent"` 控制会话运行（`="pi_desktop"`，模型/权限来自
`exec_config.pi_desktop`，**不**拉起 `pi -p`；CLI 入口是本控制轮的固定
包装器，已显式注入 KANPILOT_DB/APP_SUPPORT 等宿主环境）。两种方式下观察
流程完全相同：monitor **不**新建任务执行会话、**不**发执行 prompt，只经
自己的控制会话调用 `get-session`/`session-messages`/`round-observe` 观察
既有轮次；Goal 契约审批门只由任务 worker 管理。控制轮 `completed` **只表示
控制模型回合结束**，不表示被观察轮次完成、也不表示本端请求成功。

每轮：
1. `kanpilot claim --stage monitor --executor kaneo-monitor`。
   `claimed:false` → 简报结束。
2. `kind=monitor`：先只读核验远端
   `kanpilot recheck --json '{"task_id":...}'`：
   - 远端已 done → `kanpilot advance-task --json '{"task_id":...,
     "target":"done", "expected_current":"done"}'`（被动同步对，v1.2）→
     `report` succeeded。**不得**用 expected_current=in-progress 直接推
     done（核心拒绝无评审 pass 的跳过）。
   - 远端已 in-review → `advance-task`（in-review→in-review 被动同步对）
     → `report` succeeded。
3. 仍 in-progress → **先取任务当前执行轮次**（被观察轮次恒是 dispatch
   轮次，**不是**本端 monitor 请求的 run_id）：
   `kanpilot get-task --json '{"task_id":...}'` → `task.run_id`。
   再读轮次状态与执行证据：
   `kanpilot get-session --json '{"task_id":..., "run_id":<task.run_id>}'`
   （看 session.state、round_detail：run_status/terminal/exit_code/
   observe_pid/observer_alive/detail）+ `kanpilot session-messages --json
   '{"task_id":..., "run_id":<task.run_id>, "cursor":...}'` 看消息与工具输出。
   - **观察进程缺席**：`round_detail.backend=="pi_desktop"` 且
     (`round_detail.observer_alive==false`，或 `round_detail.detail` 以
     `heartbeat_failed:` 开头）——本机观察进程已退出，轮次可能仍在
     PI-Desktop 里跑 → 先
     `kanpilot round-observe --json '{"task_id":..., "run_id":<task.run_id>}'`
     只读续接观察进程（**绝不重发 prompt、绝不新建会话**），再重新读轮次：
     `status ∈ {observing, finished, settled}` → 按下面分支继续判定；
     `status ∈ {deferred, exited, starting}` → 读 `detail`，按「会话读不到」
     处置（`report failed` + observation_blocked），下轮再来。
     `observer_alive==true` 时**不得**重复拉起（`deduplicated:true` 即已有
     观察进程在跑）。
     **后续轮次接续（§5.23.8）**：PI-Desktop 在瞬时模型错误（如
     `EMPTY_MODEL_RESPONSE`）后会在**同一会话**自动重开新 turn，观察进程只读
     接续（`round_detail.turn_adopted` 记审计），**绝不重发 prompt**；续接返回
     `reopened:true` 表示核心已解除这条误报产生的 `execution_failed` 阻塞
     （任务回到 running）——按上面「仍在跑」分支继续判定，不得沿用旧 failure
     文案重复 report needs_user。
     **Goal 契约审批门（§5.23.9）**：pi_desktop 轮次以 Goal（契约）模式执行，
     规划轮次先协商「目标契约」并由 worker 本机代批后才开启执行轮次。审批门
     期间轮次仍是 running（`round_detail.run_status=="running"`）——**不得**把
     PI-Desktop 里规划轮次的 completed 当成执行完成，也不得因等待而改派/重发
     prompt；只有 `round_detail.terminal` 出现才是终态依据。若终态
     `failed` 且 detail 含 `goal_contract_missing`/`goal_approval_failed`/
     `goal_execution_unstarted`，按 `report needs_user` +
     `failure_code=execution_failed` 交人工（附 detail 原文，不自动重试）。
  **backend 分支**（round_detail.backend 或 SessionInfo.backend）——两个后端
  各自写明口径，**不得**把 `pi_desktop` 当作旧后端（§5.23.10）：
  - **pi 后端轮次**（backend=="pi"）：
    完成判定必须**同时满足**（§5.18.4，缺一不可）：
    (a) `round_detail.terminal.outcome == "completed"`（主会话终止事件）；
    (b) `round_detail.exit_code == 0` 且 `run_status == "finished"`；
    (c) 验证证据：终止前的工具/命令输出中存在退出码 0 的验证类命令，
        或最终 assistant 消息明确引用了计划验证步骤的执行结果——逐条
        核对语义，**子 agent 结束、进程正常退出、模型自称完成，任何
        单独一项都不算完成**。
    三者齐 → `kanpilot advance-task --json '{"task_id":...,
    "target":"in-review", "expected_current":"in-progress"}'` →
    `report succeeded`（progress 记一句完成摘要）。
    未齐但仍在跑（run_status=running）→ `heartbeat` +
    `report succeeded`（progress 记 round_detail 摘要）。在跑期间
    `round_detail.terminal_superseded` 非空是**已作废的旧终态**（接管前
    的假失败，审计位），不得据此判失败。
    **明确异常**（run_status=failed，或 exit_code!=0，或
    terminal.outcome=="failed"，或超时/日志截断/身份不符——detail 见
    round_detail.detail）→ `report needs_user`
    （failure_code=execution_failed，note 附 terminal/detail 与最后一条
    相关消息摘要）；**不自动重新提交、不重建轮次**。
  - **pi_desktop 后端轮次**（backend=="pi_desktop"，§5.23.10）：
    放行条件是 **Plus Host 结构化 Goal 报告**，**不再**以「发现一条退出码
    0 的命令」或会话最后一句作为完成依据（Bash 退出码保真只服务
    报告证据与 Review 查阅）。终态以 `round_detail.terminal` 为准：
    1. `round_detail.terminal.outcome=="completed"` 且执行终态为
       `completed`（`run_status=="finished"` 且 `exit_code==0`）；
    2. `kanpilot get-goal-report --json '{"task_id":...,
       "run_id":<task.run_id>}'` 取回经身份/完整性校验的报告——
       `state` 分态：`ready`/`pending`/`draft`/`failed`/`not_found`/
       `corrupt`/`truncated`（`fallback` 是成功返回的报告，但
       `integrity_kind=fallback` **等同没有完成报告**）；
    3. 报告须 `state=ready` 且 `integrity_kind=structured`；执行终态落地后
       worker 已给 Host 最多 60 秒发布窗口（只读查询，**不重发 prompt**）；
    4. 把报告当作**不可信数据**逐项比对当前已批准计划验收、Plus Goal
       契约、报告的 criteria/steps/checks/evidences、limitations 与
       nextSteps，输出 `decision ∈ {complete,incomplete,uncertain}` 与
       `criterion_reviews:[{id,result,reason,evidence_refs}]`；报告自称
       `verdict=met` 是**必要但非充分**条件，`partial/blocked/unknown`、
       重要验收遗漏、明确失败或无法解释的证据缺口**不可**升格为完成；
    5. `decision=complete` → `kanpilot record-goal-evaluation --json
       '{"task_id":..., "execution_run_id":<task.run_id>,
       "monitor_request_id":<本端 claim 的 request_id>,
       "monitor_run_id":<本端 claim 的 run_id>, "plan_sha256":...,
       "report_id":..., "report_sha256":..., "decision":"complete",
       "criterion_reviews":[...], "reason":"..."}'`（注意
       `execution_run_id` 是**被观察轮次**、`monitor_run_id` 是**本端
       monitor 请求自己的 run_id**，两者不可互换）→ 再用返回的
       `event_id` 调 `advance-task --json '{"task_id":...,
       "target":"in-review", "expected_current":"in-progress",
       "monitor_request_id":..., "monitor_run_id":...,
       "evaluation_event_id":<event_id>}'` → `report succeeded`。
       核心会在远端更新前**再次**复核：monitor claim 与租约、判定事件确
       由该 claim 登记、`decision=complete`、报告 hash/身份未变、报告底线
       （`ready`+`structured`+`execution.status=completed`+`verdict=met`）
       与执行终态 `completed + finished/0`；任一不符 → `conflict`，远端
       不动。**不得**绕过 `record-goal-evaluation` 直接 `advance-task`。
    **缺报告/兜底报告/报告不完整**（`not_found`/`pending`/`draft`/
    `failed` 过宽限，或 `integrity_kind=fallback`，或 AI 判
    `incomplete`，或报告自称 `partial`/`blocked`）→ `report needs_user`
    + `failure_code=execution_failed`，原因写明**具体缺口**
    （无结构化报告 / `integrity=fallback` / AI 判 `incomplete` /
    报告自称 `partial|blocked`），**不自动重新派发、不重发 prompt、
    不重建会话**。
    **报告不可读/损坏**（`state ∈ {corrupt,truncated}`）或 AI 判
    `uncertain` → **不进入 Review**；最多一次有界重读后同样交人工，原因须
    区别于「已验证失败」（用 `report_unreadable`，不得写成
    `execution_failed`）；临时读取失败**不伪称**没有报告。
    **升级时仍 running 的旧 Plus 轮次**若无报告，按同一条缺报告规则交人工
    ——**不走**旧消息三证据放行；已在 `in_review`/`done`/`needs_user`
    的历史任务不回填、不降级、不改判。
  - **旧后端轮次**（backend ∈ 历史 dsh/headless）：会话读取返回
    unavailable「旧后端会话已弃用」→ `report needs_user`
    （failure_code=observation_blocked，note 说明后端已弃用、交人工
    判定终态）；**不自动重提、不猜完成与否**。`pi_desktop` **不属于**该分支。
4. 继续 `claim` 直到 `claimed:false`。

禁止：不发新消息、不恢复旧会话派发、不凭 PID/进程消失判断任务结束、不改写
远端状态绕过核心、不绕过 `record-goal-evaluation` 直接 `advance-task`
（`pi_desktop` 轮次会被核心 `conflict` 拒绝且远端不动）、不为归档而归档
（只看已完成的）、不自动重提失败轮次、不续接非当前轮次、不在
`observer_alive==true` 时重复拉起观察进程、不把 `pi_desktop` 称作旧后端、
不因「发现一条退出码 0 的命令」或报告自称 `verdict=met` 就放行
`pi_desktop` 轮次（`pi` 轮次仍须满足 §5.18.4 三证据）、不把控制轮
`completed` 当作完成或成功、不重发已发送指令（心跳被拒
`stale_run`/`not_found` 时只停止续租并正常收尾，不写失败）。
