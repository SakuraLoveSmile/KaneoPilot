# KP-GB02：目标受阻卡片的适用性、只读失败提示与优先级修复

- 项目 / 任务编号：personal-kaneopilot / KP-GB02
- 状态：实现中（本地核心隔离验证通过；Swift 编译与原生界面待云端验证）
- 创建日期（Asia/Taipei）：2026-11-12
- 权威记录：本文件；外部材料只链接它
- 主仓库 / 当前工作目录：`/Users/sakurasep/Documents/Code/Project/Personal Project/KaneoPilot`
- 基线 / 分支 / 合入目标：基线 `859bb96`（cont02 分支 tip，含 gb01 `2352d43` 的受阻目标能力与 cont02 全部修复）；分支 `fix/20261112-kp-gb02-goal-card-fallback`；合入目标 `main`，须与 cont02/gb01 同序合入，不单独改变它们的验收结论。
- 工作树：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261112-fix-kp-gb02-goal-card-fallback`
- 运行资源：仅本地隔离测试（临时 DB/目录、假控制面）；不访问生产 DB、不调用真实模型、不操作真实任务或设备。

## 目标与范围

用户 2026-11-12 反馈：Music 任务 `wjhpqxfqxr3084vxtc19e1uo` 的执行轮次失败后，任务详情顶部出现「目标受阻，需要你决定」卡片，内容恒为 `本轮没有已批准的 Goal 契约…goal_approval_missing`，刷新只会得到同一确定性失败；卡片把真正的失败横幅、「核验执行状态」入口、继续执行条和底栏主操作全部替换掉，于是既看不到会话真实状态、也点不到可用的恢复操作。

本次只修复这一处展示与判定（KP-GB01 引入），用户要求按三点改：资格判断、`unavailable` 冒充受阻、卡片优先级。

覆盖：

1. 核心 `get-goal-blocker` 把「本轮从未建立已批准 Goal 契约」和「非 Pi Desktop 轮次」判为**不适用**（`state=none` + 明确 detail），不再报 `unavailable`；真实读取/身份核对失败仍报 `unavailable`。
2. Swift 目标卡片只在 `blocked`（含旧版 Pi 只读展示）、`accepted`、待接续（`reconcile`）时出现。
3. 读取失败/不可用改为紧凑提示，不替换失败横幅、核验执行状态入口、继续执行条与底栏主操作；不适用时完全不出现目标卡。

明确排除：不改 Goal 续跑/接受协议与门禁，不按 `completion_policy` 过滤（见下），不改其他页面、布局 token、评审/交付逻辑；不处理 Music 任务本身的补验与评审，不替用户作目标决定。

## 已确认决定

- **资格以「已批准 Goal 契约」为准，不以 `completion_policy` 为准。** 现场证据：旧轮 `446ab281-def2-4b21-9157-2140e1bfd855` 的 `job.json` 同时为 `completion_policy=command_verification` 且 `goal_approval` 已批准、Pi 存在真实 `blocked` hold，gb01 正是为其工作；若按 completion_policy 过滤会误删真实受阻目标能力。失败轮 `62d4390e…` 的差异是 `goal_approval` 缺失（没有已批准的 proposal），而不是命令验证策略本身。
- 契约缺失不是读取失败：§5.23.12 只对「缺少可核验 hold、断线、无身份或读取失败」要求 `unavailable`；从未建立已批准契约的轮次没有 hold 可核对，判 `none` 并说明不适用，任务失败仍由既有失败横幅、核验与恢复入口呈现。
- 卡片优先级：`blocked`（真实受阻）、`accepted`（用户已接受未验证）、`reconcile`（已发生决定待接续）才占用「需要你处理」首位；只读失败提示为次级紧凑行，与横幅、入口并存；`none`/未载入不占位。
- 不改协议既有语义；§5.23.12 与 ui-spec KP-GB01 补充分类与优先级说明，供后续实现与评审引用。

## 实现与验收

| 步骤 | 用户效果 / 实现范围 | 验收情形与预期 | 当前结果及证据 | 状态 |
| --- | --- | --- | --- | --- |
| T1 | 核心 `goal_blocker.py`：`get-goal-blocker` 新增契约适用性判定；无已批准契约/非 Pi Desktop → `none` + detail；真实读取失败保持 `unavailable` | 隔离测试：无 `goal_approval`、契约留痕残缺、codex 轮次均返回 `none` 且不接触 Pi；job/目录不可读仍 `unavailable` | `tests/test_goal_blocker.py` 新增 4 项通过（见下）；失败轮真实只读复现由 `unavailable` 变为 `none` | 实现中（待云端全量） |
| T2 | Swift `GoalBlockerStore`：`showsCard` 仅 blocked/accepted/reconcile；新增 `showsUnavailableNotice`；`GoalBlockerCard` 标题/空动作文案诚实；新增紧凑提示视图 | 夹具：unavailable 不显示卡片只显示提示；none 两者都不显示；blocked/accepted 行为保持 | 已实现；`macos/tests/goal_blocker_main.swift` 增加 3 组断言，待云端运行 | 实现中（待云端编译） |
| T3 | 任务详情概览与执行页接入提示且不改变优先级：横幅、核验入口、继续执行条、底栏主操作仍可用 | 桌面组合：失败轮无 `goal_approval` 时看到失败横幅与核验入口，卡片不出现 | 已接入；原生界面检查待云端构建后体验 | 未开始 |
| T4 | 规格：protocol §5.23.12 补「不适用」分类；ui-spec KP-GB01 补资格与优先级，不新增产品规则 | 文档与实现一致，无冲突 | 已更新 | 完成 |

## 执行与验证记录

| 日期 | 代码版本 / 候选差异 | 环境与运行对象 | 命令或实际操作 | 结果 / 证据位置 |
| --- | --- | --- | --- | --- |
| 2026-11-12 | 基线 `859bb96` | 本地隔离 pytest（`/tmp/kp-gb01-pytest`，Python 3.14.7 / pytest 9.2.0） | `cd core && /tmp/kp-gb01-pytest/bin/python -I -m pytest tests/test_goal_blocker.py -q` | 18 passed（基线，改动前） |
| 2026-11-12 | 生产 DB 只读副本 | 本机 `~/Library/Application Support/KaneoPilot/kanpilot.db` | `get-goal-blocker`（失败轮 `62d4390e…`） | 复现 `unavailable / goal_approval_missing`；`446ab281` 的 job 为 `command_verification` + 已批准契约（支持上述决定） |
| 2026-11-12 | 本次改动（工作树未提交） | 本地隔离 pytest（Python 3.14.7 / pytest 9.2.0） | `cd core && /tmp/kp-gb01-pytest/bin/python -I -m pytest tests/test_goal_blocker.py tests/test_goal_report_contract.py tests/test_goal_continuation.py -q` | 123 passed、42 subtests（含新增 4 项）；`tests/test_goal_accepted_delivery.py` 9 passed；根仓 `tests/test_goal_report_root_contract.py` 9 passed |
| 2026-11-12 | 本次改动 | 生产 DB 只读副本（16:57 快照） | `KANPILOT_DB=<副本> get-goal-blocker`（失败轮 `62d4390e…`） | 返回 `state=none`、detail「本轮还没有已批准的 Goal 契约，不适用目标受阻处理…」，不再返回 `unavailable`；未改真实 DB/Pi |
| 2026-11-12 | 本次改动 | 静态检查 | `python3 -m py_compile`、`git diff --check`、Swift 文件括号平衡检查 | 通过；Swift 编译与夹具未在本地运行（仓库约定仅云端） |

## 提交与集成

| 提交 | 行为变化与原因 | 验证 | 合并 / 发布情况 |
| --- | --- | --- | --- |
| `d84b12e` | 目标卡适用性、只读失败提示与优先级（核心 + 界面 + 规格），行为前后差异见上 | 本地隔离 pytest 123 passed/42 subtests、交付 9 passed、根仓合同 9 passed；Swift 编译与 goal blocker 夹具待云端 | 未合并、未发布 |

## 交付与恢复

- 体验入口：安装包含本改动的测试版后打开失败轮任务详情，确认无「目标受阻」卡片、失败横幅与「核验执行状态」可见；真实受阻目标仍显示卡片。
- 尚未完成 / 尚未验证：Swift 编译、goal blocker 夹具与原生界面检查（仅云端）；真实受阻目标轮次的回归需在有已批准契约的轮次上验证，不能伪造。
- 用户确认（明确确认后才改为已验收）：未确认。
- 下一步和恢复任务时首先核对的内容：本文件「执行与验证记录」、分支 HEAD、未提交差异；确认没有放宽 gb01 的续跑/接受门禁。
- 本任务 worktree 收尾：尚不满足清理条件（未合入、未发布、未验收）。
