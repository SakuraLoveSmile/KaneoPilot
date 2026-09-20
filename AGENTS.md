# KaneoPilot agent guide

Kaneo 调度台：本机 macOS 工作台。SwiftUI 桌面 + Python 调度核心 + SQLite。集中查看 Kaneo 任务进度、失败原因、计划文件和执行证据，并安排下一步。正常流程自动推进，异常集中处理。

## 边界

- 第一版仅本机；不做云端、多用户、手机端、Kaneo 服务端改造。
- Codex 继续负责规划/派发/Review，Devin 继续执行；本软件只做确定性的队列与状态管理。
- SQLite 是唯一状态写入源；桌面与执行端只经 `core` 的固定 JSON 命令交互，不直接改库或锁文件。
- 「已排队」≠「已开始」：执行端领取并回报心跳才算开始。

## 布局与归属

- `docs/` — 冻结规格（protocol.md / state-model.md / ui-spec.md / automation-contract.md），主 agent 唯一维护；实现分歧先改规格再动工。
- `core/` — Python 调度核心 + `kanpilot` CLI + 隔离测试。
- `macos/` — SwiftUI 桌面 app（Xcode 工程）。
- `tests/` — 集成测试与夹具（并发领取、崩溃恢复、迁移、验收）。
- `automation/` — 执行端 prompt 与切换材料。
- `scripts/` — 构建/安装脚本。

## 常用命令

- 核心 CLI：`python3 core/kanpilot.py <command> --json '<payload>'`（`KANPILOT_DB` 覆盖 DB 路径）。
- 核心隔离测试：`cd core && python3 -m pytest tests -q`（或 unittest，随实现）。
- 集成测试：`python3 -m pytest tests -q`。
- 构建 app：`scripts/build_app.sh` → `dist/KaneoPilot.app`。
- 诊断：`kanpilot doctor`；迁移：`kanpilot migrate`。

## 约束

- Python 3.9+ 标准库；第三方依赖只 vendored（沿用 pyyaml `_vendor` 模式）。
- 远端 Kaneo 写只经核心命令；服务端无 CAS，不声称本地锁阻止远端竞争。
- 不输出/不持久化 Kaneo 凭据 token。
- SwiftUI 遵循 docs/ui-spec.md 的冻结度量（13/18/11pt、8/12/16 间距、1180×760/900×600）。
