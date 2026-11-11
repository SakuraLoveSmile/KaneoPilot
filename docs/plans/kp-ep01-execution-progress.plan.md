# KP-EP01 执行页改为「最新进展」

- 任务标识：`kp-ep01`
- 状态：待体验（2026-11-12 执行页目标进度已实现；7228bb3 云端构建 215 全绿，隔离原生界面核验通过；未合入、未发布、未更新正式安装，真实任务与用户体验尚待确认）
- 分支：`feat/20261110-kp-ep01-execution-progress`
- worktree：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261110-feat-kp-ep01-execution-progress/`
- 基于：`chore/20261110-kp-wt01-task-worktree-layout` 的 `0efa23a`（当前安装构建 194、真实库 schema 7）
- 合入目标：`main`
- 记录日期：2026-11-10（台北）

## 一、目标与范围

执行页不再内嵌完整会话阅读器，也不再每 2 秒拉取/渲染整段会话。页面只显示执行端上报的**最新一条关键进展**（含更新时间），完整会话改为按需打开；既解决卡顿，也避免把工具 JSON 和过程正文摆给用户。

范围：

1. 执行端 worker 从 Plus 助手消息中提取一行进展，写入 `events.jsonl` 的 `progress` 事件。
2. 核心 `get-session.round_detail` 投影最后一条进展（`progress: {text, at}`），不新增命令、不改 schema。
3. monitor 的「本轮仍在运行」改用该进展写 `last_progress`，首页卡片同步受益。
4. 桌面执行页：复盘 + 最新进展卡片 +「查看完整会话」浮层；移除内嵌阅读器与 2 秒正文轮询。
5. 规格与验收清单同步。

不在范围：安卓端、阅读器自身的列表/搜索/管理能力、`session_poll_seconds` 对完整阅读器的实时读取、发布流程。

## 二、已确认决定

| 编号 | 决定 | 依据 |
| --- | --- | --- |
| D1 | 执行页只显示最新一条进展，不做多条进展流 | 2026-11-10 用户选择「只留最新一条进展（推荐）」 |
| D2 | 完整会话保留，按需在浮层打开（沿用既有阅读器与 2 秒增量读取） | 证据可追溯，调试不受影响 |
| D3 | 进展由执行端 worker 产出 `progress` 事件，核心只读投影 | 用户要求「让 pi-desktop 只汇报关键进展」；不改 Plus 本体 |
| D4 | 不新增核心命令、不改 SQLite schema | 复用 `get-session` 与事件日志 |
| D5 | 进展读取用完整 `get-session`，不用 `status_only` | 状态快照按既有隐私测试不得携带消息文本；完整读取只返回投影后的 160 字进展 |
| D6 | 旧日志无 `progress` 事件时回退到最后一条助手消息 | 安装新构建后当前历史轮次立即可见，不必等新轮次 |
| D7 | 时间线摘要默认，原文保留切换 | 2026-11-10 用户选择「两处统一，默认时间线 + 原文切换」 |
| D8 | 时间线在桌面折叠，不新增核心命令/schema | 复用 `session-messages` 已带来源元数据；摘要不是证据 |
| D9 | 目标状态/待办并入轮次标题下，不单独开面板 | 2026-11-10 用户追加 |
| D10 | 目标契约与每次进展不重复列为动作行 | 标题下已持续显示；避免同一信息三处出现 |

## 三、数据与判定

`progress` 事件（append-only，读取层忽略其消息语义）：

```json
{"type":"progress","seq":123,"ts":"...","text":"找到匹配的签名密钥，先核对证书","at":"...","source_message_id":"...","source_revision":1}
```

- 只从 `assistant` 消息提取；空内容、代码围栏、JSON 对象/数组开头不产出。
- 取首个非空行，去掉 Markdown 前缀，折叠空白，最多 160 字符，超出以 `…` 结尾。
- 同一消息内容不变不重复写；修订后文本变化可再写。
- `round_detail.progress = {text, at}`：优先最后一条有效 `progress`，无则回退到最后一条非空助手消息（同一规范化）；`at` 取消息时间（缺失时取事件时间）；无则 `null`。只读显示，不参与完成/失败/验证判定。
- `last_progress`：monitor 在「仍在运行」分支优先写该文本，缺失回退「本轮仍在运行」。

## 三之二、阶段二：会话时间线摘要（2026-11-10 追加）

用户追加：查看完整会话也不直接输出原文，折叠成摘要，按轮次和关键动作展示时间线。已确认范围：执行页浮层与侧栏「执行会话」页统一，默认摘要，保留「摘要 / 原文 / 状态」切换。

- 轮次按 `turn_id` 分组；规划轮以目标契约/提问识别，执行轮以待办进展/验证报告识别；轮次标题、起止时间、状态可收起。
- 关键动作优先用执行端结构化信号：`SubmitGoal`（目标契约）、`asktool`（问答）、`UpdateGoalProgress`（待办 X/Y）、`Write/Edit`（文件）、关键 `Bash`（构建/测试/验证/提交/安装/非零退出码）、`SubmitGoalReport`（逐条检查）、subagent/terminal；普通读取/查询折叠为「N 个准备操作」。
- 助手文本只保留首条、末条与间隔 ≥3 分钟的里程碑，限长单行；不把完整正文塞进摘要。
- 每条可展开短摘要；逐字内容用「查看原文」切到原文模式。不新增核心命令/schema；摘要只读，不是完成/失败/验证证据。
- 桌面新增 `SessionTimeline.swift`（Foundation 折叠器）与 `SessionTimelineView.swift`；`SessionReaderView` 的「会话内容/会话状态」改为「摘要 / 原文 / 状态」，默认摘要；解码 `turn_id`/`tool_call_id`/`is_error`/`event_type`/`agent_name`。

## 三之三、阶段三：目标进度并入轮次（2026-11-10 追加）

用户追加：Pi 执行时有目标显示，希望把目标进度直接并进时间线，按轮次显示目标状态和待办完成情况，不再单独开面板。

- 每个轮次标题下直接显示目标标题（`SubmitGoal` 首个标题，后续轮次继承）、目标状态（已提交 / 未开始 / 进行中 / 待办完成 / 报告待修正 / 报告已记录）、最新待办 X/Y、线性进度条与可展开清单；多次 `UpdateGoalProgress` 在展开区保留进展记录。
- 目标契约与每次待办进展不再作为动作行重复出现；轮次面板里只保留问答、文件、关键命令、验证报告、子代理/结束等动作。
- 无 `SubmitGoal` 但有进展时使用「执行目标」回退标题；目标摘要只读，不参与完成/失败/验证判定。

## 三之四、阶段四：刷新 30 天真实形状夹具并发布测试版（2026-11-10 追加）

用户选择先解决 30 天 Plus 形状夹具时间门，再发布 `v0.5.6-test.N` 并更新到测试版。

- 用本机真实 Plus 0.15.14 只读录制 `app/getVersion`、`agent/getStatus`、`turn/get`、`turn/get-missing`、`goalReport/list`、`plans/pending` 六件套；`goalReport/list` 与 `plans/pending` 现在需要 `sessionId` 参数。
- 形状事实更新：`agent/getStatus` 新增 `pendingInputs`，`turn/get` 新增 `endedAt`；两者仍无终态 outcome。`_captured_at` 全部更新为 2026-11-10。
- 三个 `reconstructed_from_core_parsers` 样本更新日期与说明；状态样本补 `pendingInputs`，规划期字段按 worker 契约保留。
- `tests/rehearsal` 全量 74 passed / 6 skipped。

## 四、界面

执行页顺序：复盘摘要（原样）→「最新进展」卡片。

- 卡片：标题行「最新进展」+ 运行状态 +「查看完整会话」；正文最多 3 行、可选择；底部「更新于 x 分钟前」。
- 无进展：显示「执行端尚未上报进展」；仍有完整会话入口。
- 复盘标题行的「打开会话」改为打开同一浮层，不再滚动定位。
- 浮层：复用 `SessionReaderView`，约 900×640，关闭后停止其轮询；打开时才按 2 秒读取正文。
- 执行页自身进度读取：`list-sessions` + `get-session(status_only:true)`，20 秒一次；不再调用 `session-messages` / `session-control-status`。

## 五、验收场景

| 编号 | 场景 | 期望 |
| --- | --- | --- |
| A1 | 运行中的 pi_desktop 轮次 | 进展卡片显示最新助手汇报与「更新于」，随新消息更新；无 2 秒正文轮询 |
| A2 | 助手只调用工具、暂无文字 | 保持上一条进展，更新时间不变；不显示工具 JSON |
| A3 | 新轮次/切换任务 | 卡片按 task/run 身份重建，不串轮次 |
| A4 | 读取失败 | 保留上一条进展并标注读取失败；完整会话仍可打开 |
| A5 | 打开/关闭完整会话 | 浮层内沿用轮次选择、内容/状态、搜索、管理动作；关闭后停止轮询 |
| A6 | 老轮次/非 pi_desktop | 无进展时显示空态，不伪造进展 |
| A7 | 复盘指标 | 轮次/会话消息/最近检查仍能显示（来自 list-sessions + get-session） |

## 六、未验证 / 遗留

- 执行页「最新进展」卡片与「查看完整会话」浮层的 1180×760 / 900×600、浅深色实机走查。
- 卡片 20 秒刷新与浮层关闭后停止轮询的实测。
- 摘要时间线在真实 Music 会话上的视觉走查：轮次/关键动作/折叠/原文切换，以及 900×600 浅色与 1180×760 深色。
- 轮次目标块（标题、状态、X/Y、进度条、清单、进展记录）的视觉走查。
- App 内「接收测试版」开关若未开启，后续测试版不会自动推送；本次为手动安装。
- 云端唯一红项是既有的 30 天 Plus 真实形状夹具时间门（与本任务无关，待另行决定重录或调整）。

## 七、验证与记录

（实现开始后逐条追加。）

| 时间 | 动作 | 结果/证据 | 状态 |
| --- | --- | --- | --- |
| 2026-11-10 17:45 | 建任务 worktree | 分支 `feat/20261110-kp-ep01-execution-progress`，基于 `0efa23a` | 完成 |
| 2026-11-10 18:00 | 规格 | `protocol.md` §5.18.4 增加 `round_detail.progress` 与只读边界、§5.23.3 增加 worker `progress` 事件；`ui-spec.md` §4.18 执行页改为复盘 + 最新进展、§4.5 浮层入口；`tests/ui-acceptance.md` 增加 KP-EP01 清单 | 完成（未实机） |
| 2026-11-10 18:00 | 核心 | worker 对助手消息写 `progress`；`dsh_events.progress_text/latest_progress` 与回退；`round_detail.progress`；monitor 的 `last_progress` 使用该文本 | `test_execution_progress` 9 项通过；`test_session*`、`test_pi_desktop_worker_verification`、`test_auto_monitor`、`test_desktop_event_projection` 合计 250+ 通过 |
| 2026-11-10 18:05 | 隔离副本核对 | 用 `/tmp` 库副本与当前运行目录（约 1 MB events）跑 `get-session`：`round_detail.progress = {"text":"已完成并提交结构化报告。以下是逐条结论。","at":"2026-11-10T09:50:32Z"}`，耗时 0.31 秒 | 通过（只读副本） |
| 2026-11-10 18:10 | 桌面 | 新增 `ExecutionProgressStore`（list-sessions + 完整 get-session，20 秒）、`ExecutionProgressView` 卡片与 `ExecutionSessionSheet`；执行页移除内嵌阅读器；复盘「打开会话」改为浮层 | 未编译（按约定云端编译） |
| 2026-11-10 19:00 | 全量核心测试 | `core/tests` 全量 4733 passed、1812 subtests passed、5 failed：两处事件计数把 progress 展示事件当成转录（分页 135≠68、输入恢复 [3,1,4,4,5,5]），已改为按非 progress 事件计数并复查两个文件 19 项通过；全量耗时 1:10:23 | 5 项为预期变更，已修 |
| 2026-11-10 19:00 | 云端构建 195（42802789566） | `macos-package`、`macos-fixtures` a/b、android、identity、codex-platforms 成功（Swift 编译通过）；checks 红于上述两处测试计数、既有 30 天 Plus 形状夹具和一次 codex 进程组 flake | 构建成功、checks 非绿 |
| 2026-11-10 19:10 | 修复测试预期并推送 d0c5365 | 新构建 42805302270 触发 | 完成 |
| 2026-11-10 19:20 | 云端构建 196（42805302270） | `macos-package`、`macos-fixtures` a/b、android、identity、codex-platforms 成功；`checks (1, 3.11/3.13)` 仅红于既有 30 天 Plus 形状夹具门（`plus_status_real.json`、`plus_app_getVersion_real.json` 采集于 2026-10-10）；本任务修复的两个测试文件通过，codex 进程组 flake 未复现 | 构建成功，checks 仅剩既有时间门 |
| 2026-11-10 19:52 | 安装 196 | 用户选择立即停掉并安装：停止 App/引擎/MCP/mobile/worker；旧包备份至 `/Applications/.KaneoPilot-backup-ep01-20261110T195254`；DMG sha256 与 metadata 一致、codesign 校验通过、`CFBundleVersion=196`；启动后引擎与 `--resume` 观察者恢复，执行问答保留（总览仍显示 1 项需要你） | 完成 |
| 2026-11-10 19:53 | 安装包核心核对 | 用安装包内置核心对两个轮次跑 `get-session`：`a97ec6d4`（finished）返回「已完成并提交结构化报告。以下是逐条结论。」，`9b26e786`（running）返回「核对已完成：…提交前有两处需要你确认。」；`at` 与消息时间一致 | 通过 |
| 2026-11-10 19:56 | 实机界面 | 总览、引擎心跳（12 项通过）、执行问答可见；执行页卡片与「查看完整会话」浮层未做视觉走查（用户正在使用屏幕，未抢占前台） | 待体验 |
| 2026-11-10 20:20 | 阶段二实现 | `SessionTimeline` 折叠器 + `SessionTimelineView`；阅读器改为「摘要 / 原文 / 状态」；`SessionMessage` 解码 turn/tool/error 元数据；夹具与规格同步 | 本地仅静态核对，按约定云端编译 |
| 2026-11-10 20:47 | 构建 197（42816601351） | `macos-package` 成功；`macos-fixtures (b)` 编译失败于夹具原始字符串分隔符（"# 被当作结尾）；其余 fixtures 通过；checks 仅剩既有 30 天形状门 | 测试夹具问题，已修 c7aec29 |
| 2026-11-10 21:02 | 构建 198（42819070312） | `macos-package`、`macos-fixtures` a/b、android、identity、codex-platforms 成功；新增时间线夹具通过；checks 仍仅红于既有 30 天 Plus 形状夹具门 | 构建成功 |
| 2026-11-10 21:08 | 安装 198 | 停止 App/引擎/worker；旧包备份 `/Applications/.KaneoPilot-backup-ep01-20261110T210818`；DMG sha256 与 metadata 一致、codesign 通过、`CFBundleVersion=198`；启动后引擎与 `--resume` 观察者恢复 | 完成 |
| 2026-11-10 21:20 | 真实消息元数据核对 | 完成轮 `a97ec6d4` 的 `session-messages`：309 条，305 条带 `turn_id`、172 条带 `tool_call_id`；`asktool` 的 `tool.input` 为问题 JSON，可被时间线解析 | 通过 |
| 2026-11-10 21:22 | 时间线实机视觉 | 尝试点击侧栏/任务行打开会话，坐标点击抢到前台应用且用户正在使用 DSH，已停止自动化；时间线界面未截图核对 | 未验证 |
| 2026-11-10 21:55 | 阶段三实现 | 轮次目标摘要：`SubmitGoal` 标题向后续轮次继承，`UpdateGoalProgress` 汇总最新待办 X/Y、状态、检查点与清单；目标契约/每次进展不再单列动作行、不单独开面板；新增目标块与清单展开 | 云端编译前静态核对 |
| 2026-11-10 22:06 | 构建 199（42828969469） | `macos-package`、`macos-fixtures` a/b、android、identity、codex-platforms 成功；时间线目标夹具通过；checks 仅红于既有 30 天 Plus 形状夹具门 | 构建成功 |
| 2026-11-10 22:08 | 安装 199 | 屏幕锁定状态下停止 App/引擎（前台为 loginwindow）；旧包备份 `/Applications/.KaneoPilot-backup-ep01-20261110T220712`；sha256 与 metadata 一致、codesign 通过、`CFBundleVersion=199`；启动后引擎、monitor 与 `--resume` 观察者恢复 | 完成 |
| 2026-11-10 22:40 | 30 天形状门取证 | Plus 0.15.14：`agent/getStatus` 新增 `pendingInputs`、`turn/get` 新增 `endedAt`，仍无终态 outcome；`goalReport/list`/`plans/pending` 需要 `sessionId`。`turn/get-missing` 仍返回 `{ok:false,error:{code:NOT_FOUND}}` | 形状漂移已确认 |
| 2026-11-10 22:50 | 重录夹具 | 真实重录六件套并更新 `_captured_at=2026-11-10`；三个 reconstructed 样本更新日期/说明并补 `pendingInputs`；事实断言同步 pendingInputs/endedAt；`tests/rehearsal` 74 passed / 6 skipped | 通过 |
| 2026-11-10 23:13 | 测试版发布 v0.5.6-test.201 | 推送 331d335 触发 run 42837615030；`channel=test`、`base_version=0.5.6` 手动 run 42837710184 全绿（identity/android/macos-fixtures a+b/macos-package/checks 1–5/codex-platforms/publish）；预发布 https://github.com/SakuraLoveSmile/KaneoPilot/releases/tag/v0.5.6-test.201，source 331d335 | 完成 |
| 2026-11-10 23:18 | 安装测试版 201 | 下载 DMG（sha256 6d3ca91a… 与 SHA256SUMS/manifest 一致）挂载校验 `CFBundleShortVersionString=0.5.6-test.201`、`CFBundleVersion=201`、codesign 通过；停止 199 并备份 `/Applications/.KaneoPilot-backup-ep01-20261110T231808`；启动后 MCP/mobile/engine 在跑，schema=7 | 完成 |


## 八、2026-11-12 追加：执行页直接展示目标进度

用户要求：把 pi-desktop 的目标进度（目标标题、完成比例、执行状态、待办清单、目标说明）接到任务详情的「执行」页，不必进入完整会话才能看到。

- 当前基线：主工作区 `main` / `ea02654`；原有 `.local/` 与未跟踪 `docs/plans/kp-cache01-macos-build-cache-cleanup.md` 保留。新工作树 `/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261112-feat-kp-ep01-goal-progress`，分支 `feat/20261112-kp-ep01-goal-progress`，合入目标仍为 `main`。旧任务 worktree 属既有现场，不复用或清理。
- 本次明确替代 D1/D9 的显示位置限制：执行页在最新进展卡片下新增可折叠「目标进度」，完整会话内原有轮次目标保留。保留复盘、最新进展、完整会话入口、20 秒刷新与项目视觉规范；不改调度、完成门禁、Android 或 PI-Desktop。
- 数据依据：PI-Desktop `goal-progress-presentation.ts` 按快照 items 的 completed / length 计数；`GoalProgressBar.tsx` 标题来自 proposal。当前 MCP 不提供独立 goalProgress.get，采用已有采集链中成功的 SubmitGoal / UpdateGoalProgress 结果还原。完整字段/边界唯一维护于 protocol §5.18.4；UI 规则见 ui-spec §4.18。未上报清单前显示空态，不编造初始待办。
- 分工：`pi_goal_source` 只读查证 PI-Desktop；`goal_projection` 独占 dsh_events.py 与新增隔离测试；主控负责 Swift DTO/执行页、FakeCore、解码夹具与规格/本记录；`goal_review` 只读审查组合改动。不调用付费模型或业务执行，不改真实 DB。
- 验收：执行页出现标题/完成数/清单；四态正确，失败调用不增加完成数；目标/说明可折叠，长文本换行；刷新失败目标标过期；新目标/任务不串数据；无目标隐藏，有目标无清单明确空态；1180×760 深色与 900×600 浅色的原生界面检查。
- 云端编译与原生视觉：构建 215 与隔离界面核验通过，详见下方记录。仅在云端编译/签名；本地只核验包并启动云端隔离预览。用户已明确授权本次提交、推送与云端构建；未合入、发布或更新正式安装。
- 收尾：未合入 main，工作树保留作为本轮可检查交付；合入且验证通过后按项目规则清理本轮工作树，不影响旧任务现场。

### 本轮验证记录

- 基于 `ea02654` 的本轮组合改动：目标/文字进展 `unittest discover -s tests -p 'test_execution*progress.py'` **25 passed**（含 Collector 真形状消息→事件→目标投影、新目标、修订、错误结果与宿主标题优先）。会话 `test_session*.py` **124 passed**；Desktop 事件投影 **4 passed**（子 agent），主控检查最终差异与 Python AST / `git diff --check` 通过。
- 独立审查发现目标标题来源应优先宿主 proposal.title，已修正并补回归测试；无其余已确认问题。
- `test_dsh_events_verification.py` 是 pytest 函数，unittest 发现 **0 项，未执行**；隔离解释器确认本机没有真实 pytest，未将该项记为通过，不安装依赖或降低门禁。云端使用真实 pytest。
- Swift 解码夹具与 FakeCore 已补四态/长标题示例；未本地编译。云端与原生核验结果待后续追加。

### 云端与原生核验（本轮最终结果）

- 代码提交 `7228bb30ae58302e7179d3486db4686a351f2a02`，Packages [run 43061669500 / build 215](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/43061669500) **success**：identity、macos-package、macos-fixtures a/b、Android、checks 1–5 × Python 3.11/3.13、Codex native-platforms 全部通过；可选 fulltext probe 未启用、跳过。
- 下载 `macos-update-ui-215`，SHA256 与 ui-metadata.json 一致，source_sha 与上述提交一致，`codesign --verify --deep --strict` 通过。未改签、未修改 App 包，未调用本地编译。
- 用云端独立 bundle `local.kaneopilot.software-update-check` + 本工作树 FakeCore + 独立数据目录启动。1180×760 深色实测 1/4、环形比例、四种状态、整体折叠与目标说明展开；900×600 浅色实测长目标/长待办换行、状态可见、失败保留旧目标并标过期、空清单无 0/0、无目标隐藏、恢复后正常显示。观察依据为本会话 CUA 原生截图与 AX 树；没有以 HTML/mock 页面代替原生界面。数据为隔离夹具，不声称真实任务/设备验收。
- 本地证据位于本工作树 `build/kp-ep01-goal-progress/`：云端原包/metadata、夹具日志、隔离环境与 `ui-verification.json`；预览 PID 20635 / 23229 均已结束。未更新 `/Applications/KaneoPilot.app`，未修改真实 DB 或发起业务模型请求。
- 工作树保留原因：本分支尚未合入 main，提交与验证产物仍需保留。下一步如获授权，合入/发布；合入且验证通过后迁出需要保留的证据，再清理本轮 worktree。此记录不授权后续外部发布。


## 九、2026-11-12 发布新的测试版本

用户追加要求发布新的测试版本。此前 run 215 为 push/preview，只生成 artifact，未发布；本次按 `channel=test` 手动发布。

- 发布分支：`feat/20261112-kp-ep01-goal-progress`；基础版 `0.5.7`（当前正式版 `0.5.6`，最高测试基础版 `0.5.7`），版本为 `0.5.7-test.<run_number>`。
- 为避免新测试版回退已发布功能，先合入 `v0.5.7-test.214`（来源 `585f296`）的继续执行发送结果核对/槽回收修复；无冲突，当前目标进度与继续执行独立保留。组合态本地目标/文字进展25项与继续执行12项通过；完整门禁由本次测试版工作流重新运行，不能沿用215替代。
- 公开说明：执行页新增目标进度，显示目标标题、完成比例、待办四态与可折叠说明，20秒刷新；读取失败保留并标注旧数据；包含214的继续执行修复。无数据库schema变更。构建215的隔离界面已核验，本次组合态以新工作流证据为准。
- 用户此次要求发布测试版本，未要求替换正式安装或合入 main；发布后核对Release/tag/四项资产与manifest，不自动安装到正式App。

- 组合审查发现214既有P2：未确认发送为 `eligible=false/state=none/reconcile.available=true`，但父页只在 eligible/sent/observing 时创建继续执行栏，核对按钮被隐藏。本次补上父级 reconcile.available 显示条件；不改变发送/回收业务规则。原测试发布 run43069259736（候选219）在公开前取消，修复提交重新构建发布；候选版本不能当成已发布版本。
