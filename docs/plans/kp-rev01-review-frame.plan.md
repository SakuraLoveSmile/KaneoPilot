# KP-REV01：CLI Review 输出帧修复（Codex 评审不再因分段文本被判格式失败）

- 项目 / 任务编号：personal-kaneopilot / KP-REV01
- 状态：待体验
- 创建日期（Asia/Taipei）：2026-11-23
- 权威记录：本文件；外部材料只链接它
- 主仓库：`/Users/sakurasep/Documents/Code/Project/Personal Project/KaneoPilot`
- 基线 / 分支 / 合入目标：`28ba4e0` / `fix/20261123-kp-rev01-review-frame` / `main`
- 当前 HEAD / 未提交差异：见本节后续提交记录；实现与规格在同一提交
- 运行资源：本地隔离 pytest（`core/tests`）；测试版发布走云端 `Packages`

## 目标与范围

Codex CLI Review 在第三方服务商（Magpie 网关模型，不执行 `--output-schema`）返回合法 review_text_v1 分段文本时，仍能结算出真实结论，不再被判「Codex Review 缺少有效最终 JSON」而 `review_blocked`；CLI 评审只收到一种输出帧要求。

覆盖：`review_instruction` 新增 `output_format`（CLI=`json`，Plus 保持 `text`）、Codex 最后消息的 review_text_v1 兼容解析与 `job.result_format` 留证、`docs/protocol.md` §5.24.5 与 `docs/automation-contract.md`「Claude/Codex Review」规格补充、隔离测试。

排除：不改 Plus 的 review_text_v1 传输与解析规则；不为 Claude/Qoder 增加散文回落（两者 CLI 各自强制结构化输出）；不自动重发或重新结算历史失败轮次；不切换用户当前的 Review 模型。

## 已确认决定

- 2026-11-23 用户选择实现层面修复（而非只切换模型/后端）。
- 依据：MediaPlayer 任务 Review 受阻，job 配置 `model_source=magpie`、`bailian-token-plan/deepseek-v4.1-flash-1017`；`last-message.txt` 是合法分段文本、结论 pass，`exit_code=0`、`turn.completed` 正常，失败点只是 `core/kanpilot/cli_review.py` 第 112 行的 `json.loads`。
- 根因：`review_cli.md` 要求 JSON，而共用的 `review_instruction` 又对 CLI 后端追加「不要输出 JSON，用 review_text_v1」；原生 Codex 模型被 `--output-schema` 拉回 JSON，第三方模型没有该强制，照后者作答。
- 指令只下发一种帧（CLI=JSON）；分段帧只作传输兼容，非合法分段仍 fail-closed，不猜改、不从旧消息取结论。

## 实现与验收

| 步骤 | 用户效果 / 实现范围 | 验收情形与预期 | 当前结果及证据 | 状态 |
| --- | --- | --- | --- | --- |
| T1 | CLI 评审指令只要求 JSON | 生成指令不含 `<<<KANPILOT_REVIEW_V1>>>`/「不要输出 JSON」；Plus 指令仍含分段帧 | `test_cli_instruction_sends_json_frame_only`、`test_run_stage_records_cost_before_settlement` 断言 | 待体验 |
| T2 | Codex 分段文本可结算并留证 | 传入合法文本帧时 `is_error=False`、`result_format=review_text_v1`、`job.result_format` 落库 | 同上两个用例 + 真实失败产物解析（执行记录第 3 行） | 待体验 |
| T3 | 非 JSON 散文仍阻塞 | 形如「结论：通过」的答复报「缺少有效最终 JSON」 | `test_codex_unframed_prose_still_blocks` | 待体验 |
| T4 | 规格与代码一致 | protocol §5.24.5、automation-contract 写明帧契约与兼容边界 | 文档 diff | 待体验 |
| T5 | 测试版可安装 | 推送分支 → `channel=test` 触发 `Packages` → 核对 `vX.Y.Z-test.<run>` 与资产 | run 45128936493 → `v0.5.10-test.424`：checks 5×2 全成功，预发布、四项资产齐全、指向 `d698422` | 待体验 |
| T6 | 工作树收尾 | 合入 `main` 后清理本任务 worktree 与已合并本地分支 | 未合入前保留 | 未开始 |

## 执行与验证记录

| 日期 | 代码版本 / 候选差异 | 环境与运行对象 | 命令或实际操作 | 结果 / 证据位置 |
| --- | --- | --- | --- | --- |
| 2026-11-23 | 本分支工作树（实现完成后） | 本地 Python 3.13，隔离 DB 与临时目录 | `cd core && python3 -m pytest tests/test_cli_review.py tests/test_plus_review.py tests/test_plus_review_text.py -q` | 133 passed, 20 subtests passed（2:40） |
| 2026-11-23 | 同上 | 同上 | `python3 -m pytest tests/test_cli_review.py -q -k 'text_frame or json_frame or unframed'` | 4 passed（新增用例） |
| 2026-11-23 | 同上 | 只读复核真实失败产物 | 用新回落 `_text_result` 解析 `executor-sessions/review/e70f1c1e…/last-message.txt` | 得到合法 `conclusion=pass`，`review_settle._validate_result` 通过；旧行为报 `Expecting value: line 1 column 1 (char 0)` |
| 2026-11-23 | 同上 | 本地 | `python3 -m compileall -q kanpilot` | EXIT=0 |
| 2026-11-23 | `d698422` | GitHub Actions `Packages`（run 45128936493，channel=test） | `gh workflow run packages.yml --ref fix/20261123-kp-rev01-review-frame -f channel=test -f notes=…`；`gh run watch --exit-status` | success；`gh release view v0.5.10-test.424` 为预发布、非 draft，资产 apk/dmg/manifest/SHA256SUMS，commit=`d698422` |
| 2026-11-23 | `d698422` | 同上（推送触发的预览 run 45127696854） | 已取消，避免占用 macOS 并发；其 `checks` 十个分片此前已全 success | checks 全部 success 后才取消 |

## 提交与集成

| 提交 | 行为变化与原因 | 验证 | 合并 / 发布情况 |
| --- | --- | --- | --- |
| `d698422`（2026-11-23） | CLI Review 只下发 JSON 帧；Codex 合法分段帧按兼容解析并记 `result_format` | CI checks 全分片通过；本地 review 相关 133 项 + 4 个新用例 | 已推送 `origin/fix/20261123-kp-rev01-review-frame`；测试版 `v0.5.10-test.424`；未合入 `main` |

## 交付与恢复

- 体验入口和操作步骤：重启到测试版后，对同一任务点「重新 Review」；Codex 后端即使给出分段文本也能结算。
- 尚未完成 / 尚未验证：测试版 `v0.5.10-test.424` 安装后的真实 Review 复跑（需要用户对受阻任务点「重新 Review」）；`main` 尚未合入，故工作树暂不清理。
- 用户确认：未确认。
- 下一步和恢复任务时首先核对的内容：本文件「执行与验证记录」、`git -C <worktree> status`、`core/tests/test_cli_review.py` 新增用例。
- 需要时的回退 / 数据保留方式：回滚本分支提交即恢复旧行为；失败评审的 `executor-sessions/review/<id>/` 证据不删除。
- 本任务 worktree 收尾：尚不满足清理条件（未合入）。