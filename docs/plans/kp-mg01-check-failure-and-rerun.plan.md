# 合并门检查失败的两个恢复入口（KP-MG01）

状态：实现中（待实机体验）

## 目标

合并门因 GitHub 检查失败而阻塞时，任务不再只能等待或重新规划：

1. `check_failure`：用户确认后由 AI 做一次最小修复，复审通过后推送原分支并重新核对检查；
2. `rerun-checks`：不调用模型，只重跑失败 Job，处理偶发失败。

## 已确认决定

- 两个入口都只在合并门实际点名「检查未通过」时出现，`waiting_checks`、保护规则、冲突等不进入。
- `check_failure` 复用既有 direct 修复链（同分支、同 worktree、单父提交、必须重新 Review），不新建 PR、不强推、不删除或跳过检查。
- 每次 `check_failure` 是一次模型轮次，需要用户确认；同一提交只允许一次，失败不自动重试。
- `rerun-checks` 按 run/attempt 写 `runtime_operations` 回执，只发一次；连接中断留 unknown，不自动重放。
- 两者都不改变项目自动合并策略：修好检查后仍由既有交付轮询按门禁判定合并。

## 范围

- core：`task_ai_check_failure`、`task_ai_action`/`repair_prompt`/`cmd_dispatch` 接线、`cmd_rerun_checks`、`gh_client.run_rerun_failed`、`derive` 动作。
- macos：动作标签、更多操作按钮与 `AppViewModel.rerunChecks`；`check_failure` 复用现有 AI 处理按钮，无结构改动。
- docs：protocol §5.27 AI 处理与 ui-spec 对应段落。

## 验证

- `pytest tests/test_check_failure_ai_action.py tests/test_build_failure_ai_action.py tests/test_review_delivery.py -q`：84 passed。
- `pytest tests/test_rerun_checks.py -q`：40 passed。
- 未验证：真实 Plus 修复轮次、真实 `gh run rerun` 与云端构建（走 test 版验证）。

## 遗留

- `check_failure` 目前要求 Plus 后端（与合并冲突处理一致），Codex CLI 后端尚未开放。
- 空日志或外部 CI（无 Actions 运行身份）的失败检查只能按名称提示，不冻结日志。
## 2026-11-19 补全：AI 处理按钮实际调不起模型

现象（用户）：任务 `nsa031de0gqsmkcc80mni5bc` 的合并门点名 `检查未通过：app-ci/analyze-test`，
详情页「AI 处理…」点击后没有产生修复轮次。

根因（均已实测）：

1. 已安装的 `v0.5.8-test.324`（657b411）早于 d347c9f，投影仍按 `merge_conflict` 预检；PR 实际是
   `MERGEABLE`（只是检查失败），所以只能得到「PR 已变化，或当前不是同仓库、可确认的合并冲突」。
2. 即使换成 effbf8c/main：`prepare-task-ai-action {kind:"check_failure"}` 在 `_public_candidate` 读
   `value["target_base_sha"]`，而该 kind 的冻结值没有这个键，返回 `internal` / `'target_base_sha'`；
   同时 review_repair、dispatch、交付与一次性门禁仍只认 `merge_conflict`/`build_failure`，
   领取后 `verify_frozen` 会按「修复执行轮次已变化」拒绝，复审通过也不会登记交付续接。

变更（core）：

- `constants.AI_ACTION_KINDS` 成为三类的唯一来源；`task_ai_action.KINDS` 引用它，
  review_repair（view / verify_frozen / verify_source_terminal / start / try_auto_start）、
  `core.start_fix_round_inline`、dispatch_validate、cmd_exec（running 进度、publish-plan 配置、
  复审通过登记交付续接）、cmd_delivery（严格验证证据、不静默复用旧提交）、cmd_queue（引擎补救与
  resume/retry/replan 的一次性门禁）、derive（显示名与阶段）、cmd_dispatch（会话名）、
  review_delivery（复审轮次）、engine/loop（质量重试）、cmd_remote（推进 in-review 的证据门）统一改用该集合。
- `task_ai_check_failure.prepare` 冻结 `target_base_sha`（= 本次评审依据的交付基线；检查修复原地提交，
  不合并目标分支，交付 `base_sha` 不移动）与 `raw_config_sha256`（准备期间通用配置变动即作废候选）。
- `_public_candidate` 的 `base_sha` 兼容缺失 `target_base_sha` 的历史冻结值。

变更（macos）：确认弹窗新增「修复失败的检查」分支并列出检查名；按钮文案区分
「正在核对检查…／读取失败检查日志并安排一次最小修复」。

验证：

- 新增 `core/tests/test_check_failure_action_flow.py`（6 passed）：prepare 冻结失败检查身份、
  start+claim 按 `action_config` 原地运行且不准备合并、修复提交是单父提交且 `base_sha` 不移动、
  复审通过后登记交付续接、同一提交拒绝第二次、不能当普通 Review 修复启动、显示为 AI 处理。
- 真实任务只读核对（生产 DB 副本 + 当前 core）：`prepare-task-ai-action {kind:"check_failure"}`
  返回候选：PR #11、head 8c77216、失败检查 app-ci/analyze-test、模型 deepseek-v4.1-flash / max。
- 未验证：真实 Plus 修复轮次、真实推送后检查转绿与自动合并（需 test 版实机体验）。

结论：旧 test 版需更新；更新后「AI 处理…」会先核对 PR 与失败日志，再进入一次原地最小修复。
