# Spike: Plus 承载规划/评审的结构化输出可行性 — 2026-10-10

## 结论

**可行，且已有可核验的结构化通道。** 真实 Plus（0.15.7）实测录制确认两条链路：

1. **规划侧**：`plans/pending` 返回 `history[]`，每项含
   `id` / `kind` / `action` / `executionKind` / `executionState` /
   `executionModelId` / `executionProviderId` / `createdAt` / `expiresAt` /
   `artifact{relativePath, sha256(64 hex), sizeBytes, workspaceKind}` /
   `plan`(markdown) / `markdown`。
   → **`artifact.sha256` + `sizeBytes` 就是规划产物的可核验指纹**，与核心
   `plan_sha256 = sha256(文件原始字节)` 的口径可直接对齐；`executionState`
   给出该规划的执行状态机位置。
2. **评审/终态侧**：`goalReport/list` 返回 `reports[]`，每项含
   `reportId` / `sessionId` / `turnId` / `executionId` / `proposalId` /
   `status`（实测 `pending`）/ `verdict`（实测 `unknown`）/
   `summary` / `integrity`（实测 `fallback`）/ `goalTitle` /
   `fileHash` / `filePath` / `fileSize` / `durableSeq` / `createdAt` / `updatedAt`。
   → `verdict` + `integrity` + `fileHash` 构成结构化评审结论；`durableSeq`
   给出持久化序号，可用于乱序对账。

另外 `turn/get` 的返回本身是结构化 JSON（`{sessionId,turnId,status}`），
`session/list` 返回富含结构元数据的会话清单（`modelId`/`providerId`/
`permissionMode`/`thinkingLevel`/`supportsReasoning`/`messageCount`/…）。
**结论：Plus 全程走结构化 JSON，无需从自然语言转录里抠字段。**

## 对 KaneoPilot 的实现落点

1. **规划**：`planner_runner`/`pi_desktop_planner` 已走 Plus 控制面；新增
   `plan_sha256` 与 `artifact.sha256` 的**交叉核对**（不一致 → 身份冲突，
   不静默采信），并把 `executionState` 映射进 phase×condition 状态模型。
2. **评审**：评审结论落 `review_summary`/`review_resolution`（已有结构），
   与 `goalReport.verdict`/`integrity` 对齐；`integrity != "verified"` 时
   不得当作已核验结论。
3. **不整段持久化 Plus 转录**（A10）：结构化字段逐项入库；自然语言只按需
   读取展示，不落库。
4. **失败阶梯归因**：结构化错误信封（实测 `{ok:false,error:{code,message}}`）
   按 `code` 分类——`NOT_FOUND` 归身份/对账，`INVALID_PARAMS` 归参数错误，
   超时归 transient；不把所有非零都当质量失败。

## 证据

- 真实录制夹具（`tests/rehearsal/fixtures/`，`_captured_at=2026-10-10`，
  `_provenance=real_recording_via_mcp_pi_desktop_invoke`）：
  - `plus_plans_pending_real.json` → `artifact.sha256` 为 64 位十六进制
  - `plus_goal_report_list_real.json` → `status:"pending"`, `verdict:"unknown"`, `integrity:"fallback"`
  - `plus_turn_get_real_recording.json` / `plus_turn_get_not_found_real.json`
  - `plus_agent_status_real.json` / `plus_app_getVersion_real.json`
- 形状门测试：`tests/rehearsal/test_real_shape_recordings.py` 的
  `test_scenario_real_recording_goal_report_and_plan_shapes` 钉住
  goalReport 7 字段 + plans/pending 7 字段 + artifact 4 字段 + sha256 长度。
- 核心侧对应实现：`core/kanpilot/pi_desktop_planner.py`、`planner_runner.py`、
  `plan_answers.py`、`review_settle.py`、`review_resolution.py`；
  `core/kanpilot/runtime_client.py` 的 `plans_pending`/`plans_resolve`/
  `goal_report_list`/`goal_report_get`。

## 采集方式与限制

- 采集：MCP `pi_desktop_invoke` 只读 operation；未调用 `agent_prompt`/
  `plans/resolve`/`session/*` 写操作；token 未输出未落盘。
- 脱敏：UUID 形态 id → `<redacted-uuid>`；`markdown` → 长度占位；长标题截断。
- 限制：录到的 goal report 为 `pending`/`verdict:"unknown"`，**未录到
  `pass`/`changes_requested` 的真实评审结论样本**；待沙盒 live 跑通后补录。
  `integrity:"fallback"` 的含义需 Plus 侧文档确认（当前按「非 verified」
  处理，不采信）。
