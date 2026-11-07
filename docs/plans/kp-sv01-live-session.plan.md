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
| T2 | worker 独占 SessionReaderStore.swift、session_reader_main.swift；主 agent 独占 SessionReaderView.swift、FakeCore 与规格 | 代码与隔离夹具已接入；默认内容、模式切换、增量、状态/重试均待云端及原生验证 | 实现中 |
| T3 | 主 agent 集成与原生核验，reviewer 只读检查实际差异 | Python 专项 95 项通过；独立审查发现两项已修正。待云端全部必需 job 和独立 App 默认/最小尺寸核验 | 实现中 |
| T4 | 主 agent 提交、PR、合入与发布 | main 包含源码、云端通过；annotated v0.5.0 标签；Release 四项资产身份/哈希一致；保全证据后清理本任务 worktree 与本地来源分支 | 未开始 |

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

## 交付与恢复

- 体验入口：任务详情 → 执行 → 会话内容，或独立执行会话页；切换「会话状态」查看原状态及恢复操作。
- 本次不升级数据结构，原始执行记录和调度数据保留；回退使用旧正式安装包，不改变数据库。
- 待补：具体代码 SHA、检查/构建/界面/发布证据、未验证项与清理结果。
