# KaneoPilot 调度台集成演练场景清单（冻结）

- **清单版本**：v1.1（2026-10-12 自动补救接线）
- **冻结日期**：2026-10-10
- **权威输入**：`docs/state-model.md`（§7 phase×condition、§8 失败阶梯、§9 黄金路径、§10 用户动作）、`docs/protocol.md` §5.27（engine run/status、attention-list、8 个用户动作、session-view、doctor --live）、`docs/automation-contract.md`（调度容量/预算时限/同任务修复轮/交付/待处理原因）、`core/kanpilot/pi_desktop_worker.py` 与 `core/kanpilot/pi_desktop_backend.py`（Plus 响应真实字段形状）。
- **状态**：冻结。实现分歧先改规格再动工（AGENTS.md）；本清单只描述"应然"，新增/改动场景须主 agent 评审后升版本。

## 与测试文件的对应关系

- 每个场景对应一个用例，命名固定为 `test_scenario_<场景名>`（`<场景名>` 用下方各节的稳定英文 snake_case 标识）。
- 建议按主题拆入 `tests/rehearsal/test_*.py`（每节给出"目标测试模块"），也可合并为单模块；命名约定不变。运行：`python3 -m pytest tests/rehearsal -q`。
- 测试基座复用 `tests/conftest.py`：`fake_kaneo`（`fail_next`/`fail_route` 注入 Kaneo 500/超时、`set_status`/`add_task`）、`factory`（sync_task/publish/make_ready/make_running/make_in_review/make_needs_user(send_state=…)）、`cli`、`tmp_env`、DB 后门（`expire_lease`→派生 lost、`set_task_send_state`、`set_setting`、`set_task_status`）。
- 本套件在 rehearsal 侧**新增两类本地假件**（不改 conftest）：假 Plus MCP（以 `pi_desktop_backend.MCPClient` 同口径回放 `pi_agent_status`/`pi_turn_get`/`pi_session_get`，形状见 `fixtures/plus_*_real.json`）与假 `gh`（PATH 上可编程桩，覆盖登录/超时/缺失）。
- **引擎门控（硬约束）**：涉及 `engine run`/`engine status`/`attention-list` 等 §5.27 命令的场景（标注"引擎门控"）在核心尚未实现这些命令时，用例必须以明确的"引擎未实现"失败/xfail（带 reason）收场，**不得**出现导入错误或 collection 错误。用例只经 `cli` 调用命令面并断言返回的错误信封（`error`/`code`），不在导入期触碰未实现的命令符号。
- 所有场景只写隔离临时库/仓库（`tmp_env`），不触真实 Kaneo、真实 Plus、真实 `gh`；不输出/不持久化任何 Kaneo token；所有 ID 脱敏。

## 故障注入开关词表（固定）

测试实现按下列名字注入故障；每场景"注入故障"列只能取这些名字或 `none`。多个开关可用于同一场景（分用例）。

`kaneo_timeout`、`kaneo_500`、`plus_unreachable`、`plus_timeout`、`gh_missing`、`gh_timeout`、`gh_not_logged_in`、`push_rejected`、`pr_closed_unmerged`、`git_slow`、`git_hang`、`plus_send_lost`、`verify_fail`、`empty_diff`、`engine_kill_at_step`、`lease_expired`

## 场景清单

### 1. golden_path_to_pr_merge
- **场景名**：`golden_path_to_pr_merge`
- **注入故障**：`none`
- **预期行为**：Kaneo To Do → intake → plan（publish-plan）→ confirm → dispatch（prepare→begin-send→start→confirm）→ observe → verdict（有效 diff 排除 `.pi/**`，核心亲跑 verify）→ commit（delivery-commit）→ review ⇄ fix(≤2 同分支) → PR → delivery-sync → done 全链一次走通；`task.status` 依次落到 `ready→running→in_review→done`，phase×condition 与 state-model §7 一致；每个外部副作用先落 `runtime_operations` 回执再执行；done 时 Kaneo 列 = done。
- **规格条款**：`state-model §9`、`state-model §7`、`automation-contract §同任务修复轮`、`protocol §5.27`
- **目标测试模块**：`test_rehearsal_golden.py`

### 2. plus_unreachable_before_start
- **场景名**：`plus_unreachable_before_start`
- **注入故障**：`plus_unreachable`
- **预期行为**：dispatch-start 就绪探测（连接文件缺失/不可达/缺工具）失败 → 派发不启动 worker、不 `pi_session_create`、不下发 prompt；任务停在可安全态并记 `validation_failed`/环境类阻塞（中文原因，不含 token/连接文件全文）；不创建第二会话、不重发。
- **规格条款**：`protocol §5.23.3`（`load_connection`/`probe_connection`）、`state-model §8`（阶梯 2）
- **目标测试模块**：`test_rehearsal_plus.py`

### 3. plus_unreachable_during_run
- **场景名**：`plus_unreachable_during_run`
- **注入故障**：`plus_unreachable`（`plus_timeout` 变体同路）
- **预期行为**：运行中只读调用不可达 → 只读重连最多 3 次且总计 ≤10s、重读同一连接文件；恢复后 turn 仍 `running` 或无法确认身份/终态 → `send_state=unknown`/失联记录，**绝不自动重复 `pi_session_create` 或 `pi_agent_prompt`**；同一 `run_id` 超时/重启后不得再次发送；`currentTurnId` 仅作候选证据。
- **规格条款**：`protocol §5.23.3`（T3 Plus R1 失败约束）、`state-model §8`
- **目标测试模块**：`test_rehearsal_plus.py`

### 4. send_state_unknown_no_resend
- **场景名**：`send_state_unknown_no_resend`
- **注入故障**：`plus_send_lost`
- **预期行为**：prompt 响应丢失 → `send_state=unknown`，不算失败、按 `failure_stage` 归列并附阻塞摘要；UI **不出现普通重试**；仅提供 `resolve` 的 `restore`（认定未发送）/`accept_duplicate`（知情接受可能重复）/`mark_sent`（人工确认已发）；核心在任何自动路径都不重发、不建第二会话；`send_state=unknown` 优先于一切 code 判阻塞。
- **规格条款**：`protocol §4.5-6`、`protocol §5.23.3`、`protocol §5.27.4`、`state-model §7`
- **目标测试模块**：`test_rehearsal_plus.py`

### 5. empty_diff_backup_model_then_attention
- **场景名**：`empty_diff_backup_model_then_attention`
- **注入故障**：`empty_diff`
- **预期行为**：执行终态但有效 diff 为空（空转）→ 同计划自动重试一次：配置了备用模型则用备用模型快照（探针失败即 `validation_failed`、不静默换模型、事件记 `model: fallback`），未配置则沿用主模型重试（记 `model: primary`）并提示配置；重试后仍空 diff → 升级 needs-you（attention 单一来源），不再自动重试；受 `max_fix_rounds ≤2` 约束。
- **规格条款**：`state-model §8`（质量/阶梯 3）、`automation-contract §同任务修复轮`、`protocol §5.27.3`
- **目标测试模块**：`test_rehearsal_golden.py`

### 6. verify_fail_fix_round_passes
- **场景名**：`verify_fail_fix_round_passes`
- **注入故障**：`verify_fail`
- **预期行为**：核心亲跑 verify 命令失败 → verdict 未确认，进入同任务分支修复轮（`fix_round+1`）；修复后重新执行/验证并通过 → 继续 commit→review→delivery，最终 done；`fix_round ≤2`，修复固定基线与已登记提交。
- **规格条款**：`automation-contract §执行验证证据`、`automation-contract §同任务修复轮`、`state-model §8`、`state-model §9`
- **目标测试模块**：`test_rehearsal_golden.py`

### 7. verify_fail_exhausted_needs_you
- **场景名**：`verify_fail_exhausted_needs_you`
- **注入故障**：`verify_fail`
- **预期行为**：verify 连续失败，`fix_round` 达到 `max_fix_rounds(=2)` 上限 → 升级 needs-you（阶梯 4），进入 attention inbox 单一来源，不再自动补救；`allowed_actions` 含 `replan`/`dismiss` 等，由用户 resolve 后回对应 phase。
- **规格条款**：`state-model §8`（阶梯 3 超出→阶梯 4）、`state-model §9`、`automation-contract §同任务修复轮`、`protocol §5.27.3`
- **目标测试模块**：`test_rehearsal_golden.py`

### 8. review_two_changes_requested_then_pass
- **场景名**：`review_two_changes_requested_then_pass`
- **注入故障**：`none`（评审结论由假 review 后端脚本化：changes_requested ×2 后 pass）
- **预期行为**：评审 `changes_requested` → 同任务分支修复轮（≤2 轮）；第 1、2 轮仍 changes_requested 但未超限 → 继续 `fix_round+1`；第 2 轮修复后评审 `pass` → 登记 pass 并 advance-task done；全程同分支、不新建任务分支，`fix_round` 不超过 2。
- **规格条款**：`automation-contract §同任务修复轮`、`state-model §8`、`state-model §9`、`state-model §7`（review/repairing）
- **目标测试模块**：`test_rehearsal_golden.py`

### 9. review_fix_rounds_exhausted_needs_you
- **场景名**：`review_fix_rounds_exhausted_needs_you`
- **注入故障**：`none`（假 review 后端持续 changes_requested）
- **预期行为**：评审连续 changes_requested 且 `fix_round` 达到 2 轮上限 → 升级 needs-you（阶梯 4），进入 attention inbox；提供 `replan`（修复轮用完）/`review_recovery`（历史误退时恢复 in_review）/`dismiss`；不越界自动修复。
- **规格条款**：`state-model §8`、`state-model §9`、`state-model §10`（review_recovery/replan）、`automation-contract §同任务修复轮`
- **目标测试模块**：`test_rehearsal_golden.py`

### 10. transient_backoff_capped_then_escalate
- **场景名**：`transient_backoff_capped_then_escalate`
- **注入故障**：`kaneo_timeout`（`kaneo_500` 归同一阶梯 1 瞬时段）
- **预期行为**：瞬时失败（network timeout / `remote_unavailable` / HTTP 500）归阶梯 1 → 指数退避重试，**上限 2 次/阶段**；重试间隔指数增长；超过 2 次仍失败 → 升级阶梯 2（环境），停止自动重试、明确原因等待用户；退避计数可按阶段重置，不跨阶段累计超限。
- **规格条款**：`state-model §8`（阶梯 1/阶梯 2）、`protocol §5.27.1`（退避重启口径）
- **目标测试模块**：`test_rehearsal_engine.py`

### 11. engine_killed_at_step_no_duplicate_effects
- **场景名**：`engine_killed_at_step_no_duplicate_effects`（引擎门控）
- **注入故障**：`engine_kill_at_step`
- **预期行为**：引擎在每个 step 边界（dispatch/monitor/plan/review/delivery）被杀 → 重启后经 `runtime_operations` 回执（`scope_id+operation_id` 唯一，`prepared→unknown→accepted/rejected`，`unknown` 不自动重发，同 ID 同 hash 返回已有安全结果、异 hash conflict）+ 世界探测对账，恢复现场；**不重复任何已执行的外部副作用**（不二次 `pi_agent_prompt`/push/PR/评论）；失联请求只读派生 lost，不自动释放/重启。
- **规格条款**：`protocol §5.27.1`（单实例租约+心跳+退避重启+失联判定）、`state-model §7`、runtime_operations 回执契约
- **目标测试模块**：`test_rehearsal_engine.py`

### 12. lease_expired_per_step_reclaim
- **场景名**：`lease_expired_per_step_reclaim`（引擎门控）
- **注入故障**：`lease_expired`
- **预期行为**：每类 step 的 claimed 请求租约过期且无心跳 → 读取层派生 lost（`display_status=执行端失联`），**不自动释放原请求、不自动重启**；引擎按 step 类型幂等重新入队新请求（回执去重），任务回到阻塞前可安全态；原 lost 标记只能经 `resolve release_lost` 解除；dispatch 中途→needs_user 且 send_state 保持，monitor/plan/review 中途→回原 status。
- **规格条款**：`protocol §5.27.1`、`protocol §4.3`、`protocol §5.27.4`（release_lost）、`state-model §7`
- **目标测试模块**：`test_rehearsal_engine.py`

### 13. push_rejected_no_force
- **场景名**：`push_rejected_no_force`
- **注入故障**：`push_rejected`
- **预期行为**：delivery-publish 采用**非强制推送**；push 被拒（远端 non-fast-forward）→ 记录失败并按阶梯归路（网络类→阶梯 1 退避；分支偏离 `branch_diverged`→阶梯 3 有界补救）；**绝不 force-push**；无法安全推进 → needs-you 附阻塞摘要，不伪造 PR/合并。
- **规格条款**：`automation-contract §交付：提交、推送、PR 与合并`、`protocol §5.27`（delivery-publish 非强制推送）、`state-model §8`
- **目标测试模块**：`test_rehearsal_delivery.py`

### 14. gh_not_logged_in_blocks_delivery
- **场景名**：`gh_not_logged_in_blocks_delivery`
- **注入故障**：`gh_not_logged_in`（`gh_missing`/`gh_timeout` 同属阶梯 2 环境失败，分用例覆盖）
- **预期行为**：`gh` 未登录/缺失/超时 → 交付阶段环境失败，记明确 `failure_code`（阶梯 2），**不自动重试、不升级失败**，等待用户修复环境后手动 resume；中文原因不含任何 token。
- **规格条款**：`state-model §8`（阶梯 2 环境失败：`gh_missing` 等）、`automation-contract §交付`、`protocol §5.27`
- **目标测试模块**：`test_rehearsal_delivery.py`

### 15. pr_closed_unmerged_not_done
- **场景名**：`pr_closed_unmerged_not_done`
- **注入故障**：`pr_closed_unmerged`
- **预期行为**：delivery-sync 读到 PR 已关闭但未合并 → **不得**标记 done、不得伪造合并；任务保持/回到可安全态并记阻塞（needs-you），提供对应 `allowed_actions`；只有 PR 已合并（或 Kaneo 已 done）才 advance-task done。
- **规格条款**：`protocol §5.27`（delivery-sync）、`state-model §9`（delivery：合并后才 done）、`state-model §7`
- **目标测试模块**：`test_rehearsal_delivery.py`

### 16. take_over_then_resume_auto_delivers
- **场景名**：`take_over_then_resume_auto_delivers`
- **注入故障**：`none`
- **预期行为**：自动化任务出现阶梯 3/4 失败 → `take_over` 置 `manual_takeover=true`、`delivery.mode="manual"`、回到 ready/needs_plan；环境修复后 `resume_auto`（save_state=active 且 auto_advance_paused=1）清除暂停、继续原 phase；最终仍能经手动→自动交付到达 done；动作均写 `event kind=resolved`（actor/action/note），危险动作二次确认。
- **规格条款**：`state-model §10`（take_over/resume_auto）、`protocol §5.27.4`
- **目标测试模块**：`test_rehearsal_golden.py`

### 17. budget_and_time_cap_triggered
- **场景名**：`budget_and_time_cap_triggered`
- **注入故障**：`none`
- **预期行为**：本段时长 ≥ `max_task_minutes` 或本段成本 ≥ `max_task_cost_usd`（上限为 null 时忽略成本项）→ 预算门拦截自动确认/推进，记 `auto_budget_exceeded`，进入 needs-you；`allowed_actions` 含 `resume`（在新预算段内）/`replan`/`mark_done`/`dismiss`；不超限时才允许自动确认执行。
- **规格条款**：`automation-contract §计划自动确认`（预算门）、`automation-contract §待处理原因、处置与事件`（auto_budget_exceeded）、`state-model §8`
- **目标测试模块**：`test_rehearsal_engine.py`

### 18. concurrency_two_slots_four_tasks_no_deadlock
- **场景名**：`concurrency_two_slots_four_tasks_no_deadlock`
- **注入故障**：`none`
- **预期行为**：`max_active_tasks=2` 下放入 4 个可自动推进任务 → 同时占用活跃槽 ≤2，其余排队等待；随任务完成槽位释放、排队任务被领取，全程无死锁、无重复领取；容量 = `max_active_tasks − 占用数`；串行位（state-model §5）不被并发突破；plan_wanted 任务不受容量限制但也受串行位约束。
- **规格条款**：`automation-contract §调度循环（ExecutorScheduler）`（容量）、`state-model §5`、`state-model §7`
- **目标测试模块**：`test_rehearsal_engine.py`

### 19. migration_v5_to_v6_gc_reversible
- **场景名**：`migration_v5_to_v6_gc_reversible`
- **注入故障**：`none`
- **预期行为**：显式 `migrate` 先把 v5 库做一致性备份再升级到 v6，**保留旧数据**（无破坏性 drop）、幂等；`runtime_operations`/旧 session 等孤立数据的 GC 只清理确认无引用的数据、不动活跃状态；升级可回退（凭一致性备份恢复）；普通命令/服务不隐式升级旧库（缺版本或不明已有表拒绝初始化），只有显式 `migrate` 允许升级。
- **规格条款**：`protocol §5.12`（migrate 备份/幂等/不隐式升级）、`state-model §1.1`、`state-model §1.2`、`state-model §6`
- **目标测试模块**：`test_rehearsal_engine.py`

### 20. attention_list_contract_snapshot
- **场景名**：`attention_list_contract_snapshot`（引擎门控）
- **注入故障**：`none`
- **预期行为**：`attention-list` 只读聚合：源 = `needs_user` 任务 + 活动失败请求（`failed`/`lost`）+ 最近失败请求；返回 `{"items":[{task_id, project_name, title, failure_code, failure_stage, failure_reason, blocked_summary, allowed_actions, attention_state}]}`；**同一任务不重复出现**；按阶梯优先级排序（4 > 3 > 2 > 1）；`save_state` 缺省 `active`，`include_historical=true` 时含归档/回收站历史阻塞；形状与字段名单一来源快照锁定。
- **规格条款**：`protocol §5.27.3`、`state-model §8`
- **目标测试模块**：`test_rehearsal_engine.py`

### 21. doctor_live_all_green
- **场景名**：`doctor_live_all_green`
- **注入故障**：`none`
- **预期行为**：假 Kaneo + 假 Plus + 假 gh 就绪时 `doctor --live` 返回 `ok=true`，`checks` 含 `python`/`db_writable`/`credentials`/`plan_root`/`repos_root`/`state_root`/`kaneo_ping`（`ok=true`、`detail=HTTP 200`、`skipped=false`）/`engine_lease`/`attention_backlog`；`--live` 强制连通性检查；`online` 缺省 false 时 `kaneo_ping skipped=true`。
- **规格条款**：`protocol §5.27.6`
- **目标测试模块**：`test_rehearsal_doctor.py`

### 22. doctor_live_kaneo_timeout_vs_failure
- **场景名**：`doctor_live_kaneo_timeout_vs_failure`
- **注入故障**：`kaneo_timeout`
- **预期行为**：Kaneo ping 超时 → `kaneo_ping` 检查带 `timed_out=true`（超时态），与真实失败（HTTP 非 200/连接拒绝，`ok=false` 且 `timed_out` 为 false/缺失）**明确区分**；`ok` 反映该区分，超时不与"服务端返回错误"混为一类。
- **规格条款**：`protocol §5.27.6`（kaneo_ping timed_out 标记）
- **目标测试模块**：`test_rehearsal_doctor.py`

### 23. doctor_live_plus_unreachable_no_token
- **场景名**：`doctor_live_plus_unreachable_no_token`
- **注入故障**：`plus_unreachable`（`plus_timeout` 变体同路）
- **预期行为**：Plus 连接文件缺失/超时/权限不当/token 旁文件世界可读 → 对应检查 `ok=false` 且 `detail` 为脱敏中文原因；**不输出 token、Authorization、连接文件全文**；凭据检查只验文件可读、含 baseUrl+token，不回显 token。
- **规格条款**：`protocol §5.27.6`（凭据不输出 token）、`protocol §5.23.3`（`load_connection` 校验与脱敏）
- **目标测试模块**：`test_rehearsal_doctor.py`

### 24. git_probe_slow_not_false_not_git_repo
- **场景名**：`git_probe_slow_not_false_not_git_repo`
- **注入故障**：`git_slow`（慢但正常）与 `git_hang`（真超时）分用例
- **预期行为**：冷启动/大盘仓库下 `git` 进程本身可能超过历史 70ms 探测上限 → 慢但正常完成的 git **不得**被误报 `not_git_repo`；真超时/挂起必须与 `not_git_repo` 区分（不同 `failure_code`：超时归可区分码，`not_git_repo` 仅用于确非 git 仓库）；探测超时口径见 `automation_policy.git_probe_timeout`。
- **规格条款**：`state-model §8`（`not_git_repo` 阶梯 2）、`automation_policy.git_probe_timeout`
- **目标测试模块**：`test_rehearsal_plus.py`

### 25. real_shape_fixture_staleness_gate
- **场景名**：`real_shape_fixture_staleness_gate`
- **注入故障**：`none`
- **预期行为**：夹具新鲜度门读取 `fixtures/plus_*_real.json` 顶层 `_captured_at`（ISO 日期）与当天比较：≤30 天 → ok；>30 天 → warn/fail 并提示从真实 Plus 会话重新录制、更新 `_captured_at`；同时校验 `_provenance == "reconstructed_from_core_parsers"` 与 `payload` 形状（外层 MCP tools/call 结果 `{content,structuredContent,isError}`，内层 `status`/`turn`/`session` 视图字段与 worker 读取口径一致），确保重建样本未偏离核心解析口径。
- **规格条款**：fixtures `_captured_at` 元数据契约（本清单 §与测试文件）；形状权威 `protocol §5.23.3`（worker/backend 解析口径）
- **目标测试模块**：`test_rehearsal_fixtures.py`

---

## 附：场景 → 开关覆盖核对

| 开关 | 覆盖场景 |
|---|---|
| `kaneo_timeout` | 10、22 |
| `kaneo_500` | 10（同阶梯 1） |
| `plus_unreachable` | 2、3、23 |
| `plus_timeout` | 3、23（变体） |
| `gh_missing` | 14（同阶梯 2） |
| `gh_timeout` | 14（同阶梯 2） |
| `gh_not_logged_in` | 14 |
| `push_rejected` | 13 |
| `pr_closed_unmerged` | 15 |
| `git_slow` | 24 |
| `git_hang` | 24 |
| `plus_send_lost` | 4 |
| `verify_fail` | 6、7 |
| `empty_diff` | 5 |
| `engine_kill_at_step` | 11 |
| `lease_expired` | 12 |


### 26. review_tracked_build_artifact_is_accepted（A3 回归）
- 临时仓库基线跟踪 tracked.pyc；真实CLI review阶段的假模型程序修改其review工作树的该产物；保持冻结detached HEAD。
- 核心允许Review登记并继续PR合并→done，产物dirty不误报快照变化；源码或HEAD变化的反例由core守卫测试覆盖。
- 不调用真实模型或远端服务；仅真实本地Git、核心命令和回环假件。
