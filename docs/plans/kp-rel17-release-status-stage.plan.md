# KP-REL17 发布投影不再顶替交付前的任务状态

- 日期：2026-11-26（台北）
- 分支：`fix/20261126-kp-rel17-release-status-stage`
- Worktree：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261126-fix-kp-rel17-release-status-stage`
- 合入目标：`main`（测试版先发，合入另行确认）

## 现象

2026-11-26 11:13 用户截图：MediaPlayer 两个新任务卡片的状态行都显示「等待关联修复」，第二行写「等待关联修复 · 等待原始任务与全部修复具备合并和验证证据」。用户判定状态不对，应显示规划中；同时指出「等待关联修复」在这个位置难以理解。

## 根因（只读实测）

- 安装版核心 + 任务库只读副本：`woxb5p5ne8f0e1656fb0y9jq`（status=planning）、`z8glblxzjh5y515vmkn2i9zp`（status=needs_plan）的 `display_status` 本是「规划中」「待规划」，但 `release` 投影是 `waiting_group`/「等待关联修复」，`required=true`。
- `release_delivery.projection()` 在项目开启发布后对任何未完成任务都给 DTO：既无批次也无关联修复时仍落到「组成员」分支，`locally_ready(root)` 因为任务还没进评审而为 False，于是得出 `waiting_group`。
- `derive.task_summary` 把这份投影原样放进任务载荷；桌面 `showsTaskStatus`（`required && !is_historical`）与卡片第二行 `progressLine` 都据此覆盖任务自身状态，规划中/待规划被顶掉。
- 同库扫描（400 条未完成任务）：另有 3 张 planning/needs_plan、1 张规划补材料的 needs_user 任务同样被误标；2 张 in_review 任务的「等待关联修复」是合法等待。

## 决定

1. `release_delivery.review_settled(conn, task)`：评审给出结论（`review_conclusion` pass/changes_requested、已接受的风险、当前修复记录、已合并）才算发布问题成立。
2. `release_delivery.display_projection(conn, task)`：评审未结束时返回 `None`，只供客户端显示使用。
3. `derive.task_summary` 的 `release` 字段与 `_release_display`（display_status/board_substatus）改用 `display_projection`；`projection` 与 `completion_gate` 保持原样——项目开启发布后，未完成任务没有当前身份的 published 证据仍不能进入 done。
4. 不改 Swift 与 DTO 字段：载荷无 `release` 时，卡片徽标、第二行原因、详情头部和「构建与发布」区自然回到任务自身状态。
5. `docs/release-contract.md` T1 增加显示口径条款。

## 验收

- 快照实测（改后核心，安装版库只读副本）：两张卡 `display_status` 分别为「规划中」「待规划」、`release=null`；同库 2 张 in_review 任务仍返回「等待关联修复」，合法等待未被隐藏。
- 新增 `core/tests/test_release_delivery.py::test_release_stays_out_of_task_status_until_review_settles`：planning/needs_plan/in_review 三态下 `projection` 仍为 waiting_group、`completion_gate` 仍拦截，而 `display_projection` 与 `task_summary['release']` 为 None、`display_status` 为 规划中/待规划/评审中。
- 定向回归：`test_release_delivery`、`test_board`、`test_review_delivery`、`test_release_verification_projection`、`test_delivery_completion_projection` 97 项通过。
- 完整 `core/tests` 套件结果见下（跑完补记）。

## 未验证与遗留

- 桌面实机体验需安装云端测试版后确认（本机不编译、不覆盖正式 App）。
- 合法等待场景仍沿用「等待关联修复」文案；若要继续改这个措辞，另行讨论。
- 尚未合入 `main`。