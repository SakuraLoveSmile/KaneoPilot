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
      （SF Symbols：`list.bullet.rectangle`、`bubble.left`、
      `tray.full`、`archivebox`、`trash`、`clock.arrow.circlepath`、`gearshape`）
- [ ] 侧栏可收起；顶栏全局组含自动推进按钮（运行中「暂停自动推进」，已暂停「已暂停 · 恢复自动推进」，`pause.fill`/`play.fill`）与「刷新 HH:mm」（`arrow.clockwise`，⌘R）；命令不可用原因提示仅在需要时出现
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
- [ ] 字号只用刻度 11/12/13/15/18/22pt：正文 13pt；区段标题 15pt 半粗；页面标题 18pt 半粗；辅助/时间戳 11pt 次要色（`kpSecondary`）
- [ ] 间距只出现 4/8/12/16/24 刻度；可点击行高 ≥40pt；容器只围成组的数据区（圆角 10）；控件圆角 6；不加阴影
- [ ] 状态永远「图标 + 文字」双编码，不依赖颜色单独传达

## 2. 任务视图（六列看板 + 详情浮层）

### 2.1 看板与第六列异常隔离

- [ ] 六列顺序与配色（§3 命名色板）：待规划、待办 `kpGray`；进行中 `kpBlue`；待评审 `kpOrange`；已完成 `kpGreen`；阻塞 / 失败列 `kpOrange`，行内失败徽标按 `exception.severity` 为 `kpRed`
- [ ] 第六列列名为中性「阻塞 / 失败」（`.primary` 文字）；列头圆点 8pt + 列名 13pt 半粗 + 右侧筛选后计数（胶囊，11pt）
- [ ] **分列只信核心 `board_column`**：每张卡片恰出现一次，桌面不按中文标签、
      计划文件或历史失败自行归列（与 `tests/test_board_v13.py` 的分类互斥一致）
- [ ] 混合状态分布：正常（待规划/就绪/执行中/评审中/已完成）+ 异常
      （明确失败/发送不明/失联/评审未通过）同时在场时，前五列**不混入异常卡**，
      异常全部落在第六列 📷
- [ ] 第六列卡片徽标按 `exception.severity`：`failed` 红色「失败」、
      `blocked` 橙色「受阻 / 连接异常」；徽标文字 = `board_substatus`
      （= `exception.label`，如 `发送不明`、`执行端失联`、`评审未通过·待修复`）
- [ ] 前五列受阻类子状态（规划受阻/派发受阻/执行受阻/评审受阻）的图标与圆点为橙色，文字为 `.primary`
- [ ] 第六列与阻塞卡显示 `exception.detail` / `blocked_reason`（`.primary` 文字，两行截断；颜色不承载原因语义）
- [ ] 卡片第一行：标题 13pt 两行截断，项目名 11pt 次要色，子状态徽标（图标 + 文字，淡底）；最近进展 11pt 次要色单行截断
- [ ] 列补充行：`in_progress` 显示 `最近检查 <短格式>`（无记录显 `未检查`）；
      `in_review` 有结论显示 `结论：通过/需修改/受阻`；`todo` 有 `next_claim_at`
      显示 `预计领取`
- [ ] `in_progress` 卡片操作行有 `查看实时会话` 按钮（打开会话阅读器并定位当前轮次）
- [ ] 列内排序：阻塞/失联优先 → `todo` 列按派发队列 priority ASC 再 created_at ASC
      → 其余按 `updated_at` 倒序
- [ ] 长中文标题（40+ 字）两行截断不撑破布局 📷；≥30 张卡片列内滚动流畅 📷

### 2.1a 紧凑列表（2026-10-29 验收反馈，须在云端包实机核对）📷 三尺寸 × 浅深色

- [ ] 行与行之间是通栏 1pt 横线；列表中间没有竖线（外层是 `HStack` 的行不得用 `Divider()` 做分隔线）
- [ ] 每行 52pt；行首 16pt 状态图标；第一行只有标题，第二行为「项目名 · 原因」，没有原因时只有项目名
- [ ] 状态文字（12pt，无底色胶囊）与更新时间各自成列，所有行对齐，包括有「会话」按钮的行（执行端失联、执行中）；「会话」仅悬停或聚焦时可见，显隐时状态列不挪动；非当天的时间「MM-dd HH:mm」完整显示不截断
- [ ] 最长的状态词「等待你在 Plus 中回答」在 120pt 列宽内完整显示或以省略号截断，悬停可见全文，不撑破行
- [ ] chips 紧凑排成一行（间距 8pt），下方一条通栏分隔线；900pt 宽度下换行而不横向滚动，不拉伸成等距网格
- [ ] 分组头为通栏浅灰带（28pt），计数为纯文字；「需关注」组标题前有橙色感叹圆圈图标，其余组没有图标
- [ ] 列表 chip 与分组头叫「需关注」（收件箱项加阻塞、失败任务）；侧栏、菜单栏、总览标题和「需要你」页叫「需要你」（只数收件箱）。两个数允许不同（例如侧栏 29、chip 32），界面上没有同名不同数的位置
- [ ] 详情头部状态为图标加半粗文字，无底色；看板卡片、规划、待处理里的状态徽标保持淡底
- [ ] 侧栏页脚「暂停 / 恢复」与「刷新」两个按钮带图标、等宽撑满侧栏；工具栏「更多」没有下拉箭头

### 2.2 过滤

- [ ] 工具栏搜索框按标题/项目/task_id 匹配；看板列筛选菜单含六列中文名 + 全部，标签显示「看板列：<当前值>」
- [ ] 任务页顶栏：规划待办… / 看板｜列表 / 看板列筛选 / 更多（图标按钮，`accessibilityLabel` 与 `help` 均为「更多」，内含批量选择…）/ 搜索；更多以外的控件均有可见文字；1180×760 与 900×600 下没有需要 `>>` 才能找到的控件 📷
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
- [ ] 助手/工具结果等**过长消息（>4 行或 >320 字符）逐条默认折叠**为 4 行预览，行内「展开/收起」只影响该条；短消息不折叠；搜索时正文不截断（命中行可见）
- [ ] 每条消息可复制；搜索过滤可用（工具调用/结果的折叠同上条）
- [ ] 轮次选择器可切换历史执行轮次（多轮次任务）
- [ ] 向上翻阅暂停跟随 + 「有新内容」按钮；回到底部恢复跟随
- [ ] 上滚后逐条展开/收起消息不改判跟随、不把视口拉回底部（重排中间态不算「贴底」）
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
      顶栏「已暂停 · 恢复自动推进」可解除
- [ ] 归档/回收站中的任务不出现在看板、待处理与 overview 计数

## 5. 待处理 / 运行记录 / 设置

- [ ] 待处理聚合：queued/claimed/lost 请求 + needs_user 任务 + 最近失败；
      lost 行提供「释放失联请求」；queued 行可调整优先级
- [ ] 运行记录：事件时间线倒序，可按任务过滤、可搜索；空态文案正确 📷
- [ ] 设置（高级页，2026-11-01 KP-UI03 起）：只有「后台与引擎」「诊断与数据」两个分区；「复制诊断信息」把引擎状态、诊断检查结果、路径、调度参数与队列、迁移记录放进剪贴板（不含 token 或凭据），「打开数据目录」打开状态目录；诊断与暂停开关不在设置页（诊断在总览「环境」，暂停在侧栏页脚与菜单栏）。逐项清单见文末「2026-11-01 设置页信息架构精简」
- [ ] 设置四页（运行端、项目、连接、高级）：1180×760 与 900×600、浅 / 深色各看一遍；项目页 <720pt 上下堆叠；折叠区键盘可达，VoiceOver 读出的标题带数量或异常数（设计见 docs/ui-spec.md §4.9–4.11、§4.14）📷

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
- [ ] 恢复归档任务 → 默认暂停自动推进 → 顶栏可恢复自动推进
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
- [ ] 设置页不再出现逐阶段暂停开关（2026-11-01 KP-UI03 起；原「执行端调度只剩 dispatch/monitor/review 三个 stage」随之取消，逐阶段暂停没有界面入口）

## 9. v3 追加验收：任务详情可读性与执行证据（T1–T6）

依据 `docs/plans/task-detail-readability-and-execution-evidence.md` 与 protocol §5.23.10 / ui-spec §4.1/§4.5/§4.8。
隔离数据：临时 `KANPILOT_DB`、`KANPILOT_APP_SUPPORT`、独立 Plan 根与 `KANPILOT_FAKE_STATE`；**不得**以正式 DB 作为测试输入。
参考：用户 2026-09-27 的六张现状截图（执行页长会话卡顿；计划页整篇像无格式文本；评审空态使分段导航落在窗口中下部；记录页尝试表首行被裁、事件文字密集且 `round_observe` 断词；异常卡显示执行失败；Plus 侧栏把 worktree UUID 当工作区名、把任务标识当会话名）。

### 9.1 T1 执行页阅读流畅性 📷

- [ ] 同一合成长会话（固定消息数/长输出字节数）、同一窗口、同一 2s 轮询下，记录改前/改后主线程 Hangs / Time Profiler 与滚动录像
- [ ] 无新消息轮询**不触发整列发布**、无可观察顿挫；持续滚动与搜索/折叠时无停顿、按钮及时响应
- [ ] 逐条展开/收起仍正确；长输出可展开且完整原文可复制；搜索命中内容可见
- [ ] 上滚停止跟随、「有新内容」按钮、回到底部、切轮次重置贴底语义不变
- [ ] 空增量、同 ID 修订、多轮次切换均不产生错误重排

实测记录（2026-09-27，主 agent 本人运行，可复现）：

方法：把**真实** `SessionReaderView.swift`（改前 = `git show HEAD:`、改后 = 工作区）
与真实 `Models.swift`/`CoreClient.swift` 一起编成 headless harness，驱动真实
`SessionReaderStore`；数据用 `macos/FakeCore/kanpilot-fake.py` 的
`KANPILOT_FAKE_LONG_SESSION=task-music-006` 夹具（400 条消息、每条长输出 4096 字节），
轮询沿用 `connection.poll_seconds=2`。同一台 Mac、同一构建配置、同一窗口宽度。

| 指标 | 改前（HEAD） | 改后（工作区） |
|---|---|---|
| 12s 空闲轮询中的 `messages` 发布次数（3 次运行） | 5 / 5 / 5 | 0 / 0 / 0 |
| 12s 内应发生的轮询次数 | 6 | 6 |
| 首屏 400 条加载耗时 | 6.33–6.42s | 6.33–6.43s |
| 12 行 × 4096B 折叠行的 `AttributedString` 构建（中位数） | 0.249–0.263ms/行 | 0.011ms/行（仅可见预览） |

Instruments `Time Profiler`（`xctrace`）对两个 harness 各录一份 trace：改后主线程在
`SessionReaderStore.applyMessages` 上的采样权重与改前同量级（约 3–4ms 于 557ms 与
362ms 总采样中），即「无变化轮询不重排」的收益不是缩短单次重排，而是**不再发布新数组**
——这与上表「空闲发布 5 → 0」互为印证。

行为回归（同一 harness 的 `T1_MODE=behave`，用可控 stub core 逐场景驱动真实 store）：
改前/改后均 9/9 通过——空增量不重发、同 ID 修订落地、新消息落地并按 `seq` 排序、
长正文默认折叠、折叠状态按消息 ID 跨其他消息修订保持。

说明：`rowLayout_*`（整行 SwiftUI 布局）在两次构建间无可分辨差异，不作为 T1 收益证据；
上文引用的是发布次数、`AttributedString` 构建成本与行为回归。

### 9.2 T2 计划阅读页 📷

用包含多级标题、中文有序/无序列表、代码围栏、表格、链接、图片与原生 HTML 文本的完整计划（`fixtures/plan-sample-t2.md`）。

- [ ] 标题单独占满第一行且最多两行；长标题/长路径下右上控件无裁切、无重叠、无漂浮残字
- [ ] 元数据区（状态/类型/时间/哈希/可截断路径）与独立操作行（模式分段 + 打开计划文件 + 在 Finder 显示）分区；宽度不足时操作行换行
- [ ] 渲染模式：标题/列表/代码块/引用/水平线/GFM 表格可读；H1 18pt，其余 13pt 字重与间距区分，代码 12pt 等宽
- [ ] 超宽单元格/代码行可换行或水平查看，**不撑宽整个 sheet**
- [ ] 原文模式逐字显示，含所有字符，不折叠/不重排
- [ ] 两种模式正文均可选中复制；跨块选择保留合理换行及列表/表格内容
- [ ] 链接仅点击时以 `http/https` 交系统浏览器打开；HTML **不执行**；图片只显示 alt/占位，不自动读取远端媒体
- [ ] `get-plan` 核验失败/`conflict`：不展示旧正文，且「确认并安排执行」保持禁用
- [ ] 待办列卡片主入口直达计划页；其他列默认概览

### 9.3 T3 评审页 📷

- [ ] 无数据时空态只在导航以下的内容区居中（保留图标/标题/说明），分段导航不落在窗口中下部
- [ ] 五段导航固定在浮层顶部，切页不改变浮层有效尺寸
- [ ] 有数据时保留结论/报告/`fix_task_id`/`打开报告` 原字段

### 9.4 T4 记录页 📷

- [ ] 「尝试记录」「事件时间线」为两个并列区段，不嵌套卡片，单个外层滚动
- [ ] 尝试行按时间/阶段/结果/说明四列自然撑高（结果 = 图标+文字），行高 ≥40pt，**首行不被裁**
- [ ] 事件行按时间/类型/摘要三列，行高 ≥40pt；固定列**不折断** `round_observe` 等类型
- [ ] 默认显示 `EventItem.summary`；点击一行可展开完整 note/evidence 或结构化 detail 并可复制原文
- [ ] 长 note、未知 kind（保留原值且可完整查看）均可读
- [ ] 覆盖：无评审、已有评审、1 条及多条 attempts
- [ ] 页内只称「最近记录」，不冒充全史

### 9.5 T5 Goal 报告完成判定（核心 + 隔离 Plus）

- [ ] `pi` 后端维持 §5.18.4 原三证据口径不变
- [ ] 旧 `pi` 三证据、Plus 无报告/兜底报告/不完整/完成报告的边界分别验证
- [ ] fake Plus 提供**真实形状**报告；AI 结果与报告 hash、派发轮次及领取中的 monitor 请求同时绑定
- [ ] 绕过 monitor 直接调用 `advance-task` 被核心拒绝（`conflict`，不触碰远端）
- [ ] 同 hash 幂等：不重复调用 AI
- [ ] 升级时仍 `running` 的旧 Plus 轮次 → 按缺报告规则进入人工，不走旧消息三证据
- [ ] 已在 Review/Done 的历史轮次不回写、不降级、不改判
- [ ] `Bash` 退出码保真（大小写归一后 `exit_code` 可见）
- [ ] 真实模型调用可能产生费用；**未获该调用授权时如实保持「实现中」**，不以 fake AI 冒充真实判断
- [ ] 非对照：本次历史轮次 `ozb7d8vw1sbp653zs7u0jn2f` / `71de3380-...` 保持 `needs_user`，不补写、不解阻塞

### 9.6 T6 派发命名（隔离 Plus）📷

两个同属 `Music` 的不同 worktree 路径 + 长中文计划名。

- [ ] `PI_DESKTOP_DATA_DIR=<隔离目录>`、`PI_DESKTOP_MCP_CONTROL=1`、未占用 `PI_DESKTOP_MCP_PORT` 启动隔离 Plus
- [ ] **不发送 prompt** 的创建检查：`projectPath` 是独立 worktree 路径、`projectName=Music`、`title` 等于计划页标题
- [ ] 重启 Plus 后 `project/list` 仍返回 `Music`；打开会话并激活工作区后 `project/get` 与 Sidebar 都显示 `Music`
- [ ] 会话显示完整计划名（长标题单行截断但可悬停/复制）；悬停可看真实路径/全标题
- [ ] 视觉可同名，但项目/会话身份不串、原路径仍是实际执行目录；**不按名称合并**
- [ ] 创建会话本身不为了命名而全局切换工作区
- [ ] 旧 Plus 缺能力时设置/派发前明确阻断（「版本缺少工作区命名能力」），不静默派发 UUID 名称
- [ ] 已有非 legacy 用户项目组或同路径不同自定义名称不被覆盖（`CONFLICT`）
- [ ] 旧历史轮次仍可阅读，不批量改名
- [ ] 不以改目录名或只改 KaneoPilot 本地标签冒充 Plus UI 已生效

### 9.7 原生界面构建与核对

- [ ] `APP_OUTPUT_DIR="$PWD/build/task-detail-check" scripts/build_app.sh` 构建独立产物；核对包内核心与源码哈希一致
- [ ] 以隔离应用支持目录、独立用户偏好、调度器关闭的本机 App 打开
- [ ] 1180×760 与 900×600、浅/深色对照现状截图检查四个分段；实际滚动、切页、展开、复制、点击安全链接与键盘焦点
- [ ] 确认计划文件、后台任务与用户正式 App 未被替换
- [ ] macOS 14 是最低目标；若只在较新 SDK/系统实测，记录「macOS 14 仍待体验」，不把编译通过当成旧系统视觉通过

### 9.8 本轮实测记录（2026-09-27，主 agent 本人运行）

**T5 核心闸门（隔离 fake Plus，无付费模型调用）**

- `~/.pyenv/shims/pytest -p no:cacheprovider core/tests/test_goal_report_contract.py -q`
  → **68 passed**；`core/tests/test_pi_desktop_backend.py`、`core/tests -q`
  → **453 passed**；根 `tests -q` → **298 passed**。
- 覆盖：旧 `pi` 三证据不变；`pending` 宽限与过期；`ready+fallback` 等同无报告；
  `ready+structured+met` 但 AI 判未完成；`partial/blocked/unknown`；AI `uncertain`；
  AI 矛盾提交 `complete`（审查项空/非 `met`/重复 ID/虚构引用）；report/hash/plan/
  `execution_run_id`/`monitor_run_id`/session/turn 不匹配；monitor 租约失效；
  直接调用 `advance-task` → `conflict` 且不触碰远端；同 hash 幂等；升级时旧 Plus
  轮次仍 `running`；历史 Review/Done/needs_user 不回写；`Bash` 退出码保真。
- **非对照（只读复核）**：`ozb7d8vw1sbp653zs7u0jn2f` / `71de3380-...` 在正式
  `kanpilot.db` 中仍为 `status=needs_user`、`failure_code=execution_failed`、
  轮次 `run_status=finished`，`job.goal_approval=null`——未补写、未解阻塞。
- **真实付费端到端：已获用户授权，但被上游依赖阻塞（尚未执行）**。用户已
  明确授权按最小样例跑一次真实 Goal（规划 + 执行 + 监控三次真实模型调用）。
  为减少花费，先在不花钱的范围把环境跑通，结果：
  1. Plus 侧此前**完全无法构建**——主 checkout 的 `packages/voice-runtime`
     未构建/未链接，且可选依赖（`transcribe-cpp`/`@picovoice/pvrecorder-node`/
     `@huggingface/hub`）未安装。补齐后 `electron-vite build` 成功，可起真实
     Plus；补的都是 gitignore 内的 `dist/`、`node_modules/`，未改跟踪文件。
  2. 以**真实构建的隔离 Plus**（独立 `PI_DESKTOP_DATA_DIR` + 未占用端口）经
     KaneoPilot `exec-capabilities` 真实探测：`session_naming=true`、
     `version=2025-06-18`，`tools/list` 含 `pi_goal_report_list`/
     `pi_goal_report_get` 且 `pi_session_create.projectName` 存在
     —— **T6 真实兼容门通过**。
  3. 但 **`tools_ok=false`**：唯一缺的是 `pi_turn_get`。该工具由**另一条
     未合入 main 的工作流**实现，故本 plan 的 Plus 树（基于 main）没有它：

     | 树 | `pi_turn_get` | goal 报告工具 | `projectName` |
     |---|---|---|---|
     | 本 plan 树（`feat/kaneopilot-goal-report-and-naming`） | 无 | 有 | 有 |
     | `feat/mcp-turn-get` | 有 | 无 | 无 |
     | 用户当前运行中的 Plus 安装 | 无 | 无 | 无 |

     `pi_turn_get` 已在 KaneoPilot 基线（HEAD）的
     `PI_DESKTOP_DISPATCH_TOOLS` 中要求，且其实现不在本 plan 的写入范围
     （plan 明示「不得覆盖现有 `pi_turn_get` 等未提交改动」）。因此
     **当前没有任何一棵树**能同时满足 `dispatch-start` 的 `tools_ok` 门，
     真实付费阶段**一次都没跑，未产生任何费用**。
  4. 用户决定先停下，由用户协调两条工作流合并后再验收。届时需：从当时
     `main` 建树（同时含 `pi_turn_get` 与本 plan 的 goal 报告/命名改动）→
     跑一次最小真实 Goal → 核对契约审批、Host `ready/structured` 报告、
     MCP 读回身份/hash、监控 AI 逐项判断、`record-goal-evaluation` 留痕与
     `advance-task` 硬闸。在此之前该项**既不是「实现中」也不是「已验收」，
     而是「阻塞于未合入的上游依赖」**。
- **同轮发现并修复的真实缺陷（本 plan 代码）**：
  `packages/agent-runtime/src/goal-report-tool.ts` 的草稿持久化失败分支用了
  `isError: true`，而 `AgentToolResult`（0.86.1 与 0.87.1 官方类型均）没有该
  字段——既是类型错误，更是**静默成功**：Agent 会以为报告已登记，Host 实际
  发 `fallback`。已按该运行时的约定（失败即 `throw`）修正，草稿同时置空并
  标 invalid 以便干净重试；新增 `goal-report-tool.test.ts` 回归用例覆盖失败
  路径与恢复（vitest 6/6 通过）。

**T6 隔离 Plus 命名闭环**

以真实 `crates/host-core` 二进制（`target/debug/pi-desktop-host-core`）+ 隔离
`PI_DESKTOP_DATA_DIR` 驱动真实 NDJSON JSON-RPC，**未发送任何 prompt**、未启动
agent、未调用付费模型、未触碰 `~/.pi-desktop-plus`：**14/14 通过**——
`projectName='Music'` 与长中文 `title` 精确透传；`projectPath` 仍是独立 worktree
绝对路径；同一 `Music` 下两个不同 worktree 会话 ID 与路径均不同（不按名称合并）；
空白 `projectName` / 缺 `projectPath` → `INVALID_PARAMS`；旧客户端不传
`projectName` 时新路径回落 basename、已命名路径复用已存名称；
**重启 host-core 后** `session.list` 与 `projects.list` 仍返回 `Music`。

**原生界面核对**

- `APP_OUTPUT_DIR="$PWD/build/task-detail-check" CONFIG=Debug scripts/build_app.sh`
  → BUILD SUCCEEDED，且包内核心与源码哈希校验通过。
- 隔离应用支持目录 + `FakeCore` 启动真实 App（窗口标题「任务」，六列看板齐全），
  `screencapture -l<windowid>` 取窗口像素、Vision OCR 读回文本；OCR 确认
  侧栏七节、六列列头、任务卡片与「这个状态还没有任务」空态文案。
- T2 计划页：把**已发布**的 `MarkdownReaderView` 与 T2 夹具（多级标题、中文
  有序/无序列表、代码围栏、GFM 表格、超宽代码/单元格、图片、原生 HTML 文本）
  离屏渲染，OCR 确认标题/列表/代码块/表头与单元格分栏可读、图片只显示占位、
  `<script>`/`javascript:` 链接按纯文本原样显示（不执行）。
- T3 评审页空态：OCR 确认「暂无评审数据」+ 说明居中于内容区。
- T4 记录页：OCR 确认「尝试记录」「事件时间线」两区段、四列/三列布局、
  `round_observe` 显示为「观察者续接」（修复前为「未识别事件（round_observe）」）、
  未知 kind 原样保留、长 note 完整显示不截断为单行。
- 自动点击/切页受 macOS 辅助功能授权限制（`System Events` 返回 -1719），
  分段切换与键盘焦点未做脚本化点击，仅以离屏渲染 + 窗口截图核对；
  **macOS 14 仍待体验**（本机为较新 SDK/系统，只证明编译通过）。
  用户正式 App（`/Applications/KaneoPilot.app`）与正式数据目录未被替换。


## 2026-10-06 执行会话 T1–T4 施工复核

本次基线 `166ae24e77b50cc1032546e0dd42c2b626706ff7`，仅隔离候选与临时来源；未覆盖正式应用或生产任务。详见 [本次证据记录](../docs/evidence/session-reader-20261006/report.md)。核心真实CLI/末端版本确认、实际Store/FakeCore A400与B1000、Models99项及Release构建已通过；原生核对工具stdout、连续分页贴底、搜索实际全文定位/完整复制、上滚追加、同ID修订、读取失败旧内容保留和恢复。历史截图或mock通过不代替本次原生结果。

T1待体验；T2–T4实现中，用户尚未验收。后续已解决 C 全文停顿：独占后台 TextKit 图与所属阅读器的非模态 NSPanel，最终实际 NSHostingView 夹具709个10ms探针 p95 30.53ms、max165.12ms，完整Unicode/AX/选区/复制及生命周期通过。900×600长标题两入口、Tab到消息展开/复制及Enter连续展开收起已原生核对。

严格已加载 A/B 的 Time Profiler + Activity Monitor 录制及导出成功；A主线程采样753→465ms、CPU均值5.789→4.814%，B1620→857ms、7.946→4.099%。采样窗峰值及A real memory增加见报告，不能称全生命周期峰值。Hangs单独仪器仍因系统log archive错误失败；实际VoiceOver未保持运行；1440×900受屏幕可用高度限制。最终20动作的搜索回显/缓存滚动计时端点仍补核，不使用早期额外发布正文的诊断数据宣布100ms达标。冻结验收目标未降低。


**本轮最终补核：** 输入状态隔离和实际Store A回归通过（400首载0.520s、查询0.434s、idle30s工作0）。最终20原生动作p9557.479ms、max66.913ms；4925个10ms探针max75.023ms，无≥250ms，达到代表动作目标。实际搜索匹配200条、Tab/Enter清除恢复正文已核对。T1/T2待体验；T3/T4实现中，尚缺Hangs、实际VoiceOver与精确1440×900环境证据；详见报告最终结论，前两轮超标记录保留。最终候选包已更新，未正式安装/发布/提交/推送。


## 2026-10-06 项目与仓库配置浮层裁切

PlanningView的嵌套配置sheet因缺尺寸约束、GeometryReader理想高度近零而只显示头部，已局部补最小600×480、理想760×560pt；ui-spec §4.11同步。Release及修正版DMG0.2/20261006.1构建、签名、完整包体/107个核心文件校验通过。状态仍实现中：原生检查打开规划时遇Mac锁定，配置浮层与设置页两入口及滚动尚待解锁核对。详见docs/evidence/project-mapping-20261006/report.md。未安装、未改正式任务或数据。


## 2026-10-06 GitHub SSH 别名预检修复

预检/PR/推送仓库识别共用本机SSH别名展开，原推送地址与单一目标/仓库/rewrite守卫保留。真实Blog+用户SSH配置只读验证正确识别SakuraLoveSmile/Sakura-Blog；临时DB公共get-project-mappings不再返回origin_not_github，5次54–83ms。Git/Policy初轮128通过，最终新增/受影响50通过；PR/pipeline34+29subtests通过，另一次终态写回竞态单项复跑通过，未弱化断言。Release安装包0.2/20261006.2签名、DMG、107个核心文件与包内Blog解析校验通过。状态待体验，未安装正式App、远端push/PR或启用自动处理；没有将已安装旧版UI称为新版验证。完整证据见docs/evidence/github-ssh-alias-20261006/report.md。


## 2026-10-06 有效仓库被误报未配置

PlanningView错把available与任务状态ready混用，且对所有不可选任务显示配置入口，已改用TodoItem.needsRepositoryConfiguration共享映射判定。Models111/111及Fake协议检查通过；主agent原生核验available+planning不误报/仍不可选，unmapped正确警告；配置sheet实际600×560完整显示。Release0.2/20261006.3签名、DMG与107个包内核心文件校验通过。待体验；正式用户StarTool配置已通过核心检查，刷新当时任务自动规划中，未由agent重启或重新规划。证据见docs/evidence/planning-mapping-state-20261006/report.md。


## 2026-10-07 准备规划弹窗交接

旧版看板详情sheet仍在呈现时请求规划sheet，已改为onDismiss后交接；主agent隔离原生验证面板打开/任务定位勾选、取消后刷新、缺失目标占位连续刷新、普通关闭及快速双击通过；命令日志没有prepare-plan-input/start-plans。Release0.2/20261007安装包构建、签名、DMG只读包体及107个核心文件校验通过。看板修复待体验；待处理内联入口原生补查因Mac锁定仍实现中。未替换正式App、未启动真实规划，详见[本次证据](../docs/evidence/planning-handoff-20261007/report.md)。


## 2026-10-07 等待事件日志误报读取失败

正常pending的等待说明不再写connection.detail；真实半写入无快照回退pending仍保留读取错误。核心/契约109项及实际Store隔离回归通过；主agent原生核对实际核心响应回放的等待/解析失败两态及定时更新。Release0.2/20261007.1签名、DMG和107个包内核心文件校验通过，待体验。原生恢复截图阶段Mac锁定，未声称完成该附加检查；正式App与任务未操作。详见[证据记录](../docs/evidence/session-pending-20261007/report.md)。


## 2026-10-07 会话布局与Plus开始请求

空/等待/失败页头部固定顶部、元数据左对齐；新增真实Plus状态与确认开始入口，仅重新排队同一授权未发送的规划请求。真实现场只读返回Plus已连接/执行端失联/可开始；未发真实prompt或改正式请求。核心最终联跑87+19subtests，末次专项22+20subtests；实际Store与原生1180×760、900×600布局/确认/权限禁令通过，reviewer缺工具门P2已修。Release0.2/20261007.2包体/签名/DMG/108个核心文件校验通过，待体验。真实模型启动未代用户触发。详见[证据记录](../docs/evidence/plus-session-control-20261007/report.md)。


## 2026-10-07 Plus规划快照产物兼容

只允许未跟踪.pi/plan直层普通md，保留干净tracked.pi配置、材料和安全边界；Git工作树错误保留分类与脱敏原因。Git套件122通过后新增基线case，最终受影响21通过；Plus最终定向11、交付PR30+29subtests通过。安装包内guard只读接受用户现有快照且计划hash不变。Release0.2/20261007.3构建/签名/DMG/108个核心文件校验通过，待体验；没有正式重跑任务或模型调用。见[本次证据](../docs/evidence/plus-plan-artifacts-20261007/report.md)。


## 2026-10-29 页面对齐设计画板（第二批，须在云端包实机核对）📷 三尺寸 × 浅深色

依据：设计画板（Mac）总览 / 详情 / 执行复盘 / 设置四页的「优化后」画板；规格 §3、§4.4、§4.9、§4.11、§4.12、§4.15–§4.18。逐页拿安装后的云端包对着画板看；构建通过、安装、实机核对分别记录，未做的写「未运行」。

### 总览（dash-after）

- [ ] 页面边距 24pt、区段之间 24pt；「需要你 · N」是 18pt 半粗页面标题，在面板外面，右侧是「全部阶段」菜单与「查看全部 ›」
- [ ] 「需要你」是一块带描边的分组面板，行间 1pt 细线、最后一行之后没有线；每行依次为 16pt 图标、两行文字（标题 / 原因）、「项目 · 阶段 · 自 HH:mm」、28pt 带边框的动作按钮；行高不低于 60pt
- [ ] 图标：开放执行问题为橙色感叹圆圈，执行端失联为红色三角，其余阻塞为橙色三角
- [ ] 「流水线」标题 15pt，右侧说明保持完整一句（含构建发布那半句），窄屏换行；下面一块面板五格（待规划 / 待办 / 进行中 / 待评审 / 已完成），格间竖线，圆点加名称加 22pt 数字，整格可点进入任务页对应列
- [ ] 「正在推进 · N」每行左侧标题与项目名两行，右侧 8 段迷你进度条加「阶段 · 条件」；当前段蓝色、其余灰；「全部任务 ›」可点
- [ ] 底部并排两栏：「项目自动化」（项目名 / 圆点加自动处理已开 / 等宽胶囊 live、shadow，末行「项目设置 ›」）与「环境」（引擎心跳、诊断摘要加「在线诊断」、折叠行「详情 · 诊断 N 项 · 引擎记录 M 条 ›」）
- [ ] 展开折叠行后诊断逐项与引擎记录都能读到，PID 与租约可选中复制；900×600 下无横向滚动、无重叠
- [ ] 对齐（2026-10-30）：「项目自动化」各行的状态点、状态文字与 live / shadow / legacy 胶囊各自在同一条竖列上，混有 live 与 legacy 的项目也不错位；「环境」每一行（含未通过项、读取错误、展开后的明细）左缘同为 16pt，图标列 16pt 宽，标题文字的起点一致，未通过项与诊断明细的原因缩进到名称下面；制造一个未通过的诊断项（如工作树池）和一次读取错误各看一遍；侧栏「总览」「执行会话」换了窄符号（`square.grid.2x2`、`bubble.left`），看它们不再贴着文字、与其他图标宽度接近；1180×760 与 900×600、浅色与深色

### 任务详情头部与执行问答（detail-after）

- [ ] 第一行：标题 18pt（最多两行）与右端 24pt 圆形关闭按钮；Esc 可关闭，VoiceOver 读「关闭」
- [ ] 第二行：左侧元信息（项目 · 状态 · 已等待，圆点分隔，状态半粗带图标），右侧 280pt 分段器，同在一行；900pt 宽下元信息截断而分段器不变形；头部下有通栏细线
- [ ] 「下一步」行：蓝色圆形箭头图标加一句 13pt 说明
- [ ] 问答表头：「回答 N 个问题」15pt、来源 12pt、行尾「已答 a/b」；每题有序号圆、13pt 半粗题目、「单选 / 多选」小胶囊、行尾蓝色「跳过此题」，点击后变为「已跳过 · 撤销」并让该题选项置灰
- [ ] 选项组是白底带描边的面板，选项间有细线；有说明的选项 48pt、没有的 40pt；选中行淡蓝底；最后一行是「自定义回答」
- [ ] 底栏：「提交答案」（未答完禁用）、「还差第 N 题」、行尾「更多操作」

### 执行复盘（exec-compare）

- [ ] 收起：44pt 面板行，箭头、「复盘」15pt、已用时、一句摘要、带边框的「打开会话」；展开后摘要隐去
- [ ] 展开：关键指标四格在同一块面板里，竖线分隔，数值 18pt、备注 12pt，「未核验 N 条」前有橙色三角；面板底部是「更多指标：…」折叠行
- [ ] 「执行时间轴」15pt 在面板外；泳道 28pt、轨道 12pt 高圆角，右侧「N 分钟 · n%」；时间刻度；面板底部「分段明细 · N 段」折叠行
- [ ] 「重点」「验证与检查」各一块面板，宽 ≥640pt 并排；验证面板有「未核验 N」橙色小胶囊
- [ ] 复盘区下沿没有多余横线

### 设置·连接 / 规划与评审 / 高级（画板「优化后」；2026-11-01 起被文末 KP-UI03 清单取代，页名、保存行、高级页分区以后者为准）

- [ ] 每一行是「标题 + 其下 12pt 说明 + 右侧控件」，说明不单独成行；脚注在分组之外
- [ ] 保存行：「保存」在前、「重新读取」其后、状态句靠右（连接、规划、Review 各一处）
- [ ] 连接页：PI 配置里「连接文件路径」折叠行不再夹在模型字段之前；状态行带圆点、说明与右侧按钮；「Plus 控制面」一行仍写「本版本不需要会话挂接」
- [ ] 规划与评审页：Desktop 自动领取只有一个「修改运行时间…」折叠行，打开后规划与 Review 各有自己的编辑区
- [ ] 高级页：「开机启动」是第一行；分区依次为「后台与引擎」「自动推进」「诊断与路径」；诊断、路径、调度参数默认折叠，折叠行右侧有摘要
- [ ] 手机连接按行拆开，行间有系统分隔线，二维码与已配对设备仍可用

### 设置·项目（画板「优化后」；2026-11-01 起「保存配置」按钮被项目页保存栏取代，其余字段与版式不变）

- [ ] 列表行：状态点加文字，未完成任务数与活动请求数合并为一行文字
- [ ] 详情标题行：名称 18pt，状态紧随其后，项目 ID 等宽靠右并带复制图标
- [ ] 「仓库映射」「自动处理」「引擎」三张卡：标题在卡外，卡内每个字段一行（标签在左、控件在右、行间细线）；「自动处理」标题右侧写「策略版本 r<版本>」
- [ ] 「保存配置」为主要按钮，「解除映射…」为次要按钮并保持主文字色（不是红色）；影响提示是卡内独立一行

### 共性缺陷

- [ ] 任一页面没有因外层是横向栈而画成竖线的 `Divider()`；悬停行底色为淡蓝；侧栏页脚状态标题 13pt 半粗

## 2026-11-01 设置页信息架构精简（KP-UI03，须在云端包实机核对）📷 1180×760 与 900×600 × 浅深色

依据：画布「第三轮 · 设置页信息架构精简（KP-UI03）」页的 `ui03-set-*` 五块画板；规格 §4.4、§4.9–§4.11、§4.14、§4.16、§5.25；计划 `docs/plans/设置页信息架构精简_20261031.plan.md`（编号 A1–A10 与计划一致）。逐页拿安装后的云端包对着画板看；构建通过、安装、实机核对分别记录，未做的写「未运行」。

- [ ] A1 设置默认落在「运行端」；分段顺序「运行端 / 项目 / 连接 / 高级」；总览「项目设置 ›」直达项目页
- [ ] A2 运行端页：规划、执行、评审三行的摘要随所选后端变化（规划 Codex / Claude / Qoder / Plus / Codex Desktop，执行 pi / Codex CLI / Plus，评审各后端）；就绪是圆点加文字；900×600 首屏内放得下（估算 463pt，以实测为准）；读取中有进度，读取失败有「重试」
- [ ] A3 编辑面板（规划、执行、评审）：打开即读取已保存值；改后「取消」丢弃，再开仍是已保存值；「保存」只调用本角色命令，成功 toast 加关闭；失败面板不关、草稿保留、显示核心原文；执行面板首次保存前须选权限模式（保存不可用，脚栏写「保存不可用：请先选择权限模式」）；切换运行端不覆盖草稿，Plus 候选刷新不覆盖非空草稿；Esc 取消、Return 保存；900×600 下脚栏常驻、正文滚动
- [ ] A4 Desktop：「允许 Desktop 领取」即时生效；「管理…」面板里两个定时检查开关即时生效，失败重读并显示错误；「保存运行时间」才写入，未保存草稿在关闭、重开面板后仍在且「修改运行时间…」标题有「有未保存改动」；「在 Codex 中提前执行」、打开聊天可用；规格要求的两句说明原文在页面脚注上
- [ ] A5 项目页保存栏：改 1 项，栏出现并写「有 1 项未保存更改 · 行名」；同时改映射、策略、引擎模式、备用模型一次保存，全部成功且无假冲突；中途失败（如策略校验被拒）时前面成功的清除改动标记、失败项显示核心原文、其余草稿保留，再点保存只提交剩余项；「放弃」只丢未保存草稿并读回，已保存项不回滚；开启自动合并 / 构建发布 / 启用 Live 的确认在第一条命令前一次问完，取消则一条命令也不发（核对事件日志没有对应写入），策略类与 Live 同时需要确认时合并为一个弹窗；保存失败时红色横幅指向出错的卡片，原文在该卡片内，策略失败不再另弹全局提示；目标分支暂未核验（`unverified`）时只有高级选项里的就地提示和成功横幅里的一句话，保存栏不留改动行；「解除映射…」有未保存更改时置灰并写明原因，无更改时弹窗不变；从未映射的项目一次保存先映射再保存其余；切换项目草稿不丢、列表行标「未保存」；历史兼容验证环境仅在已有旧值时出现且并入保存栏；<720pt 上下堆叠、保存栏仍固定在底部
- [ ] A6 连接页：本机 MCP 服务启动 / 停止、错误「重试」、外部服务只读提示正常；「连接 Plus」用已保存路径探测，未保存的草稿路径不参与；「本版本不需要会话挂接」全设置只出现一次；手机连接（开关、网卡与端口、二维码、复制、撤销设备、更换地址确认）不变
- [ ] A7 高级页只有两个分区；在途上限步进后即时提交，失败退回并显示核心原文；「检查状态」「允许需要你通知」「接收详情」同前；「复制诊断信息」内容含路径、调度参数、诊断结果、迁移记录，检索确认不含 token 或凭据；「打开数据目录」打开状态目录；整页首屏内（估算 521pt，余量小，以实测为准）
- [ ] A8 总览：首次进入自动离线诊断一次，摘要不再停在「尚未诊断」；这一次不占全局忙碌状态（诊断进行时任务页的按钮仍可点）、核心不可用时启动不弹窗、摘要停在「尚未诊断」；「在线诊断」仍可用，结果覆盖摘要，失败仍弹提示；设置页没有诊断入口
- [ ] A9 回归：侧栏页脚与菜单栏的暂停 / 恢复正常；设置页不再出现「重新读取」「重新载入」（模型列表弹层内的「重新读取」除外）；规格要求的说明文字仍在
- [ ] A10 键盘与无障碍：Tab 可到每个「编辑…」「管理…」；面板与保存栏的按钮、不可保存的原因由 VoiceOver 读出；就绪状态读出文字
