# KP-E02 控制轮确定性化（dispatch / monitor 不再经模型）

- 任务编号：kp-e02
- 分支：`feat/20261122-kp-e02-deterministic-control-rounds`
- worktree：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261122-feat-kp-e02-deterministic-control-rounds`
- 基点：main `c0d9774`；创建日期 2026-11-22（台北）
- 合入目标：main（PR）
- 状态：未开始（本文件为计划，待确认后实施）

## 一、问题

调度（派发 dispatch / 监控 monitor）今天仍然靠模型跑：引擎和 App 调度器拉起
`executor_runner --stage dispatch|monitor`，它渲染 prompt（`core/executor_prompts/dispatch.md`
69 行、`monitor.md` 151 行）后起一个模型回合——`pi -p`，或 Pi-Desktop-Plus 的控制会话
（`exec_config.controller_backend` 决定，见 `core/kanpilot/executor_runner.py:185-203`、
`core/kanpilot/pi_desktop_controller.py`）。

但**决策**早就是确定性的：`core/kanpilot/engine/decide.py` 是纯函数表
（“No DB, no I/O, no clock”），派发/重试/升级都在那里。模型在这两层里只做两件事：

1. 按 prompt 写死的命令序列调用 `kanpilot`（dispatch 全流程、monitor 的状态读取与分支）；
2. monitor 里有一段**真正的语义判断**：`monitor.md:100-105` 要求把 Plus Goal 报告当不可信
   数据，逐条比对计划验收、契约、criteria/steps/checks/evidences，产出
   `decision ∈ {complete,incomplete,uncertain}` 与 `criterion_reviews`。

代价：每轮一次模型调用（dispatch 与 monitor 各一趟）、两个额外失败点（CLI 起不来、
Plus 控制会话建不起来）、以及 `controller_backend` 这个配置项和「pi 还在不在支持范围」的反复纠缠。

## 二、目标

1. **dispatch 完全确定性**：不经任何模型，直接调用与 prompt 等价的核心命令。
2. **monitor 的机械部分确定性**：状态读取、分支、心跳、续租、归档全部代码化。
3. **语义判断只保留一处，且不放在调度循环里**：Plus Goal 报告的完成判定改为一个
   独立的、有结构化输出的判定步骤，走评审侧的后端与模型配置（`review_config`），
   不再借用「监控运行端」。
4. **删掉 `controller_backend`**：settings 键、`exec-capabilities.controller`、UI 的
   「派发与监控运行端」整行、§5.24.1/§5.24.2/§5.24.4 相关口径。
5. 执行端不变（Pi-Desktop-Plus），评审端不变（Codex CLI / Pi-Desktop-Plus）。

## 三、现状拆解：哪些是机械，哪些要判断

| 步骤 | 出处 | 性质 |
|---|---|---|
| `claim` → `dispatch-prepare` → `dispatch-begin-send` → `dispatch-start` → 轮询 session → `dispatch-confirm` / `dispatch-fail` | `dispatch.md:18-60` | 全机械：固定顺序 + 固定判据 |
| 心跳 ≤10 分钟、`stale_run/not_found` 收尾 | `dispatch.md:61-64` | 全机械（已是核心命令语义） |
| monitor：`claim` → `recheck` → `advance-task`（被动同步） → `report` | `monitor.md:24-49` | 全机械 |
| monitor：观察进程续接（`round-observe`）、`observer_alive` 去重、`turn_adopted` | `monitor.md:39-56` | 全机械（核心已有去重） |
| monitor：pi_desktop 终态 + 报告状态门（`get-goal-report` 的 state/integrity 判定、60 秒发布窗口） | `monitor.md:87-99`、`123-136` | 机械（状态机 + 白名单） |
| monitor：**报告语义判定**（criterion_reviews、complete/incomplete/uncertain） | `monitor.md:100-105` | **需要模型** |
| monitor：pi 后端三证据（terminal + exit_code + 验证命令语义） | `monitor.md:67-86` | 半机械；pi 执行已下线，本任务一并删除该分支 |
| monitor：旧后端 dsh/headless → `observation_blocked` | `monitor.md:137-140` | 全机械（历史兼容） |
| delivery | `executor_runner.py` `STAGES` | 无 prompt 渲染，已是确定性命中 |

结论：只有「Goal 报告语义判定」必须模型，其余全部可以代码化。

## 四、目标架构

```
engine/decide.py（确定性决策，已存在）
   └─ spawn step
        ├─ dispatch  → core/kanpilot/control/dispatch_worker.py   （新，纯 Python，无模型）
        ├─ monitor   → core/kanpilot/control/monitor_worker.py    （新，纯 Python，无模型）
        ├─ delivery  → 现状不变
        └─ verdict   → core/kanpilot/goal_eval.py（新，唯一带模型的判定步骤）
                        后端/模型沿用 review_config（codex|pi_desktop），
                        输入：计划验收 + Goal 契约 + 报告 + 证据；
                        输出：JSON schema {decision, criterion_reviews, reason}
                        结果仍经 record-goal-evaluation 落库，之后才允许 advance-task
```

要点：

- monitor 发现 pi_desktop 轮次「执行终态 completed + 报告 ready/structured」时，
  **不再自己下判断**，而是入队一个 `verdict` 步骤；verdict 完成才推 `in-review`。
  这样 monitor 不再依赖任何模型，`controller_backend` 失去意义。
- verdict 的后端沿用 `review_config.backend`（用户已定：评审端 Codex CLI / Plus），
  失败与超时按既有 review 失败阶梯处理（不进调度循环的重试）。
- 执行端保持 `exec_config.backend=pi_desktop`；`pi` 执行随本次一并标记为「不再支持」
  （核心仍能读旧快照，但新轮次不可选——与 kp-mag03 的 UI 口径一致）。

## 五、关键决定（D）

- **D1** dispatch 去掉模型与 prompt，改为 `control/dispatch_worker.py`；命令顺序与
  `dispatch.md` 一一对应，异常分支原样保留（`evidence_invalid`、`remote_conflict`、
  `deduplicated`、`starting` 轮询 ≤5min、`send_state` 判据）。
- **D2** monitor 机械部分代码化；语义判定拆成 `verdict` 步骤（新增 stage 名，写入
  `engine/runners` 与 CLI，但不进 `STAGES` 的 prompt 渲染）。
- **D3** `verdict` 的模型配置**新建** `settings.verdict_config`（默认继承 `review_config`），
  不直接复用 review 的会话语义——避免「评审跑两次」的歧义；若认为共用更简单，可改为
  直接复用 `review_config`（**待定，见第九节**）。
- **D4** 删除 `controller_backend`：`exec_config.save/load` 不再接受该字段（旧值读取后忽略，
  零迁移），`exec-capabilities.controller` 去掉，Swift `caps.controller` 与
  「派发与监控运行端」行一并删除。
- **D5** 删除 `pi_desktop_controller.py`（1115 行）与 `executor_prompts/{dispatch,dispatch_mcp,monitor,monitor_mcp}.md`；
  `executor_runner` 只保留 review/delivery 两个 stage，或整体被 `control/*` 取代。
- **D6** `pi` 执行分支在 monitor 里删除（与 kp-mag03 一致），历史轮次仍可读、可 `observation_blocked`。

## 六、实施步骤

- **T1 确定性 dispatch worker**：新增 `core/kanpilot/control/dispatch_worker.py`，把
  `dispatch.md` 的 1–8 步逐条实现；`executor_runner --stage dispatch` 改为直接调用它
  （先并行保留旧模型路径，用开关切换）。验收：`test_auto_dispatch`、`test_dispatch_*`
  全绿；新增「不调用模型」断言（mock 模型入口必须零调用）。
- **T2 确定性 monitor worker**：同上，产出 `control/monitor_worker.py`；把
  `monitor.md` 中除报告语义判定的全部规则代码化，含 pi_desktop 的终态门、报告状态门、
  续接去重、pi/旧后端分支处理。验收：`test_auto_monitor`、`test_monitor_interval` 等。
- **T3 verdict 步骤**：新增 `core/kanpilot/goal_eval.py`（复用 `cli_review` 的结构化输出
  机制与后端适配），monitor 在需要判定时入队；`record-goal-evaluation` 的
  `monitor_request_id/run_id` 语义改为由 verdict 步骤持有（**需同步 §5.23.10 口径**）。
- **T4 删除 controller 面**：`exec_config`（controller_backend/snapshot/capabilities/check）、
  `cmd_config` 的 `exec-capabilities.controller`、`executor_runner.controller_backend`、
  `pi_desktop_controller.py`、Swift 的 `caps.controller` 与 UI 行、mobile 契约无需改（无依赖）。
- **T5 影子比对**：新 worker 与旧模型轮在隔离 DB 上回放同一批历史请求（fixtures），
  对比最终 `runtime_operations/requests` 结果一致性；不一致的样本逐个人工确认后再切默认。
- **T6 文档与清理**：protocol §5.18/§5.24 口径收敛（控制轮改为确定性、判定独立）、
  删除 prompt 文件、更新 `docs/automation-contract.md` 中「调度用模型」的描述。

## 七、兼容与迁移

- 旧 `settings.exec_config.controller_backend`：读取后忽略，不迁移、不报错；下一次保存时
  自然消失。
- 旧 App + 新核心、或新 App + 旧核心的混用：App 内嵌核心，按整包发布，不承诺跨版本混用；
  但 `exec-capabilities` 删除 `controller` 字段时，Swift 侧必须同一版本改完（同 PR）。
- 历史 `executor_runs.dispatch/monitor` 记录保留，语义由「模型回合」变为「确定性 worker」；
  UI 文案改为「控制轮（确定性）」。
- Plus 轮次的 Goal 判定从「monitor 回合内」挪到「verdict 步骤」后，历史上已判定的任务不动；
  未判定的按新路径走。

## 八、验收场景

1. dispatch 一个请求：命令序列与 `dispatch.md` 一致；无模型进程（`ps` 无 `pi -p`/Plus 控制会话）；
   `deduplicated`、`starting` 轮询、`dispatch-fail` 的 `send_state` 判据与旧实现一致。
2. monitor 一个 pi_desktop 轮次：终态 completed + 报告 ready → 入队 verdict → verdict
   产出 complete → `record-goal-evaluation` → `advance-task in-review`；全过程 monitor
   侧零模型调用。
3. 报告缺失/兜底/损坏/uncertain：分别落到 needs_user，`failure_code` 与旧实现一致
   （`execution_failed` / `report_unreadable`）。
4. pi/旧后端历史轮次：走 `observation_blocked`，不重发、不判完成。
5. `exec-capabilities` 不再有 `controller`；设置页无「派发与监控运行端」；旧值 DB 打开不报错。
6. 隔离测试：`cd core && python3 -m pytest tests -q` 全绿；新增 worker 的单测覆盖
   `dispatch.md`/`monitor.md` 的每一条分支（模仿 `test_engine_decide` 的「每行一条」）。

## 九、未决问题（需要拍板）

1. **verdict 的模型配置**：新建 `settings.verdict_config`（默认继承 review）还是直接复用
   `review_config`？我倾向新建独立键，避免「评审」与「完成判定」语义混在一起。
2. **判定失败的重试**：verdict 调不通（模型/网络）时，是走 review 的失败阶梯（重试→
   needs_user），还是让监控轮下一轮重新入队？我倾向后者（判定是幂等只读）。
3. **是否保留 pi 执行的核心解析**：本次只删 monitor 的 pi 分支，`pi` 执行链路是否一并彻底
   下线（含 `EXEC_CONFIG_DEFAULTS.backend="pi"`）需要单独确认，避免又一次「UI 收了、核心还在」。

## 十、风险与回滚

- 最大风险是 **monitor 的边界条件**（`monitor.md` 151 行、分支很多）。缓解：T5 影子比对 +
  「每行一条」单测 + 先 shadow 模式跑一段时间再切默认。
- 回滚：保留 `EXECUTOR_CONTROL_MODE=legacy|deterministic` 开关一个版本；出问题切回旧模型轮，
  配置与代码都不删（删除放在 T6，确认稳定后单独提交）。
- 判定步骤迁移若导致 `record-goal-evaluation` 的身份字段错位，核心会 `conflict` 拒绝推进——
  这条守卫保留，不放松。

## 十一、影响面清单

- 新增：`core/kanpilot/control/dispatch_worker.py`、`control/monitor_worker.py`、`goal_eval.py`
- 删除：`core/kanpilot/pi_desktop_controller.py`、`core/executor_prompts/{dispatch,dispatch_mcp,monitor,monitor_mcp}.md`
- 修改：`executor_runner.py`、`engine/decide.py`（verdict 入口）、`engine/runners/__init__.py`、
  `exec_config.py`、`cmd_config.py`、`cli.py`、`macos/KaneoPilot/Views/SettingsView.swift`、
  `macos/KaneoPilot/Core/Models.swift`、`docs/protocol.md`（§5.18/§5.23/§5.24）、
  `docs/automation-contract.md`
- 测试：`test_executor_prompt_contract.py`（随 prompt 删除重写为 worker 契约）、`test_plus_controller.py`（删除）、
  `test_auto_dispatch.py`/`test_auto_monitor.py`/`test_monitor_interval.py`（回归）、新增 worker 单测
