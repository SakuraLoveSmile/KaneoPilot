# KaneoPilot 桌面界面规格 v1（冻结）

定位：本机原生工具型工作台。不做人格化设计、不做营销首页、不用大卡片/装饰图。系统浅/深色自适应。

## 1. 技术形态

- SwiftUI + AppKit 互操作最小化；macOS 14+ SDK；单窗口 `WindowGroup`。
- 数据全部经 protocol.md CLI 获取/变更（`Process` 调 `kanpilot`，JSON 进出）；不直接读写 SQLite，不内嵌调度逻辑。
- 命令调用放后台线程/actor，主线程零阻塞；UI 状态用 `@MainActor` view-model。
- 开发期数据源可用 `KANPILOT_CORE` 环境变量指向核心脚本；缺省找 `../core/kanpilot`（repo 布局）与应用内 `Resources/core/`。

## 2. 窗口与导航

- 默认窗口 1180×760，最小 900×600；标题 `Kaneo 调度台`。
- 顶层 `NavigationSplitView`：侧栏固定四节 —— **任务**、**待处理**、**运行记录**、**设置**（SF Symbols：`list.bullet.rectangle`、`tray.full`、`clock.arrow.circlepath`、`gearshape`）。
- 内容区为「列表 + 详情」双栏；宽度不足（SplitView 折叠）时详情压栈为单独页面（`navigationStack` push），系统返回。
- 侧栏可选收起；工具栏放：刷新（`arrow.clockwise`）、暂停/恢复自动推进（`pause.fill`/`play.fill`，全局态）、命令不可用原因提示。

## 3. 字体、颜色、度量

- 正文 13pt（`Font.system(size: 13)`）；区段/详情标题 18pt semibold；辅助/时间戳/次要信息 11pt（secondary 色）。
- 颜色只用系统语义色：`Color.primary/.secondary`、`Color(nsColor: .separatorColor)`、`Color(nsColor: .controlBackgroundColor)`、`Color(nsColor: .windowBackgroundColor)`；状态色用系统 `.green/.orange/.red/.blue/.gray` 且**必配文字**，不单靠颜色。
- 间距只用 8 / 12 / 16pt 三档；列表行高 ≥40pt；分隔用系统 `Divider`；圆角/阴影默认系统值，不自定义卡片容器。
- 图标只用 SF Symbols（见 §5 映射）。

## 4. 四个视图

### 4.1 任务（主列表 + 详情）

- 列表行：项目名（11pt 徽标文本）、标题（13pt，单行截断）、当前阶段/状态（文字+图标）、最近进展（11pt secondary 单行）、阻塞原因（有则 11pt 红）、预计领取时间（有 queued 请求则显示 `HH:mm` 或 `约 x 分钟后`）。
- 排序：需人工/失联置顶 → 已排队（按 priority+创建时间）→ 执行中 → 其余按 updated_at 倒序；项目可分组标题。
- 过滤：工具栏搜索框（标题/项目匹配）+ 阶段筛选菜单。
- 详情（选中任务）：标题 18pt + 状态行；分组区块（间隔 16，区块内 8/12）：
  1. **计划**：plan_path 文件名、plan_kind、生成时间、计划哈希前缀；按钮 `打开计划文件`（NSWorkspace open）、`在 Finder 显示`。
  2. **执行**：run_id、session_title/session_id（可复制文本）、session_marker；有 session 记录时给 `打开会话`（若记录为本地会话标识则仅展示/复制，不臆造 URL）。
  3. **Kaneo**：kaneo_status、最后核验时间（kaneo_checked_at）；按钮 `打开 Kaneo 任务`（links.kaneo_task_url，`NSWorkspace` 打开，仅当 URL 存在）。
  4. **Review**：report 文件名、结论、fix_task_id；按钮 `打开报告`（有 report_path 时）。
  5. **尝试与时间线**：attempts 表（时间、阶段、结果、note）+ events 时间线（kind/时间/摘要，11pt）。
  6. **阻塞**：needs_user/失联时显示 failure_code/failure_reason/证据摘要与原尝试关键字段。
- 详情底栏操作区：按钮由 `get-task` 返回的 `allowed_actions` 渲染，映射：
  - `enqueue` → `安排下一步`（task needs_plan/ready/running/in_review 时按核心推断 kind；label 按 kind 变：`安排派发`/`安排规划`/`安排检查`/`安排评审`）
  - `retry` → `重试派发`（仅 send_state=not_sent 等明确未发送）
  - `restore` → `恢复原任务`（unknown）
  - `accept_duplicate` → `接受可能重复，重新派发`（unknown；需二次确认对话框写明风险）
  - `mark_sent` → `登记已发出`（unknown；确认对话框）
  - `replan` → `重新规划`
  - `release_lost` → `释放失联请求`（失联时）
  - `mark_done` → `登记完成`（确认对话框）
  - `dismiss` → `清除阻塞标记`
  - `recheck` → `重新校验`（始终可用，off 时明确结果反馈）
  - `set-priority` → 对 queued 请求提供 `上移/下移` 或优先级菜单
  - `set-paused` → `暂停自动推进/恢复自动推进`（全局，工具栏+设置双入口）
- 发送不明（send_state=unknown）必须展示原尝试证据块（attempt evidence），并在按钮区只给 restore/accept_duplicate/mark_sent，**不**显示普通重试。

### 4.2 待处理

- 聚合：活动请求（queued/claimed/lost）+ needs_user 任务 + 最近失败请求。
- 行：请求 kind/stage 图标+文字、任务标题、状态（已排队 X 分钟/进行中/失联 红）、claimed_by、预计/已等待时长。
- 支持：对 queued 行 `调整顺序`（优先级）、`取消排队`（若协议提供 cancel——当前版本没有则不给该按钮）；对 lost 行 `释放失联请求`；对 needs_user 行跳转任务详情。
- 空态：`暂无待处理项` + 说明文字。

### 4.3 运行记录

- 时间线列表（events 倒序 + attempts 摘要），可按任务过滤；行显示时间、事件 kind（中文映射）、任务标题、摘要。
- 搜索框过滤文本；空态文案。

### 4.4 设置

- 环境诊断：`doctor` 结果列表（每项 名称+ok 图标+detail；`重新检查` 按钮；online 勾选 `包含 Kaneo 连通性`）。
- 自动推进：全局暂停开关 + 各 stage 开关（读取 overview.paused）；`领取间隔`等只读展示 settings 值。
- 路径只读展示：Plan 根、仓库根、DB 路径、核心路径；`打开所在目录` 按钮。
- 迁移状态：`settings.migration` 存在则显示导入时间与备份路径。

## 5. 状态图标映射（图标+文字同时出现）

| display_status | SF Symbol | 颜色（系统） |
|---|---|---|
| 待规划 needs_plan | `doc.badge.plus` | secondary |
| 规划中 planning | `doc.text.magnifyingglass` | blue |
| 就绪 ready | `checkmark.circle` | green |
| 已排队 queued | `clock` | blue |
| 派发中 dispatching/prepared/submitting | `paperplane` | blue |
| 执行中 running | `play.circle` | blue |
| 评审中 in_review | `eye` | orange |
| 需人工 needs_user | `exclamationmark.triangle` | orange |
| 执行端失联 lost | `exclamationmark.triangle.fill` | red |
| 已归档 done | `archivebox` | gray |

## 6. 反馈与空态（全部必须实现）

- 加载：列表区中央 `ProgressView` + `正在读取…`；命令执行中按钮转 disabled+spinner。
- 空队列：`暂无任务`/`暂无待处理项` + 一句说明 + `刷新` 按钮。
- 离线/核心不可达：顶部横幅 `无法连接调度核心：<原因>` + `重试`；数据保留展示并标 `可能已过期`。
- 读取失败：错误横幅（红图标+文字）+ `重试`；不清空旧数据。
- 操作成功：短暂确认（工具栏旁 `checkmark` + 文字或轻量 toast），并立即刷新数据。
- 操作失败：alert 或横幅给出核心返回的 `error` 原文。
- 禁用：按钮置灰 + tooltip/标签写明原因（如 `已有活动派发请求`）。
- 危险操作（accept_duplicate、mark_done、release_lost、restore）：`NSAlert`/`confirmationDialog` 二次确认，写明后果。

## 7. 键盘、焦点、无障碍

- 列表支持 ↑↓ 选择、回车进入详情、⌘R 刷新；按钮有默认快捷键不冲突。
- 所有可点控件系统 focus ring 可见；`Tab` 顺序合理。
- 每个图标状态配 `.accessibilityLabel`（中文状态名）；按钮均有文字标题或 label；列表行 `accessibilityElement(children: .combine)` 读出一条完整摘要。
- 状态永远「图标+文字」双编码；不依赖颜色单独传达。
- VoiceOver 通过 macOS 辅助功能检查清单核验（见 tests/ui-acceptance.md）。

## 8. 验收基线

- 默认 1180×760 与最小 900×600 两种尺寸截图检查；浅/深色各一遍。
- 长中文标题（40+ 字）截断不撑破布局。
- 主线程无阻塞：所有 core 调用异步；连续点击 `安排下一步` 不重复派发（核心幂等，UI 结果一致）。
- 关闭窗口再打开：队列/状态从核心重新读取（窗口关闭不取消请求）。
