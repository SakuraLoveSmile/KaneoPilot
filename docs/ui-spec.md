# KaneoPilot 桌面界面规格 v1（冻结）

定位：本机原生工具型工作台。不做人格化设计、不做营销首页、不用大卡片/装饰图。系统浅/深色自适应。

## 1. 技术形态

- SwiftUI + AppKit 互操作最小化；macOS 14+ SDK；单窗口 `WindowGroup`。
- 数据全部经 protocol.md CLI 获取/变更（`Process` 调 `kanpilot`，JSON 进出）；不直接读写 SQLite，不内嵌调度逻辑。
- 命令调用放后台线程/actor，主线程零阻塞；UI 状态用 `@MainActor` view-model。
- 开发期数据源可用 `KANPILOT_CORE` 环境变量指向核心脚本；缺省找 `../core/kanpilot`（repo 布局）与应用内 `Resources/core/`。

## 2. 窗口与导航

- 默认窗口 1180×760，最小 900×600；标题 `Kaneo 调度台`。
- 顶层 `NavigationSplitView`：侧栏固定七节 —— **任务**、**执行会话**、**待处理**、**归档**、**回收站**、**运行记录**、**设置**（SF Symbols：`list.bullet.rectangle`、`bubble.left.and.bubble.right`、`tray.full`、`archivebox`、`trash`、`clock.arrow.circlepath`、`gearshape`）。
- **规划面板**（§4.7）：从「任务」区工具栏按钮 `规划…`（SF Symbol `doc.text.magnifyingglass`）与待处理/请求详情中的 `准备规划` 动作打开；独立窗口/大 sheet，约 760×600。
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
  - `prepare_plan` → `准备规划`（打开规划面板定位到该任务；v2 起 `安排规划` 不再直接排队）
  - `confirm_dispatch` → `确认并安排执行`（任务 `plan_execution_hold` 为真时替代普通「安排派发」；点击后核心以 `confirm_plan:true`+`expected_plan_sha256` 同事务复核，失败展示 `conflict` 原文）
- 任务 `plan_execution_hold=true`（publish-plan 成功后）：状态行显示 `待确认执行`（图标+文字），详情「计划」区块给哈希前缀；未确认前不显示普通 `安排派发`。
- 发送不明（send_state=unknown）必须展示原尝试证据块（attempt evidence），并在按钮区只给 restore/accept_duplicate/mark_sent，**不**显示普通重试。

### 4.2 待处理

- 聚合：活动请求（queued/claimed/lost）+ needs_user 任务 + 最近失败请求。
- 行：请求 kind/stage 图标+文字、任务标题、状态（已排队 X 分钟/进行中/失联 红）、claimed_by、预计/已等待时长。
- 支持：对 queued 行 `调整顺序`（优先级）、`取消排队`（若协议提供 cancel——当前版本没有则不给该按钮）；对 lost 行 `释放失联请求`；对 needs_user 行跳转任务详情。
- **请求详情页（v2，protocol §5.21.1）**：点击请求行进入详情（`get-request`）。分区块展示：请求字段（kind/stage/status/priority/not_before/claimed_by/租约时间）、关联任务摘要（`task` 为 null 显示「无关联任务」）、`attention_state` 徽标（`actionable` 需处理/`historical` 历史/`informational` 记录，图标+文字）、`attempts` 列表（每次尝试的时间/result/note/failure/send_state/evidence 可展开）、`log_path` 存在时给 `打开日志`。
- 详情底栏按 `allowed_actions` 渲染按钮（UI 只渲染不自判）：`view`→查看关联、`set-priority`→调整顺序、`release_lost`→释放失联请求、`retry`→重试、`replan`→重新规划、`prepare_plan`→准备规划（打开规划面板）、`fetch_todos`→重新获取 To Do（打开规划面板）。`blocked_reason` 非空时在按钮区下方以 11pt secondary 展示。
- `read_only=true` 详情（purged 关联/终态无动作）：顶部固定 `只读` 徽标 + 原因，按钮区为空。
- 空态：`暂无待处理项` + 说明文字。

### 4.3 运行记录

- 时间线列表（`list-events`，`id` 倒序）；行显示：时间、事件 kind（中文映射）、`task_title`、`project_name`（11pt 徽标）、`stage`、`result`、`summary`（中文一句话，11pt secondary 单行截断）。
- **游标翻页**：底部 `加载更多`（有 `next_before_id` 时可用，点击追加下一页；加载中转 spinner）；翻页期间新事件不影响已翻页顺序。`limit` 固定 100。
- **过滤与搜索**：工具栏搜索框 + `stage`/`result` 筛选菜单；搜索与筛选由核心在**全部历史**上执行（不只当前页），输入防抖后重新查询并清空已翻页游标。
- 行点击展开详情：原始 `detail` JSON、完整 `task_id`/`request_id`、actor（均可复制）；无 `task_id` 的事件同样可展开。
- 已 purged 任务的历史事件照常显示；`task_title` 用核心回填值（含 `未保留标题 · <短id>` 占位），UI 不自行猜测。
- 空态文案。

### 4.4 设置

- 环境诊断：`doctor` 结果列表（每项 名称+ok 图标+detail；`重新检查` 按钮；online 勾选 `包含 Kaneo 连通性`）。
- 自动推进：全局暂停开关 + 各 stage 开关（读取 overview.paused）；`领取间隔`等只读展示 settings 值。
- 路径只读展示：Plan 根、仓库根、DB 路径、核心路径；`打开所在目录` 按钮。
- 迁移状态：`settings.migration` 存在则显示导入时间与备份路径。
- **PI 执行配置（v1.6，protocol §5.17）**：独立分节，四个选择器（provider / 模型 / 思考等级 / agent.md 预设）+ 环境检查列表：
  - 选项与能力来自 `exec-capabilities`（加载中显示进度；失败保留已有内容 + `重试`）。
  - 思考等级选项**只列当前模型支持的值**；预设只列 `agents_dir` 有效项（`<name>.md`）；模型/provider 不在能力列表时禁用保存并写明原因。
  - 保存经 `set-exec-config`，成功 toast「已保存执行配置（仅影响新轮次）」；失败展示核心 `error` 原文并保留原值。
  - 环境检查行：pi 可执行、模型清单（models.json）可读、agent.md 预设可用、凭据（pi auth check 就绪态，**绝不显示值**）；红叉配文字。
  - 说明文案：仅影响新轮次；不修改 pi 全局配置；凭据只查就绪态，不保存密钥值。
- **执行端调度（v1.6，protocol §5.19）**：独立分节——启用总开关（App 内置调度器仅在应用运行期间触发，App 退出后执行端不再运行）；四个 stage 各一行显示下次触发时间与最近一次运行状态（executor-status），附「立即运行」按钮。

### 4.5 会话阅读器（v1.5，protocol §5.16）

- 阅读器头部增加**保存状态筛选**（正常会话 / 回收站，两段控件）与对当前轮次的 `移入回收站` 按钮：
  - destructive 样式 + `confirmationDialog` 二次确认，说明「仅移入调度台会话回收站；保留任务、执行记录与工作目录；不停止执行进程」。
  - 禁用时 tooltip/标签写明 `blocked_reason`（仍在执行、失联待处理、发送状态不明等）。
  - 删除成功：toast 简短反馈 → **停止该会话轮询、清空当前选择与消息、刷新列表**；在途旧响应按代数守卫丢弃，不得让已删除会话重新出现。
- 筛选「回收站」时空态 `回收站为空`；筛选「正常会话」且无记录时空态 `没有执行会话`（两种空态文案必须区分）。
- 回收站内轮次行显示 `恢复` 按钮（非 destructive）；恢复成功后可重新打开、继续查看完整历史。
- **轮次行操作菜单（v2，protocol §5.20.2）**：会话列表行右键菜单/行尾 `…` 按钮按当前状态给动作——正常会话 `移入回收站`；回收站内 `恢复` + `永久从列表移除`（destructive，`confirmationDialog` 文案固定为「永久从列表移除；保留历史记录与文件，不释放磁盘空间。」）。
- `purge-session` 成功后该轮次从列表消失（toast 反馈）；失败展示核心 `conflict`/`not_found` 原文。已 purged 轮次不出现在任何列表，`get-session` 明确 ID 查询返回 `read_only:true` 审计详情（若 UI 持有旧引用则显示只读占位，不报错「不存在」文案之外的猜测）。
- 加载/失败/重试/禁用原因/焦点与 VoiceOver 沿用 §6/§7 既有规格；度量不变（18/13/11pt、8/12/16、行高 ≥40、1180×760 / 900×600）。

### 4.6 归档与回收站列表（v2）

- 「归档」节：归档任务列表（沿用既有行规格与 `恢复` 操作）。
- 「回收站」节：顶部**两段控件** `任务 / 会话`（segmented，宽度自适应）：任务段列 trashed 任务，会话段列 trashed 轮次（`list-sessions save_state=trashed`）。
- 回收站行除 `恢复` 外提供 `永久从列表移除`（destructive）：`confirmationDialog` 二次确认，文案固定「永久从列表移除；保留历史记录与文件，不释放磁盘空间。」；调 `purge-task`/`purge-session`（带 `expected_save_state:"trashed"`）。
- 可选「清空当前分类」按钮：仅对**本次确认时快照的对象集合**逐项 purge（逐项失败单独报告，不中断其余）；不清理确认后新进入的对象。
- 空态区分：`回收站为空` / `没有执行会话`；purged 对象不出现（`list-tasks`/`list-sessions` 默认排除）。

### 4.7 规划面板（v2，protocol §5.22）

- 入口：「任务」区工具栏 `规划…`；待处理/请求详情 `prepare_plan`/`fetch_todos` 动作。窗口约 760×600（sheet 或独立 Window）。
- **第一步 获取 To Do**：`刷新` 调 `fetch-todos`；列表行=标题、项目徽标、`local_status`（未导入/需重新规划/已有计划…）、附件计数、`selectable=false` 时灰显 + `blocked_reason`。仅 `selectable` 行可勾选进入下一步。
- **第二步 准备材料**：对勾选任务逐条 `prepare-plan-input`（自动或点 `准备`）：展示 `files[]`（名称/大小/来源）、`issues[]`（红图标+文字）、`can_start`；提供补充说明输入框、本地文件选择（`local_paths`）、失效附件排除（`excluded_remote_ids` 勾选）。`issues` 未清零前 `can_start=false`，开始按钮禁用并写明原因。
- **第三步 开始规划**：`开始规划` 调 `start-plans`（已勾选且 `can_start` 项批量提交）；逐行显示结果——成功 `已排队`、幂等 `已在队列`、失败 `code`+`error` 原文。成功后核心立即唤起规划 runner；面板显示进行中轮次与最近日志摘要。
- 已授权排队中的任务显示 `已排队等待规划`；旧未授权 plan 请求显示 `等待确认材料`（点行直接进入第二步）。
- 失败/加载/空态沿用 §6；面板不阻塞主窗口操作。

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
| 待确认执行 ready（plan_execution_hold） | `checkmark.circle` | green（文字必须为「待确认执行」） |
| 等待确认材料（未授权 plan 请求） | `doc.badge.questionmark` | orange |
| 需处理 actionable | `exclamationmark.circle` | orange |
| 历史记录 historical | `clock` | secondary |
| 纯记录 informational | `info.circle` | secondary |

## 6. 反馈与空态（全部必须实现）

- 加载：列表区中央 `ProgressView` + `正在读取…`；命令执行中按钮转 disabled+spinner。
- 空队列：`暂无任务`/`暂无待处理项` + 一句说明 + `刷新` 按钮。
- 离线/核心不可达：顶部横幅 `无法连接调度核心：<原因>` + `重试`；数据保留展示并标 `可能已过期`。
- 读取失败：错误横幅（红图标+文字）+ `重试`；不清空旧数据。
- 操作成功：短暂确认（工具栏旁 `checkmark` + 文字或轻量 toast），并立即刷新数据。
- 操作失败：alert 或横幅给出核心返回的 `error` 原文。
- 禁用：按钮置灰 + tooltip/标签写明原因（如 `已有活动派发请求`）。
- 危险操作（accept_duplicate、mark_done、release_lost、restore、purge-task、purge-session）：`NSAlert`/`confirmationDialog` 二次确认，写明后果；purge 文案固定「永久从列表移除；保留历史记录与文件，不释放磁盘空间。」

## 7. 键盘、焦点、无障碍

- 列表支持 ↑↓ 选择、回车进入详情、⌘R 刷新；按钮有默认快捷键不冲突。
- 所有可点控件系统 focus ring 可见；`Tab` 顺序合理。
- 每个图标状态配 `.accessibilityLabel`（中文状态名）；按钮均有文字标题或 label；列表行 `accessibilityElement(children: .combine)` 读出一条完整摘要。
- 状态永远「图标+文字」双编码；不依赖颜色单独传达。
- VoiceOver 通过 macOS 辅助功能检查清单核验（见 tests/ui-acceptance.md）。

## 8. 验收基线

- v2 追加：回收站两段视图（任务/会话）+ purge 确认与结果；请求详情页（attempts/allowed_actions/attention_state/只读态）；运行记录翻页与全史搜索；规划面板三步流（获取→准备→开始，含 issues 阻断与逐条结果）；`待确认执行` 与确认派发闭环截图。
- v1.5 追加：会话删除/恢复闭环截图（阅读器按钮 → 确认框 → toast → 回收站筛选 → 恢复）；设置页 PI 执行配置错误状态截图（能力加载失败重试、非法组合禁用保存、凭据未就绪行）与执行端调度区（含最近一次运行状态）；两种窗口尺寸 × 浅/深色各一遍。

- 默认 1180×760 与最小 900×600 两种尺寸截图检查；浅/深色各一遍。
- 长中文标题（40+ 字）截断不撑破布局。
- 主线程无阻塞：所有 core 调用异步；连续点击 `安排下一步` 不重复派发（核心幂等，UI 结果一致）。
- 关闭窗口再打开：队列/状态从核心重新读取（窗口关闭不取消请求）。
