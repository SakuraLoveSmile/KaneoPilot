# Spike: MCP Other Consumers Deferred — 2026-10-10

## 结论

MCP 工具集中除 Plus 规划/执行/评测外的其他消费者（如 review、verification、session control）已在 `mcp_tools.py` 中独立注册；Phase 0 不扩展 MCP，engine rewrite 再统一评估是否需要拆分为独立 MCP 服务器。

## 证据

1. `core/kanpilot/mcp_tools.py` 注册了多组工具：
   - `kp_runtime_*`：Plus 规划/执行/评测（`runtime_client.py` + `harness_plus_adapter.py`）。
   - `kp_desktop_*`：桌面队列/自动化控制（`desktop_queue`、`desktop_automation_control`）。
   - `kp_*`：本地任务管理（`enqueue`、`resolve`、`archive`、`trash`、`restore` 等）。
2. `core/kanpilot/harness_plus_adapter.py` 仅封装 Plus 协议，其他工具不经过该适配层。
3. 本次 Phase 0 新增的 `abandon-task` 走本地 `cmd_abandon.py`，不注册 MCP，证明本地命令与 MCP 解耦。

## 影响

- Phase 0 无需改动 MCP consumer 结构；engine rewrite 可先保持现有工具集，待 Phase 1+ 再拆。
- 若后续 Plus 协议变更，仅影响 `kp_runtime_*` 工具组，不影响本地任务管理。
