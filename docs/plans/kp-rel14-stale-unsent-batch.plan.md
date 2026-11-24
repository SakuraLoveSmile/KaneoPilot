# KP-REL14：已不可能发送的未发送批次不再显示可启动

状态：实现中

- 项目 / 任务编号：personal-kaneopilot / kp-rel14
- 创建日期（Asia/Taipei）：2026-11-24
- 权威记录：本文件；外部材料只链接它
- 主仓库：`/Users/sakurasep/Documents/Code/Project/Personal Project/KaneoPilot`
- 工作树：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261124-fix-kp-rel14-stale-unsent-batch`
- 基线 / 分支 / 合入目标：`main@28ba4e0` / `fix/20261124-kp-rel14-stale-unsent-batch` / `main`
- 运行资源：无新增端口或进程；核对运行库 `~/Library/Application Support/KaneoPilot/kanpilot.db` 一律 `mode=ro`。

## 目标与范围

用户 2026-11-24 15:0x 反馈：点「继续构建发布…」转圈后「没有然后了」。只读核对确认核心确实跑了发送前核对并被拒（`release_context_changed`，回执 15:06:18），原因是 MediaPlayer `main` 在批次冻结后又前进了 2 个提交（`c2a94f8a`、`a2633358`），而冻结批次把工作流 ref head 钉在 `4f9d9ac`。该批次 `dispatch_status=not_sent`、无 dispatch 回执、无 tag，永远不可能再发出。

问题在于：核心对这个已失效批次仍给 `can_start=true`，界面于是持续显示「继续构建发布…」，按下去必然得到同一句拒绝——看起来像「点了没反应」。

本次覆盖：未发送批次若已因**终态原因**被拒（发布证据/版本清单/成员/工作流 ref head/策略/成员关联变化），核心不再给 `can_start`，只保留 `can_abandon`；任务详情「构建与发布」在上述状态下显示一行说明，引导先「放弃未发送批次…」再重新准备。

明确排除：不改冻结与发送的判定本身、不改弃用命令语义、不改 `unknown`（调度结果待确认）分支、不改批次冻结内容或 tag 规则、不动 rel12 的底栏改造（该分支未合入）。

## 已确认决定

- 终态原因集合：`release_context_changed`、`release_version_changed`、`release_tag_collision`、`release_members_changed`、`release_ref_changed`、`release_not_unsent`、`release_batch_abandoned`、`release_policy_changed`。这些在发送前核对里都意味着「冻结时的事实已经不在」，重试同一批次无解。
- 传输类瞬时失败（如 `release_evidence_unconfirmed`、GitHub 网络错误）不在此列，仍保留 `can_start` 供重试——测试用 `RuntimeError` 触发该码并断言 `can_start` 仍为 true。
- 只改 DTO 的 `can_start` 计算与一句界面说明，不新增核心命令、不加 DTO 字段；Swift 侧只加一个纯计算属性 `frozenBatchNeedsAbandon` 与一行提示。

## 实现与验收

| 步骤 | 用户效果 / 实现范围 | 验收情形与预期 | 当前结果及证据 | 状态 |
| --- | --- | --- | --- | --- |
| T1 | `release_delivery._dto`：终态拒绝的未发送批次 `can_start=false`，`can_abandon` 不变 | 该任务当前批次 DTO：`can_start` 由 true 变 false，`can_abandon` 仍 true | 单元测试 `test_workflow_ref_moves_at_last_read_never_dispatches` 断言；真实批次回执 + 原拒绝原因只读模拟得 `can_start=false`/`can_abandon=true`，传输类失败仍 true | 已完成 |
| T2 | 「构建与发布」在 `frozenBatchNeedsAbandon` 时只显示弃用引导 | 打开该任务：不再出现可点的「继续构建发布…」，只见「放弃未发送批次…」与说明 | `frozenBatchNeedsAbandon` + 说明文案 + 夹具断言；Swift 编译由云端确认 | 已完成（待云端编译） |
| T3 | 核心测试与云端 Packages 测试版 | 相关模块通过、Swift 夹具通过、预发布核对一致 | 待填 | 待云端 |

## 执行与验证记录

| 日期 | 代码版本 / 候选差异 | 环境与运行对象 | 命令或实际操作 | 结果 / 证据位置 |
| --- | --- | --- | --- | --- |
| 2026-11-24 | 分支开发版 | 隔离测试 | `python3 -m pytest tests/test_release_delivery.py -k "workflow_ref_moves or transient_dispatch_failure or report_or_workflow_drift"` | 3 passed；去掉核心修改时 `can_start` 断言失败 |
| 2026-11-24 | 分支开发版 | 运行库 `mode=ro` + 真实批次回执 | 用真实回执与修复前那条拒绝原因做只读模拟，`_dto` 计算 | `can_start=false`、`can_abandon=true`；换成 `release_evidence_unconfirmed` 仍 `can_start=true`；0 次库写入 |
| 2026-11-24 | 分支开发版 | 隔离测试 | `pytest tests/test_release_delivery.py tests/test_release_verification_projection.py tests/test_review_delivery.py -q` | **71 passed, 79 subtests passed**（190s） |
| 2026-11-24 | 用户操作 | 运行库 | 用户 15:13:37 已自行弃用未发送批次；15:13:51 重新校验 | 该任务现在 `待构建发布`（`can_start=true`、`batch_id=null`），可直接按当前 `main` 重新准备 |

## 提交与集成

| 提交 | 行为变化与原因 | 验证 | 合并 / 发布情况 |
| --- | --- | --- | --- |
| 待填 | 失效未发送批次不再可启动，界面引导弃用后重新准备 | 待填 | 待填 |

## 交付与恢复

- 体验入口：安装含本改动的测试版后打开该 MediaPlayer 任务「构建与发布」。预期：不再显示「继续构建发布…」，改为弃用引导；点「放弃未发送批次…」后回到「待构建发布」，再「继续构建发布…」按当前 `main` 重新冻结并派发。
- 尚未完成 / 尚未验证：未在真实窗口点击核验；未代用户弃用或重发批次（属外部发布动作）。
- 用户确认（明确确认后才改为已验收）：未确认。
- 下一步和恢复任务时首先核对的内容：分支 HEAD 与本文件差异；该任务 DTO 的 `can_start`/`can_abandon`；测试版标签与 run。
- 需要时的回退 / 数据保留方式：还原提交即恢复旧的 `can_start` 计算；不改数据库、批次与发布回执。
- 本任务 worktree 收尾：合入 `main` 且验证通过后在同一任务内删除工作树与已合并本地分支。