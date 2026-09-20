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

### 用 FakeCore 演示（开发期数据源）

```bash
# 方式一：环境变量
KANPILOT_CORE=$PWD/macos/FakeCore/kanpilot-fake.py \
  open .../Build/Products/Debug/KaneoPilot.app

# 方式二：命令行直接体验协议
python3 macos/FakeCore/kanpilot-fake.py list-tasks
python3 macos/FakeCore/kanpilot-fake.py get-task --json '{"task_id":"task-comic-010"}'
```

`macos/FakeCore/kanpilot-fake.py` 覆盖全部 display_status 与 allowed_actions 分支：待规划/规划中/就绪/已排队/派发中/执行中/评审中/需人工（send_state=not_sent 与 unknown 两种，后者带证据块）/执行端失联（monitor 与 dispatch 两种）/已归档，以及 40+ 字长中文标题任务。动作（enqueue/set-paused/set-priority/resolve/recheck）写入 `$KANPILOT_FAKE_STATE`（默认 `$TMPDIR/kanpilot-fake-state.json`）做有限状态模拟，删除该文件即复位。

## 结构

```
KaneoPilot/
  KaneoPilotApp.swift      WindowGroup（默认 1180×760，最小 900×600）
  AppViewModel.swift       @MainActor view-model：读取/动作/横幅/toast
  Core/
    CoreLocator.swift      核心路径解析
    CoreClient.swift       Process 执行 + 超时/错误映射
    Models.swift           协议 Codable 模型 + 时间格式化
  Views/
    RootView.swift         四节侧栏 + 工具栏 + 横幅/toast
    TasksSectionView.swift 任务列表（排序/搜索/筛选/键盘）+ 详情列
    TaskDetailView.swift   详情六区块 + allowed_actions 操作区
    PendingView.swift      待处理（活动请求/需人工/最近失败）
    RunsView.swift         运行记录时间线
    SettingsView.swift     诊断/自动推进/参数/路径/迁移
    Components.swift       状态徽章、横幅、区块、映射表、NSWorkspace
FakeCore/
  kanpilot-fake.py         假核心（见上）
```

## 已知取舍 / 契约缺口

- **settings 值与路径无读取命令**：protocol v1 没有暴露 `lease_seconds` 等 settings 值、`settings.migration`、Plan 根/DB 路径的命令。实现：`overview` 响应可选解码 `settings` 对象（FakeCore 提供，真核心 v1 不返回则对应区块隐藏/显示「—」）；路径从 `doctor` 各项 `detail` 字符串提取。
- **「取消排队」未实现**：协议无 cancel 命令（ui-spec §4.2 明确「没有则不给该按钮」）。
- **打开会话**：session_id/marker 为本地会话标识，按规格仅展示 + 「复制会话标识」，不臆造 URL。
- **已排队排序键**：`TaskSummary.priority` 用作排队优先级代理（协议未在 summary 暴露请求级 priority）。
- **运行记录**：以 `list-events` 时间线为主，attempts 摘要体现在 claimed/reported 等事件中（无跨任务 attempts 查询命令）。
- **组头**：§4.1「项目可分组标题」为可选项，当前为平铺列表（行内项目徽标），保证全局排序不被分组打断。
