# KP-R02 发布配置 AI 辅助填写

- 状态：待体验（云端检查及隔离原生复测完成；未调用真实模型）。目标：用户在项目「构建发布配置」中调用 AI，根据仓库资料查看工作流、运行分支、初始版本、安装包及 job 建议，再采纳至未保存草稿。
- 基线：`origin/main` / `845f9aa`；分支 `feat/20261104-kp-r02-release-config-assist`；工作树 `/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261104-feat-kp-r02-release-config-assist`；拟合入 `main`。主工作区的 AGENTS.md 和 KP-B01 计划原有改动保留。
- 当前授权实现及隔离验证；用户于 2026-11-04 明确同意提交、推送本功能、运行普通云端预览构建并用产物进行隔离界面验证。不包含真实模型调用、合并、正式安装或发布。二维码改动属于独立 KP-M02 分支，不混入本次提交。本次用户给出的规则要求 Swift/Android 编译只在云端；不沿用基线 AGENTS.md 的本地编译例外。

## 已确定的行为与接口

共享契约维护在 `docs/release-contract.md` 的「发布配置 AI 辅助」；其余发布策略及完成门保持原规则。

界面沿用 `ProjectMappingsView` 现有表单，在发布开关下、手动配置折叠前增加「AI 辅助填写…」和简短说明。先只读准备，展示将使用的 Codex CLI 模型及可能费用，再由用户开始分析。分析中显示进度；结果就地展示推荐值、依据与缺项，用户点「采用建议」后才改动本项目草稿。手动编辑始终可用。切项目/仓库、放弃或分析期间修改发布配置后，旧结果不能覆盖新草稿。建议不会切换渠道、开关、保存设置或触发发布。

使用已保存的 `standard` Codex 模型组，独立于主规划后端，并明确显示。现有 Plus 没有可靠的只读/禁工具 API，不扩展其公共接口、不隐式换后端。Codex 复用只读、临时会话、忽略用户插件/MCP 的现有启动器，在临时材料目录中分析；不声称其工具被完全禁用。

## 实施与分工

- T1 主 agent：冻结契约、CLI 接入、Swift DTO、原生界面和草稿保护，最终组合检查。独占 `docs/**`、`core/kanpilot/cli.py`、`macos/**`。
- T2 后端 worker：独占新增 `core/kanpilot/release_config_assist.py` 及 `core/tests/test_release_config_assist.py`；按共享契约实现有界快照、校验、只读模型调用及幂等回执。与 T1 在契约确定后并行。真实模型不调用；测试只用临时 DB/仓库与假 CLI。
- 前置只读 analyst 已核实底层复用及 Plus 能力边界；未修改源码或运行模型。原子任务不再派下级。

## 验收与证据

1. 有兼容工作流时可得到有依据的建议，采纳只改五项配置草稿，保存沿原流程。
2. 没有工作流、版本化文件名无法确定或工作流不支持 Pilot 输入/凭证时显示缺项，不用示例 Music 文件名补齐。
3. 准备、取消、失败不改策略；重复同 operation 不重复调用模型；超时清理本次子进程；仓库/资料/模型变化拒绝旧确认。
4. 切项目、换仓库、放弃、刷新或分析中编辑后，过期响应不覆盖新草稿。加载、失败、建议与手动编辑均有明确入口。
5. Python 隔离验证覆盖上述核心规则；Swift 编译和原生界面需包含本次代码的云端构建后核验，未运行不得记为通过。

## 执行记录

- 2026-11-04：已核对主工作区、HEAD、索引、worktree；fetch 后从当前 `origin/main` 新建本任务工作树。既有工作树均不属于本任务。
- 已实现：有界仓库材料和模型快照 → 费用确认 → 一次只读分析 → 核心校验及依据/缺项预览 → 重核后采纳草稿 → 原保存栏。CLI/DTO/FakeCore、共享规格及过期响应保护均已接入；未修改原发布调度及完成门。
- 首轮本地证据对象：提交 `0762c9f` 所含代码（运行时尚未提交），macOS 本地 Python 3 环境。主 agent 实测 `python3 -m unittest discover -s core/tests -p test_release_config_assist.py -q`：13 通过；项目映射专项：8 通过；发布生命周期专项：36 通过，共 57。后端用例仅用临时 DB/仓库和假 Codex CLI，包括 JSON CLI 全链、超时子进程组清理及 unknown 不重复调用。
- 主 agent 实测 FakeCore CLI 的准备、预览、重放、缺工作流与全部已保存配置保留；初始临时检查把 FakeCore 动态生成的展示时间纳入全对象比较而失败，查明后固定输入夹具，直接核对保存对象逐字段不变，通过。未改变产品测试预期或门禁。
- Python AST 与 `git diff --check` 通过；独立 reviewer 已只读审查 UI/DTO/CLI/FakeCore，未发现实质问题；主 agent 检查后端并修复了超时父进程退出后子进程可能残留的问题。
- 首轮云端预览：`0762c9f`，Packages [41701248905 / #100](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/41701248905)，15 个必需 job 全部通过。校验源码 SHA、ZIP SHA-256、文件大小、云端签名和 arm64 架构后，以全部隔离路径及 FakeCore 运行；没有本地编译/签名。主 agent 在 1180×760 深色原生界面确认费用确认前无分析调用、取消、建议及依据、采纳仅改草稿、原保存确认与保存、失败保留手填入口、缺工作流禁用采纳、分析中手改版本不被旧结果覆盖及放弃读回。证据在 `build/kp-r02-native-preview-100/`，该预览进程已停止。
- 首轮发现并修复：延迟分析时 List 先写入 selection，`selectProject` 的 ID 比较不会清掉旧确认区域；旧结果仍被项目快照拦截，但展示不符合目标。修正提交 `91d2973` 在 selection 的 onChange 直接清理候选。独立 reviewer 针对修正及相邻异步清理核查，未发现实质问题；源码审查不能替代原生复测。
- 当前云端证据：`91d2973efa2f122f1c1194703b15cd28e6b76fca`，Packages [41703762957 / #101](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/41703762957) 成功，15 个必需 job 全部通过（10 个 Python 分片、Android、2 组桌面夹具、macOS 打包、identity）；可选全文探针及 publish 按预览条件跳过，未降低门禁。原始 job 结论及 19 项 artifact 元数据保存于 `build/kp-r02-native-preview-101/actions-run.json`、`actions-artifacts.json`。
- 当前产物：artifact `macos-update-ui-101` / `13263608087`，云端 ZIP 的 SHA-256 为 `b9936d2332d8d4f6ee5a4593112df9fab76a4fbb65ac9e550aab55448e40c7ef`、大小 31,378,044 字节；已对照元数据核对源码 SHA 和构建号，并通过本地只读 `codesign --verify --deep --strict` 与 arm64 架构核验。未在本地编译、签名或修改 App 包。
- 原生复测阻塞：新产物已用独立 FakeCore、数据、凭据空夹具与偏好目录启动（`build/kp-r02-native-preview-101/`，PID 7916；启动前需重新核对 PID 归属），但 CUA 返回「The Mac is locked and automatic unlock could not unlock it」。已请用户手动解锁；未绕过锁屏。新 App 尚未完成最后的切项目/完整采纳保存复测，900×600 尚未检查，不能沿用 #100 的界面证据覆盖该修正。
- 2026-11-04 恢复复测：Mac 已解锁，按 #101 App 的完整绝对路径绑定 CUA，并核对预览进程归属，未操作正式 App 或 KP-UI06 预览。1180×760 深色通过完整确认→分析→依据→采纳→原发布确认→保存回读；FakeCore 日志证实采纳前未发生设置写入，确认保存后仅调用一次 `set-project-automation`。延迟 8 秒分析期间切到 Library，旧确认立即清空，迟到结果未进入 Library，Music 草稿仍保留。截图与 AX 记录在同一 `build/kp-r02-native-preview-101/`。
- 最小窗口：900×600 浅色通过费用确认、建议区滚动及采用/取消按钮可达，已保存 `900-light-confirmation.png`、`900-light-suggestions-actions.png`；采用后手动改版本为 0.2.0，保存/放弃均可达，放弃恢复 0.1.0。手动编辑阶段窗口自动增高到 900×720，证据命名为 `900-width-manual-draft.png`，不将其计作精确 900×600 保存栏截图；未为此改变全局窗口行为。
- 下一步：用户体验与后续集成。真实 Codex 服务未验证，也未获准调用；未合并、正式安装或发布，已安装 App 尚未包含本次功能。继续使用云端产物及全部隔离环境，不运行会编译的 preview 脚本，不重复请求已批准的提交、推送和普通预览授权。
- 收尾：尚未合入，工作树及两个构建证据目录保留用于最后界面验证和后续集成；合入且适用验证通过后清理本任务工作树，不清理其他任务或已安装 App。

## KP-REL01 集成记录（2026-11-04）

用户随后明确授权合并最近修改及正式发布，并确认 v0.4.2；此前“未合入/未发布”的文字是各轮历史状态。完整来源提交已纳入远端 main 的 `856dbeb13ea275424ab274eac2492d70f6a30eab`，同 SHA 的 Packages #107 15 个必需 job 通过。组合原生覆盖、未复测项、正式发布与证据保全清理统一记录于 [KP-REL01](kp-rel01-release-042.plan.md)，不以本分支旧结果冒充组合实测。真实服务/模型、安装升级和手机实扫仍未验收，状态保持待体验。
