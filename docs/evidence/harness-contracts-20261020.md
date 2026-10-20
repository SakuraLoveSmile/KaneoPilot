# 跨 Harness 契约实施与验证记录（2026-10-20）

状态：待体验。本地实现、适用隔离验证与候选打包已完成；真实模型、正式安装和用户验收尚未执行。

## 工作区与范围

- 原目录：`/Users/sakurasep/Documents/Code/Project/Personal Project/KaneoPilot`，分支 `codex/review-result-format-20261018`，基准提交 `a459904ca4e2bdb59081fa7e663a1d6f2413b04d`。
- 候选：`/Users/sakurasep/.codex/worktrees/harness-contracts/KaneoPilot`，分支 `codex/harness-contracts-20261020`。保留原工作区全部既有改动，再叠加本次实现；相对 HEAD 的整份 diff 不能直接视为本次修改。
- 备份：`/var/folders/3f/d270m3fx56b4ff4x8l0xcn8m0000gn/T/kaneopilot-harness-baseline-_rz6e9jd`，保存原 tracked patch、684 文件的内容清单及候选增量清单。原目录 tracked patch 与开始时逐字节一致；原 RECORD 的历史 CRLF 换行保留，新 worktree checkout 为 LF。
- 规格：[harness-contract.md](../harness-contract.md)，已同步 protocol §5.30、automation-contract 与 state-model，无 DB schema 迁移。

## 实施交付

| 编号 | 已实现行为 | 本轮验证重点 |
| --- | --- | --- |
| T1 | `kp-stage-v1` 冻结阶段身份、工作树、输入、配置、权限和原文命令；结果信封绑定契约 SHA256 | 版本、字段、身份、JSON 上限、凭据字段、业务 payload、损坏记录拒绝 |
| T2 | `kp-harness-v1` 语义能力与适配器注册；Plus 原生格式留适配层，CLI 仅声明真实规划/评审能力 | supported/unsupported/unknown、可信 scope、防参数切换 harness、保持证据来源 |
| T3 | 规划共用输入门、实际槽位/源码/材料冻结及结果门；评审共用结算与恢复校验 | Plus 与本地 Codex/Claude stub；旧配置恢复、父记录缺包、忽略材料篡改、符号链接、错轮次拒绝 |
| T4 | 执行冻结实际指令/正文/prompt/修复来源；核心核对真实 request、报告及源码。交付核信封后才推进，写前回执 | 原文命令、重复/矛盾证据、明确失败与未确认区分、真实临时 DB+CLI 核证；unknown 只读恢复、明确拒绝的新 claim 重试 |
| T5 | 文档、打包模块/规格核验、独立审查与基线对照 | 隔离测试、源码保全、候选 App 构建/签名、包内源码一致性 |

规划契约在实际工作树准备后冻结，受管材料独立读取，不依赖 Git 是否忽略。恢复使用原工作树和原配置；新记录缺结果不能退回旧路径。交付明确拒绝允许按原策略在新领取登记重试；写入已发送或结果未知仍只读对账，不能用新 claim/参数绕过。PR 创建已成功但后续读取失败保留 unknown。

独立 reviewer 的 5 项发现已在源码复审中关闭：规划工作树/配置、已完成父轮缺包、执行指令指纹、交付 rejected 重试、被 Git 忽略的材料指纹。任务内 AI 处理的确认快照忽略交付审计信封更新，继续保留 head、PR、评审、业务状态及未决操作守卫。

## 本次实测

解释器：`/Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11`（3.11.4，真实 pytest 9.1.1）。测试使用临时 DB、Git 仓库、fake HOME、假 Kaneo/MCP/CLI。项目自有测试可在 Pilot 开发环境运行；产品运行时仍禁止 Pilot 代跑任务 verify。

统一命令前缀：

```sh
PYTHONPATH=core:core/tests /Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11 -m pytest
```

| 检查 | 文件/范围 | 当前结果 |
| --- | --- | --- |
| 原目录基线 | 下方同范围 11 模块 | 346 passed、12 failed、53 subtests passed，442.05s |
| 候选同范围对照 | 同一 11 模块 | **346 passed、12 failed、53 subtests passed，417.75s；失败 node IDs 与基线完全相同，无新增失败** |
| 当前契约专项 | test_stage_contract、test_stage_evidence、test_harnesses、test_stage_runner_contracts、test_execution_stage_contract、test_delivery_stage_contract | **95 passed、145 subtests passed，70.38s** |
| 执行与任务 AI 集成 | test_execution_stage_contract、test_task_ai_action、test_direct_repair_prompt | **37 passed、48 subtests passed，46.63s** |
| 交付/GitHub 回归 | test_delivery_pr、test_delivery_runner、test_delivery_stage_contract、test_gh_client | **95 passed、31 subtests passed，76.82s**，交付 worker 执行 |
| worker/dispatch 专项初轮 | test_pi_desktop_backend、test_pi_desktop_worker_verification、test_pi_desktop_worker_recovery、test_dispatch、test_dispatch_branch、test_automation_policy | 152 passed、3 failed、16 subtests passed，238.65s；下方记录 3 项原因与复跑 |
| 新契约真实子进程派发/恢复 | TestDispatchPiDesktop（修正上述夹具并新增缺报告能力负例后） | **7 passed，11.24s** |
| 差异/脚本 | git diff --check、bash -n scripts/build_app.sh | 通过 |
| App 构建 | APP_OUTPUT_DIR 指向独立临时目录，scripts/build_app.sh | 构建、包内模块导入/源码哈希核验及本地 ad-hoc 签名通过；未公证/安装 |

11 模块同范围列表：

```text
core/tests/test_cli_review.py
core/tests/test_plus_planner.py
core/tests/test_plus_review.py
core/tests/test_review_settle.py
core/tests/test_engine_verdict.py
core/tests/test_harness_plus_adapter.py
core/tests/test_required_verification_transport.py
core/tests/test_mcp_review_regressions.py
core/tests/test_execution_input_runtime.py
core/tests/test_runtime_transcript_paging.py
core/tests/test_mcp_scopes.py
```

基线 12 项精确 node IDs：

```text
core/tests/test_cli_review.py::test_run_stage_records_cost_before_settlement[False-claude]
core/tests/test_cli_review.py::test_run_stage_records_cost_before_settlement[False-codex]
core/tests/test_cli_review.py::test_run_stage_records_cost_before_settlement[True-claude]
core/tests/test_cli_review.py::test_run_stage_records_cost_before_settlement[True-codex]
core/tests/test_cli_review.py::test_cli_branch_review_shares_artifact_guard[False-claude]
core/tests/test_cli_review.py::test_cli_branch_review_shares_artifact_guard[False-codex]
core/tests/test_cli_review.py::test_cli_branch_review_shares_artifact_guard[True-claude]
core/tests/test_cli_review.py::test_cli_branch_review_shares_artifact_guard[True-codex]
core/tests/test_plus_review.py::TestReviewConclusions::test_changes_requested_creates_fix_before_register
core/tests/test_plus_review.py::TestReviewConclusions::test_incident_format_settles_changes_requested_with_audit
core/tests/test_plus_review.py::TestReviewConclusions::test_text_result_registers_raw_markdown_and_creates_one_fix
core/tests/test_plus_review.py::TestReviewRecovery::test_recovery_completes_without_duplicate_fix_or_replay
```

CLI 8 项因原槽位夹具身份先行拒绝；Plus 3 项仍期望跨任务创建 Review fix，与原工作区既有同任务修复行为不同；另一项期望旧 `fix_created` checkpoint。这里保留现状，未通过放宽新契约或修改旧断言掩盖失败。

worker/dispatch 初轮的 3 项为 `TestDispatchPiDesktop.test_round_observe_keeps_other_blocks / test_round_observe_reattaches_after_observer_loss / test_round_observe_reopens_stale_execution_failure`。原目录三项通过（6.37s）；候选现场保存的 job 明确为 `failed / not_sent`，原因是旧 fake 工具集不含 `verification_report`。这是新契约要求报告机制的能力门：已在新派发夹具声明报告工具，历史 `TestRunReal` 及全局旧工具集保持原样，并新增缺报告能力时不创建会话、不发送 prompt 的负例。受影响整个派发测试类复跑 7 项通过，能力门保持原生启动前拒绝。独立 reviewer 已复核此调整。

原始日志：`/tmp/kaneopilot-harness-baseline-pytest.log`、`/tmp/kaneopilot-harness-candidate-pytest.log`、`/tmp/kaneopilot-harness-contract-tests.log`、`/tmp/kaneopilot-harness-integration-tests.log`、`/tmp/kaneopilot-harness-worker-dispatch-tests.log`、`/tmp/kaneopilot-harness-worker-failures-baseline.log`、`/tmp/kaneopilot-harness-dispatch-final.log`、`/tmp/kaneopilot-harness-build.log`。各批次存在交叉覆盖，不将通过数相加。

候选 App：`/tmp/kaneopilot-harness-build-Qr4cBn/KaneoPilot.app`。154 个 runtime 文件及 4 份打包规格与候选源码逐字节核对一致；这里只交付本地包，没有启动它来连接生产服务。

## 体验边界

执行仍仅支持现有 Plus；Codex/Claude CLI 用于共同规划/评审契约的另一实现机制，不宣称已支持其执行。新 harness 需要实现真实能力及原生适配，并通过共同契约检查。

结构约束、身份哈希与状态机可以阻止错轮次、软件输入漂移和格式错误被误收，不能证明模型声明的内容真实。Plus checks 保持 `model_declared`，未冒充宿主独立命令采集。真实 harness/付费模型、正式安装、生产任务、远端发布及 UI 体验本次均未执行；没有提交、push、PR、merge 或替换已安装 App。
