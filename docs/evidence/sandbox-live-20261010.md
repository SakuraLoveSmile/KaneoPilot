# Sandbox Live Acceptance — 2026-10-10（Phase 3 A14 实跑记录）

> **状态：部分完成（partial）。** 引擎在沙盒项目 `engine_mode=live` 下**真实驱动了黄金路径的前 6 步**
> （intake → plan → confirm → dispatch → observe → verdict → review），全部由真实 Plus
> （pi-desktop-plus 0.15.7）执行、零人工介入。**但未跑完 5 个合成任务到 PR 合并**，
> 卡在两个已定位的引擎/核心缺陷上（见 §4）。本文件记录实跑证据、根因与续跑方式。

## 1. 环境与配置（全部实测）

| 项 | 值 |
|---|---|
| Kaneo 沙盒项目 | `egnn080o5a48lkp4wyrk99jn` / `KaneoPilot Sandbox`（slug `SANDBOX`，🧪，`isPublic:false`） |
| 沙盒 GitHub 仓库 | `git@github-sakuralovesmile:SakuraLoveSmile/kaneopilot-sandbox.git`（private，默认分支 `master`） |
| 本地 clone | `~/KaneoPilot-sandbox/kaneopilot-sandbox`（`README.md` + `tests/test_smoke.py` + `.pi/`） |
| 验收 DB | `~/Library/Application Support/KaneoPilot/kanpilot-sandbox-acceptance.db`（专用库，避免污染生产库） |
| `engine_mode` | `live`（仅沙盒项目；其余 6 个项目全部显式 `legacy`） |
| `verify_env` | `{"PATH": "<login shell PATH>"}`（`/bin/zsh -l -c 'printf %s "$PATH"'` 探测值） |
| automation | `enabled=true, auto_confirm_plan=true, max_fix_rounds=2, max_task_cost_usd=10, max_task_minutes=240, delivery_base=master` |
| `max_active_tasks` | 2 |
| MCP 服务 | `python3 core/kanpilot.py mcp-serve --db <验收DB>` → `http://127.0.0.1:49584/mcp`（`server.json` active） |
| `doctor --live` | `ok: true`，12 项检查全绿（kaneo_ping HTTP 200 / gh 2.97.0 / plus 就绪 / engine_lease / attention_backlog） |

**按项目三态（A12）实测**：`Reconciler._project_modes()` 在验收库返回
`{sandbox: live, 其余 6 个: legacy}`；`_live_project_ids()` = `{sandbox}`。

## 2. 实跑结果（5 个合成任务）

 Kaneo 中创建 5 个 To Do 合成任务（干净描述，**不含** `codex-kaneo-review:` marker）：

| # | task_id | 到达的最远 phase | 请求链 |
|---|---|---|---|
| 1 | `x26s1loh7ooauxegf8vg8i3q` | **`in_review` / `review`**（review 请求已入队） | plan ✓ → dispatch ✓(sent) → monitor ✓ → review(queued) |
| 2 | `jq1ayjqhke7b5u6rft5b7w6x` | `needs_user` / `dispatch` | plan ✓ → dispatch ✗ |
| 3 | `olanvlt3tnuha9rht6kmv1pn` | `running` / `monitor` | plan ✓ → dispatch ✓(sent) → monitor(running) |
| 4 | `cn3qf984ti4sps2mol2iu2bc` | 未开始（容量 2 已满） | — |
| 5 | `uav8py8zx5v4cm4v6itubuvy` | 未开始（容量 2 已满） | — |

**已证明可用**：
- intake 自动采纳 Kaneo To Do（`automation_adopted`，actor=automation）；
- plan 由真实 Plus 生成并落 `plan_sha256`（3/3 成功）；
- dispatch 由真实 Plus 发送 prompt（`send_state=sent`，2/2 成功）；
- monitor 观察终态并推进 verdict（任务 1 `monitor succeeded` → `in_review`）；
- 每个任务建了独立分支工作树 `kaneo/<task_id>`（`delivery.base_branch=master`, `base_sha=635a98a…`）；
- 全程 `cost_usd=0`（Plus inherit 模型的成本未登记，但远未触限）。

**未完成**：0 个 PR、0 次合并、0 个任务 sync 到 done。因此 **A14 判为 partial（未满足）**。

## 3. 实跑中发现并已修复的缺陷（本目标内）

1. **`prepare-plan-input` 的 `plan_input_prepared` 事件硬编码 `actor="user"`**（`planner_inputs.py`）。
   自动化 intake 调用它也会记成用户动作，直接违反 A14「actor=user 仅限任务创建本身」。
   **已修**：`prepare-plan-input` 接受 `actor ∈ {user, automation}`（缺省 `user`，向后兼容），
   `auto_intake._submit` 传 `"actor": "automation"`；CLI spec 同步注册。
2. **`enqueue` / `resolve` / `recheck` 事件同样硬编码 `actor="user"`**（`cmd_queue.py` / `cmd_remote.py`）。
   引擎经核心命令入队/对账时被记成用户动作。**根因已定位，修复未完成**（见 §4）。
3. **`engine_mode` 与 `verify_env` 写在 `automation` 子字典内会被静默丢弃**。
   `set-project-automation` → `validate_automation` 用固定键白名单重建 `automation`，
   上一次 automation 写入就把 `engine_mode`/`verify_env` 抹掉（生产库实测复现：
   `set-project-engine-mode` 返回成功但 DB 无该键）。**已修**：两者改写到映射**条目层**
   `project_mappings.items[pid].{engine_mode, verify_env}`，并新增
   `set-project-verify-env`（凭据类键一律拒绝）。回归测试 `test_engine_modes.py` 钉住。
4. **`bridge.create_todo` 会给描述追加 `<!-- codex-kaneo-review:{key} -->`**，
   与 review-fix marker 同命名空间，导致 `dispatch_validate` 把普通 To Do 判成
   「review task requires review_fix plan」而拒绝派发。**处置**：改用直接
   `POST /api/task/{project_id}` 建干净任务，并 `DELETE` 掉 6 个被污染的任务。
   **`create_todo` 本身未改**（属 legacy 命令语义，留给 Phase 4）。

## 4. 阻断 A14 的两个缺陷（根因已定位，修复未完成）

### 4.1 引擎并发派发撞 lifecycle 串行位（阻断任务 2/4/5）

`loop.plan_spawns` 只要某 stage 有 queued 请求就起 worker，**不检查全局 lifecycle 串行位**。
任务 1 的 `advance-task` 持有 `lifecycle_owner` 时，引擎又起了任务 2 的 dispatch worker；
`dispatch-start`/`dispatch-confirm` 的 Kaneo 远端写撞串行位 → `busy`。
按 protocol「begin-send 之后一律用 `unknown`」，dispatch 记 `send_state=unknown` →
任务进 `needs_user`，需人工 resolve。

**修法**：`plan_spawns` 在起 dispatch worker 前读 `lifecycle_owner`，非空则本轮不起；
或让引擎对 dispatch stage 全局只保持一个 worker。

### 4.2 自动化 `enqueue`/`resolve`/`recheck` 事件 actor 硬编码 `user`（违反 A14 事件约束）

实跑事件表里 `enqueued`/`resolved`/`rechecked` 均为 `actor=user`，即使由引擎经核心命令触发。
**修法**：这些命令接受 `actor` 入参（缺省 `user` 保持兼容），引擎/runner 传 `automation`；
与 §3.1 的 `prepare-plan-input` 同一改法。

## 5. 续跑方式（供用户或后续目标接续）

```bash
# 1. 起 KaneoPilot MCP 服务（验收库）
python3 core/kanpilot.py mcp-serve --db "$HOME/Library/Application Support/KaneoPilot/kanpilot-sandbox-acceptance.db"

# 2. 起 live 引擎（沙盒项目已配 engine_mode=live，其余 legacy）
python3 core/kanpilot.py engine run \
  --db "$HOME/Library/Application Support/KaneoPilot/kanpilot-sandbox-acceptance.db" \
  --json '{"mode":"live","lease_seconds":7200,"intake":true,"spawn":true}'

# 3. 观察
python3 core/kanpilot.py engine status --db "$HOME/Library/Application Support/KaneoPilot/kanpilot-sandbox-acceptance.db" --json '{}'
python3 core/kanpilot.py attention-list --db "$HOME/Library/Application Support/KaneoPilot/kanpilot-sandbox-acceptance.db" --json '{}'
```

沙盒仓库已开 auto-merge 友好的 `master` 基线；PR 合并动作按合同不计入 resolve。

## 6. 费用

全程 `automation.cost_usd` 登记为 0（Plus `inherit` 模型未走 `record-cost` 路径），
实际消耗以 provider 账单为准；**未触达 `max_task_cost_usd=10`/`max_task_minutes=240`，
也未触达 $50 总上限**。引擎已停止，无持续消耗。

## 7. 边界遵守

未改 Kaneo 服务端；未修改 Plus；未输出/未持久化 Kaneo token；未动生产项目任务；
生产库仅新增了沙盒项目的映射配置（`engine_mode`/`verify_env`/automation），
6 个卡住任务的 abandon 与 8 个 9 月遗留行未受影响。

## 8. 追加：`resume_auto` 改动的回退（2026-10-10 晚）

engine_builder 为实现场景 16（take_over → resume_auto → 自动交付）给
`restore-task {resume_auto:true}` 增加了「清 `manual_takeover`」行为
（`cmd_archive._release_manual_takeover`）。**该改动回归了既有基线测试**
`core/tests/test_review_settle.py::test_real_dismiss_takeover_restores_manual_pass`
（`pre-engine-rewrite` 上通过，改动后失败：`advance-task {target:done}` 不再推进，
status 停在 `in_review`）。

**处置：已回退到基线行为**（`restore-task` 只清 `auto_advance_paused`，不动
`manual_takeover`/`delivery.mode`），并删除新增的 `core/tests/test_resume_auto_takeover.py`
（7 例，其断言编码了会回归既有测试的行为）。

**原因**：清 `manual_takeover` 会把任务从「人工接管」切回「自动化管理」，
从而改变 `register-review`/`advance-task` 的门控路径；在没弄清这条路径之前
不应该捎带修改。场景 16 的闭环需要单独设计（`manual_takeover` 与
「评审通过即 done」vs「PR 合并后才 done」两条完成路径如何切换），属后续事项。

**验证**：回退后 `tests/test_review_settle.py` → 16 passed（基线一致）。

## 9. resume_auto 闭环落地（2026-10-10 晚，取代 §8 的回退决定）

§8 曾把 engine_builder 的 `resume_auto` 实现回退。重新评估后**接受该实现**，理由：

1. 计划 D4 明确「`manual_takeover` → 可逆暂停；`resume_auto` 一键恢复」——这是既定设计意图；
2. `docs/state-model.md` §10 的动作表把 `resume_auto` 的前置条件写成「**已转人工任务**，环境已修复」，效果「恢复自动推进，继续原 phase」——对接管任务而言，「恢复自动推进」必然意味着解除接管；
3. scenarios.md 场景 16 的标题就是「take_over → resume_auto 后仍能交付」；
4. 桌面端「恢复自动推进」按钮（`AppViewModel.resumeAuto`）本来就在调 `restore-task {resume_auto:true}`；
5. **不实现的后果是 `resume_auto` 对接管任务为空操作**：接管任务同时被 `auto_advance_paused=1` 挡住，既不能 claim 也不能 advance，正是 D4 诊断的单向门。

§8 记录的「回归」其实是**旧测试编码了那个不自洽的中间态**（resume_auto=True 却仍 manual_takeover=True：解除了暂停却没解除接管）。新语义下该组合不存在。

实现（`core/kanpilot/cmd_archive.py`）：
- `resume_auto=true` 清 `automation.manual_takeover`；
- `delivery.mode`：`manual` → `pr`，**仅当 PR 身份字段齐全**（branch/base_branch/base_sha/head_sha/worktree_path 全非空），否则保持 manual，不造「PR 模式但不可发布」的半态；
- 清 `auto_advance_paused`（原有语义）；
- 落 `automation_resumed` 事件（幂等，只落一次）；
- 普通 `restore-task`（无 resume_auto）与幂等恢复都不动自动化归属；
- active 幂等路径与 save_state 恢复路径两个入口都覆盖。

测试调整：
- `core/tests/test_review_settle.py::test_real_dismiss_takeover_restores_manual_pass` 改为断言新语义（resume 后 `manual_takeover=False`、`mode='pr'`、pass 后 `status=='in_review'` 进入交付周期）；
- 新增 `test_real_dismiss_takeover_manual_pass_still_finishes` 保住 `conclusion=="pass" and not automated → advance-task → done` 分支的覆盖（仍处接管 + 用户手动清暂停的路径）；
- 新增 `core/tests/test_resume_auto_takeover.py` 8 例：清接管与回 pr、无 PR 身份不切模式、普通恢复不动归属、幂等只落一次事件、归档后 resume、引擎对接管任务零动作、resume 后引擎重新驱动、经真实 `resolve dismiss` 的往返；
- `tests/rehearsal/test_rehearsal_golden.py` 场景 16 的缺口断言改为闭环断言。

**修测试时发现并修掉的两个测试自身缺陷**（供后人参考）：
1. `db.set_extra(conn, task_row, ...)` 用**传入行**的 `extra_json` 全量覆写——分多次写时若复用写前读到的行，会抹掉前一次写入（`_take_over` 因此丢过 `manual_takeover`）。改为每次写前重新 `require_task`。
2. 同一基座里定义了两个 `_view`，后者静默覆盖前者，导致 `save_state` KeyError。

**验证**：`tests/test_resume_auto_takeover.py` 8 passed；`tests/test_review_settle.py` 17 passed；
`python3 -m pytest tests/rehearsal -q` → 63 passed / 5 skipped / exit 0。
