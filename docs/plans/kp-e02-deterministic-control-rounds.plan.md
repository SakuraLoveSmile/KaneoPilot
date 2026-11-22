# KP-E02 控制端配置退役与遗留清理（计划 v2，已修正前提）

- 任务编号：kp-e02
- 分支：`feat/20261122-kp-e02-deterministic-control-rounds`
- worktree：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261122-feat-kp-e02-deterministic-control-rounds`
- 基点：main `c0d9774`；创建日期 2026-11-22（台北）
- 状态：计划（v2 修正了 v1 的错误前提，见「一」）

## 一、修正：调度**已经**是确定性的

v1 计划说「dispatch/monitor 由模型跑」，这是把遗留 prompt 当成了现行链路。核实后：

- `core/kanpilot/executor_runner.py:175-179`：`dispatch` / `monitor` / `delivery` 三个 stage 直接委派给
  `auto_dispatch` / `auto_monitor` / `delivery_runner`，**根本不会走到** `render_prompt` 分支。
- `core/kanpilot/auto_dispatch.py`（202 行）、`auto_monitor.py`（290 行）里没有任何模型调用：
  没有 `subprocess`、没有 `render_prompt`、没有 `pi_desktop_controller`；全部经 `cli.run` 调核心命令。
- monitor 的完成判定也已确定性化：`auto_monitor.py:132` 调 `engine-verdict`（核心判定），
  不再由模型读报告写 `criterion_reviews`。
- `controller_backend` 只在一处还有作用：`executor_runner.run_stage` 里 review 的**兜底**选择
  （`review_config` 未保存 backend 时读它），dispatch/monitor 不读。

所以本任务不再是「把调度改确定性」，而是**把已经无主的遗留配置与死代码收掉**，避免
配置项、UI 行、prompt 文件继续误导（我自己就被误导过一次）。

## 二、遗留清单

| 对象 | 现状 | 处置 |
|---|---|---|
| `settings.exec_config.controller_backend` | 只有 review 兜底在用；dispatch/monitor 不读 | 退役：review 兜底改读 `review_config`（其默认已是 `codex`），键不再写入；旧值读取后忽略 |
| `exec-capabilities.controller` | 前端只为「派发与监控运行端」那一行服务 | 删除字段 + 删除 Swift 的 `caps.controller` 与那一行 UI |
| `exec_config.controller_snapshot / controller_capabilities / check_controller_available / controller_backend` | 死路径 | 删除；对应测试同步删除 |
| `core/executor_prompts/dispatch.md / dispatch_mcp.md / monitor.md / monitor_mcp.md` | 无调用方 | 删除 |
| `pi_desktop_controller` 的控制轮会话部分 | 未被调用 | 只删这部分；`invocation_dir / EventLog / write_json / read_transcript` 仍被 review、desktop_queue、release_verification 使用，保留 |
| `executor_runner.render_prompt` 的 dispatch/monitor 残留分支 | 不可达 | 收窄为 review-only，并加断言 |
| §5.24.1 / §5.24.2 / §5.24.4 的「控制端后端」口径 | 描述的是已失效的分流 | 收敛为「dispatch/monitor/delivery 为确定性流程；控制端配置已退役」 |

## 三、不做

- 不动 `auto_dispatch` / `auto_monitor` 的既有行为（它们已是目标形态）。
- 不动执行端（Pi-Desktop-Plus）与评审端（Codex CLI / Plus）。
- 不删除 `desktop_queue`、`codex_desktop` 相关子系统（那是 Desktop 领取，另一条线）。
- 不重命名 `pi_desktop_controller`（改名会牵动 6 个模块，收益不抵风险）。

## 四、实施步骤

- **T1 删除 controller 配置面**：`constants.CONTROLLER_BACKEND_KEY`、`exec_config` 的
  controller_* 四函数与 `_controller_backend`、`cmd_config` 保存/快照里的该键、
  `exec-capabilities.controller` 字段；review 的兜底改为 `review_config.load` 的默认值。
  验收：`test_role_config`、`test_plus_controller`（删）、`test_exec_*` 回归；旧 DB 打开不报错。
- **T2 删除死提示词与不可达分支**：删 4 个 md；`executor_runner` 的 prompt 路径仅保留 review，
  并对 dispatch/monitor 走到该分支的情况直接报错（防御）。
  验收：`test_executor_prompt_contract` 重写为「只存在 review prompt」。
- **T3 前端收尾**：Swift 删 `caps.controller`、删除「派发与监控运行端」行与其状态；
  保留脚注「派发、监控与交付由核心命令执行，不调用控制端模型」。
  验收：云端 `macos-package` 编译 + 实机面板无该行。
- **T4 文档收敛**：`docs/protocol.md` §5.24.1/§5.24.2/§5.24.4 改写；`docs/automation-contract.md`
  去掉「控制端后端」表述；`AGENTS.md` 的「dispatch/monitor/delivery 为确定性流程」保持并补一句
  「控制端配置已退役（kp-e02）」。

## 五、兼容

- 旧 `exec_config.controller_backend`：读取后忽略，不迁移、不报错；下次保存自然消失。
- `exec-capabilities` 少一个字段：App 与核心整包发布，Swift 侧同 PR 改完。
- 旧轮次与历史 `executor_runs`：不受影响（stage 记录语义不变）。

## 六、验收场景

1. 设置页没有「派发与监控运行端」；`exec-capabilities` 返回里没有 `controller`。
2. 带旧 `controller_backend` 的 DB：打开、读配置、保存都不报错；保存后该键消失。
3. dispatch / monitor 各跑一轮（隔离 DB）：行为与改前一致，日志里没有任何模型/prompt 痕迹。
4. review 未保存 backend 时，兜底仍得到 `codex`（不再读 controller 键）。
5. `cd core && python3 -m pytest tests -q` 全绿；`scripts/tests` 打包检查不变。

## 七、风险

- 风险集中在「删除共享函数时漏改调用方」：`pi_desktop_controller` 的删除范围必须先
  `grep` 全仓确认（review_settle、desktop_queue、release_verification、cmd_exec 都在用工具函数）。
- review 兜底改源属于行为微调：若某库只有 `controller_backend=pi` 而 review 配置未保存，
  改后兜底从 `pi` 变 `codex`——与该用户已定的「评审端 = Codex/Plus」一致，但要在计划里写明。

## 八、影响面

- 删除：`core/executor_prompts/{dispatch,dispatch_mcp,monitor,monitor_mcp}.md`
- 修改：`executor_runner.py`、`exec_config.py`、`constants.py`、`cmd_config.py`、`cli.py`(字段)、
  `macos/KaneoPilot/Core/Models.swift`、`macos/KaneoPilot/Views/SettingsView.swift`、
  `docs/protocol.md`、`docs/automation-contract.md`、`AGENTS.md`
- 测试：删 `test_plus_controller.py`（如需保留工具函数用法，改写成 `test_pi_desktop_controller_utils.py`）、
  改写 `test_executor_prompt_contract.py`、回归 `test_role_config` / `test_auto_dispatch` / `test_auto_monitor`
