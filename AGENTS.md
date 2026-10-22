 # KaneoPilot agent guide
 
 Kaneo 调度台：macOS 工作台与 Android 远程控制台。SwiftUI 桌面 + Python 调度核心 + SQLite；Android Kotlin/Compose 通过局域网或已有 VPN 连接 Mac。集中查看 Kaneo 任务进度、失败原因、计划文件和执行证据，并安排下一步。正常流程自动推进，异常集中处理。
 
 ## 边界
 
 - Mac 保持唯一数据和调度所有权；Android 是个人远程控制台。手机接口遵循 docs/mobile-console-contract.md，不做云端、多用户或 Kaneo 服务端改造。
 - 执行端由 App 常驻调度器按固化后端拉起；dispatch/monitor/delivery 为确定性流程；Plus 轮次经本项目 MCP runtime 域控制，不挂接 task grant。所有状态写入经核心命令。
 - SQLite 是唯一状态写入源；桌面与执行端只经 `core` 的固定 JSON 命令交互，不直接改库或锁文件。
 - 「已排队」≠「已开始」：执行端领取并回报心跳才算开始。
 - strangler 三态切换：`engine_mode: legacy|shadow|live`（按项目切换，不跨项目混用）。旧模块按门控保留；当前实施范围见 docs/plans/自动化流水线最新计划_20261012.plan.md，未完成实机验收/生产切换不声称已上线。
 - 验证由固化的执行端运行，Pilot 只核对报告与证据并推进状态。Plus live verdict 沿 protocol §5.27.11；Codex CLI 沿 §5.31，所有引擎模式均核对结构化报告、原生命令退出码和源码快照。冻结计划的每条 verify 必须有通过记录，源码快照排除 `.pi/**` 与交付构建产物；任何入口均不得在 Pilot 本地代跑 verify。其他后端 legacy 保留历史完成策略，旧转录证据回执仅留审计。
 - 外部副作用先写 `runtime_operations` 回执再执行；远端写只经核心命令；服务端无 CAS，不声称本地锁阻止远端竞争。
 
 ## 布局与归属
 
 - `docs/` — 冻结规格（protocol.md / state-model.md / ui-spec.md / automation-contract.md），主 agent 唯一维护；实现分歧先改规格再动工。
 - `core/` — Python 调度核心 + `kanpilot` CLI + 隔离测试。
 - `macos/` — SwiftUI 桌面 app（Xcode 工程）。
 - `android/` — Kotlin/Compose 安卓远程控制台（Gradle/JDK17）。
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
 
 ## 工作树位置与合并后的清理
 
 - 新工作树放在维护者指定的统一外部工作树根目录，创建前核对工作区状态与现有工作树归属；不使用仓库内部或工具默认目录。
 - 目标分支已包含预期提交或等效改动、适用验证通过后，及时清理本任务的临时工作树。先确认没有独有成果、未保存文件、运行任务或仍被使用的路径；需要保留的未跟踪、忽略文件先做可核验、可恢复的备份。不能确认安全时保留现场并说明原因。
 - 从工作树外使用 `git worktree remove`，核对工作树列表与目录。本地来源分支按项目及任务授权处理；删除远端来源分支必须得到明确授权。保留共享根目录、主工作区和活跃任务。
