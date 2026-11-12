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
- 修正后提交 `f3245b6`；`Packages` run 43241920822（workflow_dispatch，channel=preview）**全部 job 成功**：identity / checks（1–5 × py3.11/3.13）/ macos-fixtures a,b / macos-package / android / codex-platforms native-platforms。`publish` 按 preview 通道跳过（不创建 Release），这是预期行为。
- 产物（未发布，仅 Actions 附件）：`macos-arm64-local-signed-246`（32.5 MB）、`android-debug-246`（37.0 MB）、各 check 报告与 `codex-platforms-43241920822-1`。
- 测试版发布：run 43245863522（channel=test，base_version=0.5.7，notes 为本次六项修复要点）全部 job 成功，publish 创建预发布 **v0.5.7-test.247**（B=247 > 已装 240）。
- 装机：下载 `KaneoPilot-0.5.7-test.247-macos-arm64.dmg`，SHA256 与 SHA256SUMS 一致；退出 App、旧包移到 `~/Library/Application Support/KaneoPilot-install-backups/20261112-233349-0.5.7-test.240`，替换 `/Applications/KaneoPilot.app`，清除 quarantine，启动。
- 装机验证：`CFBundleShortVersionString=0.5.7-test.247`、`CFBundleVersion=247`、ad-hoc 签名；六项修复在安装包内全部存在（constants 3600 / 证据上限 128 KiB / verify 设备命令 / 空转说明 / plan_body_lines / 人工验收解析与 Review 规则 / get-task 投影）。
- 行为核验（直接用安装包内置 core 执行）：8 项全 PASS——默认等待 3600、exec-config 合并默认、证据上限、.pi 判定、adb 被 verify 拒收且错误指向人工验收、普通命令仍通过、人工验收段解析、正文行数不含 frontmatter。
- 运行时：engine tick 正常、mcp-serve/mobile-serve/观察 worker 复起，`kanpilot.db` 72 MB 原样保留，wjhpq/u5s2/busobt 三条任务仍在。
- 未做：真实任务体验确认；回退方式：把备份的 0.5.7-test.240.app 移回 /Applications。
### 第二轮装机与 Review 验证（v0.5.7-test.249，2026-11-13 00:49–00:54）

- 发布：run 43259491260（channel=test，base_version=0.5.7）全部 job 成功，预发布 **v0.5.7-test.249**；DMG SHA256 校验通过。
- 装机：旧包 0.5.7-test.247 备份到 `KaneoPilot-install-backups/20261113-004958-0.5.7-test.247`，安装 249 并清除 quarantine；`cli_review.py` 内 `recovered_error_events`/`completed_after_failure` 均在。
- 重跑 Review：`resolve action=resume`（review_blocked → in_review，请求 98e2c569）→ Review 正常登记结论，不再出现「Codex Review 失败（退出码 0）」。
- 新报告 `reports/busobtd4z291blpp8ynswzzm/ea3e9283-....md` 的结论：未发现可确定代码缺陷；阻塞只剩一条自动验证缺口（subsonic_api 离线解析改了 6 个依赖，verify 通过不能代表冻结 lock 环境），并明确写「不能归为人工听感或设备体验」。Windows 原生、窗口观察、音频听感、真实控件切歌全部列为「待体验项（不作为本轮阻塞）」。
- 该轮 codex 事件流无 error 事件，恢复路径由单元测试覆盖；`run_codex` 的判定修复已在 249 中生效。

剩余（新任务）：executor 需要在与冻结 lock 一致的依赖环境下重跑 subsonic_api 检查并保留解析版本证据；wjhpq 的「记录/证据收尾轮无源码交付物」口径仍需产品决定。
### Review 口径实测（busobt，2026-11-13）

用 `supplement-review` 带用户说明重新发起 busobt 的 Review（新 prompt 已随 0.5.7-test.247 生效）。模型输出（executor-sessions/review/b8e9477f-.../last-message.txt）显示规则按预期生效：

- 结论仍是 `blocked`，但理由已不含 Windows 原生测试、人工听感、人工窗口观察；模型明确写「自动验收仍有证据缺口」「不属于人工体验」。
- 剩余阻塞只有两条自动验证缺口：subsonic_api 的通过结果不对应冻结 lock（离线解析降级 6 个依赖）；原计划的集成测试要求经真实控件操作，实际测试直接调 `service.next()`。

**但该轮被丢弃**：`cli_review.run_codex` 只要事件流出现 `error`/`turn.failed` 就置 failed，即便 Codex 已重连恢复、退出码 0、最终 JSON 完整有效。本次事件流有 "Reconnecting... waiting for network" 后仍产出有效结果，却被判「Codex Review 失败（退出码 0）」，任务保留旧报告与旧 review_blocked。

待办（下一任务）：① 该 runner 应在「退出码 0 且最终结构化输出合法」时保留结果，把 error 事件降为证据；② busobt Review 需在修复后重跑。

### 装机后真实任务复测（wjhpqxfqxr3084vxtc19e1uo）

2026-11-12 23:39 由用户授权 `resolve replan`（actor=user），同任务同材料重新规划：

| 指标 | 修复前（21:20 那轮及 12:40 两轮） | 修复后（23:39 这轮） |
| --- | --- | --- |
| 规划材料 | 222 文件 / 103.7 MB | **108 文件 / 1.13 MB** |
| 规划耗时 | 3 次 `planner_timeout`（1800s） | **4 分 27 秒**（15:39:46 claim → 15:44:13 publish） |
| 计划正文 | 426 行 | **24 行**（文件 49 行含 frontmatter） |
| 人工验收 | 混在正文与 verify 里 | 独立 `## 人工验收（不阻塞）` 6 条 |
| verify | 含设备/证据脚本 | 3 条可自动执行（dart format / flutter analyze / flutter test） |
| 发布记录 | 无篇幅指标 | `plan_body_lines=24` |

自动链路照常推进：15:44:47 `confirm_plan` → 15:44:52 dispatch → 15:44:56 `pi_desktop` 执行轮次开始，任务进入 `monitor/running`。


- 体验入口：改动在源码树 `core/`；正在运行的 `/Applications/KaneoPilot.app` 仍是旧副本，需要走云端 `Packages` 构建安装后才生效。
- 尚未验证：真实任务重跑（规划材料体积、verify 拒收、提问超时）需要装机后复测；本机全量测试套件未跑完（见上表）。
- 现场证据：修复期间任务 `wjhpqxfqxr3084vxtc19e1uo` 的第 4 次规划又于 2026-11-12 21:50:42 以 `planner_timeout（1800s）` 失败，与 T1 指向的原因一致。
- 回退：四项均为独立小改，按文件回退即可；`docs/protocol.md` 中 2026-11-12 标注段落一并回退。
