 # KaneoPilot agent guide
 
 Kaneo 调度台：本机 macOS 工作台。SwiftUI 桌面 + Python 调度核心 + SQLite。集中查看 Kaneo 任务进度、失败原因、计划文件和执行证据，并安排下一步。正常流程自动推进，异常集中处理。
 
 ## 边界
 
 - 第一版仅本机；不做云端、多用户、手机端、Kaneo 服务端改造。
 - 执行端由 App 常驻调度器按固化后端拉起；dispatch/monitor/delivery 为确定性流程；Plus 轮次经本项目 MCP runtime 域控制，不挂接 task grant。所有状态写入经核心命令。
 - SQLite 是唯一状态写入源；桌面与执行端只经 `core` 的固定 JSON 命令交互，不直接改库或锁文件。
 - 「已排队」≠「已开始」：执行端领取并回报心跳才算开始。
 - strangler 三态切换：`engine_mode: legacy|shadow|live`（按项目切换，不跨项目混用）。旧模块按门控保留；当前实施范围见 docs/plans/自动化流水线最新计划_20261012.plan.md，未完成实机验收/生产切换不声称已上线。
 - 验证由 Plus 执行，Pilot 只获取并核对 Plus 结构化 Goal 报告并推进状态：live verdict 按 protocol §5.27.11 核对报告身份与底线，要求冻结计划的每条 verify 命令在报告 checks 中有退出码为 0 的通过记录，并核对工作树源码快照，排除 `.pi/**` 与交付构建产物；live/shadow/手动入口均不得在 Pilot 本地代跑 verify。legacy 保留历史完成策略，旧转录证据回执仅留审计。
 - 外部副作用先写 `runtime_operations` 回执再执行；远端写只经核心命令；服务端无 CAS，不声称本地锁阻止远端竞争。
 
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
 - 调度台集成演练：`python3 -m pytest tests/rehearsal -q`。
 - 构建 app：`scripts/build_app.sh` → `dist/KaneoPilot.app`。
 - 自动接收：`cd core && python3 -m kanpilot.auto_intake`；只读预览加 `--dry-run`。调度每 20 秒检查，intake 每 300 秒。
 - 引擎长驻：`engine run`（DB 级单实例租约+心跳+退避重启，见 protocol §5.27）。
 - 引擎状态：`engine status`（只读，见 protocol §5.27）。
 - 待处理列表：`attention-list`（只读，见 protocol §5.27）。
 - 会话总览：`session-view {task_id?, run_id?}`（只读）。
 - 实时诊断：`doctor --live`（在线模式，含 Kaneo 连通性）。
 - 诊断：`kanpilot doctor`；迁移：`kanpilot migrate`。
 
 ## 约束
 
 - Python 3.9+ 标准库；第三方依赖只 vendored（沿用 pyyaml `_vendor` 模式）。
 - 远端 Kaneo 写只经核心命令；服务端无 CAS，不声称本地锁阻止远端竞争。
 - 不输出/不持久化 Kaneo 凭据 token。
 - SwiftUI 遵循 docs/ui-spec.md 的冻结度量（13/18/11pt、8/12/16 间距、1180×760/900×600）。
 
 ## 分支合并后的清理
 
 - PR 或来源分支确认已合并后，必须删除已合并的本地、远端来源分支，并清理对应的临时工作树；不得将分支清理留作可选后续步骤。删除前核对目标分支已包含来源提交，保留 `main` 等仍在使用的目标分支。工作树有未提交改动或需要保留的文件时，先将进行中的工作迁移到新的工作分支或建立可核验、可恢复的备份，再清理原来源分支；不得丢弃现有工作或数据。完成后核对本地/远端分支和工作树列表，并报告清理结果。
