# KP-SV01：实时查看会话内容并发布 0.5.0

- 状态：实现中；创建日期：2026-11-07（Asia/Taipei）。本文件是本任务唯一记录。
- 用户目标：在截图的任务详情「执行」页直接查看实时会话内容，并按项目工作流发布正式版 `v0.5.0`；包含提交、推送、PR、合入 main、云端构建、正式发布与本任务工作树收尾。
- 基线：`05027190c9752c4e608d350ecd3aaf258eea3707`，远端 main；主工作区已有 `.local/` 和缓存清理计划为其他工作，保留。
- 分支：`feat/20261107-kp-sv01-live-session`；工作树：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261107-feat-kp-sv01-live-session`；合入目标：main。

## 已确认决定与依据

1. 独立会话页、详情内嵌页共用 `SessionReaderView`。保持任务标题、轮次、正常/回收站筛选、刷新和管理动作；默认显示「会话内容」，可切换「会话状态」。状态页继承现有布局与恢复操作，内容页复用已有消息列表、搜索、长消息/工具折叠和原文窗口，沿现有字体/间距，不换肤。
2. 内容只读。复用 protocol §5.15.5 的 `get-session(status_only:true)`、`session-messages`、游标、source_version、ID/revision 与默认 2 秒轮询；不增加 DB schema、依赖或新的写入通道，不调用模型、不发送运行端消息。状态和正文均持续更新，失败保留旧内容并提示可能过期；切换状态页暂停正文读取，返回内容页继续增量读取；切换轮次丢弃过期请求。
3. 内容按运行端已导出的消息更新，不承诺逐 token。Codex/PI/Plus 执行有本地事件；Qoder 只显示其提供的正文。Codex Desktop 无原生对话桥接，在内容页明确说明未导出对话、可复制会话 ID 到 Desktop 查看。Plus 规划/评审的导出时机需核实，在有证据前不宣称运行中正文可见。
4. 保留重新执行、Plus 状态核验和恢复的全部既有权限及并发守卫；启用正文不得隐藏原有功能。ui-spec 的 2026-10-08 仅状态要求由本次用户新增正文请求取代，相应更新权威规格。
5. Swift/Android 编译及打包仅走云端 Packages；本地仅运行隔离 Python 检查、包校验和云端已签名独立身份 App 的 FakeCore 原生核验。真实调度库、已安装 App 和其他运行进程不动。

## 实现与验收

| 步骤 | 负责结果与归属 | 验收场景与交付 | 状态 |
| --- | --- | --- | --- |
| T1 | analyst 只读核实后端事件/边界；主 agent 冻结本记录及 ui-spec/protocol | 已核实可复用入口、导出时机、数据影响及边界；不新建外部桥接 | 待体验 |
| T2 | worker 独占 SessionReaderStore.swift、session_reader_main.swift；主 agent 独占 SessionReaderView.swift、FakeCore 与规格 | 代码及隔离夹具已接入，最终源码云端 16 个必需 job 成功；Store 行为检查通过 | 待体验 |
| T3 | 主 agent 集成与原生核验，reviewer 只读检查实际差异 | Python 95 项、原候选云端与最小窗口增量/失败/复制/恢复通过；最终默认窗口发现历史位置恢复缺口，正在修复并重新核验 | 实现中 |
| T4 | 主 agent 提交、PR、合入与发布 | PR #10 已合入 main，合入树与候选一致，main 云端通过；待最终原生检查后创建并推送 annotated v0.5.0，核验公开四项资产并收尾 worktree | 实现中 |

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
- 最终源码 `c31fe10d949c8e279873441286cbebb5d18682ae` 的分支 [Packages #147](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/42341663710) 与 PR [#148](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/42341668046) 均全部 16 个必需 job 成功；可选全文探针及分支 publish 按条件跳过。最终增量独立复核未发现发布阻塞，未修改或降低检查门禁。
- #147 独立签名 UI App 的 ZIP 31,709,574 bytes、SHA256 `2aac51851dde693a22927a20522b9ca9e2b1cfcb397ec5eba40df5442d738f63`，metadata 的源码/build=147 匹配，严格深度签名核验通过。最终 900×600 light：初始 1 条、显式暂停后增量追加 2 条并出现新内容提示通过；同一长错误及显式十行错误均保留两条正文、过期/重试提示与暂停意图，截图均为 1800×1200 像素。AX help/value 完整含 `LONG-ERROR-END`；选择正文后优先出现系统原生 Copy 菜单，原生选区包含完整错误，但点 Copy 时 Mac 锁屏，尚未完成实际粘贴核对。证据：主仓库 `build/kp-sv01/native-147/900x600-light-short/observations.json`，环境/夹具/请求日志同目录。
- [PR #10](https://github.com/SakuraLoveSmile/KaneoPilot/pull/10) 已合入 main：`b3181bc7a1fda85ab804c43d4436cf2f7bd75787`。`git diff c31fe10 origin/main` 为空、来源为 main 祖先；合入提交的 [Packages #149](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/42342954698) 全部 16 个必需 job 成功。主工作区已快进同步，原有 `.local/`、缓存清理计划均保留。
- 发布准备：说明已在 `docs/releases/v0.5.0.md`；本地 `build/kp-sv01/verify-release.py` 准备核对公开四项资产、manifest、SHA256SUMS、GitHub digest、版本/源码/build/latest 及 Android 与 v0.4.9 的证书延续。APK 核验可使用现有 `/opt/homebrew/opt/openjdk@17/bin/java -jar /Users/sakurasep/Library/Android/sdk/build-tools/36.0.0/lib/apksigner.jar`，避免本机失效的 jenv shim；未修改全局 Java 配置。

- 解锁后 #147 的 900×600 light 完整错误复制/粘贴、清除错误自动恢复、状态/内容往返和新内容按钮通过；证据已更新同目录 observations.json。1180×760 dark 的 32→33 实时追加及暂停通过，但原生滚动到 0.4（step_0010…0018）后，状态/内容往返却回到首条，30 秒后仍在顶部。此结果取代首版对最终锚点的推断，作为发布前必须解决的缺口。
- 锚点缺口只读分析：GeometryReader 行偏好不随 macOS 原生滚动更新；原生 clip 观测仅回传距底，造成 readingAnchor 陈旧。另外历史定位未设置 settling 保护，可被中间 distance 回调取消。修正采用稳定内容坐标的行布局 + 原生实际 viewport 联合选取锚点，并给历史恢复批次同等保护；不改变页面风格、核心接口或数据。
- 追加修复已实现：行框保持在稳定内容坐标，原生 viewport 单独变化也更新锚点；状态页/搜索重排/分页/待恢复及定位批次期间阻止临时几何覆盖。所有定位均受批次保护，真实手动滚动仍能取消。针对性 Swift 夹具加入纯视口滚动、模式往返、临时贴底与用户打断；没有删除/跳过原检查。主 agent 检查组合差异与 diff --check 通过；新增 Swift 夹具与原生结果待此提交的云端候选核验。

## 交付与恢复

- 体验入口：任务详情 → 执行 → 会话内容，或独立执行会话页；切换「会话状态」查看原状态及恢复操作。
- 本次不升级数据结构，原始执行记录和调度数据保留；回退使用旧正式安装包，不改变数据库。
- 当前待完成：用户已解锁，最终 #147 原生默认窗口发现历史锚点恢复缺口，正在修复；没有创建或推送 v0.5.0 标签，没有发布正式 Release。未使用真实模型/生产任务，未安装覆盖正式 App，未做 Android 实机验收。
- 续接：修复原生滚动锚点观测与恢复批次保护，云端重新编译/检查并完成默认窗口、最小窗口和详情内嵌页核验；随后合入 main，按正式标签流程发布并核验四项公开资产。
- 保留的运行资源：#147 独立 App 在主仓库 `build/kp-sv01/cloud-147/native/KaneoPilot Update Check.app`；#147 两个隔离预览已退出。不要停止正式 App 或其他任务进程。
- worktree 尚保留：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261107-feat-kp-sv01-live-session`，用于待完成的界面/发布续接；来源提交已在 main，尚不声明整体收尾完成。发布及适用验证完成后核对忽略/未跟踪文件、运行引用与独有成果，从外部正常移除此 worktree 和已合入本地来源分支；保留远端分支、其他任务与主仓库全部恢复证据。
