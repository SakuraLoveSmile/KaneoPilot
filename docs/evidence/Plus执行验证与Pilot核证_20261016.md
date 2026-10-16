# Plus 执行验证，Pilot 核对证据

状态：待体验。用户于 2026-10-16 确认职责边界并授权实施；本次适用的隔离检查、原生预览走查及本地构建已完成，既有 Review 失败如下单列。未安装到运行中的正式应用、未调用真实模型、未变更生产任务，未提交、推送或创建 PR。

## 用户可观察的变化

- 验证命令由 Plus 在任务工作树执行。Pilot 不再因自身 Java、SDK 或设备环境另跑一遍检查并阻断任务；live、shadow 和手动验证入口均遵守此规则。
- Pilot 根据当前执行轮次的真实工具结果核对计划命令、退出码、完整终态及源码指纹。文字“通过”、旧轮次结果、验证后的源码改动都不能放行。
- 「重新核验验证证据」只重新读取已有证据；「让 Plus 重新执行并验证」明确创建新执行轮次，沿用原计划与工作树，保留已有成果。后者由用户发起，未自动调用模型。
- 旧本机验证回执仅保留历史审计，不覆盖 Plus 结果；项目 `verify_env` 标记为历史兼容配置。

## 共享规则与兼容

权威定义见 protocol §5.18.6 / §5.27.11、automation-contract 执行验证证据及 ui-spec 待处理动作。

新执行记录使用 `verification_evidence_version=1` 与成功终态的 `verification_source_sha256`。核心从固化 spec/job/events 和登记工作树读取证据，不接收调用方伪造的检查结果。通过收据标记 `evidence_source=plus_tool_results`，保留源码、文件指纹及提交树绑定。

所有代码修改、终端状态检查、子任务应在最后逐条验证之前结束。验证后只有明确 Read/Grep/Glob 和最终文字不会撤销结果；其他工具保守视为可能写入。Plus 指令同步说明这个边界。

缺失验证证据与身份矛盾分开：前者不自动质量补救，可由用户发起新 Plus 轮次；实际源码、会话、计划或工作目录矛盾只允许重新核对/检查身份。历史 job 不补造当时源码指纹。已完成 Plus 会话中任意人工追加 turn 不会自动采纳为旧执行证据。

判定、提交及推进在 SQLite 写锁外复核 Plus 当前空闲状态和原 turn 的 completed 终态；旧发送未知不重复派发。Plus 与本地 Git/SQLite 没有跨系统 CAS，实时读取只能缩小并发窗口。终态快照与工具顺序也不能冒充逐命令的源码签名。

保留历史 Goal report 和 legacy 完成策略，当前请求身份、monitor claim、报告及执行状态守卫不放宽。

## 实施归属

| 编号 | 交付与归属 | 输入、接口和依赖 | 验收 |
| --- | --- | --- | --- |
| T1 | Sol worker 独占 verify_evidence、pi_desktop_worker、dsh_events 和对应新增测试 | 固化 spec/job/events；新增严格 matcher 供 T2/T3 使用；不派生 | 旧 turn、审批前、纯文字、错误退出码、后续写工具、并发未结束工具均不能通过 |
| T2 | Sol worker 独占 verdict、engine commands、auto_monitor 和对应测试 | T1 matcher 与终态快照；T3 身份读取函数；不派生 | 本机 runner 调用即失败时仍可接受合格 Plus 证据；源码/证据漂移、目录越界、提交恢复保持守卫 |
| T3 | Sol worker 独占 verification recovery/checks、cmd_queue、derive、受影响 Swift/FakeCore 和恢复测试 | T1 matcher；复用既有 execution_retry；不派生 | 本地重跑入口拒绝；新轮次复用原工作树、保留计划与发送/需求/终态守卫；身份不明不开放重执 |
| T4 | Sol worker 独占 delivery、manual review 与 rehearsal/legacy 集成夹具 | T1–T3 接口完成后迁移旧“核心亲跑”前提；不改业务实现 | 原提交、hook、index、崩溃恢复、环境阻塞和轮次断言保留，证据改由 fake Plus 提供 |
| 集成 | 主 agent 独占 docs、AGENTS、派发提示、远端推进门及 Review 材料；独立 reviewer 只读审查 | T1–T3 可并行；联合验证依赖实现。保留现有脏改动，不访问生产服务 | 新旧回执不互相覆盖；打包来源一致；原生界面仅用隔离 FakeCore |

只读分析由两个 Sol analyst 并行完成，分别追踪 Plus 证据链和恢复/UI 链；随后上述 worker 实际并行实施。主 agent 亲自执行联合检查，不以子 agent 自报代替最终证据。

## 验证记录

以下结果只代表本地隔离数据和 fake 服务，不代表真实 Plus 模型任务已验收。

- 已保存先红后绿证据：原 HEAD 的本地验证器在新 Plus 成功用例中触发「Pilot must not verify」失败；新实现接受工具证据，不调用本机验证器。
- 最终核心联合定向集由主 agent 运行：557 passed、24 subtests passed，覆盖证据匹配、事件投影、worker、判定、监控、提交、恢复、重新执行、历史兼容及推进/Review 边界。日志：`/tmp/kaneopilot-plus-focused-final.log`。
- 手动 detached 评审夹具迁移后，主 agent 补跑 3 passed、2 subtests passed；保留推进不提交、验证后源码篡改及 HEAD 漂移零写入断言。日志：`/tmp/kaneopilot-plus-manual-review-final.log`。
- 首轮集成全量：387 passed、5 skipped、4 subtests passed，9 个失败均为旧演练夹具依赖 Pilot 本地验证。完成夹具迁移后，完整 engine/golden/legacy 三文件重跑 30 passed，保留源码、暂存区并发、hook、崩溃恢复、合并及产物过滤断言。日志：`/tmp/kaneopilot-plus-integration-tests.log`、`/tmp/kaneopilot-plus-fixture-final.log`。此为首轮全量加失败文件复跑，未声称最终源码重新全量通过。
- Swift 模型夹具 133/133；engine supervisor、外部 owner、故障、所有权及验证身份夹具均通过。日志：`/tmp/kaneopilot-plus-swift-models.log`、`/tmp/kaneopilot-plus-swift-engine.log`。
- 主 agent 完成 Release 构建、核心/提示词来源哈希核对与本地签名检查。产物：`dist/KaneoPilot.app`；日志：`/tmp/kaneopilot-plus-build-final.log`。这只证明本地构建及签名一致性，不代表正式安装或公证发布。
- Git 全局签名导致隔离夹具提交超时；测试仅在进程环境设置 `commit.gpgsign=false`，未修改用户全局 Git 配置。
- 核心首轮全量：2881 passed、663 subtests passed、52 failed。其中 40 项受最终核心联合定向集覆盖并通过，3 项手动评审旧夹具补充迁移后重跑，另 9 项为下述独立复现的既有 Review 失败。该轮启动后发生的实现和夹具修改，以最终联合定向集及失败文件复跑为准；未重新运行最终全量，不将这些分项数字相加冒充一次全量结果。
- 原生界面：开始因锁屏受阻，用户解锁后主 agent 已用 CUA 完成隔离 FakeCore 实测。检查了证据不足和旧 Java 阻塞两页、按钮布局及新轮次确认说明；取消后无 resolve/dispatch，确认后仅记录 `resolve(action=retry_verification)`。只读重新核验入口明确不调用模型，点击后记录 `resolve(action=reverify)`，缺证据时仍阻塞。整个操作记录没有 `run-verification-checks`。
- 预览应用为独立 bundle `local.kaneopilot.plus-verification-preview`，HOME、配置、DB、凭据均隔离，关闭执行调度；命令记录：`/var/folders/3f/d270m3fx56b4ff4x8l0xcn8m0000gn/T/kp-plus-verification-preview-f472ytun/commands.jsonl`。界面结果仅代表隔离 fake 数据，不冒充生产任务或真实 Plus 模型效果。
- 真实 Plus 模型调用、生产任务恢复、正式安装：未执行。

## 既有失败的基线

保留用户原有 `core/tests/test_cli_review.py` 改动。将 HEAD 原始业务代码导出到临时目录，仅覆盖这份原有测试文件后运行：8 failed、12 passed，失败均为槽位请求身份已变化，未进入评审进程。当前工作区相同测试文件与临时基线 SHA256 一致。本次不改写这份现有工作来掩盖基线失败。

全量另有 `test_claude_cli.py::test_invalid_settings_env_settles_review_without_launch` 相同槽位身份失败，最新源码与原始 HEAD 隔离基线均已单独复现。既有失败合计 9 项；日志：`/tmp/kaneopilot-plus-cli-review-baseline.log`、`/tmp/kaneopilot-plus-claude-review-baseline.log`。这 9 项仍失败，不计为本次通过，也未修改其测试或相关 Review 业务逻辑。
