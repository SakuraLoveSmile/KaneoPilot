# KP-SV01：实时查看会话内容并发布 0.5.0

- 状态：待体验；创建日期：2026-11-07（Asia/Taipei）。本文件是本任务唯一记录。正式版 v0.5.0 已发布，适用验证与工作树收尾已完成；未标记用户验收。
- 用户目标：在截图的任务详情「执行」页直接查看实时会话内容，并按项目工作流发布正式版 `v0.5.0`；包含提交、推送、PR、合入 main、云端构建、正式发布与本任务工作树收尾。
- 基线：`05027190c9752c4e608d350ecd3aaf258eea3707`，远端 main；主工作区已有 `.local/` 和缓存清理计划为其他工作，保留。
- 分支：`feat/20261107-kp-sv01-live-session`；工作树：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261107-feat-kp-sv01-live-session`；已合入 main，本地任务分支与工作树已移除，远端来源分支保留。

## 已确认决定与依据

1. 独立会话页、详情内嵌页共用 `SessionReaderView`。保持任务标题、轮次、正常/回收站筛选、刷新和管理动作；默认显示「会话内容」，可切换「会话状态」。状态页继承现有布局与恢复操作，内容页复用已有消息列表、搜索、长消息/工具折叠和原文窗口，沿现有字体/间距，不换肤。
2. 内容只读。复用 protocol §5.15.5 的 `get-session(status_only:true)`、`session-messages`、游标、source_version、ID/revision 与默认 2 秒轮询；不增加 DB schema、依赖或新的写入通道，不调用模型、不发送运行端消息。状态和正文均持续更新，失败保留旧内容并提示可能过期；切换状态页暂停正文读取，返回内容页继续增量读取；切换轮次丢弃过期请求。
3. 内容按运行端已导出的消息更新，不承诺逐 token。Codex/PI/Plus 执行有本地事件；Qoder 只显示其提供的正文。Codex Desktop 无原生对话桥接，在内容页明确说明未导出对话、可复制会话 ID 到 Desktop 查看。已核实 Plus 规划/评审控制回合在结束后才导出，不宣称这些回合的正文运行中可见。
4. 保留重新执行、Plus 状态核验和恢复的全部既有权限及并发守卫；启用正文不得隐藏原有功能。ui-spec 的 2026-10-08 仅状态要求由本次用户新增正文请求取代，相应更新权威规格。
5. Swift/Android 编译及打包仅走云端 Packages；本地仅运行隔离 Python 检查、包校验和云端已签名独立身份 App 的 FakeCore 原生核验。真实调度库、已安装 App 和其他运行进程不动。

## 实现与验收

| 步骤 | 负责结果与归属 | 验收场景与交付 | 状态 |
| --- | --- | --- | --- |
| T1 | analyst 只读核实后端事件/边界；主 agent 冻结本记录及 ui-spec/protocol | 已核实可复用入口、导出时机、数据影响及边界；不新建外部桥接 | 待体验 |
| T2 | worker 独占 SessionReaderStore.swift、session_reader_main.swift；主 agent 独占 SessionReaderView.swift、FakeCore 与规格 | 代码及隔离夹具已接入，最终源码云端 16 个必需 job 成功；Store 行为检查通过 | 待体验 |
| T3 | 主 agent 集成与原生核验，reviewer 只读检查实际差异 | Python 95 项、最终候选 #150 云端和原生默认/最小窗口/详情内嵌路径通过，修复历史锚点缺口；无真实模型或生产数据 | 待体验 |
| T4 | 主 agent 提交、PR、合入与发布 | PR #10/#11 已合入 main，最终 #152 与正式 #153 云端通过；签名 annotated v0.5.0 已公开为 latest，四项公开资产和两端包签名核验通过，任务 worktree 与本地分支已清理 | 待体验 |

共享 Store 接口：初始化默认 `statusOnly:false`，`@Published private(set) statusOnly`；新增 `setStatusOnly(_:) async` 供视图切换，保留消息/滚动状态并使在途请求失效。内容轮询同时更新状态，重试权限不再以是否展示内容作为条件。专家自行完成原子任务、不继续派生；同一文件唯一写入者。所有核心/界面协议修改由主 agent 维护。

## 验证与运行资源

- 本地测试使用项目临时目录/隔离 DB；开始前核实夹具，不使用真实付费执行端。无编译命令在本地运行。
- 原生 UI 使用云端 `macos-update-ui-<run>` 独立 App、独立 HOME/状态/空凭据、FakeCore 与关闭的调度器；数据与日志保存在主仓库 `build/kp-sv01/`，不保留临时 worktree 的运行引用。
- 适用验证完成后状态为「待体验」；只有用户明确确认才为「已验收」。发布成功不能代替真实模型、真实生产数据或 Android 实机验证。

## 执行记录

- 2026-11-07：核对本地/远端 main 同为基线，当前 latest 为 v0.4.9，v0.5.0 尚无标签；按命名工具核对无碰撞后创建隔离工作树。分析确认旧 UI 明确禁用正文；核心已有分页增量读取。
- T1/T2：已接入默认内容页、搜索、跟随控制、内容/状态切换与原状态操作；Store 两种模式均轻量读取状态，正文模式额外增量读取。模式切换保留正文/游标/锚点，落地消息后才提交游标，防止后台准备期间切模式漏页；新增云端 Store 夹具覆盖此链路。Plus 控制回合结束才导出的既有边界保留，不扩外部桥接。
- 本地专项：首次 `python3 -m pytest <文件>` 因仓库 shim 无真实 pytest、仅接受 discover 目录而未启动测试；随后按既有 unittest 入口运行 `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=core:core/tests python3 -m unittest test_session test_session_status test_desktop_event_projection test_desktop_session -q`，95 tests / 24.228s，退出 0。夹具临时目录和 FakeKaneo，未连接真实服务。核心无改动，该结果验证复用接口的现有分页、修订、状态只读与 Desktop 记录边界；Swift 尚待云端。
- 独立审查发现模式切回时消息列表首次挂载没有消费已有阅读锚点；主 agent 已修正 onAppear 优先恢复锚点，再按需跟随，待原生核验无新消息时的恢复。
- 审查另指出短会话在显式暂停后，布局距底为 0 会取消暂停；已新增独立的显式暂停意图，仅「回到最新」、真实手动滚动或切换会话解除，并补行为夹具。未调整检查门禁、未删除或跳过既有测试。
- `bd667db2567dfe33d4b7616fd56b8d06042ca278` 云端 Packages [#143](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/42339337802) 全部 16 个必需 job 成功，全文可选探针与分支发布按条件跳过。独立签名 App metadata 的源码/build=143 匹配，ZIP 31,706,784 bytes，SHA256 `ef74c882229746ea3ce1ca56cedfb6689ab3ac27d947e670698e31ca6a4f0744`；严格深度签名验证通过。未本地编译、未安装覆盖正式 App。
- #143 原生隔离核验（FakeCore、空凭据、独立目录、引擎停用）：1180×760 dark 的独立阅读器与任务详情执行页均可读消息；自动追加 32→33、上滚后追加 34 条保留上下文并出现新内容按钮、回到最新、LIVE-B 搜索唯一匹配、全文窗口、无新消息时切状态/内容恢复历史锚点通过。状态页 13.45s 期间正文请求恒为 44，状态请求 55→61，证据 `build/kp-sv01/native/1180x760-dark-content/status-mode-verified.json`。
- 900×600 light 初始与短会话显式暂停后追加通过（仍暂停、有新内容提示），读取失败保留 2 条正文和可能过期/重试提示，恢复后 Desktop 未导出提示正确。但失败横幅后窗口被内容最小高度撑到约 900×659，不能冒充严格最小窗口通过。只读分析指向正文最小尺寸上传，已在阅读器内容区和正文 ScrollView 显式允许 minHeight=0，待新云端编译与该场景复验；不改全局窗口规则。证据均保留在主仓库 `build/kp-sv01/` 与本会话 CUA，首轮两个预览进程均已退出。
- `cff10ddb30ce7db19b02732aaa6112b709b7aa8c` 的 #145 独立签名 App 通过来源/hash/签名核验（ZIP 31,707,351 bytes，SHA256 `bb857f50a170b7173f8272879b955a9529cffe8717035e9e1db3e5e26d0b5f4e`）。正文 minHeight=0 修正仍未解决长错误提示撑高：相同 2 条短消息，`read_error=E` 保持 900×600，较长错误提示上撑到约 900×799；新增证据指向横幅无界换行参与窗口最小尺寸计算。沿现有顶部横幅模式，移除错误正文的固定垂直尺寸、最多显示两行，悬停查看完整错误，右键「复制错误详情」，保留文本选择/无障碍全文；只读 analyst 复核支持此方向。继续云端与原生复验，不改全局窗口规则或验收尺寸。
- 横幅修正候选 `c31fe10d949c8e279873441286cbebb5d18682ae` 的分支 [Packages #147](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/42341663710) 与 PR [#148](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/42341668046) 均全部 16 个必需 job 成功；可选全文探针及分支 publish 按条件跳过。该轮增量独立复核未发现发布阻塞，未修改或降低检查门禁。
- #147 独立签名 UI App 的 ZIP 31,709,574 bytes、SHA256 `2aac51851dde693a22927a20522b9ca9e2b1cfcb397ec5eba40df5442d738f63`，metadata 的源码/build=147 匹配，严格深度签名核验通过。最终 900×600 light：初始 1 条、显式暂停后增量追加 2 条并出现新内容提示通过；同一长错误及显式十行错误均保留两条正文、过期/重试提示与暂停意图，截图均为 1800×1200 像素。AX help/value 完整含 `LONG-ERROR-END`；选择正文后优先出现系统原生 Copy 菜单，原生选区包含完整错误，但点 Copy 时 Mac 锁屏，尚未完成实际粘贴核对。证据：主仓库 `build/kp-sv01/native-147/900x600-light-short/observations.json`，环境/夹具/请求日志同目录。
- [PR #10](https://github.com/SakuraLoveSmile/KaneoPilot/pull/10) 已合入 main：`b3181bc7a1fda85ab804c43d4436cf2f7bd75787`。`git diff c31fe10 origin/main` 为空、来源为 main 祖先；合入提交的 [Packages #149](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/42342954698) 全部 16 个必需 job 成功。主工作区已快进同步，原有 `.local/`、缓存清理计划均保留。
- 发布准备：说明已在 `docs/releases/v0.5.0.md`；本地 `build/kp-sv01/verify-release.py` 准备核对公开四项资产、manifest、SHA256SUMS、GitHub digest、版本/源码/build/latest 及 Android 与 v0.4.9 的证书延续。APK 核验可使用现有 `/opt/homebrew/opt/openjdk@17/bin/java -jar /Users/sakurasep/Library/Android/sdk/build-tools/36.0.0/lib/apksigner.jar`，避免本机失效的 jenv shim；未修改全局 Java 配置。

- 解锁后 #147 的 900×600 light 完整错误复制/粘贴、清除错误自动恢复、状态/内容往返和新内容按钮通过；证据已更新同目录 observations.json。1180×760 dark 的 32→33 实时追加及暂停通过，但原生滚动到 0.4（step_0010…0018）后，状态/内容往返却回到首条，30 秒后仍在顶部。此结果取代首版对最终锚点的推断，作为发布前必须解决的缺口。
- 锚点缺口只读分析：GeometryReader 行偏好不随 macOS 原生滚动更新；原生 clip 观测仅回传距底，造成 readingAnchor 陈旧。另外历史定位未设置 settling 保护，可被中间 distance 回调取消。修正采用稳定内容坐标的行布局 + 原生实际 viewport 联合选取锚点，并给历史恢复批次同等保护；不改变页面风格、核心接口或数据。
- 追加修复已实现：行框保持在稳定内容坐标，原生 viewport 单独变化也更新锚点；状态页/搜索重排/分页/待恢复及定位批次期间阻止临时几何覆盖。所有定位均受批次保护，真实手动滚动仍能取消。针对性 Swift 夹具加入纯视口滚动、模式往返、临时贴底与用户打断；没有删除/跳过原检查。主 agent 检查组合差异与 diff --check 通过；新增 Swift 夹具与原生结果待此提交的云端候选核验。

- 阅读位置修复 `c8645a447a77932787f069fa5aa0b823e5edac9b`：分支 [Packages #150](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/42347293415)、[PR #11 / #151](https://github.com/SakuraLoveSmile/KaneoPilot/pull/11) 均全部 16 项必需 job 成功；独立静态组合复核未发现阻塞，未降低门禁。PR #11 合入 main `1f77caa7807bfcfef720b80b8f76e160da0a7002`，与候选源码树完全一致；[main #152](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/42348828048) 也全部 16 项必需 job 成功。
- #150 独立签名 UI ZIP：31,712,464 bytes，SHA256 `f8458f53fb0195bd400b3d3e354cef3d69ff9403c5dcfc7ef8b94fc587f6b168`；下载因网络缓慢先保全片段，最终四段组合通过 GitHub artifact 整包 digest、ZIP metadata 来源/size/hash 和严格深度签名核验。未重签名或本地编译，独立 HOME/目录与 FakeCore 同前。
- 最终 #150 原生 1180×760 dark：未点击暂停按钮、直接原生滚动到历史后，真实 viewport 对应 `m00013`；状态/内容往返在有/无新增消息两种情况下均恢复该消息，暂停跟随保留。搜索新消息唯一命中，清除恢复历史；原文窗口 Escape 返回原位置；有新内容按钮回到底部，持续追加 33→34 自动跟随。状态页 28.59s 内正文请求 31→31，状态检查额外 14 次。任务详情执行页正文可读，34→35 实时追加通过。sheet 滚动条 AX setter 在 fresh tree 后仍两次报告元素失效，未将它记为内嵌历史位置实测；共用阅读器的原生历史路径已在独立页验证。
- 最终 #150 原生 900×600 light：短会话 1→2 条、显式暂停和新内容提示保留；十行错误保留正文、过期/重试提示和暂停意图，截图严格为 1800×1200 像素；AX help/value 保留完整错误；清除错误自动恢复通过。完整错误复制/粘贴已在同一横幅实现的 #147 原生验证通过，本轮未机械重复。默认预览正常退出；最小窗口预览核验后向其独占 PID 发送 SIGTERM 并确认退出。全部新观察在主仓库 `build/kp-sv01/native-150/`，无活动预览进程。
- 正式发布已启动：从干净 main 提交 `1f77caa7807bfcfef720b80b8f76e160da0a7002` 创建并验证签名 annotated `v0.5.0`，标签直接指向此提交，已推送；[Packages #153](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/42350232674) 正在构建。创建前核对 latest=v0.4.9、无 v0.5.0 碰撞及并行稳定发布；没有移动或覆盖标签/资产。
- 正式发布完成：[Packages #153](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/42350232674) 的全部 16 个必需 job 与 publish 成功，可选全文探针按条件跳过；公开 [v0.5.0 Release](https://github.com/SakuraLoveSmile/KaneoPilot/releases/tag/v0.5.0)（ID `437413800`）为非 draft、非 prerelease 的 latest，发布于 `2026-11-07T07:27:18Z`。产品源码为 `1f77caa7807bfcfef720b80b8f76e160da0a7002`，build 153；签名 annotated 标签直接指向该提交且 GPG 验证成功。工作流结论保存于 `build/kp-sv01/ci-42350232674.json`。
- 公开四项资产已完整下载并通过 GitHub digest、SHA256SUMS、manifest、源码/版本/build/latest 一致性核验。DMG 39,951,738 bytes，SHA256 `5a679617e176bce93b40d30f20446d31eaba42d2aa102edd3ffe2854c229f877`；APK 8,557,684 bytes，SHA256 `3139cd2a7bfebce7582be787cc576f57e41550ace61ab7859f80a25a095168e4`。下载及身份结果在 `build/kp-sv01/release-0.5.0/verification.json`，四项原包和清单同目录保留。
- 安装包只读核验通过：DMG 完整性、DMG 签名指纹与 manifest 一致；挂载内 App 严格深度签名、`local.kaneopilot`、0.5.0/build153、仅 arm64 正确，已卸载只读挂载。Android `com.sakurasep.kaneopilot`、0.5.0/versionCode153、APK 签名正确，实际证书 `5062db967ce58d65df2c47895649df43464bab9a6eadf135f3a1a81ff4785ada` 与 v0.4.9 延续一致；未安装覆盖正式 App。结果和命令日志在 `build/kp-sv01/release-0.5.0/package-verification.json` 及同目录日志。
- 公开说明只移除了从签名标签注释带入的 PGP 签名块，保留所有功能说明、安装要求和 `kaneopilot-ci:<sha>:153` 标记；读回确认 Release 身份、标签及全部资产均未改变，前后正文证据保留在公开包目录。
- 工作树收尾：再次核对来源 `c8645a4` 为 main 祖先且产品源码树一致，任务工作树无暂存、未暂存或未跟踪成果、无运行文件引用，三个已忽略 Python 字节码已归档并逐项核对 SHA256。随后从主仓库正常 `git worktree remove` 并 `git branch -d feat/20261107-kp-sv01-live-session`；目录和本地分支均不存在、worktree 列表已核对，远端来源分支保留。证据为 `build/kp-sv01/worktree-cleanup.json`、`worktree-cache-snapshot.json` 与可恢复的 `worktree-cache-snapshot.tar.gz`；其他工作树、用户原有文件及主仓库全部恢复材料保留。

## 交付与恢复

- 体验入口：任务详情 → 执行 → 会话内容，或独立执行会话页；切换「会话状态」查看原状态及恢复操作。
- 本次不升级数据结构，原始执行记录和调度数据保留；回退使用旧正式安装包，不改变数据库。
- 已完成：历史位置缺口修复、最终候选原生复验、正式 #153 构建/发布、四项公开资产和两端安装包身份/签名核验，以及本任务 worktree 收尾。
- 用户体验：升级 [v0.5.0](https://github.com/SakuraLoveSmile/KaneoPilot/releases/tag/v0.5.0) 后，从上述入口查看已有会话；内容按运行端已导出的记录默认每 2 秒更新。仍未使用真实模型/生产任务、未安装覆盖正式 App、未做 Android 实机验收；这些边界不以云端构建或 FakeCore 核验冒充通过。
- 保留的运行资源：#147/#150 独立 App、下载片段、JSON 证据、FakeCore 状态/请求日志在主仓库 `build/kp-sv01/`，均不进产品提交；本任务所有预览已退出。正式 App 与其他任务进程未操作。
- 本任务 worktree 与已合入的本地来源分支已清理，远端分支、其他任务与主仓库全部恢复证据保留。后续只有用户明确确认后才将状态改为「已验收」。
