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
 - 构建与发布：仅使用 GitHub 托管 Actions 的 `Packages` 工作流；分支手动构建用 `gh workflow run packages.yml --ref <branch>`，正式 Release 按 `docs/build-release.md` 的 annotated 稳定标签流程。
 - 自动接收：`cd core && python3 -m kanpilot.auto_intake`；只读预览加 `--dry-run`。调度每 20 秒检查，intake 每 300 秒。
 - 引擎长驻：`engine run`（DB 级单实例租约+心跳+退避重启，见 protocol §5.27）。
 - 引擎状态：`engine status`（只读，见 protocol §5.27）。
 - 待处理列表：`attention-list`（只读，见 protocol §5.27）。
 - 会话总览：`session-view {task_id?, run_id?}`（只读）。
 - 实时诊断：`doctor --live`（在线模式，含 Kaneo 连通性）。
 - 诊断：`kanpilot doctor`；迁移：`kanpilot migrate`。
 
 ## 约束
 
 - KaneoPilot 自身软件的编译、签名、打包与 Swift/Android 编译检查均在云端 Actions 执行，不在本地运行 Xcode、Gradle 或会自动编译的预览脚本。本地允许无需编译的 Python 隔离测试、静态检查及下载包核验；不要伪造 Actions 环境绕过脚本约束。构建成功、真实安装/设备验证、发布分别报告。
 - 本地缓存清理仅限无活动引用、可再生成的明确路径；`build/` 内的证据、备份、DB及嵌套仓库，`dist/` 的交付/恢复包，以及已安装软件和用户数据均保留。不得把整个忽略目录当缓存删除。
 - Python 3.9+ 标准库；第三方依赖只 vendored（沿用 pyyaml `_vendor` 模式）。
 - 远端 Kaneo 写只经核心命令；服务端无 CAS，不声称本地锁阻止远端竞争。
 - 不输出/不持久化 Kaneo 凭据 token。
 - SwiftUI 遵循 docs/ui-spec.md §3 度量刻度（字号 11/12/13/15/18/22pt，间距 4/8/12/16/24，窗口 1180×760/900×600）。
 
 ## Worktree 位置与合并后的清理
 
 - 所有新 worktree（包括子 agent、临时验证和集成工作树）统一放在 `$HOME/Documents/Code/Project/Project Worktree/KaneoPilot/<任务标识>/`；工具不能指定位置时使用展开 `$HOME` 后的绝对路径调用 `git worktree add`，不再使用同级、仓库内部或工具默认目录。创建前检查工作区、`git worktree list` 和任务归属。
 - PR 或来源分支确认已合入约定目标且适用验证通过后，在同一任务内及时清理本任务已完成的临时工作树和已合并本地任务分支，无需再次确认。删除前核对目标已包含来源提交或等效改动，保留 `main` 等目标分支；无未保存或独有成果，无运行任务、服务或仍需保留的路径引用。需保留的未跟踪、忽略文件先迁出或建立可核验、可恢复的备份；无法确认安全时保留并报告具体路径与原因，不强制删除。
 - 从工作树之外先执行 `git worktree remove`，再按授权删除已合并本地分支，核对工作树列表、本地分支与实际目录并报告清理结果。保留主工作区、其他任务、共享目录和恢复归档。远端来源分支删除须有明确授权，不与本地 worktree 收尾自动绑定。
