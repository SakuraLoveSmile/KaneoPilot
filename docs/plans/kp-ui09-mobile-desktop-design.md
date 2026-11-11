# KP-UI09 · 手机接手与 macOS 工作台设计

- 用户目标：离开电脑后开发继续推进，需要时通过手机查看、决策与接手；电脑前管理多个项目为次要场景。新需求来自 Feedback，Pilot 承接已有任务的推进与交付。
- 本轮范围：已确认的 macOS 局部设计进入 SwiftUI 实现；不改 Android、调度逻辑、数据库或核心接口。状态：**实现中**。手机仅设计已验收。
- 手机草图：**已验收（仅设计）**。2026-11-11 用户明确“手机设计已定稿”；后续修改需有新的用户反馈。
- macOS 设计：**已验收（仅设计）**。2026-11-11 用户回复“目前来看没啥问题 下一步把”，据此进入实现。2026-11-11 用户明确“在现有的基础上修改，不需要重构”，随后反馈“首页上主要是想关注项目进度”，并提出环境检查“可以收到左下角”。当前局部对比稿已按该位置修订并获用户确认。此前整页草图超出范围，撤回其作为后续设计依据的地位。

## 设计权威源

- 手机定稿：`/Users/sakurasep/.codex/visualizations/2026/11/10/01a1c644-94bb-7481-9a9c-5ff354f0764a/kaneopilot-mobile-handoff.html`
- macOS 当前基线：现有 `RootView.swift`、`AutomationDashboardView.swift`、`TasksSectionView.swift`、`TaskDetailTabs.swift` / `TaskDetailContentView.swift`、`MobileConnectionView.swift`，以及 `docs/ui-spec.md`。后续以现状和明确局部差异交付，不另造工作台布局。
- macOS 局部对比稿：`/Users/sakurasep/.codex/visualizations/2026/11/10/01a1c644-94bb-7481-9a9c-5ff354f0764a/kaneopilot-macos-overview-refinement.html`，包含“项目进展优先”和“现状参考”，使用相同参考任务。推进中的两条任务是设计样例，不代表真实执行；默认开启该预览，以延续用户已切换的展示状态。
- 已撤回的 macOS 整页草图保留作历史：`/Users/sakurasep/.codex/visualizations/2026/11/10/01a1c644-94bb-7481-9a9c-5ff354f0764a/kaneopilot-macos-workbench.html`，不得据此施工或将其中改动视为用户认可。
- 实现前只将用户最终确认的局部差异补入 `docs/ui-spec.md`，再安排施工；手机定稿不随 macOS 调整。

## macOS 当前范围

- 保留原生侧栏、导航分组、现有页面与操作路径、系统深浅色及既有字体 / 间距 / 颜色令牌。
- 总览沿用“状态标题与阶段计数 → 需要你 → 环境（有告警时）”的顺序，在原“正在推进”位置按项目展示进展，将正常环境入口收进左下角的自动推进控制区；没有待处理事项时整块“需要你”隐藏。环境告警仍在原环境区，不随正常摘要折叠。不能直接套用手机布局，也不新增“可体验成果”区。
- 任务页沿用现有看板 / 列表，详情仍用现有浮层与概览 / 计划 / 执行 / 评审 / 记录五个分段；不改为上一稿的双栏任务工作台。
- 自动推进控制仍在侧栏底部，手机连接仍在设置的连接页；上一稿新增工具栏连接入口不作为默认改动。
- 仅围绕已确认目标处理具体区域的提示层级、边界、对齐、留白与冗余说明；需要改变页面结构或操作路径的设计须另有用户依据，不因手机定稿自动扩大桌面范围。
- 暂停、领取、答案送达与继续执行的真实状态含义保持；不修改调度行为、数据来源或业务规则。

## 本轮局部差异

1. 侧栏底部显示“自动推进”及“运行中 / 已暂停”徽标，沿用原位置、在途数量、暂停 / 恢复与刷新操作。
2. “需要你”行将项目、阶段、等待时长移至标题和原因下方；标题增强字重，原因允许换行。保留原分组面板、任务行入口及悬停显示推荐动作的表现。
3. 原“正在推进”位置改为“项目进展”，沿用外置标题、分组面板、细分隔线及 16pt 留白。先显示项目名与当前任务的非零状态计数，再显示具体任务与看板列 / 条件；项目组可展开或折叠。将待处理和推进中的参考任务按 `taskId` 去重后归入项目，样例为 Music 需处理 1、博客执行中 1、桌面工具待评审 1。数字表示展示任务的阶段分布，不表示全部已配置项目或项目完成百分比。
4. 正常环境入口移到左下角自动推进区的暂停 / 刷新按钮下方，不再占据正文页尾。入口为图标和“环境检查”文字，默认收起；点击后向上显示无尾箭头的小面板，包含引擎状态、检查计数及在线诊断入口，可再次点击入口、按 Esc 或点击关闭按钮收起。保留诊断结果含义，不将“未检查”改成“通过”。首页去掉项目配置数量 / 空配置行，项目配置继续由既有“设置 → 项目”维护。

本次只改进展区域的组织和摘要的显示。侧栏、标题计数、需要你、任务看板、任务详情、设置分段、既有颜色和窗口尺度均延续现有基线；原生诊断、调度和数据契约未改。

## 本次只读核查依据

- `TaskSummary` 已有 `taskId/projectId/projectName/displayStatus/boardColumn/condition/attention/exception`，可支持按项目组织现有任务。项目键沿现有 `ObserveScope.projectID`：优先非空项目 ID，再回退项目名及未分配；同名不同 ID 不合并。项目映射的 `activeTaskCount/activeRequestCount` 不能当作各阶段任务计数。
- `ObserveGroup` 的“需关注”包含 attention 或 exception，范围宽于首页“需要你”的 actionable 收件箱；`needsAttention` 也不能代表全部 exception。后续正式实施须保留该差异，任务按 `taskId` 去重，不能按收件箱项数推算任务数，也不能把 `boardColumn == in_progress` 当成已开始执行。本稿展示有限样例，不据此增加核心接口或改变状态含义。
- 设置只有“运行端 / 项目 / 连接 / 高级”；完整环境诊断当前在总览，设置高级只提供复制诊断 / 打开数据目录。本稿将摘要入口移到左下角，不虚构独立诊断页面或将诊断入口指向设置高级；完整检查内容的原生呈现纳入本轮施工。

以上是已确认的局部设计差异，不将旧整页草图、窄屏聊天预览的响应式排版或草图样例任务作为产品新增需求。真实 App 的最小窗口仍沿用 900×600。

## 设计阶段核对与验证边界（施工前历史）

- 2026-11-11 本次核对：主工作区 HEAD `35cc2fc` 的总览代码、现有 `.sheet` 详情入口，以及界面规格的现有分组和行为。
- 通过原生界面读取正在运行的 `/Applications/KaneoPilot.app` 总览，核对侧栏、阶段计数、事项行、推进区及摘要；安装包元数据显示版本 `0.5.6-test.203` / build `203`，不据此声称运行包来自主工作区 HEAD。读取后已恢复原先“需要你”中的歌词任务详情，未触发原生暂停、重跑或诊断操作。
- 首轮局部稿（本次修订前）曾通过静态检查及深色下 1180px / 900px、暂停 / 恢复预览。暂停后仍优先显示“1 项需要你”，与现有 `DashboardHeadline.make` 的事项优先级一致；该历史通过记录不替代当前修订稿的核验。
- 前一稿（页尾入口版）曾查看深色下 1248px 默认内容宽度及 900px；项目展开 / 折叠、环境检查展开 / 折叠、Enter 键操作、刷新后保留展开状态及两个版本切换均实测。该记录仅用于保留未改区域的验证历史。
- 当前左下角版重新通过 `visualize/scripts/render.py --check`。900px 内容宽度下查看入口与向上面板，核对面板完全位于 App 内、没有横向溢出，展开前后的正文几何位置一致。实测点击展开、Return 展开、Esc 收起、关闭按钮收起以及关闭后焦点回到入口；修正同一交互状态回传时重复绘制导致的焦点丢失，未发现控制台错误。
- 320px 内容宽度仅用于聊天展示：本轮查看新增小面板，核对其位于 App 边界内、没有页面横向溢出。核验结束已清除浏览器临时尺寸，停止本次预览服务器；本轮未操作原生 App。
- 两个版本使用相同标题、原因、元信息及任务样例；数据不代表实时任务或项目配置。侧栏导航、任务详情、查看全部及在线诊断仍主要是静态表现；环境面板只呈现摘要，完整检查条目未在草图中重制。
- 旧草图的静态与浏览器通过记录仅说明该历史草图可交互，不能用于证明现有 App 或修订后的设计已通过验证。
- 本轮未改产品源码、冻结规格或手机草图，未做构建、安装、提交、推送或发布。尚未验证原生候选实现、真实手机、实际执行端、浅色外观、当前修订稿的暂停 / 恢复及宿主 Tweak 控件切换后的空事项 / 无推进任务状态；浏览器草图通过不替代这些证据。

## 实施与验收（2026-11-11）

- 基线：从已 fetch 的 `origin/main` `17a38476b9c6ba5fef1dc75c9261e46f38f09f29` 创建独立工作树，避免带入主区独有的 KP-WT01 规格提交 `35cc2fc`。
- 分支：`feat/20261111-kp-ui09-macos-overview`；工作树：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261111-feat-kp-ui09-macos-overview`；预期合入目标 `main`；用户已明确允许提交/推送本分支、运行仅产预览 artifacts 的云端构建和隔离原生界面核验。未授权合并或正式/测试版发布。
- 主区既有 `.local/`、`docs/plans/kp-cache01-macos-build-cache-cleanup.md` 保留。本会话唯一 UI09 记录已核对内容后搬至本工作树，主区不留副本。
- T1：先同步 `docs/ui-spec.md` §2/4.15/4.16 的四项局部决定、项目身份/去重/状态桶口径。
- T2：纯项目聚合与 fixture，由 Sol worker 独占 `Core/Models.swift`、`macos/tests/dashboard_status_main.swift`、`Views/TaskCompactListView.swift` 的项目键转发；复用既有项目身份规则。主 agent 独占 Root/Dashboard/Components 与规格记录；文件不交叉写。无需独立端口、数据库或服务。
- T3：主 agent 实现左下角自动推进/环境入口、无尾箭头面板、需要你信息排版、项目分组。诊断数据与明细展示提取为现有编译源中的共享组件，避免两套健康判断；不新增核心读取或诊断循环。
- T4：独立只读审查最终组合态；本地仅做无编译静态核验。项目强制 Swift/Android 编译仅走云端 Packages，既有 dashboard fixture 已接入该工作流，不在本地运行 swiftc/Xcode/Gradle。
- T5：已获任务分支推送授权，跑仅产生 artifacts 的 preview Packages；核对所有 jobs、源 SHA 与包 metadata/hash，再下载独立 bundle 用 FakeCore/临时 HOME 核验 1180×760、900×600、深浅色，保留 `/Applications/KaneoPilot.app` 与真实数据。最终 #207 已完成四种窗口/外观组合的原生检查；整条 CI 的既有样本新鲜度失败见下方。

验收场景：项目展开/折叠和刷新后保持、任务去重、同名不同 ID、不完整 attention 的项目名回退、已排队不算执行中、已完成/保存任务排除、空项目进展；需要你换行与键盘入口、原详情五段；左下角运行/暂停徽标、原有禁用条件、环境入口 Return/点击展开与 Esc/关闭/再次点击收起和焦点恢复；诊断未检查不计通过、异常继续首页显式展示；窄窗口/深浅色无溢出、正文不被面板挤动。

验证范围变化依据：首页已移除配置摘要，并用全项目分组替代原推进最多 5 行，因此旧摘要文本/五行上限断言随已删除展示退出，新 fixture 应验证聚合行为；不降低原状态标题、告警计数或其他门禁。

## 当前进展与剩余

- T1–T4 已完成本轮实现与独立复核；T5 的 #207 包核验、四种窗口/外观及相关原生交互已完成。整体状态仍为**实现中**：当前 UI 已有可体验预览，整条云端门禁仍因两项既有样本新鲜度测试失败；未将其改成通过或标记用户已验收。原生检查绑定代码 `89a91b0`，不沿用旧包通过记录。
- 当前实现：`RootView.swift` 用窗口根锚点 overlay 向上呈现面板，避免列裁剪与系统 popover 尾箭头；`Components.swift` 共用环境状态投影、告警与诊断明细；Dashboard 移除配置摘要与额外项目映射读取，新增可折叠项目分组；共享项目键沿用任务列表规则。
- 独立 Sol 审查发现 Tab 到事项行后容器焦点守卫会屏蔽↑↓；已改为每事项 attentionId 显式焦点，箭头更新行焦点，事项/项目头/环境入口分别绑定获焦时的 Return；独立复核通过，未发现新的明确问题。侧栏消失时收环境面板，关闭后焦点回入口。
- 本次本地静态核验：最终修改阶段 `git diff --check` 与 `bash -n macos/tests/run-dashboard-status-fixtures.sh` 通过；逐项比对原暂停/刷新/诊断命令和禁用条件、告警组成、详情容器，未改核心与 Android。没有本地编译；云端 fixture 和原生候选包的代码与证据分别记录于下方。
- 用户已对具体异步问题答复“允许，继续云端构建与界面验证”。本轮继续提交/推送本任务分支、跟进 preview Packages、下载和核验同 SHA 隔离包。尚未发布或替换正式 App；合并与发布不包含在此次授权内。

### 首轮云端与原生检查

- 代码 `7087b1f7361dc1968736dc843336bebca3da6012`，Packages [#204](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/42996132003)。macOS package、Android、macOS fixtures a/b 和 Python 其余八个分片通过；Python shard 1 在 3.11/3.13 均失败。后续推送触发并发取消，codex-platforms 与 publish cancelled，工作流最终 cancelled，不能声称整条通过。下载 `macos-update-ui-204`，source SHA/build 204/ZIP size/SHA-256/独立 bundle ID 与深度签名均核对通过，证据 `build/kp-ui09/artifacts/204/verification.json`。
- 使用 FakeCore 和独立 HOME、全部核心根目录、空凭据文件，手机服务 disabled；`/Applications/KaneoPilot.app` 生产进程保持。3 项活跃假任务、1 项 actionable；1180×760 深色已看到三项目分布、原因换行与下方元信息；项目折叠与刷新后保持、无尾箭头环境面板向上展开和滚动诊断明细均实测。截图在 `build/kp-ui09/ui/1180x760-dark/`，此轮截图仅对应 7087b1f。
- 发现缺口：Esc 可收环境面板，但 Return 不重开。通过 Raise 确认窗口前台后仍复现；独立审查支持 focusable 目标与恢复时机问题，未把系统设置视为已证明根因。修复为四类新增键盘目标显式 `.focusable()`，打开时先清入口焦点，面板出现后下一主队列设关闭焦点；关闭时先清入口焦点、面板移除后再恢复，且过期恢复不抢新面板。未改用户系统设置。
- 首轮隔离 App 已正常退出。后续须用含修复的新云端包复测，不以 #204 编译或截图代替新代码验证。

### 第二轮构建与基线问题

- 焦点修复代码 `7edce33ffe56a0287bb0f12bb8d7695f3ae717c2` 已提交并推送同一任务分支，Packages [#205](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/42998111536) 已结束，最终 failure。identity、macOS package、macOS fixtures a/b、Android、codex-platforms 和 Python 其余八个分片全部通过；Python shard 1 在 3.11/3.13 失败，session fulltext probe 与 publish skipped。全部 jobs 证据 `build/kp-ui09/artifacts/205/workflow-status.json`；dashboard fixture 的云端日志已下载，明确输出 `dashboard status fixtures passed`。
- 下载 `macos-update-ui-205`，source SHA/build 205/ZIP size/SHA-256/独立 bundle ID、plist 与深度签名核对通过，证据 `build/kp-ui09/artifacts/205/verification.json`；ZIP SHA-256 为 `21dfd6f4c1435cd3589ef19bee35bf09963efa82da618c5235943ab83aafdd91`。包核验不能代替原生界面验证。
- #204 的两个失败均为录制样本新鲜度门：两版本各 `2 failed, 75 passed`。三份 reconstructed 样本和六份真实录制均采于 2026-10-10，2026-11-11 为 32 天，超过现有 30 天门限。主 agent 核对九个文件及两个测试在基线 `17a3847` 与当前 `7edce33` 内容完全一致，证据 `build/kp-ui09/baseline-fixture-evidence.json`；不是本次 UI 改动引入。
- 独立只读分析确认三份重建 parser 视图与六份 Plus 0.15.7 原始录制的契约不同，当前 Plus 0.15.16 可能有新增字段。真实重采和契约核对属于独立维护工作；UI09 保留原测试与日期，不放宽新鲜度、不重写历史事实，也未连接 Plus 或创建付费轮次。
- #205 的两个失败 job（`130447519000`、`130447519064`）日志亦已下载：均为同两个新鲜度函数失败，各 `2 failed, 75 passed`，其余检查未新增失败。日志位于 `build/kp-ui09/artifacts/205/job-*.log`。
- #205 已用隔离 FakeCore/独立 HOME 启动；首次读取原生界面时 Mac 锁屏，工具无法继续。已请用户手动解锁；本轮没有取得 #205 原生界面或焦点修复通过证据，1180×760/900×600 × 深浅色及其交互检查仍待完成。
- 本轮停止前，核对 PID 对应云端独立测试 App 后终止隔离进程 `19397`，确认退出；正式 `/Applications/KaneoPilot.app` 进程 `35156` 保持运行。启动与锁屏阻塞记录 `build/kp-ui09/ui/205/1180x760-dark/{launch,blocked}.json`；没有安装、重签、合并或发布。
- 下一步：用户解锁后，沿用已核验 #205 云端包，创建新的隔离 case（保留既有启动记录），先完成打开→Esc→Return 重开及关闭后焦点回入口，再覆盖两尺寸/深浅色、事项键盘入口/五段详情、项目折叠刷新、异常和空态。若当前包仍复现焦点缺口，按新证据修复并云端重建；不得引用 #204 截图冒充修复后通过。
- 本工作树保留：本任务成果尚未合入 `main`，且有未完成的原生验收和仅存于忽略目录的包/证据，因此不满足清理条件；未清理其他任务工作树。合并与发布仍需本次范围之外的用户授权。

### 解锁后复测与 Return 路由修复

- 用户回复“已解锁”后，创建 `build/kp-ui09/ui/205/1180x760-dark-attempt-2/`，保留锁屏时的首个 case。#205 环境面板打开后关闭按钮获焦，Esc 后入口获焦，Return 可重开；Return 关闭、再次点击入口收起均实际通过。无尾箭头向上面板、需要你长原因换行与下方元信息、正常环境不占正文均已查看。
- 项目鼠标折叠后刷新保持；Tab 到事项后 Return 打开详情，原概览 / 计划 / 执行 / 评审 / 记录五段逐一读取通过。没有触发重新执行、真实模型或外部写入。
- 新发现：关闭事项详情后 Tab 到 Music 项目头，AX 明确焦点在项目头；Return 却打开上一事项详情。证据 `project-keyboard-return-failure.txt`。独立只读复核确认事项 action 强制容器焦点、三个独立 FocusState 与窗口级无修饰 Return 快捷键可同时保留旧路由。
- 必要修复：Dashboard 用单个 Hashable enum 表达容器 / attentionId / projectId 互斥焦点；事项、项目头、环境入口、面板关闭均使用本地 `.onKeyPress(.return)` 并返回 `.handled`，移除这四类新增控件的窗口级 Return 注册。父 Return 仅处理容器焦点且没有已开详情；↑↓ 仅在容器或事项焦点处理，项目焦点不参与事项导航。事项开详情前保留该事项焦点；环境 Esc 与先清焦点、再异步恢复入口沿用已测路径。
- #205 的本轮隔离 App 已用 Quit 正常退出；当前修改静态差异检查通过，待独立复核及新云端包实测。未把 #205 原生通过项当作当前修复后的验收。

### #206 键盘回归与焦点可见性

- Return 路由修复代码 `005e89a636970fc3846755693102074d63b5b0d1`，Packages [#206](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/43005784916)。macOS package 通过；同 SHA/build 206/ZIP size/hash/plist/bundle ID/深度签名均核对通过，证据 `build/kp-ui09/artifacts/206/verification.json`。后续推送触发并发取消，工作流最终 cancelled，全部 jobs 结论在 `build/kp-ui09/artifacts/206/workflow-status.json`；不能声称整条 CI 通过。
- `build/kp-ui09/ui/206/1180x760-dark/` 已实测：环境打开→Esc→入口焦点→Return 重开，关闭按钮 Return 收起；Tab 到事项→Return 打开详情→Esc→Tab 到项目头→Return 正确折叠当前项目，再 Return 展开且刷新后保持。与 #205 的错路由复现一致的路径现已通过。
- 同一隔离 case 临时加入另两条 actionable：Shift-Tab 从项目头到最后事项，Up/Up/Down 后焦点在第二事项；Return 打开的确是该事项详情。AX 与初始 state 备份分别保存，未调用真实执行端。隔离 App 已用 Quit 正常退出。
- 发现项目头焦点不可见：父 Dashboard 静态 `.focusEffectDisabled()` 同时屏蔽新增子控件原生环。独立只读核查 [Apple 官方说明](https://developer.apple.com/documentation/swiftui/view/focuseffectdisabled(_:)) 确认高层设置会覆盖子层；因此不是在子层重复加 false，而是父层仅在 `focusedTarget == .dashboard` 时隐藏整页大框，让事项 / 项目焦点显示原生效果。不改变字体、颜色或交互规则；该必要单行修复仍须新云端包查看实际焦点环。

### #207 最终预览与原生检查

- 最终实现代码 `89a91b0e8a51ef4bb0f788648f364b558ad99320` 已提交、推送任务分支。Packages [#207](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/43007841949) 已结束，最终 **failure**：18 个 jobs 中 14 success、2 failure、2 skipped。identity、macOS package、macOS fixtures a/b、Android、codex-platforms 和其余八个 Python 分片通过；失败仅 `checks (1, 3.11)` / `checks (1, 3.13)`；session fulltext probe / publish skipped。完整证据 `build/kp-ui09/artifacts/207/workflow-status.json`。
- 两个失败 job `130479793638` / `130479793652` 日志均已下载，仍为 `test_scenario_real_shape_fixture_staleness_gate` 与 `test_scenario_real_recordings_freshness_and_shape`，各 `2 failed, 75 passed`。2026-10-10 的样本在 2026-11-11 为 32 天，超过 30 天门限；九件样本及两个测试与基线完全一致的核对已绑定当前 SHA，见 `build/kp-ui09/baseline-fixture-evidence.json`。未改采集日期、历史事实或门禁，真实重采及 Plus 版本契约核对仍属独立维护事项。
- 下载本轮 `macos-check-reports-207-fixtures-a`，`fixture-reports-a/run-dashboard-status-fixtures.log` 明确输出 `dashboard status fixtures passed`；本轮没有本地编译。该云端 fixture 覆盖项目身份、同名不同 ID、去重、attention 项目回退、状态桶、已完成/保存排除和空分组，不能代替下述原生界面证据。
- `macos-update-ui-207` 的源 SHA、build 207、独立 bundle ID `local.kaneopilot.software-update-check`、plist、ZIP 大小 `31898238`、SHA-256 `32baf44ecfd28466474d30ff943481e1cb0b2fd17a45881ae89ba0baa44791b5` 和 `codesign --verify --deep --strict` 均核对通过，见 `build/kp-ui09/artifacts/207/verification.json`。未重签、修改 bundle 或安装到 Applications。
- 四个隔离 case 均以该包启动，各自独立 HOME、FakeCore state/命令日志、核心目录和空凭据，手机服务 disabled。`launch.json` 源 SHA 全为 `89a91b0`；证据根为 `build/kp-ui09/ui/207/`。两尺寸截图实际分别为 2360×1520、1800×1200 像素（2×）；截图 API 返回 JPEG 字节，沿本轮既有 `.png` 文件名保存。

| 原生窗口 / 外观 | 本轮观察结果与证据 |
| --- | --- |
| 1180×760 深色 | 项目分组、长原因换行、需要你下方元信息、正常环境不占正文均已查看。环境打开后关闭按钮获焦，Esc 回入口、Return 重开、Return 关闭、点击关闭及再次点击入口收起均通过。事项 Tab/Return 打开正确详情，原概览/计划/执行/评审/记录五段逐一选择并保存 AX。关闭详情→Tab 到项目→Return 正确折叠，刷新保持，再 Return 展开；事项和项目的蓝色原生焦点环已实际查看（`attention-focus.png` / `project-focus.png`）。加入另两条隔离 actionable 后，Shift-Tab、Up/Up/Down 到第二事项，Return 打开的确为该事项。 |
| 1180×760 浅色 | 首页与三项目布局已查看；左下角面板向上展开、无尾箭头、正文位置不变，点击关闭后入口焦点恢复。截图 `overview.png` / `environment-open.png`。 |
| 900×600 浅色 | 无横向溢出，左下角入口与主要区域可用。隔离暂停/恢复的橙色“已暂停”与绿色“运行中”徽标、说明正确，事项仍优先显示。诊断明细展开后面板在窗口内，滚动至末项 Kaneo 连通性“未检查”，摘要保持 6 通过 / 1 未检查；再次点击入口收起并恢复焦点。截图 `paused.png` / `environment-details.png` / `environment-details-bottom.png`。 |
| 900×600 深色 | 首页与环境面板已查看，无横向溢出。Esc 收起后入口恢复焦点。隔离模拟心跳丢失并刷新后，首页标题显示环境告警、环境区显式出现，左下角显示红色心跳未确认，未被正常摘要折叠隐藏（`heartbeat-loss-refreshed.txt` / `heartbeat-loss.png`）。恢复心跳、清空 attention 并将夹具任务归档后，“需要你”整块隐藏、项目进展显示 0 个项目及空态（`empty.txt` / `empty.png`）。所有状态变更仅在该 case 的 `state.json` 原子替换，保留前态。 |

- 四个 #207 隔离 App 均用 Quit 正常退出。收尾发现锁屏时 #205 首个 case 遗留 FakeCore PID `19840`；核对绝对命令、PPID、工作目录和该 case 的 engine marker 后，仅对该进程发送 SIGTERM，确认退出及 marker 为 stopped，见 `build/kp-ui09/ui/cleanup.json`。最终没有本任务预览 App / FakeCore 残留，正式 `/Applications/KaneoPilot.app` PID `35156` 保持；核对结果在 `ui/207/process-verification.json`。
- 当前剩余：CI 样本维护阻塞、用户对真实候选界面的体验确认；真实调度、Plus 执行、手机设备与正式安装不属于此次隔离 UI 核验。预览包与上述截图可供体验，但不能据此称整条 CI 通过、已验收或已发布。
- 工作树继续保留：成果未合入 `main`，且忽略目录内仍有唯一包与原生证据；尚不满足清理条件。未合并、发布 Release/prerelease、替换正式 App 或清理其他工作树。后续仅在另有相应授权并满足验证后合入，随后按保全规则收尾本任务工作树。
