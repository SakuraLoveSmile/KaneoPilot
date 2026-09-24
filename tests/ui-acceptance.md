# KaneoPilot 桌面人工验收清单

依据 `docs/ui-spec.md`（v1.1 冻结）逐条验收。每项完成打勾；截图点标注 📷。
验收基线：macOS 14+，浅色/深色各一遍，三种窗口尺寸 **1180×760 / 900×600 / 1440×900**。

前置：`scripts/build_app.sh` 构建成功（`dist/KaneoPilot.app`），并用 FakeCore
（`KANPILOT_CORE=<repo>/macos/FakeCore/kanpilot-fake.py`）驱动；核心接口以
`docs/protocol.md` v1.3 为准。

## 0. 启动与窗口

- [ ] 应用启动后窗口标题为 `Kaneo 调度台`
- [ ] 默认窗口尺寸 1180×760；可缩至最小 900×600，更小被拒绝 📷（三尺寸各一张）
- [ ] 侧栏固定**七节**：任务 / 执行会话 / 待处理 / 归档 / 回收站 / 运行记录 / 设置
      （SF Symbols：`list.bullet.rectangle`、`bubble.left.and.bubble.right`、
      `tray.full`、`archivebox`、`trash`、`clock.arrow.circlepath`、`gearshape`）
- [ ] 侧栏可收起；工具栏含刷新（`arrow.clockwise`）+ 全局暂停/恢复自动推进
      （`pause.fill`/`play.fill`）+ 命令不可用原因提示
- [ ] 任务节为六列看板，占满内容区完整宽度；详情以浮层（sheet）打开，关闭即回看板
- [ ] 窗口收窄到 SplitView 折叠时，内容压栈为单独页面，可系统返回 📷
- [ ] 关闭窗口再打开：队列/状态从核心重新读取（窗口关闭不取消请求）

## 1. 三尺寸 × 浅深色外观

- [ ] 1180×760 浅色 / 深色：六列齐全、等宽、无硬编码颜色 📷
- [ ] 1440×900 浅色 / 深色：列宽不拉伸变形、卡片留白正常 📷
- [ ] 900×600 浅色 / 深色：可用宽度 < 1044pt 时**整板横向滚动**，列宽恒 ≥160pt
      不被压缩、列间距保持 12pt 📷
- [ ] 列内独立纵向滚动，列标题固定不随滚；空列保留列容器并居中显示
      `这个状态还没有任务`；搜索/筛选无匹配时显示 `没有匹配任务`
- [ ] 正文 13pt；区段/详情标题 18pt semibold；辅助/时间戳 11pt secondary
- [ ] 间距只出现 8/12/16 三档；列表行高 ≥40pt；分隔为系统 Divider；圆角 8pt、边框 1pt
- [ ] 状态永远「图标 + 文字」双编码，不依赖颜色单独传达

## 2. 任务视图（六列看板 + 详情浮层）

### 2.1 看板与第六列异常隔离

- [ ] 六列顺序与配色：待规划 `.gray` / 待办 `.blue` / 进行中 `.orange` /
      待评审 `.purple` / 已完成 `.green` / 失败·异常 `.red`
- [ ] 第六列列名 `失败 / 异常` 为红色文字；列头状态圆点 8pt + 列名 13pt semibold
      + 右侧筛选后计数（13pt secondary）
- [ ] **分列只信核心 `board_column`**：每张卡片恰出现一次，桌面不按中文标签、
      计划文件或历史失败自行归列（与 `tests/test_board_v13.py` 的分类互斥一致）
- [ ] 混合状态分布：正常（待规划/就绪/执行中/评审中/已完成）+ 异常
      （明确失败/发送不明/失联/评审未通过）同时在场时，前五列**不混入异常卡**，
      异常全部落在第六列 📷
- [ ] 第六列卡片徽标按 `exception.severity`：`failed` 红色「失败」、
      `blocked` 橙色「受阻 / 连接异常」；徽标文字 = `board_substatus`
      （= `exception.label`，如 `发送不明`、`执行端失联`、`评审未通过·待修复`）
- [ ] 前五列受阻类子状态（规划受阻/派发受阻/执行受阻/评审受阻）为橙色文字
- [ ] 第六列与阻塞卡显示 `exception.detail` / `blocked_reason`（红，两行截断）
- [ ] 卡片第一行：项目徽标（11pt）+ 子状态徽标（11pt）；标题 13pt 两行截断；
      最近进展 11pt secondary 单行截断
- [ ] 列补充行：`in_progress` 显示 `最近检查 <短格式>`（无记录显 `未检查`）；
      `in_review` 有结论显示 `结论：通过/需修改/受阻`；`todo` 有 `next_claim_at`
      显示 `预计领取`
- [ ] `in_progress` 卡片操作行有 `查看实时会话` 按钮（打开会话阅读器并定位当前轮次）
- [ ] 列内排序：阻塞/失联优先 → `todo` 列按派发队列 priority ASC 再 created_at ASC
      → 其余按 `updated_at` 倒序
- [ ] 长中文标题（40+ 字）两行截断不撑破布局 📷；≥30 张卡片列内滚动流畅 📷

### 2.2 过滤

- [ ] 工具栏搜索框按标题/项目/task_id 匹配；阶段筛选菜单含六列中文名 + 全部
- [ ] 筛选命中非本列时，其余列显示 `没有匹配任务`；数量始终对应筛选结果

### 2.3 详情浮层

- [ ] `sheet` 呈现，标题 18pt；最小约 640×480，初始建议 900×640，可扩大
- [ ] 顶部 `执行会话 / 任务信息` 分段切换（`Picker` `.segmented`）：
      「任务信息」六区块（计划 / 执行 / Kaneo / Review / 尝试与时间线 / 阻塞）；
      「执行会话」内嵌会话阅读器（当前任务当前轮次）
- [ ] 浮层内动作完成后刷新看板并**保持浮层打开**
- [ ] 按钮 `打开计划文件`、`在 Finder 显示`、`打开 Kaneo 任务`（有 URL 时）、
      `打开报告`（有 report_path 时）均可用
- [ ] 底栏操作按钮与 `get-task` 的 `allowed_actions` 一致（无多余按钮）；
      v1.3 新增 `archive`/`trash`/`restore`/`resume-auto` 渲染与置灰规则正确
- [ ] 已完成（`board_column=done`）与已归档（`save_state=archived`）明确区分：
      已完成仍在看板第五列，归档只出现在「归档」节
- [ ] 危险操作（accept_duplicate、mark_done、release_lost、restore、trash）弹
      `NSAlert`/`confirmationDialog` 二次确认，写明后果
      （删除确认须写明「移入本地回收站，不影响 Kaneo」）
- [ ] `重新校验` 始终可用，离线时给出明确结果反馈

## 3. 执行会话视图（v1.3 新增）

- [ ] 侧栏「执行会话」进入会话列表；按任务分组，显示任务标题 + 会话标题 +
      会话更新时间 + 连接状态徽标
- [ ] 会话阅读器：消息 13pt，代码/终端等宽 12pt，消息间距 12pt
- [ ] 工具调用/结果默认折叠，可展开；每条消息可复制；搜索过滤可用
- [ ] 轮次选择器可切换历史执行轮次（多轮次任务）
- [ ] 向上翻阅暂停跟随 + 「有新内容」按钮；回到底部恢复跟随
- [ ] 连接状态徽标四态：正常（订阅）/ 每 2 秒增量（degraded）/ 连接中断重连中 /
      接口不兼容（unavailable）——图标 + 文字双编码
- [ ] 读取失败：保留已加载消息 + 横幅 `读取失败：<原因>` + `重试`；
      **不清空**已读内容
- [ ] 无关联会话显示 `会话待关联`（pending，不得按标题猜测）
- [ ] 分别显示「会话更新时间」与「最近状态检查」
- [ ] 长输出消息完整展示/可滚动，不因长度截断或卡死 📷
- [ ] 会话内容仅作进度展示：不得因收到新日志而改变任务状态或解除异常
      （与 `tests/test_session_contract.py::TestSessionDoesNotAdvanceLifecycle` 一致）

## 4. 归档 / 回收站视图（v1.3 新增）

- [ ] 「归档」列表显示已归档任务：原阶段、移出时间（saved_at）、`恢复` 按钮
- [ ] 「回收站」列表显示已回收任务：同样字段 + `恢复` 按钮
- [ ] 列表项可打开详情（只读查看，不提供推进操作）
- [ ] 禁用归档/删除时按钮置灰 + tooltip 说明原因（进行中/评审中/失联未解除/发送不明）
- [ ] 恢复确认对话框说明「默认暂停自动推进」，勾选 → `resume_auto=true`
- [ ] 恢复后任务回到看板原列，且 `auto_advance_paused=1` 时显示暂停徽标；
      工具栏「恢复自动推进」可解除
- [ ] 归档/回收站中的任务不出现在看板、待处理与 overview 计数

## 5. 待处理 / 运行记录 / 设置

- [ ] 待处理聚合：queued/claimed/lost 请求 + needs_user 任务 + 最近失败；
      lost 行提供「释放失联请求」；queued 行可调整优先级
- [ ] 运行记录：事件时间线倒序，可按任务过滤、可搜索；空态文案正确 📷
- [ ] 设置：doctor 逐项列出 + `重新检查`；`包含 Kaneo 连通性` 勾选生效；
      全局暂停 + 各 stage 开关与 `overview.paused` 一致；
      Plan 根/仓库根/DB 路径/核心路径只读展示 + 「打开所在目录」；
      已迁移时显示迁移时间与备份路径

## 6. 反馈与状态（全部必须验证）📷 每态一张

- [ ] 加载中：中央 ProgressView + `正在读取…`
- [ ] 命令执行中按钮 disabled + spinner
- [ ] 空队列：`暂无任务` + 说明 + `刷新` 按钮
- [ ] 核心不可达（kill 核心/错误 KANPILOT_CORE）：顶部横幅 `无法连接调度核心：<原因>`
      + `重试`；旧数据保留并标 `可能已过期`
- [ ] 读取失败：错误横幅 + `重试`，旧数据不清空
- [ ] 操作成功：工具栏旁 checkmark 或轻量 toast，并立即刷新数据
- [ ] 操作失败：alert/横幅显示核心返回的 `error` 原文
      （如 `任务已被执行端领取，暂不能移出看板`、`任务已暂停自动推进，请先恢复自动推进`）
- [ ] 连续点击 `安排下一步`：核心幂等返回同一请求，UI 不重复派发 📷

## 7. 键盘 / 焦点 / 滚动 / 无障碍

- [ ] 看板键盘选择：↑↓ 列内移动、←→ 跨列移动、回车打开详情浮层、Esc 关闭浮层、⌘R 刷新
- [ ] 看板横向滚动：900×600 下可用键盘/触控板横向滚动到第六列（列宽不被压缩）
- [ ] 列内纵向滚动时列标题不随滚；滚动位置切换列后保持合理
- [ ] 会话阅读器：向上翻阅暂停跟随、回到底部恢复；搜索框 ⌘F 不与应用快捷键冲突
- [ ] 所有可点控件 focus ring 可见；Tab 顺序合理
- [ ] 状态图标均有中文 accessibilityLabel（如「需人工」「执行端失联」「失败」）
- [ ] 卡片 `accessibilityElement(children: .combine)` 读出一条完整摘要
      （标题 + 子状态 + 阻塞原因如有）
- [ ] VoiceOver 读出列名与数量（如 `待规划，3 项任务`）与每张卡片的任务状态
- [ ] VoiceOver 可完成：选任务 → 进详情 → 触发「安排下一步」全流程
      （辅助功能检查清单见 tests/ui-acceptance.md 本节）

## 8. 端到端验收（automation-contract §5 检查点 + v1.3）

- [ ] 桌面 `安排下一步` → 任务显示 `已排队`；执行端领取+心跳 → 显示 `派发中`/`执行中`
- [ ] 执行失败/发送不明/失联/评审未通过任务全部落到第六列，前五列干净 📷
- [ ] 评审未通过（changes_requested）：原任务进第六列 `评审未通过·待修复`，
      修复任务独立走 needs_plan→…→done；新一轮 in-review 后原任务回「待评审」列
- [ ] 观测异常（observation_blocked）在监控恢复后自动清除，任务回「进行中」列
- [ ] 归档 → 重启应用（或关闭再开窗口）→ 状态仍在归档节；再次 sync-tasks 不重导入看板
- [ ] 恢复归档任务 → 默认暂停自动推进 → 工具栏可恢复自动推进
- [ ] 排队取消与领取竞争：执行端领取与「归档/回收站」同时发生时，UI 显示核心返回的
      冲突原因，不出现「既取消又领取」或重复派发
- [ ] 主线程无阻塞：核心调用期间 UI 可滚动/缩放（长时间 fake 延迟下验证）

## 验收记录

| 日期 | 构建 | 尺寸/外观 | 验收人 | 结果 | 备注 |
|---|---|---|---|---|---|
|  |  | 1180×760 浅/深 |  |  |  |
|  |  | 900×600 浅/深 |  |  |  |
|  |  | 1440×900 浅/深 |  |  |  |
| 2026-09-21 | `scripts/build_app.sh`（Release，BUILD SUCCEEDED → `dist/KaneoPilot.app`） | 1247×760（默认，深色） | qa-eng（t5 集成验证） | 部分通过 | 见下方 t5 记录 |

### t5 集成验证记录（2026-09-21，qa-eng）

自动验证（全部通过）：

- `cd core && python3 -m pytest tests -q` → **119 passed**（2.31s）
- `python3 -m pytest tests -q` → **251 passed**（327s）；逐项证据见下表
- `scripts/build_app.sh` → **BUILD SUCCEEDED**；`dist/KaneoPilot.app` 已生成，
  且 `Contents/Resources/core` 与工作树 `core/kanpilot` 完全一致（`diff -rq` 无差异），
  v1.3 新文件 `cmd_archive.py` / `cmd_session.py` / `headless_adapter.py` /
  `session_store.py` 与 v1.6 pi 后端文件 `pi_worker.py` / `pi_backend.py` /
  `dsh_events.py` 均在包内
- 出厂件可执行（隔离环境 `KANPILOT_DB`/`KANPILOT_APP_SUPPORT` 指临时目录）：
  内置 `kanpilot.py overview` 返回六列 board 计数、`list-tasks {save_state:all}` 正常、
  `list-sessions` 无数据源返回 `{"sessions":[]}` exit 0、
  `get-session` 未知任务返回 `not_found` exit 1

GUI 验证（部分覆盖，环境受限）：

- 已做：应用以 FakeCore（`KANPILOT_CORE=macos/FakeCore/kanpilot-fake.py`）启动成功，
  窗口默认 1247×760；对窗口截图做 OCR，确认渲染出六列看板（待规划/待办/进行中/待评审/
  已完成/**失败／异常**）、侧栏七节（任务/执行会话/待处理/归档/回收站/运行记录/设置）、
  异常副状态卡（评审未…、规划中、派发中、失联等）、「查看实时会话」入口、
  「最近检查 / 预计领取」卡片字段
- **未覆盖（环境限制，非产品缺陷）**：① 浅色外观截图——切换系统外观后窗口截图被系统
  拒绝（`screencapture -l` 返回 "could not create image from window"，全屏截图为纯黑，
  即本会话未获屏幕录制授权），仅取得深色外观截图；② 900×600 与 1440×900 两档尺寸——
  AppleScript/辅助功能控制窗口尺寸超时（未获辅助功能权限），无法改窗口大小；
  ③ 键盘/焦点/滚动/VoiceOver 需人工在真机按第 7 节清单执行；
  ④ 真实 pi 会话——v1.6 起会话数据源为本机 pi 轮次目录的
  `job.json`/`events.jsonl`（~/.npm-global/bin/pi 已实测存在），会话契约由
  `tests/test_session_contract.py` 经文件级夹具与 `KANPILOT_PI_*` 接缝覆盖，
  旧后端（backend≠pi）弃用回归由 `test_legacy_backend_unavailable` 固化。

已知产品缺口（v1.2 既有、非 v1.3 回归，已上报用户决定，本轮不改）：

1. 已发送（`sent=1`）任务 replan 后重派发被 dispatch 校验 6 拒绝
   （`validation_failed`），且 §4.10 无 resolve 动作可清 `send_state="sent"` 证据
   → replan 后重派发无路可走。固化于
   `tests/test_board_v13.py::TestHistoryDoesNotPollute::test_execution_failed_then_recovered_running`。
2. `sync-tasks` 无 `in_review → running` 回退分支 → 评审未通过修复完成后，
   原任务无法从 Kaneo 卡片拖回 in-progress 回到「待评审」列；新一轮评审周期只能靠
   `tests/conftest.py::set_task_status` 后门构造（与 `core/tests/test_board.py` 同构）。
   固化于 `tests/test_board_v13.py::TestReviewFixChain::test_source_returns_to_in_review_after_new_round`。

用户完成标准逐项证据（自动测试侧）：

| 完成标准 | 证据（`python3 -m pytest tests -q` 全绿） |
|---|---|
| 分类互斥与计数 | `test_board_v13.py::TestClassificationMutex::*`（3）、`test_board.py::TestBoardConsistency` |
| 执行→评审→完成链 | `test_lifecycle.py::TestHappyPath::test_full_chain`、`test_dispatch.py::TestDispatchChain::test_happy_chain_state_transitions` |
| 评审修复链 | `test_board_v13.py::TestReviewFixChain::*`（2）、`test_lifecycle.py::TestReviewFix::*`（3） |
| 历史失败不污染当前状态 | `test_board_v13.py::TestHistoryDoesNotPollute::*`（6） |
| 归档/删除/恢复/重启/再次同步 | `test_save_state.py::TestArchiveTrash`(17)/`TestRestore`(5)/`TestRestartAndResync`(2)/`TestSchedulingExclusions`(7) |
| 排队取消与领取竞争不漏执行不误派发 | `test_concurrency.py::TestClaimRace::*`(4)/`TestCancelVsClaimRace::*`(2)（真实多进程）、`test_queue.py::TestClaimRace::*`(2) |
| sync-tasks 不重导入 | `test_save_state.py::TestRestartAndResync::test_state_survives_restart_and_no_reimport`、`test_lifecycle.py::TestSyncTransitions::test_sync_resync_no_duplicate` |
| 会话全文/长输出/修订/隔离/轮次/断线/重启补齐 | `test_session_contract.py::TestFullHistoryAndShape::*`(4)/`TestIsolationAndRounds::*`(4)/`TestCursorAndReconnect::*`(3) |
| 异常判定表（五类异常） | `test_board_v13.py::TestExceptionKindTable::*`（11） |
| schema v1→v2 迁移 | `test_schema_v2.py`（7）、`test_migration.py` |
| schema v1→v3 迁移（轮次索引） | `test_schema_v2.py`（含 execution_rounds 列与逐级备份） |
| 会话回收站删除/恢复/禁止条件/不复活 | `test_session_contract.py`（T1，随 v1.5 新增）、`core/tests/test_session.py` |
| PI 配置校验/保存/能力选项/凭据红线 | `core/tests/test_cmd_config.py`、`test_pi_config_contract.py`（T3 新增） |
| PI 派发全链/重复启动不建二进程/配置失败阻断/历史证据兼容 | `test_pi_dispatch.py`（8 例） |
| PI worker 生命周期/半写/崩溃/超时/轮次隔离 | `core/tests/test_pi_worker.py`、`test_pi_backend.py`、`test_pi_round_contract.py`（T2 新增） |

## 8. v2 追加验收（protocol §5.20–5.22 / ui-spec §8）

- [ ] 回收站两段控件 `任务 / 会话`：任务段列 trashed 任务，会话段列 trashed 轮次；
      行内 `恢复` + `永久从列表移除`（destructive，`confirmationDialog` 固定文案
      「永久从列表移除；保留历史记录与文件，不释放磁盘空间。」）📷
- [ ] purge 成功后对象从所有列表消失、历史事件仍在运行记录可见；再次 purge 幂等；
      非 trashed 对象拒绝并展示核心 `conflict` 原文；可选「清空当前分类」只清理
      确认时快照集合、逐项失败不中断
- [ ] 待处理 → 请求详情页：请求字段/关联任务摘要/attention_state 徽标
      （需处理/历史/记录）/attempts 展开（result/note/failure/send_state/evidence）/
      `打开日志`；`allowed_actions` 渲染按钮（prepare_plan/fetch_todos 打开规划面板）；
      `blocked_reason` 11pt secondary 展示；`read_only` 顶部徽标且无按钮 📷
- [ ] 运行记录：`limit=100` 游标翻页 `加载更多`（spinner + next_before_id 驱动）；
      搜索框（防抖）与 stage/result 筛选在**全部历史**上过滤后分页；行含
      task_title/project_name 徽标/stage/result/summary；行展开 detail 原文可复制；
      purged 任务历史显示「未保留标题 · <短id>」占位 📷
- [ ] 规划面板（约 760×600）三步流：① 获取 To Do（刷新/项目徽标/local_status/
      附件计数/selectable=false 灰显+原因）② 准备材料（files/issues/can_start、
      补充说明、本地文件选择、失效附件排除勾选；issues 未清零开始按钮禁用）
      ③ 开始规划（逐行结果 已排队/已在队列/code+error 原文）📷
- [ ] publish 成功后任务显示「待确认执行」（checkmark.circle + 文字），详情无普通
      「安排派发」；`确认并安排执行` 调 enqueue confirm_plan + expected_plan_sha256，
      失败展示 conflict 原文；确认后 dispatch 可领取，确认前 dispatch 请求 claim
      返回 plan_hold 📷
- [ ] 会话列表行菜单：正常 `移入回收站`；回收站内 `恢复` + `永久从列表移除`
- [ ] 设置页执行端调度只剩 dispatch/monitor/review 三个 stage（plan 已移除）
