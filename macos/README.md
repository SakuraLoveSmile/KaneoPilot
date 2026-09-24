# KaneoPilot macOS 桌面端

原生 SwiftUI 工作台「Kaneo 调度台」。数据全部经核心 CLI（`kanpilot`，JSON 进出）获取与变更，不直接读写 SQLite。

## 构建

```bash
xcodebuild -project macos/KaneoPilot.xcodeproj -scheme KaneoPilot -configuration Debug build
```

产物在默认 DerivedData 下（`…/Build/Products/Debug/KaneoPilot.app`）。也可直接用 Xcode 打开 `macos/KaneoPilot.xcodeproj` 运行。

- deployment target：macOS 14
- bundle id：`local.kaneopilot`
- 沙盒：未启用（无 entitlements）；硬化运行时：关闭。需要进程执行与文件访问权限以调用核心 CLI、NSWorkspace 打开文件。
- 无第三方依赖。

## 运行

应用启动时按以下顺序解析核心路径（`Core/CoreLocator.swift`）：

1. 环境变量 `KANPILOT_CORE`（可指向 `.py` 脚本或可执行 shim）
2. `<App bundle>/Resources/core/kanpilot.py`（或 `kanpilot`）
3. repo 相对 `../core/kanpilot.py`（由源码位置推导，另扫 bundle 祖先目录兜底）

调用形态（`Core/CoreClient.swift`）：

```
python3 <corePath> <command> --json '<payload>'     # .py / 无执行位
<corePath> <command> --json '<payload>'             # 可执行 shim
```

每个命令一个独立 `Process`，在后台 detached task 执行（60s 超时，`doctor`/`recheck` 120s）。非零退出解析 stdout 的 `{"error","code"}` 原文；超时/找不到核心/启动失败 → 顶部「无法连接调度核心」横幅；坏 JSON → 读取失败横幅。

### 执行端调度（v1.6，protocol §5.19）

执行后端为 pi CLI，四个执行端（plan/dispatch/monitor/review）由 App 内置
`ExecutorScheduler`（`Core/ExecutorScheduler.swift`）按错开分钟拉起
`python3 -m kanpilot.executor_runner --stage <s>`（plan 0/15/30/45、
dispatch 4/19/34/49、monitor 8/23/38/53、review 12/27/42/57 分；
每次触发先 `executor-status` 查单飞）。**仅 App 运行期间触发**，App 退出后
不再拉起（替代旧 Codex automation 定时执行端）。开关与各阶段状态在
设置页「执行端调度」（UserDefaults `executorSchedulingEnabled`，缺省开启）。

### 用 FakeCore 演示（开发期数据源）

```bash
# 方式一：环境变量
KANPILOT_CORE=$PWD/macos/FakeCore/kanpilot-fake.py \
  open .../Build/Products/Debug/KaneoPilot.app

# 方式二：命令行直接体验协议
python3 macos/FakeCore/kanpilot-fake.py list-tasks
python3 macos/FakeCore/kanpilot-fake.py get-task --json '{"task_id":"task-comic-010"}'
```

`macos/FakeCore/kanpilot-fake.py` 覆盖全部 display_status 与 allowed_actions 分支：待规划/规划中/就绪/已排队/派发中/执行中/评审中/需人工（send_state=not_sent 与 unknown 两种，后者带证据块）/执行端失联（monitor 与 dispatch 两种）/已归档，以及 40+ 字长中文标题任务。看板字段（board_column/board_substatus/blocked_reason/last_checked_at/review_summary）按 protocol §2/§5.15.4 派生，六列全覆盖：规划受阻、发送不明（派发受阻）、失联执行中、待修复（带 fix_task_id）、评审受阻、规划/派发/执行失败，共 21 张卡验证列内滚动。动作（enqueue/set-paused/set-priority/resolve/recheck）写入 `$KANPILOT_FAKE_STATE`（默认 `$TMPDIR/kanpilot-fake-state.json`）做有限状态模拟，删除该文件即复位。

### v1.3 新增夹具（protocol §5.15）

- **保存状态**：`save_state`/`saved_at`/`auto_advance_paused` 字段；`list-tasks {save_state: active|archived|trashed|all}` 过滤（缺省 active，非法值 `bad_request`）；罐头顶一条归档（`task-lib-022`）与一条回收站（`task-comic-023`）。
- **异常归列（§5.15.4）**：`lifecycle_column` / `exception{kind,label,severity,detail,failure_code,fix_task_id}` / `board_column` 按 v1.3 口径派生——needs_user、评审未通过·待修复、失联一律进第六列 `failed`；`board_substatus` 在第六列等于 `exception.label`（severity=failed 红、blocked 橙）。
- **归档/回收站/恢复**：`archive-task` / `trash-task` / `restore-task {resume_auto?}`。禁用条件与协议一致（正在规划/派发/执行/评审、未解除失联、发送不明 → `conflict` + 中文原因）；移出时同事务取消该任务 `queued` 请求；幂等返回 `{"ok": true, "deduplicated": true}`。
- **本机执行会话（§5.15.5，v1.6 pi 事件流）**：`list-sessions {task_id?}` / `get-session {task_id, run_id}` / `session-messages {task_id, run_id?, cursor?, limit?}`。夹具覆盖：多执行轮次（`task-music-006` 的 a1 历史轮次与 b2 当前轮次）、六类消息（user/assistant/tool_call/tool_result/command_output/system）、工具调用默认折叠、游标增量（增量时把最新消息升版一次，演练 `id`+`revision` 去重/更新）、三种连接状态（ok/degraded/unavailable）、`pending` 会话待关联（`task-fb-005` 等）。

会话读取失败演练（顶部「连接中断，正在重连」横幅 + 旧内容保留 + 「可能已过期」）：

```bash
KANPILOT_FAKE_SESSION_FLAKY=task-music-006 \
  KANPILOT_CORE=$PWD/macos/FakeCore/kanpilot-fake.py open .../KaneoPilot.app
```

`KANPILOT_FAKE_SESSION_FLAKY=<task_id>` 让该任务的会话读取失败一次（记入状态文件，删除状态文件即复位）。

## 结构

```
KaneoPilot/
  KaneoPilotApp.swift      WindowGroup（默认 1180×760，最小 900×600）
  AppViewModel.swift       @MainActor view-model：读取/动作/横幅/toast
  Core/
    CoreLocator.swift      核心路径解析
    CoreClient.swift       Process 执行 + 超时/错误映射
    Models.swift           协议 Codable 模型 + 时间格式化
    ExecutorScheduler.swift 执行端调度器（§5.19：错开分钟拉起 executor_runner）
  Views/
    RootView.swift         七节侧栏 + 工具栏 + 横幅/toast
    TasksSectionView.swift 任务节入口（包 BoardView）
    BoardView.swift        六列看板（排序/搜索/筛选/键盘/卡片）+ 详情浮层（执行会话/任务信息分段）
    TaskDetailView.swift   详情六区块 + allowed_actions 操作区（含归档/回收站/恢复）
    SessionReaderView.swift §4.4 会话阅读器（独立视图与详情浮层内嵌复用）
    SavedTasksView.swift   §4.4 执行会话列表 + §4.5/§4.6 归档/回收站列表
    PendingView.swift      待处理（活动请求/需人工/最近失败）
    RunsView.swift         运行记录时间线
    SettingsView.swift     诊断/PI 执行配置/执行端调度/自动推进/参数/路径/迁移
    Components.swift       状态徽章、横幅、区块、映射表、NSWorkspace、NSAlert
FakeCore/
  kanpilot-fake.py         假核心（见上）
```

## 已知取舍 / 契约缺口

- **settings 值与路径无读取命令**：protocol v1 没有暴露 `lease_seconds` 等 settings 值、`settings.migration`、Plan 根/DB 路径的命令。实现：`overview` 响应可选解码 `settings` 对象（FakeCore 提供，真核心 v1 不返回则对应区块隐藏/显示「—」）；路径从 `doctor` 各项 `detail` 字符串提取。
- **「取消排队」未实现**：协议无 cancel 命令（ui-spec §4.2 明确「没有则不给该按钮」）。
- **打开会话**：session_id/marker 为本地会话标识，按规格仅展示 + 「复制会话标识」，不臆造 URL。
- **`resume-auto` 无独立命令**：protocol §5.15.6 只把它列为 allowed_action，未定义命令。实现按 §5.15.2「restore-task 对 active 任务幂等」的语义走 `restore-task {task_id, resume_auto: true}`（置 `auto_advance_paused=0`）。
- **连接中断态为本地派生**：协议 `connection.state` 只有 ok/degraded/unavailable；读取失败（连接类错误）时 UI 派生「连接中断，重连中」，不清空已加载消息。
- **已排队排序键**：`TaskSummary.priority` 用作排队优先级代理（协议未在 summary 暴露请求级 priority）。
- **运行记录**：以 `list-events` 时间线为主，attempts 摘要体现在 claimed/reported 等事件中（无跨任务 attempts 查询命令）。
- **组头**：§4.1「项目可分组标题」为可选项，当前为平铺列表（行内项目徽标），保证全局排序不被分组打断。
