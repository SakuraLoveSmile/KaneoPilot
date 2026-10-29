# KaneoPilot macOS 桌面端

原生 SwiftUI 工作台「Kaneo 调度台」。数据全部经核心 CLI（`kanpilot`，JSON 进出）获取与变更，不直接读写 SQLite。

## 云端构建

使用 [Packages Actions](https://github.com/SakuraLoveSmile/KaneoPilot/actions/workflows/packages.yml) 构建和下载 Universal DMG，本机不再使用 Xcode 或编译型预览脚本生成 App。分支构建提供测试包，正式版本从 [Releases](https://github.com/SakuraLoveSmile/KaneoPilot/releases) 下载；完整操作见 [构建与 Release 规范](../docs/build-release.md)。

云端构建脚本将产物和 DerivedData 放入 runner 临时目录。App / DMG 为 ad-hoc 签名、未公证，不包含 Python 运行时。

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
  open /path/to/downloaded/KaneoPilot.app

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
    RootView.swift         工作台/记录/配置侧栏 + 工具栏 + 横幅/toast
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
- **排队规划可手动终止**：任务/请求详情经 `cancel-request` 撤销 queued plan；已领取则拒绝，保留材料和历史，等待手动重新规划。派发排队不在此操作范围。
- **无记录残留会话可强制删除**：核心允许时，会话菜单提供 `force-purge-session`；仅永久移出列表，保留历史和文件。活跃请求、已有会话及无法核验的记录受保护。
- **打开会话**：session_id/marker 为本地会话标识，按规格仅展示 + 「复制会话标识」，不臆造 URL。
- **`resume-auto` 无独立命令**：protocol §5.15.6 只把它列为 allowed_action，未定义命令。实现按 §5.15.2「restore-task 对 active 任务幂等」的语义走 `restore-task {task_id, resume_auto: true}`（置 `auto_advance_paused=0`）。
- **连接中断态为本地派生**：协议 `connection.state` 只有 ok/degraded/unavailable；读取失败（连接类错误）时 UI 派生「连接中断，重连中」，不清空已加载消息。
- **已排队排序键**：`TaskSummary.priority` 用作排队优先级代理（协议未在 summary 暴露请求级 priority）。
- **运行记录**：以 `list-events` 时间线为主，attempts 摘要体现在 claimed/reported 等事件中（无跨任务 attempts 查询命令）。
- **组头**：§4.1「项目可分组标题」为可选项，当前为平铺列表（行内项目徽标），保证全局排序不被分组打断。

## 自动化工作台预览（2026-10-09）

默认首页为自动化总览；需要用户决定的事项优先展示，任务默认用列表阅读，保留看板切换。详情突出一个当前主操作，其余放入“更多操作”。确认执行先进入核验计划页；手动规划为选择任务、核对材料并开始两步。

以下保留历史隔离预览的设计说明。`scripts/preview_automation_ui.sh` 包含编译步骤，仅允许云端 runner 执行；本地使用已下载的 Actions 产物进行体验，不重新编译。

预览使用独立 bundle ID 和 build/ui-simplification/fixture 存储；缺少隔离环境时核心不可用，不回落到正式核心。计划正文为静态演练响应，不代表真实计划文件已核验。

以下 Swift 夹具检查在云端执行：

```bash
bash macos/tests/run-fixtures.sh
bash macos/tests/run-planning-input-fixtures.sh
```

## 引擎总览（Stage B）隔离体验

默认首页按「需要你 → 流水线计数 → 正在推进 → 项目自动化与环境并排」显示。总览、菜单栏和通知读取同一个 `attention-list` 快照；任务仍通过五段详情进入计划、执行会话、评审报告和记录。设置为连接、项目、规划与评审、高级四页；项目引擎设置分别经固定核心命令保存并带映射版本校验。

`EngineSupervisor` 只拉起 `engine-run`，进程默认 `legacy`，由项目配置决定是否自动推进。关闭窗口保留后台引擎；退出只停止本实例，不结束 Plus 会话。手动规划保留核心显式授权队列。`ExecutorScheduler.swift` 与其历史夹具保留作旧路径参考，不再编入 App 的调度所有权。通知许可只能由「高级 → 允许需要你通知」显式请求；FakeCore 不请求通知、不启动真实 MCP 或真实服务。

`macos/tests/launch-engine-ui-check.sh` 只消费已准备好的隔离候选 App，不负责编译。需要特殊 bundle ID 的候选必须先在云端生成；默认 Packages 安装包不能冒充该隔离候选。

启动脚本要求独立 bundle id，创建临时 HOME、FakeCore 状态和全部数据根。可将最后参数改为 `900x600` 验证最小窗口，关闭窗口后从菜单栏重开，再用应用退出完成引擎收尾。不要以此假数据体验代替真实项目验收。

走查清单：默认总览区块顺序、需要你数量与菜单栏一致；搜索与看板列筛选、↑↓/回车打开五段详情；进行中阶段仅突出核心提供值；设置四页可达、项目模式/验证环境/备用模型保存与回读；拒绝凭据环境键且保留草稿；关闭窗口后菜单栏仍在；退出不结束其他引擎或 Plus。

云端验证入口：`bash macos/tests/run-engine-fixtures.sh`（健康启动、外部实例、失败退避、同源收件箱、默认模式、CAS 与凭据环境拒绝）以及既有 `run-fixtures.sh`、规划材料、回答、评审决议、验证回执与会话夹具。
