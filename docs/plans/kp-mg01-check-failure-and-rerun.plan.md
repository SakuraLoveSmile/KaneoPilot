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