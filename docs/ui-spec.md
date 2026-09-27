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
- 详情（选中任务，T4 五段式）：详情容器保持 sheet 尺寸、标题/状态与关闭入口；顶部分段控件 `概览 / 计划 / 执行 / 评审 / 记录` 五段，每页独立滚动、切换不丢当前任务。键盘左右键切换分段，焦点与 VoiceOver 读出页名。
  1. **概览**：任务状态行、Kaneo 信息（kaneo_status/最后核验时间/`打开 Kaneo 任务`）、阻塞证据（needs_user/失联时 failure_code/failure_reason/证据摘要）与允许操作摘要。
  2. **计划**：按 §4.8 计划阅读页展示已发布计划。
  3. **执行**：轮次字段（run_id/session_id/session_title/session_marker/worktree_path）+ 现有 `SessionReaderView`；无会话显示空态。
  4. **评审**：结论/报告路径/fix_task_id/`打开报告`；无评审数据显示空态。
  5. **记录**：两个并列区段（不嵌套卡片）——「尝试记录」（时间、阶段、结果、说明，结果列图标+文字，行高 ≥40pt，说明按内容自然撑高不截断单行）与「事件时间线」（时间、类型、摘要三列，行高 ≥40pt，类型列固定宽度且不折断 `round_observe` 等标识）。单个外层滚动；点击一行展开完整 note/evidence 或结构化 `detail` 原文（可复制）；默认显示核心 `EventItem.summary` 中文一句话；未知 kind 保留原值且可完整查看。此页只称「最近记录」（数据源 `get-task.attempts` + `list-events(limit:200)`），不冒充全史。
  没有对应数据时显示清晰空态文案，不显示空白表。
  五段导航始终固定在浮层顶部，切换分段不改变浮层的有效尺寸；内容区填满导航以下的可用高度且只滚动一次。
- **待办列直达计划页（T1）**：待办（`board_column=todo`）列卡片的主入口直接打开同一详情容器的**计划页**（非概览）；其他列仍默认打开概览。计划页可切回概览/其他页；「准备/重新规划」仍跳转既有规划面板。
 - 计划页头部按 §4.8 展示任务标题（18pt，单独首行、最多两行）、`待确认执行` 状态、`plan_kind`、生成时间与 plan_sha256 前 12 位（可复制）；模式分段与打开/定位按钮置于独立操作行，不与元数据争宽。
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
- **Codex 规划配置（v2.1，protocol §5.22.6）**：独立分节（置于 PI 执行配置之后）——
  - 只读区：服务商（provider_id/名称/base_url/wire_api）、CLI 版本、配置来源（model_catalog_json 或「无」）、认证就绪态；不提供密钥输入或服务商编辑。
  - 两组独立设置「普通规划」「评审修复规划」：模型文本输入（本地元数据提示）、思考等级已知时用 Picker、未知时允许文本输入并显示「能力未验证」。
  - `重新读取本机配置` 调 `planner-capabilities`；`保存规划配置` 调 `set-planner-config`（保存期间禁用重复提交；失败保留草稿并显示核心 error 原文；成功 toast「已保存，仅影响新建规划请求」）。
  - 加载中显示进度；说明文案：仅影响新规划请求；不修改 codex 全局配置；不显示凭据值。
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
- **滚动与实时跟随**：打开轮次（首次消息落地）直接定位到**最新消息**（底部），不做「停在第一条 prompt」的中间态；每 2s 增量轮询到新内容且仍贴底时自动跟到最新。用户主动上滚离开底部（距底 ≥48pt）→ **不被拉回**，出现「有新内容」按钮，点击回到最新并恢复跟随；切换轮次重置为贴底跟随。判底依据**真实滚动几何**：滚动了多少由原生滚动视图的边界变化实测（SwiftUI 的 GeometryReader 偏好只在内容重新布局时回传，macOS 上用户滚动拿不到新位置，不得据其判底），内容高度仍以 LazyVStack 布局后回传为准；自动滚动的二次纠正期间不得把中间态判成「离开底部」而产生假「有新内容」；折叠/展开与搜索过滤引起的重排中间态不得判成「贴底」，即逐条展开/收起只改变该条高度，不把上滚中的视口拉回底部。
 - **无变化轮询不重排（T1）**：轮询到无新消息且会话状态未变时，不得重新发布消息数组、不得安排自动滚动；有新消息或修订时只处理改变的行，仍按 `seq` 排序，折叠/展开状态跟随消息 `id`（修订替换沿用原状态）。折叠的长正文只排版可见预览，完整原文仍可复制；搜索命中时仍能看到命中内容。上滚停止跟随、回底部、「有新内容」按钮与切轮次重置贴底的语义不变。
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
- **规划方式选择（T2，protocol §5.23.2）**：第二步增加明确的**两项**选择——`内置规划`（builtin）与 `Spec Planner 插件`（spec_plan）。默认预选取 `get-planner-config` 的 `default_method`（缺省 builtin）。`planner-capabilities.planner_methods.spec_plan.available=false` 时该项禁用并展示 `reason`；选择对本批次选中任务生效（每项 `start-plans` 带 `planner_method`），第三步提交前在汇总区明示所选方式。spec_plan 选项附边界说明：「该方式经 Codex 插件技能执行，可能加载本机用户配置中的其他能力（MCP/规则）；不使用隔离用户配置的只读内置链路。」普通规划/评审修复规划仍各自沿用既有模型/思考等级设置。
- **第三步 开始规划**：`开始规划` 调 `start-plans`（已勾选且 `can_start` 项批量提交）；逐行显示结果——成功 `已排队`、幂等 `已在队列`、失败 `code`+`error` 原文。成功后核心立即唤起规划 runner；面板显示进行中轮次与最近日志摘要。
- 已授权排队中的任务显示 `已排队等待规划`；旧未授权 plan 请求显示 `等待确认材料`（点行直接进入第二步）。
- 失败/加载/空态沿用 §6；面板不阻塞主窗口操作。

### 4.8 计划阅读页（T1，protocol §5.23.1）

- 数据源：`get-plan {task_id, expected_plan_sha256}`；进入时携带当前 `get-task` 的 plan_sha256 做预期核验。读取在后台进行，**任务切换/在途响应归属用 request generation 守卫**——旧任务响应不得覆盖新页面。
 - 头部（固定，不随正文滚动）：标题（18pt）**单独占满第一行且最多两行**（可选中复制）；`待确认执行` 状态、`plan_kind`、生成时间、`plan_sha256` 前 12 位（可复制）与可截断路径置于元数据区，长路径**中间截断**但可复制并经 `在 Finder 显示` 定位；**渲染/原文**模式分段与 `打开计划文件`、`在 Finder 显示` 置于**独立操作行**（宽度不足时在该行换行），不与元数据争宽。右上无裁切、重叠或漂浮残字。底栏确认条与按钮可用性规则不变。
 - **渲染/原文双模式**：默认渲染视图以 Foundation `.full` Markdown 的 `PresentationIntent` 语义结构（标题/列表/代码/引用/分隔/GFM 表格）做**块级**原生排版，不用单个 `Text` 承载整篇，也不手写正则解析器；H1 用 18pt，其余标题/正文以 13pt 的字重与间距区分，代码沿用 12pt 等宽，颜色/分隔沿系统语义色。超宽单元格/代码行可换行或在内容区水平查看，不撑宽整个 sheet。原文模式逐字显示（含所有字符，不折叠/不重排）。两种模式正文均可选中复制，跨块选择保留合理换行及列表/表格内容。
 - **安全约束**：渲染**不执行**内嵌 HTML/脚本，不自动加载远端媒体（图片仅显示 alt/占位）；Markdown 链接以文字+图标显示，点击经 `NSWorkspace` 打开系统默认浏览器（仅 `http/https`）。页面内**不允许**修改计划内容。
 - **核验失败状态**（`get-plan` 返回错误）：保留任务摘要区，显示错误原因（中文）、`重试`、`打开计划文件` 入口；底栏执行确认（`confirm_dispatch` 等写操作）**保持禁用**直到核心重新核验通过——不把不可信正文当作已批准计划展示，也不允许「确认并安排执行」。
 - `plan_sha256` 与 `expected_plan_sha256` 不符（`conflict`）：展示「计划自发布后已变更」+ `重试`/`在 Finder 显示`；不渲染旧快照。

### 4.9 角色化设置页（T5，protocol §5.23）

- 顶层侧栏仍只有「设置」；设置页内部以 `规划环境 / 执行环境 / Review 环境 / 通用` 四段切换（segmented，**不按** Codex/PI 软件名分节）。沿用 grouped Form、现有控件密度与系统语义色。
- **规划环境**：Codex 认证/服务商只读区（`planner-capabilities`）、两组模型与思考等级（§5.22.6）、`planner_methods` 可用性与 `default_method` 预选（含 spec_plan 的边界说明文字）。
- **执行环境**：任务执行者后端切换（`pi` / `pi_desktop`）；pi 分支为四选择器（§4.4 原 PI 执行配置内容），pi_desktop 分支为连接文件路径（`mcp-control.json`）、provider/model/thinking_level/`permission_mode` 选择——**permission_mode 首次须用户显式保存**，不得自动选 `auto`。两分支各自的能力检查列表、dispatch/monitor 调度状态行与说明「dispatch/monitor 控制端仍继承本机 PI 默认设置」。
- **T3 Plus 执行环境细节（T3.5）**：桌面标签为 `Pi-Desktop-Plus（本机 MCP）`，tag 仍 pi_desktop；path 草稿为空时填入 `~/.pi-desktop-plus/mcp-control.json` 的绝对路径，仍可手改。切入 Plus 时调用只读 `pi-desktop-catalog` 取本地 provider/model 候选与 Plus 默认值；默认组合只填空草稿，不改已有配置；「重新获取」刷新候选，不覆盖非空草稿。provider/model 保留可编辑 ID 和离线保存能力，并以候选弹出列表选择；model 候选有独立搜索词。thinking 依据 Plus 明示的模型/provider级别展示，缺少级别信息时显示全合法枚举并标记支持范围未确认。permission_mode 必须用户显式选择，绝不读取 Plus 会话或自动设为 auto。「检测已保存配置」仍只检查已保存配置；读取/检测各自显示状态，不互相覆盖。加载失败保留草稿，错误详情优先 detail 再 reason 且允许换行；读取期间用户修改或切走后丢弃过期响应。桌面保存只校验五字段，不受 PI CLI 顶层 ok 阻挡；保存 pi 不提交空桌面块，保存桌面不提交无关 CLI 草稿。保留 grouped Form、四段、字号与间距，不变更全局设计。
- **Review 环境**：`get/set-review-config`——`inherit`（继承本机 PI 默认会话配置）/ `custom`（provider/model/thinking 由 `exec-capabilities` 校验的显式选择）；显示当前生效来源与「仅影响下一次 review 启动」说明。
- **通用**：doctor、自动推进/暂停、队列参数、路径、迁移信息（把 §4.4 原「环境诊断/自动推进/路径/迁移」归入）。
- 各页保存失败保留草稿与核心 error 原文；加载失败保留旧内容 + `重试`；标注「默认/已保存」与「仅影响新轮次」。不修改 `~/.pi`、`~/.codex` 或 PI-Desktop 全局配置。

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
