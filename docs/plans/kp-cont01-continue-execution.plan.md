# KP-CONT01 继续执行同一 Pi 目标

- 任务标识：`kp-cont01`
- 状态：待体验（v0.5.6-test.203 已发布并安装）
- 分支：`feat/20261110-kp-cont01-continue-execution`
- worktree：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261110-feat-kp-cont01-continue-execution/`
- 基于：`fa678fc`（kp-ep01 测试版 v0.5.6-test.201 的代码与夹具）
- 合入目标：`main`
- 记录日期：2026-11-10（台北）

## 一、问题

Pi Desktop Plus 的 Goal 轮次收尾后，目标可能处于「受阻 / 待处理」（例如 9/13 待办、4 项证据失败）。当前 KaneoPilot：

- `round-observe` 对 `job.status=finished` 的轮次直接返回 `finished`，不能接管；
- `resolve resume` 只能新开一轮执行（新会话/新提示），不是同一目标；
- `retry_verification` 只补验证报告，不能补做失败的原生证据；
- 用户只能回到 Pi 本体，在输入框发送「继续执行目标」，控制台不会接管新 turn。

用户要求：任务页给一个「继续执行」按钮，真正续跑**同一 Plus 会话/目标**，不重新规划，并在控制台继续观察/核验。

## 二、设计

新只读+一次性写命令：

```
continue-execution {task_id, run_id, confirm: true, operation_id?}
```

前置（任一不满足即拒绝，不发送）：

1. 任务当前轮次等于 `run_id`，`status=needs_user`，`failure_code ∈ {verification_unconfirmed, execution_failed, execution_interrupted}`；
2. 轮次 `backend=pi_desktop`，`run_status ∈ {finished, failed}`；`job.json` 身份、计划哈希、配置快照与任务一致；
3. `send_state=sent`，有 `session` 事件，`session_id`/`turn_id` 齐备；
4. Plus 控制面可达，当前 turn 与 job 的 `turn_id` 一致且已持久终态；没有更新的 turn；
5. 没有 `queued`/`claimed` 请求，没有 `prepared`/`unknown` 的 continue 操作；
6. 配置权限为 `auto`；用户已确认（`confirm=true`）。

流程：

1. `runtime_operations.reserve_downstream` 抢占发送槽；已存在 → 幂等返回/冲突，不重发；
2. 用 execution scope 的 `RuntimeClient` 向**同一 session** 发送继续指令（新 turn，产生模型费用）：
   `继续执行当前目标…沿用原计划与冻结验证要求，不重新规划；补齐未完成项后重新提交结构化 Goal 报告。`
3. 拿到新 `turnId` 后：
   - 快照 old job 到 `continue_previous`；
   - job 改 `running`、`turn_id=new`、`send_state=sent`、`observation_resumed_at=now`、`turn_adopted` 追加 manual_continue；清 `ended_at/exit_code/detail/pid/observe_pid/observation_finalizing`；
   - 任务清阻塞、转 `running`，写 `execution_continuation` 与事件；
   - 调用 `round-observe {executor:"user"}` 拉起 worker `--resume` 观察新 turn；
   - 旧 terminal 由 `observation_resumed_at` 标记 superseded，监控/verdict 继续。
4. 发送响应丢失：记 `unknown`，绝不重发；任务保留现场并提示人工核对。

限制：同一终态 turn 只允许一次继续；不自动循环；失败仍回 `needs_user`；不改变计划/工作树/交付策略。

## 三、界面

- `get-task.task.continue_execution = {eligible, reason, run_id, turn_id}`（只读投影）。
- 执行页复盘/最新进展下方增加「继续执行」卡片/按钮，仅 eligible 时显示：
  - 文案说明「在同一 Plus 会话继续当前目标，不重新规划；会调用当前执行模型并产生费用」；
  - 二次确认；提交中禁用；成功后刷新并 toast；失败展示核心原文。
- 原有「重新执行（沿用计划）」「补验证」保留，语义区分。

## 四、验收

| 编号 | 场景 | 期望 |
| --- | --- | --- |
| C1 | 阻塞的 pi_desktop 轮次 | 显示继续执行；确认后同一 session 新 turn，job 重开，观察者接管 |
| C2 | 已继续一次 | 再次点击被拒绝/幂等，不重复发送 |
| C3 | 无终态/运行中/有活动请求 | 拒绝并说明原因，不发送 |
| C4 | 发送响应丢失 | 记 unknown，不重发，任务保留现场 |
| C5 | 新 turn 完成 | 观察者收尾，监控/verdict 继续，旧 terminal 标记 superseded |
| C6 | 强制关闭 Pi 后 | 继续执行前置检查给出明确原因，不影响既有证据 |

## 五、验证记录

| 时间 | 动作 | 结果 | 状态 |
| --- | --- | --- | --- |
| 2026-11-11 00:05 | 设计定稿 | 用户选择「发送 + 自动接续」：不重开 job；发送后轮询 `check-execution-status`，新 turn 结束再复用现有 `continue-execution` 接续/排队核验 | 完成 |
| 2026-11-11 00:05 | 核心发送命令 | `request-continuation`：校验/发送槽/同一 session 发送/记录 `execution_continuation_request`；不改 job/任务状态 | 实现中（待测试） |
| 2026-11-11 00:05 | 接续扩展 | `check-execution-status` 对 finished + 已登记新 turn 产出 continuation；`execution-continuation` 接受 verification_unconfirmed + 已发送请求 | 实现中（既有 42 项续接/状态测试通过） |
| 2026-11-11 00:20 | 发送 + 同轮重开 | `request-continuation` 发送后快照 job、绑新 turn、清旧终态、任务清阻塞转 running、`round-observe` 拉起 worker；worker 轮次预算改用 `continuation_started_at` | 实现中 |
| 2026-11-11 00:20 | 桌面入口 | 执行页新增「继续执行同一 Goal」条与二次确认；`get-task.continue_execution` 投影；FakeCore 与解码夹具 | 未编译 |
| 2026-11-11 00:24 | 核心测试 | `test_request_continuation` 4 项通过；续接/状态/桌面队列 101 项 + 53 subtests 通过 | 通过 |
| 2026-11-11 00:26 | 推送构建 202 | 分支 `c21d110` push run 42854506499：identity/checks 1–5/android/macos-fixtures a+b/macos-package/codex-platforms 全绿 | 通过 |
| 2026-11-11 00:57 | 测试版 203 | 手动 `channel=test`、`base_version=0.5.6` run 42857275049 全绿，发布 v0.5.6-test.203（source c21d110） | 通过 |
| 2026-11-11 01:00 | 安装 203 | DMG sha256 与 SHA256SUMS/manifest 一致；`CFBundleShortVersionString=0.5.6-test.203`、build 203、codesign 通过；旧 201 备份 `/Applications/.KaneoPilot-backup-cont01-20261111T010001`；启动后 MCP/mobile/engine 与新运行观察者恢复 | 通过 |
| 2026-11-11 01:02 | 实机投影核对 | 安装包核心 `get-task` 返回 `continue_execution={eligible:false, state:"none", reason:"任务不在需人工处理状态"}`；当前任务已被用户重新执行到新轮次 `cc6167fa`，running，因此入口按预期隐藏 | 通过（无阻塞轮次可点） |
| 2026-11-11 01:02 | 未验证 | 阻塞任务的 eligible 按钮视觉与真实发送/同轮接管端到端；需等下一次 Pi 轮次阻塞后由用户点击 | 待体验 |
