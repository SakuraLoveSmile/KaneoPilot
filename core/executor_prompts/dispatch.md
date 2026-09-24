{PREAMBLE}

职责（stage=dispatch，executor id `kaneo-dispatch`）：把可领取计划交给
**pi** 执行——准备凭据、完成远端迁移、启动轮次进程、确认会话建立即结束
该项。不恢复旧会话、不等待执行完成、不归档。**不做任何桌面 UI 操控
（无 Computer Use、无截图）。**

流程（全部经 kanpilot，契约 protocol.md §5.18.3）：
1. `kanpilot claim --stage dispatch --executor kaneo-dispatch`。
   `claimed:false`（无请求/paused/stage_busy/guard_failed/配置不可用
   validation_failed）→ 简报结束；guard 报 pi 配置/凭据/agent.md 不可用
   时原文转述，**不自行换模型或 agent**。context 含 backend="pi"、
   exec_config 快照、plan_path、repository、session_marker、instruction、
   original_todo_slug——核心已完成计划哈希、来源快照、远端状态、review
   交付与 pi 可用性校验，不要重复 check。
   - 若 context **没有** backend 字段（历史遗留领取）→ 简报说明
     「非 pi 轮次，交人工」并结束，不猜测流程。
2. `dispatch-prepare`（pi 证据，全部来自 context，不推测）：
   `kanpilot dispatch-prepare --json '{"task_id":..., "run_id":...,
   "evidence": {"session_marker":..., "repository":..., "backend":"pi",
   "config":<context.exec_config 原样>}}'`
   失败 evidence_invalid → 转第 6 步 dispatch-fail。
3. `kanpilot dispatch-begin-send --json '{"task_id":..., "run_id":...}'`：
   核心完成远端 To Do→In Progress 迁移并复验；返回
   remote_conflict/unavailable → 按错误原文
   `dispatch-fail --send-state unknown`。
4. `kanpilot dispatch-start --json '{"task_id":..., "run_id":...}'`：
   - `deduplicated:true` → 该轮已启动，跳到第 5 步
     （**绝不重复启动进程**）。
   - `status:"started"` 且带 session_id → 直接到第 5 步。
   - `status:"starting"`（session 事件未就绪）→ 每 ≤30 秒
     `kanpilot get-session` 轮询 session_id，≤5 分钟仍未就绪 →
     `dispatch-fail --send-state unknown --reason "worker 会话未建立"`。
   - 错误 `validation_failed`（配置/凭据/agent.md 不可用）→
     `dispatch-fail --send-state not_sent`；`internal`（进程拉起失败）→
     `dispatch-fail --send-state unknown`。
5. session_id 就绪后 `dispatch-confirm`（pi 证据）：
   `kanpilot dispatch-confirm --json '{"task_id":..., "run_id":...,
   "evidence": {"session_marker":..., "repository":..., "backend":"pi",
   "session_id":..., "protocol_seen":true, "config":<与 prepare 相同>}}'`
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
