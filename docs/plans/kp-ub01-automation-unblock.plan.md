# kp-ub01：解除自动化阻塞的六项修复

- 项目 / 任务编号：personal-kaneopilot / kp-ub01
- 状态：实现中
- 创建日期（Asia/Taipei）：2026-11-12
- 权威记录：本文件
- 基线 / 分支 / 合入目标：main（ea02654）→ main
- 当前 HEAD：ea02654（工作区改动未提交）

## 目标与范围

用真实任务复现后确认：编排链路本身可用，阻塞来自材料、验收、判定三处口径互斥。
本次按顺序修四件事，全部只改 core（Python）与冻结规格，不碰 Swift/Android：

1. 规划材料：`.pi/**` 证据不再整包进入 `execution_worktree` 材料。
2. verify 门禁：拒收设备/模拟器/录屏类命令，这类验收改走人工验收清单。
3. 空转判定：只有 `.pi/**` 变动的轮次仍判 `empty_diff`，但说明必须可执行。
4. 执行提问最长等待：默认 86400 → 3600 秒。
5. 计划篇幅契约：所有规划后端按改动面定档（轻档正文 ≤60 行、重档 ≤180 行），禁止复述材料/历史轮次、沿用旧计划结构、罗列证据清单与配额换算；发布事件记录 `plan_body_lines` 作可观测指标。
6. 人工验收脱离完成门禁：真机/设备/听感/视觉/跨平台项写进计划 `## 人工验收（不阻塞）` 段，不进 verify、不阻塞 Review 与完成，登记为「待体验」交用户确认。

## 已确认决定

- 不降低任何验证门禁：修的是“证据进错通道”和“说明不可执行”，不是放宽通过条件。
- verify 采用首词/前缀匹配的设备命令黑名单，不用白名单（避免误伤项目自有命令）。

## 实现与验收

| 步骤 | 实现范围 | 验收情形与预期 | 结果及证据 | 状态 |
| --- | --- | --- | --- | --- |
| T1 | `constants.PLAN_INPUT_EVIDENCE_FILE_MAX_BYTES=128KiB`；`planner_inputs._evidence_material_path` + `_read_worktree_material(limit_reason=)`；`.pi/**` 单文件超限登记 `reason=evidence_too_large` | 5 MB lsof JSON 不再内联，小报告仍可读，源码不受该上限影响 | `test_evidence_material_is_capped_but_small_text_still_inlined` 通过 | 实现中 |
| T2 | `verify_evidence._device_command` + `_DEVICE_COMMAND_ERROR`；`planner_prompt.md` 增常规则 | `adb/emulator/xcrun simctl/scrcpy/flutter run` 被拒且错误指向人工验收清单；常规命令仍通过 | `test_device_command_rejection_is_actionable`、`test_host_runnable_commands_still_pass` 通过 | 实现中 |
| T3 | `engine/verdict._evidence_only_changes` + `_check_result(evidence_changes=)`；两处调用点 | 只有 `.pi/**` 变动时 `kind` 仍为 `empty_diff`，`detail` 写明“N 项”与下一步 | `test_evidence_only_round_reports_actionable_detail` 通过 | 实现中 |
| T4 | `constants.EXECUTION_QUESTION_MAX_WAIT_SECONDS=3600`；`docs/protocol.md` §5.28 同步 | 派发快照冻结新默认；旧快照仍按 86400 解释 | 既有 `test_cmd_config`/`test_role_config` 按常量断言通过 | 实现中 |
| T5 | `planner_prompt.md` 定档与篇幅纪律；`_PLUS_INSTRUCTION`、`SPEC_PROMPT_PREFIX` 同步；`planfile.body_line_count` + `plan_published.plan_body_lines`；protocol §5.22.3 | 轻档正文 ≤60 行、重档 ≤180 行；不再继承旧计划篇幅；发布事件可核对行数 | `test_plan_brevity.py` + `test_lifecycle` 的 `plan_body_lines` 断言通过 | 实现中 |
| T6 | 计划 `## 人工验收（不阻塞）` 段；`planfile.manual_acceptance` 解析 + `extra.plan_manual_acceptance` + `get-task` 投影；`review.md`/`review_cli.md` 判定规则；protocol §5.22.3 | 人工项不进 verify、不阻塞完成；Review 不再因缺真机/Windows/听感判 blocked；发布后视图可读到待体验清单 | `test_manual_acceptance_is_recorded_as_pending_experience` 与解析/提示词用例通过 | 实现中 |

## 执行与验证记录

| 日期 | 代码版本 | 环境 | 命令 | 结果 |
| --- | --- | --- | --- | --- |
| 2026-11-12 | 工作区（ea02654+改动） | 本机 Python 3.10 隔离库 | `cd core && python3 -m pytest tests/test_verify_evidence.py tests/test_engine_verdict.py tests/test_planner_inputs_automation.py -q` | 190 passed, 18 subtests passed |
| 2026-11-12 | 同上 | 同上 | `cd core && python3 -m pytest tests/test_auto_monitor.py tests/test_cmd_config.py tests/test_codex_exec_config.py tests/test_delivery_artifact_filter.py tests/test_engine_verdict.py tests/test_execution_input_worker.py tests/test_execution_questions.py tests/test_planner_inputs_automation.py tests/test_role_config.py tests/test_verify_evidence.py tests/test_verify_evidence_execution.py -q` | 513 passed, 58 subtests passed |
| 2026-11-12 | 同上 | 同上 | `cd core && python3 -m pytest tests -q` | **未跑完**：本机两次都在一个与本改动无关的 `select.poll` 等待上卡住（12 分钟无进展后终止）。相关模块已用上一行子集覆盖 |
| 2026-11-12 | 同上 | 历史计划兼容性 | 对 `~/Documents/Code/Plan` 下 41 份含 verify 的计划重放 `parse_verify_block` | 仅 1 份因既有“verify 被嵌套围栏”规则被拒，设备命令规则 0 命中 |
| 2026-11-12 | 工作区（含 T5/T6） | 本机 Python 3.10 隔离库 | `cd core && python3 -m pytest`（19 个相关测试文件：brevity/lifecycle/verdict/verify/planner/execution-questions/dispatch/review/board 等） | **718 passed, 63 subtests passed** |

## 交付与恢复

### 云端构建记录

- 分支 `fix/20261112-kp-ub01-automation-unblock`，提交 `55d36d1`。
- 首次 `Packages` run 43239792169 在 `checks (3, 3.13)` / `checks (3, 3.11)` 的 “Integration fixture tests” 失败：`tests/test_pi_config_contract.py::test_get_exec_config_roundtrip_after_set` 仍断言旧默认 86400。该用例属本次默认值调整的预期更新，已改为 3600 并本地验证本文件 12 passed；这是本次改测试预期的唯一依据。


- 体验入口：改动在源码树 `core/`；正在运行的 `/Applications/KaneoPilot.app` 仍是旧副本，需要走云端 `Packages` 构建安装后才生效。
- 尚未验证：真实任务重跑（规划材料体积、verify 拒收、提问超时）需要装机后复测；本机全量测试套件未跑完（见上表）。
- 现场证据：修复期间任务 `wjhpqxfqxr3084vxtc19e1uo` 的第 4 次规划又于 2026-11-12 21:50:42 以 `planner_timeout（1800s）` 失败，与 T1 指向的原因一致。
- 回退：四项均为独立小改，按文件回退即可；`docs/protocol.md` 中 2026-11-12 标注段落一并回退。
