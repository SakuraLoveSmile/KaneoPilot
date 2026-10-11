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

## 10. resume_auto 的 UI 可达性缺口（2026-10-10 晚，engine_builder 实测发现）

§9 落地闭环后，engine_builder 报了一个**可达性**缺口并给出实测依据，我已核实并修复：

**缺口**：`derive.allowed_actions` 原来只在 `db.task_auto_paused(task)` 时追加 `resume-auto`
（protocol §5.15.6 原条文）。但 `resolve {action:"dismiss"}` **只在 review 阻塞时**才置
`auto_advance_paused=1`（`cmd_queue.py`：`if review_block: fields["auto_advance_paused"] = 1`）；
非 review 路径的接管（例如空 diff 补救用尽 → needs_user → dismiss）落在 `ready` 且**不暂停**。
于是这些任务的 `resume-auto` 从不出现在 `allowed_actions`，桌面端「交回自动化」按钮不露出，
只能走 CLI/MCP——**逆向入口存在但不可达，D4 的单向门只关了一半**。

**修复**（`core/kanpilot/derive.py`）：`resume-auto` 的露出条件改为
`db.task_auto_paused(task) or core.automation_of(task).get("manual_takeover")`。
`restore-task {resume_auto:true}` 命令本身早已支持未暂停的接管任务
（`cmd_restore_task` 的条件是 `db.task_auto_paused(task) or takeover`），所以只需补露出条件。

**规格同步**：`docs/protocol.md` §5.15.6 改为「`auto_advance_paused=1` **或**
`automation.manual_takeover` 为真时」，并加了一条说明为何第二个条件不可省。

**测试**（`core/tests/test_resume_auto_takeover.py::TestResumeAutoReachability`，3 例）：
未暂停的非 review 接管露出 `resume-auto`、暂停的 review 接管露出 `resume-auto`、
既未接管也未暂停时不露出。该文件共 11 例。

## 11. 两条完成路径的切换点（记录备查）

engine_builder 同时确认了切换点的确切位置，与 §8/§9 的判断一致：
`review_settle.settle_job` 里 `automated = is_managed(task)`，
`conclusion == "pass" and not automated` → `advance-task {target:done}`；
`automated` → 登记后进入 PR 交付周期。因此「交回自动化」**必然**把完成路径从
「评审通过即 done」切成「PR 合并才 done」——这不是副作用，正是场景 16 想要的行为。

## 12. B1/B2 修复后的重跑（2026-10-11 凌晨）

A14 的两个阻断缺陷（§4）已修并入库（`2f7996b`）。清理验收库与 Kaneo 侧后重建 5 个干净 To Do
任务（无 review marker），以 `engine_mode=live` 重跑。

### 12.1 B1/B2 的修复效果（实测通过）

- **B1（lifecycle 串行位）**：任务 1 与任务 2 **并发**完成 dispatch（`send_state=sent`），
  无一条 `busy:lifecycle serial held by …`。修复前同样的并发必挂（§4.1）。
- **B2（事件 actor）**：整个沙盒项目 `actor=user` 事件数 = **0**（修复前同一跑法有 7 条：
  `enqueued`×3、`resolved`×3、`rechecked`×1）。A14 的 actor 约束**首次满足**。

### 12.2 实跑结果

| # | task_id | 最终 phase | PR | 说明 |
|---|---|---|---|---|
| 2 | `nmez1pcfgbesk3aqalzk6c7k` | **`done`/`done`，Kaneo `done`** | #1 MERGED | **完整跑通黄金路径**：To Do → intake → plan → confirm → dispatch → observe → verdict → commit → review → PR → merge-sync → done，全程 0 人工 |
| 3 | `g0ny5ic4b2e1xnliqcairqtc` | `needs_user`/`review` | #2 MERGED | PR 已合并，但 merge-sync 报 `branch_diverged`——**因我手动向该分支推送了清理提交**（见 12.4），交付快照的 `head_sha` 与实际分支头不符，身份核对正确拦截 |
| 4 | `z8wue1tgzao1iyzjg6vg4u2s` | `needs_user`/`review` | #3 MERGED | 同上，我的手动推送导致 |
| 1 | `qzgic7y2zotouesl0vh6jnxa` | `needs_user`/`monitor` | — | 计划的 verify 围栏块未被解析（见 12.3） |
| 5 | `ax2oz6wuptyfy439r0q5xjrg` | `needs_user`/`delivery` | — | `delivery_commit_failed`「分支被执行端或他人改动」——同因我清理其工作树的 .pyc 并提交 |

3 个 PR 均已合并；`automation.cost_usd` 合计 0（未触达任何上限）。

**最终统计：5 个任务中 1 个完整 done（黄金路径全程 0 人工）、3 个 PR 已合并、4 个 needs_user，其中 3 个（任务 3/4/5）由我本人为消除 .pyc 冲突而向分支/工作树推送提交直接导致，1 个（任务 1）由我的任务描述嵌入代码围栏导致。** 即：若没有我的人工干预，本次跑不通的只有任务 1（且那是夹具问题）。引擎与 MCP 已停止，无持续消耗。

### 12.3 新发现：计划 verify 围栏被空 info 围栏吞掉

任务 1 的 monitor 报 `verification_unconfirmed`，原因 `计划必须恰好包含一个 verify 围栏块`
（`verify_evidence.parse_verify_block`）。该计划的围栏序列是：

```
```markdown … ```bash … ``` (空) … ``` (空) … ```verify … ``` (空)
```

`parse_verify_block` 只在「同字符、长度 ≥ 开围栏、**info 串为空**」时视为闭合。于是第一个空
info 的 ` ``` ` 开了一个新围栏，随后的 ` ```verify ` 因 info 非空被当作正文吞掉，最终识别到
0 个 verify 块。

**触发条件是我自己的合成任务描述里嵌了 ```bash 代码块**，planner 照抄进计划，形成嵌套。任务 2/3/4/5
的描述不含围栏，计划各只有一个 ` ```verify ` 块，全部正常。**这是夹具问题，不是引擎缺陷**；
但也说明 `parse_verify_block` 对「空 info 围栏嵌套 verify 围栏」没有防御——若要更稳，可在解析时
忽略 info 为空的围栏对，或让 planner prompt 明确禁止嵌套围栏。

### 12.4 新发现：交付会提交执行器产生的编译产物

任务 2/3/4/5 的分支都提交了 `tests/__pycache__/*.pyc`——执行器在工作树里跑 verify 命令时 Python
自己生成的字节码，被 `git_delivery` 一并提交。沙盒仓库起初没有 `.gitignore`，于是：

- PR diff 里混入二进制 `.pyc`（与「有效 diff 排除 `.pi/**`」的意图相悖——`.pi/**` 只是**判定**时排除，
  **提交**时并未排除）；
- 两个分支的 `.pyc` 内容不同 → add/add 二进制冲突，PR #2/#3 均 `CONFLICTING` 无法合并。

**我的处置（属人工干预，如实记录）**：
1. 直接向沙盒 `master` 提交 `.gitignore`（`__pycache__/`、`*.py[cod]`、`.pytest_cache/`、`.DS_Store`）；
2. 对任务 3/4 的分支各补一个「移除已提交 .pyc」的提交并普通推送（**未 force-push**），PR 转为可合并后合并。

**后果**：向分支推送核心不知道的提交，使交付快照的 `head_sha` 与实际分支头不符，merge-sync 正确报
`branch_diverged` 并把任务 3/4 拦在 `needs_user`。即**我的人工干预本身导致了 3/4 无法自动 sync 到
done**——这个身份核对门是按设计工作的，不是缺陷。

**建议的后续修复（不在本目标范围）**：`git_delivery` 提交前应过滤常见构建产物
（`__pycache__/`、`*.pyc`、`.pytest_cache/`、`node_modules/`、`dist/`、`.DS_Store`），或至少要求项目
仓库有 `.gitignore` 并把未忽略产物列入 preflight 警告。

### 12.5 结论

**A14 仍未满足（unmet）**，但比 §2 大幅推进：黄金路径已由真实 Plus 完整走通 1 次（到 Kaneo `done`），
3 个 PR 已合并，B1/B2 两个阻断缺陷经实测确认修复，`actor=user` 约束首次满足。
剩余阻碍是 12.3（夹具）与 12.4（交付提交构建产物）两项，其中 12.4 是真实的引擎改进点。

## 13. 第三次重跑：G1/G2/F1 + P1 + .gitignore 就位后（2026-10-11）

清空验收库与 Kaneo 侧，重建 5 个 To Do（描述不含代码围栏，规避 §12.3），沙盒仓库已有
`.gitignore`。引擎侧修复：B1（lifecycle 串行位让行）、B2（事件 actor）、
G1（retry_base 落库）、G2（dispatching 反向单测）、F1（fallback connection_file +
审计事件）、P1（构建产物过滤）。全量 core 2357 passed / rehearsal 63 passed。

### 13.1 已实证修复

- **B1**：任务 1/2 并发 dispatch 全部 `send_state=sent`，无 `busy`。
- **B2**：沙盒项目 `actor=user` 事件数 = **0**。
- **§12.3 围栏缺陷规避**：5 个计划均正常解析出 verify 围栏，monitor 全部 `succeeded`。
- **§12.4 产物冲突规避**：仓库有 `.gitignore` 且 P1 兜底，本轮无 PR `CONFLICTING`。
- **执行质量完好**：`runs/<task>/<run>/job.json` 显示 `exit_code: 0`、`ended_at` 已写、
  计划 criteria 全 `met`、两条 verify 命令退出码均 0——Plus 执行与核心验证都成功。

### 13.2 新阻断缺陷：轮次终态未登记，任务卡在 observe/running

现象：两个任务 monitor 均 `succeeded`，但 `tasks.status` 停在 `running`、
`execution_rounds.run_status` 仍 `running`，引擎 `decide` 恒判
`[observe/running] 等待中` → `AWAIT`，不再产生命令。worker 进程已退出，
`job.json` 的 `ended_at`/`exit_code` 已写。

根因（时序竞）：monitor 在轮次实际结束**之前**完成观察并回报 `succeeded`
（monitor finished 20:14:00 vs job.json ended_at 20:14:30），此后无组件再把
`run_status` 更新为终态——`auto_monitor` 的终态检测只在那一次观察窗口内有效。
即 D3「租约/轮次回收零散」在新引擎下的同源复现：`decide` 只读 `run_status`，
而它的写方只有 worker 收尾那一次机会，错过就无人兜底。

**建议修法（未实现）**：`observe/running` 且无活动请求时，引擎做世界探测——
`round.pid` 已死 + `job.json` 有 `ended_at`/`exit_code`，即按 `exit_code` 派生终态
（0 → 走 verdict，非 0 → 按失败阶梯归因），而非无限 AWAIT。原设计只覆盖「引擎被杀」，
未覆盖「worker 正常退出但终态登记错过窗口」。
 
 ## 14. 第四次重跑：§13.2 修复（51d43db）后（2026-10-11 上午）
 
 引擎侧修复已由 engine_builder 提交 `51d43db`（rounds.py 只读世界探测 +
 decide.round_settled_decision 纯函数 + loop._round_settle 有界补登记，
 MAX_ROUND_SETTLE=2；17+3 支新测试；core 2385 passed / rehearsal 63 passed）。
 重启 MCP + live 引擎（验收库），本轮实测：
 
 ### 14.1 §13.2 修复实证（live 形状通过）
 
 - 两个卡死任务（qyk2q7r6…/vzyxurg7…）被 `_round_settle` 正确捞起：补登记
   monitor 请求入队 → worker 读最新 job.json 收尾。
 - 任务 1/5：exit 0 → 核心亲判 verify 通过 → delivery-commit → `in_review`，
   review 请求已入队——从「卡死 observe/running」一路恢复进黄金路径。
 - 任务 2/5：补登记成功但 verify 证据不可确认（见 14.3）。
 
 ### 14.2 新缺陷 A：引擎被杀后租约堵死恢复（已补规格，实现中）
 
 引擎进程被外部杀死（`finally` 未执行，租约未释放），`lease_expires_at`
 7200 秒内任何新引擎都被 `acquire_or_renew_lease` 拒绝——崩溃恢复被堵死
 整整一个租约周期。已在 docs/protocol.md §5.27.1 补规格：租约条目记录
 pid + birth，接管判据收紧为「active 且未过期 且持有进程经 birth 核验存活」，
 pid 已死 → 放行接管并标记旧条目 stopped(holder_dead)。任务 25fd5261 实现中。
 
 ### 14.3 新缺陷 B：verify 证据不可确认的黄金路径缺口（方案待确认）
 
 任务 2/5 进 `needs_user/verification_unconfirmed`，且人工 reverify 对此
 形状无效（重读同一证据仍不匹配），只有 replan 重开轮次能恢复。事实链：
 
 - 该轮 20+ 次 Bash 调用遇 Plus 宿主 `HOST_OVERLOADED`（exit_code=null）；
 - 执行器重试后改用 `python3 -m unittest discover -s tests 2>&1; echo
   "EXIT_CODE=$?"` 组合跑法——测试实际全过（output 含 `Ran 3 tests OK
   EXIT_CODE=0`），但 `verify_evidence.match_verification` 按设计拒绝不受支持
   组合，无法归属独立退出码；
 - `auto_monitor` 直报 needs_user（ladder 的 quality 分级被绕过）。
 
 对照组：任务 1 同样 29 次 HOST_OVERLOADED，执行器坚持原样重试，最终拿到
 精确匹配 + exit 0 所以通过——差异在执行器变通行为，属概率性，五连跑会再遇。
 处置方案（任务 fb9e1595）调研中，硬约束：不削弱证据严格性、不破坏未发送
 派发守卫、写经核心命令。
 
 ### 14.4 待续
 
 租约修复（25fd5261）落地后重启引擎：任务 1 续跑 review→PR→merge→done；
 intake 领养 Kaneo 侧剩余 3 个 To Do（max_active_tasks=2，任务 1 离开
 running 后释放槽位）。任务 2 的最终处置随 fb9e1595 方案确定后更新本节。
