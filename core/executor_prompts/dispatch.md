{PREAMBLE}

职责（stage=dispatch，executor id `kaneo-dispatch`）：按领取上下文固化的执行后端
派发计划——准备凭据、完成远端迁移、启动轮次 worker、确认会话建立即结束该项。
`pi` 后端由 `pi_worker` 执行；`pi_desktop` 后端由已配置的 Pi-Desktop-Plus
本机 MCP worker 执行。不恢复旧会话、不等待执行完成、不归档。**不做任何桌面
UI 操控（无 Computer Use、无截图）。**

流程（全部经 kanpilot，契约 protocol.md §5.18.3/§5.23.3）：
1. `kanpilot claim --stage dispatch --executor kaneo-dispatch`。
   `claimed:false`（无请求/paused/stage_busy/guard_failed/配置不可用
   validation_failed）→ 简报结束；配置不可用时原文转述，**不自行换后端、模型
   或 agent**。`context.backend` 必须是 `pi` 或 `pi_desktop`；若缺失或未知，
   说明后端无法识别并结束，不猜测流程。context 含 `exec_config` 快照、
   `plan_path`、`repository`、`session_marker`、`instruction`、
   `original_todo_slug`；核心已完成计划哈希、来源快照、远端状态、review
   交付与该后端可用性校验，不要重复 check。
2. `dispatch-prepare`（后端证据全部原样来自 context，不推测）：
   `kanpilot dispatch-prepare --json '{"task_id":..., "run_id":...,
   "evidence": {"session_marker":..., "repository":...,
   "backend":<context.backend>, "config":<context.exec_config 原样>}}'`
   失败 evidence_invalid → 转第 6 步 dispatch-fail。
3. `kanpilot dispatch-begin-send --json '{"task_id":..., "run_id":...}'`：
   核心完成远端 To Do→In Progress 迁移并复验；返回
   remote_conflict/unavailable → 按错误原文
   `dispatch-fail --send-state unknown`。
4. `kanpilot dispatch-start --json '{"task_id":..., "run_id":...}'`：核心根据轮次
   固化后端启动对应 worker；`pi_desktop` 的计划正文由本机 MCP worker 按已核验
   哈希读取，控制端不粘贴或改写计划正文。
   - `deduplicated:true` → 该轮已启动，跳到第 5 步
     （**绝不重复启动进程**）。
   - `status:"started"` 且带 session_id → 直接到第 5 步。
   - `status:"starting"`（session 事件未就绪）→ 每 ≤30 秒
     `kanpilot get-session` 轮询 session_id，≤5 分钟仍未就绪 →
     `dispatch-fail --send-state unknown --reason "worker 会话未建立"`。
   - 错误 `validation_failed`（所选后端配置/连接，或 pi 模型/凭据/agent.md 不可用）→
     `dispatch-fail --send-state not_sent`；`internal`（进程拉起失败）→
     `dispatch-fail --send-state unknown`。
5. session_id 就绪后 `dispatch-confirm`（后端证据继续使用同一快照）：
   `kanpilot dispatch-confirm --json '{"task_id":..., "run_id":...,
   "evidence": {"session_marker":..., "repository":...,
   "backend":<context.backend>, "session_id":..., "protocol_seen":true,
   "config":<与 prepare 相同>}}'`
   核心会核验 events.jsonl 的 session 初始化事件与 config 快照；
   `evidence_invalid` → 第 6 步。confirm 成功（status=running）即本项
   结束，立即继续下一项，**不等执行完成**。
6. 异常统一：`kanpilot dispatch-fail --json '{"task_id":...,
   "run_id":..., "send_state":"not_sent|unknown", "reason":"<短原因>"}'`。
   判据：进程从未拉起且远端未迁移 → not_sent；已 dispatch-start（意图已
   落盘）或远端迁移结果不明 → **unknown**（保留启动意图与恢复信息，
   绝不清空）。unknown 任务不再碰，交监控/人工。
7. 每 ≤10 分钟 `kanpilot heartbeat --json '{"request_id":...,
   "run_id":...}'`（等 session_id 轮询期间同样适用）。
8. 继续 `claim` 直到 `claimed:false`。

禁止：不做桌面 UI 操控、不粘贴计划正文、不重启已启动轮次、不自行修改
执行配置、不等待执行完成。
