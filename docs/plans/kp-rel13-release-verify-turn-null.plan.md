# KP-REL13：补验证复核接受 Plus 重启后清空的 currentTurnId

状态：待体验

- 项目 / 任务编号：personal-kaneopilot / kp-rel13
- 创建日期（Asia/Taipei）：2026-11-24
- 权威记录：本文件；外部材料只链接它
- 主仓库：`/Users/sakurasep/Documents/Code/Project/Personal Project/KaneoPilot`
- 工作树：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261124-fix-kp-rel13-release-verify-turn-null`
- 基线 / 分支 / 合入目标：`main@28ba4e0` / `fix/20261124-kp-rel13-release-verify-turn-null` / `main`
- 运行资源：无新增端口或进程；核对运行库 `~/Library/Application Support/KaneoPilot/kanpilot.db` 一律以 `mode=ro` 只读打开，未写入状态。

## 目标与范围

用户 2026-11-24 反馈：MediaPlayer 任务 `au8bcziabscy8fjrhdr2e3s8`（「中文域名在服务地址栏显示为编码，建议增加中文域名适配」）概览显示「发布受阻 / 独立验证会话仍运行或当前执行轮次已变化」，底栏「继续构建发布…」按不动这条阻塞。

根因：核心复核已验证的补验证时，`release_verification._completed_report` 要求 Plus 会话的 `currentTurnId` 仍等于记录的执行轮次。Plus 重启或会话结束后该指针会变成 `null`（`docs/release-contract.md`「重启恢复」已写明这是正常形态，同文件 `_resolve` 路径早已用 `in (None, turn)`）。于是已经 `verified` 的尝试在每次发布准备时必然复核失败，引擎每约 5 分钟的交付同步都会把同一条阻塞重新写一遍。

本次覆盖：已接受且 `verified` 的这份证据允许在「会话空闲且当前轮次为 `null`」时重读（发布准备与发送前复核都会重读它），其余硬证据（`turn_get` 的 session+turn 身份与 completed、报告 state/ID/正文 SHA、逐字检查退出码 0、独立工作树源码指纹）一律保留；其它尝试状态仍要求当前轮次为本记录轮次，重启资格仍只由显式重启恢复给出。新增隔离测试断言「换到别的轮次仍拒绝」。

明确排除：不改发布批次规则与派发流程、不改桌面/安卓界面、不重跑补验证（不再付模型费用）、不自动重试或重发发布、不改 Kaneo 远端状态。

## 已确认决定

- 只读核对（2026-11-24 13:2x）确认任务投影 `state=blocked`、`title=发布受阻`、`reason=独立验证会话仍运行或当前执行轮次已变化`、`blockers=[release_verification_turn]`、`can_start=true`、`can_abandon=true`、`checked_at=2026-11-24T05:22:36Z`；批次 `04bbeaba-…`（`v0.4.11-pilot.1795424173`）`dispatch_status=not_sent`，从未派发、无 tag、无 GitHub 运行。
- 同一时刻的补验证回执 `verification:cd6735…` 为 `state=verified`、报告 `rep-b8fe…`、`execution_turn_id=5ebea0c5…`；`pi_agent_status` 显示 `isRunning=false`、`currentTurnId=null`，`pi_turn_get` 显示该轮 `completed`（endedAt 2026-11-23T08:50:46Z）。
- 只读模拟（仅在内存中把该比较放宽为 `None` 或本轮次）后，`_report` 完整通过：报告 SHA、4 条冻结命令退出码 0、工作树源码指纹全部一致。说明证据完好，唯一卡点是这个可被重启清空的指针。
- 首版把判据整体放宽为 `in (None, turn)`，`tests/test_release_verification_history.py::test_restart_preview_after_plus_reset_is_read_only_and_preserves_old_failure` 立刻失败：该测试要求「非重启路径遇到空指针必须拒绝」，即重启资格不能由空指针旁路给出。据此收窄为**只对已接受且 `verified` 的这份证据**接受空指针，其它状态仍要求当前轮次为本轮次——空指针只用于重读既有证据，不产生新证据。
- 判据与同文件 `_resolve_missing_release_verification`（`currentTurnId in (None, turn)`）及「重启恢复」对 `null` 的认定保持一致；错误消息文本不改，保留既有回执的措辞与检索口径。

## 实现与验收

| 步骤 | 用户效果 / 实现范围 | 验收情形与预期 | 当前结果及证据 | 状态 |
| --- | --- | --- | --- | --- |
| T1 | `release_verification.py`：已接受且 `verified` 的证据在会话空闲、`currentTurnId` 为 `null` 时可重读；注释写明空指针只用于重读 | 该任务那份 verified 证据的 `verified_proof` 通过；verified 尝试换到其它轮次仍抛 `release_verification_turn`；blocked/failed 尝试遇空指针仍拒绝 | `_completed_report` 判据按状态收窄；`docs/release-contract.md`、`docs/protocol.md` 同步口径 | 已完成（提交 `024c7dc`） |
| T2 | 新增隔离测试 `test_verified_proof_rechecks_after_plus_restart_clears_current_turn` | 修复前按原报错失败；修复后通过；换轮次仍拒绝 | 去掉修复时 `release_verification_turn` 失败；含修复时通过 | 已通过 |
| T3 | 云端 Packages 发测试版 | macos/Fixtures/Python 分片全通过；预发布资产与说明核对一致 | run 45338107402 全绿；发布 `v0.5.10-test.442` | 已发布 |

## 执行与验证记录

| 日期 | 代码版本 / 候选差异 | 环境与运行对象 | 命令或实际操作 | 结果 / 证据位置 |
| --- | --- | --- | --- | --- |
| 2026-11-24 | `main@28ba4e0`（未改核心） | 运行库 `mode=ro` + 本机 Plus 只读连接 | `get-task`、`agent_status`、`turn_get`、`_report` 只读重放 | 见「已确认决定」第 1–3 条 |
| 2026-11-24 | 首版（整体放宽为 `in (None, turn)`） | 隔离测试 | `python3 -m pytest tests/test_release_verification.py tests/test_release_verification_history.py tests/test_release_verification_automation.py tests/test_release_verification_questions.py tests/test_release_delivery.py tests/test_release_verification_projection.py -q` | 141 passed / 1 failed：`test_restart_preview_after_plus_reset…` 断言非重启路径遇空指针必须拒绝 → 判定口径过宽，已收窄 |
| 2026-11-24 | 收窄版 `024c7dc` | 隔离测试 | 同上 6 个模块，含新增用例 | **142 passed, 168 subtests passed**（415.87s） |
| 2026-11-24 | 收窄版 `024c7dc` | 运行库（`mode=ro`）+ 本机 Plus 只读连接 | 用修复后的核心对真实任务 `verified_proof` 只读复核 | 通过：`verification_id=cd6735…`、`target_sha=4f9d9ac`、4 条冻结命令 exit 0；`conn.total_changes` 前后差 0 |
| 2026-11-24 | `024c7dc` | 本地全量 core 套件 | `python3 -m pytest tests -q` | 跑到 23% 后主动中止（本地全量约需 1 小时以上）；全量由云端 `checks` 10 个分片覆盖 |

## 提交与集成

| 提交 | 行为变化与原因 | 验证 | 合并 / 发布情况 |
| --- | --- | --- | --- |
| `024c7dc` | 补验证复核不再把 Plus 重启后清空的 `currentTurnId` 当成轮次变化（仅限已接受且 verified 的证据） | 6 个相关模块 142 项通过；真实任务只读复核通过；云端 run 45338107402 全绿（16 success、1 预期 skipped） | 已发布测试版 `v0.5.10-test.442`；**尚未合入 main** |

## 交付与恢复

- 体验入口：设置 → 高级 → 软件更新 获取 `v0.5.10-test.442`（需开启「接收测试版」）后，打开该 MediaPlayer 任务「构建与发布」并点「继续构建发布…」。预期：不再出现 `release_verification_turn` 阻塞，批次 `v0.4.11-pilot.1795424173` 正常派发；常驻引擎要换到包含本改动的版本后才会自动停掉这条重复核对。
- 尚未完成 / 尚未验证：未在真实窗口做原生点击核验，未安装该测试版；该任务的真实发送（会创建 tag 与 GitHub Actions 运行）由用户在界面确认。分支尚未合入 `main`。
- 用户确认（明确确认后才改为已验收）：未确认。
- 下一步和恢复任务时首先核对的内容：分支 HEAD（`024c7dc`）与本文件差异；该任务 `release.can_start` 与批次 `dispatch_status`；测试版标签 `v0.5.10-test.442`（run 45338107402）。
- 需要时的回退 / 数据保留方式：还原本任务提交即恢复原判据；不改数据库、不改批次与发布回执。
- 本任务 worktree 收尾：合入 `main` 且验证通过后在同一任务内删除工作树与已合并本地分支。