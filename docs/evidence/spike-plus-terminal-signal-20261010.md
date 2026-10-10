# Spike: Plus agent/goal 模式能否给出可靠 terminal 信号 — 2026-10-10

## 结论

**能，但不能只靠一个端点。** 在真实 Plus（pi-desktop-plus 0.15.7）上实测录制后确认：

| Plus 端点 | 实测返回 | 能否单独作为 terminal 信号 |
|---|---|---|
| `agent/getStatus` | `{sessionId, isRunning, pendingToolConfirmations}` | **不能**。只有粗粒度存活/待确认计数，无 `outcome`/`terminal`/`conclusion`/`verdict`/`endedAt` 任何终态字段 |
| `turn/get` | `{sessionId, turnId, status}` | **不能单独**。只给 `status ∈ {running, completed, error, aborted}` 一个字符串；无 `startedAt`/`endedAt`/`errorCode`/`outcome`。未知 turn 返回 `{ok:false, error:{code:"NOT_FOUND", message:"turn not found"}}` |
| `goalReport/list` | `[{reportId, sessionId, turnId, status, verdict, summary, integrity, goalTitle, fileHash, …}]` | **可以（goal 模式）**。带 `status` + `verdict` + `integrity` + `fileHash`，是 durable 的结构化终态记录 |
| `plans/pending` | `{history:[{id, kind, action, artifact:{relativePath,sha256,sizeBytes,workspaceKind}, executionState, plan, markdown, createdAt, expiresAt, …}]}` | 可以（**规划侧**结构化产物，sha256 + sizeBytes 可核验） |

Plus 自己的 MCP `initialize` 指令也这么说的（原文摘录）：

> "Poll `pi_session_get` or `pi_agent_status` for live progress and `pi_turn_get` for a turn's durable terminal state."

即：**live progress 用 `agent_status`/`session_get`，durable terminal state 用 `turn_get`**；goal 模式另有 `goalReport/list|get` 的结构化终态。

## 对 KaneoPilot 的含义（这是本 spike 的落点）

1. **`turn_get.status` 是必要不充分的**：`status=completed` 只说明轮次结束，不说明任务完成。核心必须再叠加：
   - 身份核对（`session_id`/`turn_id`/`run_id` 与任务绑定一致）；
   - `goalReport` 的 `verdict`/`status`/`integrity`（goal 模式）；
   - **核心亲跑的验证证据**（这就是 A10「verdict 由核心亲判」的根据）。
2. **`agent_status` 只能用于「是否还在跑」的节流判断**，不得用于完成判定——它连 `outcome` 都没有。
3. **`NOT_FOUND` 与超时必须分开归因**：实测 `turn/get` 对不存在 turn 返回 `{ok:false,error:{code:"NOT_FOUND"}}`；网络/超时是另一类。前者属「身份/对账」问题，后者属 transient。核心已有 `_turn_status`/`_turn_identity_matches`，需保证不把 NOT_FOUND 当成超时重试。
4. **`send_state=unknown` 优先于一切 code 判阻塞**的既有规则不变：`turn_get` 拿不到确定终态时，不得自动重发。

## 证据

- 真实录制夹具（`tests/rehearsal/fixtures/`，`_provenance=real_recording_via_mcp_pi_desktop_invoke`，`_captured_at=2026-10-10`）：
  - `plus_app_getVersion_real.json` → `Pi-Desktop-Plus 0.15.7`，`protocolVersion 11`
  - `plus_agent_status_real.json` → 上述三字段
  - `plus_turn_get_real_recording.json` → `{sessionId, turnId, status:"running"}`
  - `plus_turn_get_not_found_real.json` → `{ok:false,error:{code:"NOT_FOUND",message:"turn not found"}}`
  - `plus_goal_report_list_real.json` → 含 `status:"pending"`, `verdict:"unknown"`, `integrity:"fallback"`
  - `plus_plans_pending_real.json` → 含 `artifact.sha256`(64 hex)/`sizeBytes`/`executionState`
- 形状门测试：`tests/rehearsal/test_real_shape_recordings.py`（6 个用例，含「agent_status 不含终态字段」「turn/get 无 startedAt/endedAt」「NOT_FOUND 不是超时」三条反向断言，防 Plus 升级后形状漂移被默默忽略）。
- 核心侧对应实现：`core/kanpilot/pi_desktop_worker.py` 的 `_turn_status`/`_turn_identity_matches`/`prepare_goal_continuation`；`core/kanpilot/runtime_client.py` 的 `agent_status`/`turn_get`/`goal_report_list`/`goal_report_get`。

## 采集方式与限制

- 采集：MCP `initialize` → `notifications/initialized` → `tools/call pi_desktop_invoke`，**全部只读 operation**；未调用任何写操作；token 未输出、未落盘。
- 脱敏：所有 UUID 形态 id 替换为 `<redacted-uuid>`，`markdown` 截断为长度占位，长标题截断；夹具内含 `Authorization`/`Bearer `/`accessToken`/`127.0.0.1`/`localhost` 任一子串即被测试判失败。
- 限制：录制时只有一个 `running` 轮次与一个 `pending` goal report，**未录到 `completed`/`error`/`aborted` 的真实终态样本**。终态样本待沙盒 live 跑通后补录（属 Phase 3 沙盒验收副产物）。
