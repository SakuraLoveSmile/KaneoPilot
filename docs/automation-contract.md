# KaneoPilot 执行端契约（现状）

现行冻结契约以 protocol §5.27 为准。App 的 EngineSupervisor 监督 engine run 并读取核心状态；引擎 intake 每 300 秒；dispatch/monitor/delivery 均为确定性 Python，不调用控制模型。controller_backend 只作旧配置展示，不作为保存或运行门。

## 现行路由与结算

本节覆盖当前生效行为；完整接口、载荷和失败语义见 protocol §5.27。

- **dispatch/monitor** 不再调用模型，不看 controller_backend，经 `cli.run` 驱动确定性流程；monitor 使用 `task.run_id` 观察执行，`advance-task` 不传 `request_id`，被动同步 `in-review`/`in-review` 与 `done`/`done` 均允许。
- legacy 验证缺失或最后一次失败进入 `verification_unconfirmed`；live 以核心 verdict 与已验证提交树为判定源，质量失败自动补救，身份不明与环境失败分别进入 attention，见 protocol §5.27.11–12。
- intake 每 300 秒，只接收开启且就绪项目的 To Do，保留人工筛选前提；自动确认由 tick 执行。
- Review 统一结算：手动任务保留跨任务修复，自动化任务 `changes_requested` 在同任务分支最多修复 2 轮并受段预算约束，`pass` 只登记。
- delivery 提交后 Review，pass 后推送/开 PR/评论，合并才 done，不自动 merge。
- Plus 会话 `create/configure/prompt`，不挂接 task grant；状态全部由 runner 经核心命令完成。

## 不变量（执行端必须遵守）

- 一切状态读写只经 kanpilot 命令；不直接改 DB、不复活旧文件锁、不写替代管理脚本。
- 无请求立即结束；不轮询刷屏。
- 远端 Kaneo 写只能经核心命令（`dispatch-begin-send`、`advance-task`、`create-review-fix`、`delivery-publish`、`delivery-sync`）；批量远端读取经 bridge。
- 派发证据字段全部来自 context/协议事件观察；不推测、不修饰。
- 关闭/中断不意味着操作结束：已 claimed 请求由租约派生失联，人工 resolve 才释放。
- 暂停只影响新领取；执行端不得因全局暂停中止已 running 的轮次。

## 旧调度循环（ExecutorScheduler，仅历史兼容）

以下保留旧 CLI / 旧 App 行为记录；新版 App 不启动这套定时器，自动驱动仅限 live 项目。显式用户授权的手动规划由引擎消费，见 protocol §5.27.14。

- 四个执行端（plan/dispatch/monitor/review/delivery）由 **KaneoPilot App 内置 ExecutorScheduler** 拉起：定时器按错开分钟触发（plan 0/15/30/45、dispatch 4/19/34/49、monitor 8/23/38/53、review 12/27/42/57、delivery 按 `next_sync_at` 动态）。
- 每次触发：`executor-status` 查单飞（该 stage `running` 非空则跳过）→ `python3 -m kanpilot.executor_runner --stage <stage>`（core 根为 cwd；PATH 注入 `~/.npm-global/bin`）。
- **常驻菜单栏**：关窗后继续调度，登录项可启用；退出 App 停止调度与本实例 MCP 服务，但不结束 Plus 会话。
- executor_runner 职责：渲染 `core/executor_prompts/<stage>.md`（`{PREAMBLE}` 展开、路径占位符替换）→ 拉起 `pi -p --mode json --session-dir <app_support>/executor-sessions/<stage>/ --no-extensions --no-context-files --no-skills` → 事件流落 `<app_support>/executor-logs/<stage>-<ts>.log` → 超时（settings `executor_timeout_seconds`，缺省 1800s）终止进程组。
- runner 自己把运行记录写 `settings.executor_runs[stage]`（`pid/started_at/log/exit_code/finished_at`）——`executor-status` 读取的唯一数据源。
- `executor-status {}` → `{"stages": {"<stage>": {"running": <entry>|null, "last": <entry>|null}}}`；`running` = entry.pid 存活探测通过。

## 自动接收（intake）

入口：`python3 -m kanpilot.auto_intake [--dry-run]`，由调度器每 300 秒或菜单「立即检查」拉起。

1. **单飞**：`executor_runs["intake"]` 记录的 pid 仍存活 → 直接退出。
2. **暂停检查**：全局暂停，或 `paused_stages` 包含 plan → 写摘要后退出。
3. **项目筛选**：逐个读取已开启的项目；前置检查不通过的项目跳过，并记入摘要。
4. **容量**：
   - 容量 = `max_active_tasks − 占用数`；
   - 占用数 = 受自动化管理、且状态不是 done/needs_user、也不在「pass 后等待合并」的任务数。
5. **先处理 plan_wanted 任务（不受容量限制）**：
   - 条件：状态为 needs_plan、`plan_wanted=true`、没有活动的 plan 请求、未转人工；
   - 若还没有 `delivery.branch`，先补建分支工作树；fetch 与 resolve_base 成功后，可收养仅本地存在、origin 无同名分支、预期路径登记到该分支且目录存在、相对 base 无自有提交、包含全部未跟踪文件的工作树状态为空的遗留分支；只执行 merge --ff-only 到 base，不 force/reset/delete/覆盖未知路径；
   - 然后准备材料并提交规划。
6. **新候选**：
   - 来源：`fetch-todos` 的条目。
   - 条件：本地没有该任务；或本地状态为 needs_plan、从未有过 plan 请求、且没有 automation 对象。另外要求 save_state 为 active，且不是 auto_advance_paused。
   - 顺序：按远端创建时间升序（字段缺失时按任务号）。
   - 数量：每轮最多接收 3 个，且不超过容量。
7. **标签过滤**：
   - label_filter 为空 → 不过滤。
   - 列表条目自带 labels（由包 E 确认）→ 直接比较。
   - 否则逐个调用 `remote.get_task_labels`：每轮最多 20 次；不匹配的结果缓存 30 分钟，存在 settings `automation_intake_state.label_checked`。
   - 比较方式：大小写不敏感的精确匹配，命中任一标签即可。
8. **接收单个任务**：
   1. `cli.run("prepare-plan-input", {task_id, expected_requirements_sha256: <列表里的指纹>, local_paths: []})`。
   2. `git_delivery.prepare_branch_worktree`。
   3. `cli.run("start-plans")`，条目带内部字段 `automation`：`{authorized_by:"automation", policy_revision, delivery:{...第 3 节返回值, mode:"pr"}}`。核心在同一事务里完成：
      - 写入任务的 automation（`plan_attempts=1`，开启第一段）和 delivery；
      - 建 plan 请求：`requested_by="automation"`，`manual_authorized` 仍为 true，以便既有 claim 过滤放行；
      - 载荷增加 `authorized_by`、`plan_cwd`、`delivery_mode`、`delivery_head_sha`；
      - 记录事件 `automation_adopted`；本次收养以临时 adopted 标志传入，在持久化 delivery 的同一事务记录一次 branch_worktree_adopted（branch/worktree_path/base_sha），adopted 不落入 delivery。
9. **plan_wanted 任务的材料准备**：
   - prepare-plan-input 传任务当前的 `requirements_sha256`；
   - 远端需求不符 → 任务进入 needs_user（failure_code=remote_conflict，note「远端需求已变化（新增内容未经筛选）」）。
10. **失败处理**：
    - 分支失败（branch_exists/delivery_base_unknown）→ 照常导入任务行，写入 automation（`plan_wanted=false`），然后转 needs_user 并带该 failure_code；保留已有 delivery，只有本次新建/收养成功且带 branch 才写 mode=pr，空参数/准备失败不得把 manual 改为 pr。branch_exists 分别说明已推 origin、有自有提交、工作树脏、预期路径未登记、目录缺失五类原因；delivery_base_unknown 显示 origin 不存在分支及可确定的真实默认分支，不能泄露无意义 Git 原文。
    - 确定性失败（validation_failed、bad_request、conflict）→ 清掉 plan_wanted，在 `last_skip` 记录原因，然后转 needs_user。
    - 瞬时失败（remote_unavailable、git_transient）→ 下轮重试。
11. **摘要**：写入 settings `automation_last_intake`，形状为 `{started_at, finished_at, projects:[{project_id, ready, preflight, adopted, skipped:[{task_id, reason}]}], errors}`。
12. **dry-run**：只做 fetch-todos、标签读取和前置检查，输出将会接收的候选；不调用 prepare-plan-input，不写任何东西。

保存显式 delivery_base 时，Git 核验全部在写事务外：origin 本地引用确认存在即保存；需要刷新时有界 fetch --prune origin，复用 git_probe_timeout；刷新成功仍不存在则 bad_request，显示真实默认分支；fetch/探测失败则允许保存并返回 delivery_base_check=unverified，不将网络失败当不存在。get-project-mappings 只读本地引用返回 origin_default_branch 与 delivery_base_exists，不确定为 null，不 fetch，不新增阻塞式 automation_preflight。resolve_base 使用相同可读不存在说明。

## 计划自动确认

主控在 tick 中实现，每次 tick 扫描一遍。

**确认条件（全部满足）：**

- 任务受自动化管理，状态为 ready，且带 `plan_execution_hold`；
- 项目已开启自动处理，`auto_confirm_plan=true`，前置检查通过；
- `plan_verify.plan_sha256 == task.plan_sha256`，且 commands 非空；
- 预算未超：本段时长 < max_task_minutes；本段成本 < max_task_cost_usd，或该上限为 null；
- `db.task_schedulable(task)` 成立；
- 未处于全局暂停，也未暂停 dispatch。

**动作：** `confirm_plan_inline(conn, task, now, expected_plan_sha256=task.plan_sha256, actor="automation", note="auto")`。该函数从 `cmd_enqueue` 24–111 中抽出，行为与 `confirm_plan` 完全相同。

**跳过时：**

- 不阻塞任务，只把 `automation.last_skip` 写为 `{reason, at}`；
- 原因与上次不同时才记一次 `auto_confirm_skipped` 事件；
- reason 取值：disabled、auto_confirm_off、preflight、verify_missing、verify_mismatch、budget、paused；
- 用户仍可手动确认。

## 同任务修复轮

**register-review 的 same_task 分支（主控，cmd_exec 815–905）：**

- **适用范围：** 只用于受自动化管理的任务，payload 带 `fix_mode:"same_task"`、`fix:{title, description, priority}`、`review_key` 和既有的 Review 身份字段。对这类任务，旧的 `fix_task_id`/create-review-fix 路径一律返回 conflict。
- **幂等：** `fix_request.review_key` 或 `review_history[-1].review_key` 已等于本次 key → 返回 `{already_registered:true}`。
- **未超上限**（same_branch_fix_ready 成立、`fix_round < max_fix_rounds`、本段时长和成本未超、项目仍开启）：调用 `core.start_fix_round_inline`，在同一事务内完成：
  1. 本次 Review 归档进 review_history；
  2. `fix_round += 1`；
  3. fix_request 写入并置为 open；
  4. `plan_wanted=true`，`plan_attempts=0`；
  5. 清空列 review_report_path、review_report_sha256、review_key，删除 extra 的 review_conclusion 和 review_registered_at；
  6. **重置发送字段**：`sent=0, send_state=NULL, sent_at=NULL, lock_retained=0, manual_recovery_required=0`；以 db.py 的 tasks 列为准，主控逐列核对；
  7. in_review → needs_plan；
  8. 记录事件 `fix_round_started`。
- **无法同分支修复：** 先于额度判断，复用 protocol §5.27.12 的纯内存守卫。fix_request 写入并置为 held，needs_user/fix_branch_unavailable，显式 failure_stage=review，不计 fix_round，不调用 start_fix_round_inline。
- **超出上限：** fix_request 写入并置为 held，任务进入 needs_user。failure_code 按触发原因取：
  - 修复轮用完 → fix_rounds_exhausted；
  - 时长或成本超出 → auto_budget_exceeded；
  - 项目已关闭 → automation_stopped。
- **pass 与 blocked：** 自动化任务的 pass 和 blocked 同样追加 review_history；pass 后把 fix_request 置为 null。

**publish-plan 的 same_task 分支（主控，cmd_exec 614–812）：**

- 核验元数据：`fix_mode=="same_task"`、`review_source_task_id==task_id`、`fix_round==automation.fix_round`；review_report_path、review_report_sha256、review_key 三项都与 fix_request 一致。
- 跳过 758–776 的跨任务检查，不执行 795–802 的修复回写，也不写 Review 列。
- 629 行的远端状态检查改用 `core.expected_remote_slugs(task)`。

**远端状态口径：** `core.expected_remote_slugs(task)` 的取值：

- 默认为 `TODO_SLUGS`；
- 受 PR 交付管理且已有提交时，再加上 `REVIEW_SLUGS`（修复轮开始时，远端停在 in-review）。

publish-plan:629 和 dispatch_validate:111 都改用它。begin-send 依靠 claim 时记录的 `original_todo_slug`（即 in-review），无需改动即可把远端从 in-review 迁到 in-progress。

**dispatch_validate 第 5 步（包 C）：** 先判断 `meta.fix_mode=="same_task"`。成立时：

- 报告必须在 `constants.review_reports_root()` 内，sha 与 fix_request 和计划元数据都一致；
- `review_source_task_id==task_id`；
- `fix_round==automation.fix_round`；
- `review_key==fix_request.review_key`；
- 不做 `REVIEW_MARKER_PREFIX` 标记和修复回写检查。

旧的跨任务 review_fix 检查保持不变。

**计划元数据（包 G）：** same_task 计划的 frontmatter 从请求载荷取 review_report_path/sha256、review_key 和 review_source_task_id（取自身），并增加 `fix_mode: same_task` 和 `fix_round: N`。这些字段由 runner 写入，不由模型生成。

**sync-tasks（主控）：** 受自动化管理的任务只更新元数据、kaneo_status 和事件，不做任何状态转移，也不改 requirements_sha256。这样需求变化会由 advance-task 和 dispatch_validate 的既有指纹校验拦下，停给用户处理。

## 执行验证证据（legacy / shadow 兼容路径）

Live 以 protocol §5.27.11 的核心判定及提交树绑定为准，不接受转录或人工override绕过验证；以下保留旧匹配路径。

**计划格式：**

- 计划正文恰好一个 \```verify 围栏块，含 1–5 条非空命令行。
- 允许 `#` 注释行。
- 不允许：行尾续行 `\`、heredoc `<<`；以 `git commit`、`git push`、`git reset`、`git checkout`、`git switch` 开头的命令；以 `gh ` 开头的命令。
- 解析规则：比较前把连续空白压成一个空格并去掉首尾空白；命令不做任何改写。

**解析与存储：**

- 解析函数：`verify_evidence.parse_verify_block(text)`（包 A）。
- 存储：publish-plan 发布时（主控 Step 14 接入）把结果写入 `extra.plan_verify`，形状为 `{plan_sha256, commands}` 或 `{plan_sha256, error}`。

**提取命令结果：** `verify_evidence.extract_command_results(messages)` 只认工具调用事件：`raw.tool.input` 中的 JSON 字段 `command`，以及整数 `exit_code`。消息形状以下面三处为准：

- dsh_events.py:196–208；
- pi_backend.py:142；
- pi_desktop_worker.py:505。

exit_code 缺失或不是整数时记为 None。

**匹配规则：**

- 每条声明的命令，在本执行轮次的结果里找「规范化后完全相等」或「可证明成功的顶层纯 `&&` 链中某段相等」的调用；
- 子段匹配必须识别引号和转义，仅允许顶层纯 AND 链；混合 `||`、`;`、管道、换行、注释、子 shell/替换及会终止或改变当前 shell 控制流的前置命令不作子段成功证据。不能确认时要求独立 exact 工具结果；保留普通 `cd <目录> && <验证命令>` 与普通验证命令纯 AND 链。
- 取最后一次匹配，exit_code 必须为 0；
- 只统计当前执行轮次（`task.run_id`）的消息。

**判定：**

- 有任一声明命令缺失、最后一次失败或 exit_code=None → `verification_unconfirmed`。
- 用户选 `accept_verification` → 写入 `verification_override{execution_run_id: task.run_id, actor:"user", at, note}`，任务回到 running，并新建 monitor 请求；monitor 仍要核验终态和退出码。
- 手动任务同样适用此规则；计划里没有 verify 块的旧任务完成后，由用户「接受验证结果」。
- 不做任何猜测：不从最终消息文字中推断「已验证」。Plus 工具事件如果不带 exit_code，停下汇报。

## 交付：提交、推送、PR 与合并

**delivery-commit（包 C，`cmd_delivery.py`）：**

- **调用：** `{task_id, request_id, run_id, expected_execution_run_id}`，由 monitor 在完成判定之后调用。
- **身份：** 用 `core.require_stage_identity(stages=("monitor",))`，并要求 `expected_execution_run_id` 等于 `task.run_id`。
- **三段式：**
  1. 第一段（事务）：核对身份和任务状态 running。
  2. 第二段（无事务）：执行 git 操作。
  3. 第三段（事务）：复核身份并确认 head 未变，然后追加 commits、更新 head_sha，记录事件 `delivery_committed`。
- **幂等：** commits 中已有该 execution_run_id → 返回 `{outcome:"already_committed", sha}`。
- **崩溃恢复：** HEAD ≠ head_sha，但 HEAD 的父提交等于 head_sha，且提交信息带有本次 run 的 `KaneoPilot-Run` trailer → 视为已提交，直接补记。其他 HEAD 漂移 → blocked，failure_code 为 `delivery_commit_failed`，note「分支被执行端或他人改动」。
- **候选文件：** 来自 `git status --porcelain=v1 -z --untracked-files=all`，需正确处理 rename 的双路径。被 .gitignore 忽略的文件本来就不进入候选。
- **拦截（delivery_blocked_files）：** 候选中出现以下任一 → 不提交，列出文件名：
  - `.env` 或 `.env.*`（`.env.example` 除外）；
  - `*.pem`、`*.key`、`*.p12`、`id_rsa*`；
  - 单文件 >5MB。
- **无改动：** 候选为空 → blocked，failure_code 为 `no_changes`。
- **提交：**
  - `git add -A`，然后 `git commit -F <临时消息文件>`；
  - 照常执行钩子，超时 300 秒，`GIT_TERMINAL_PROMPT=0`；
  - 不设置提交身份，不加 --no-verify，不加 --no-gpg-sign；
  - 失败 → blocked（delivery_commit_failed），note 附 stderr 末尾 ≤2KB（先脱敏）。
- **提交信息：**
  ```
  kaneo(<task_id>): <标题，截断到 72 字符>
  
  Kaneo-Task: <task_id>
  KaneoPilot-Run: <execution_run_id>
  Fix-Round: <fix_round>
  ```
- **返回：** `{outcome:"committed"|"already_committed"|"blocked", sha?, files?, failure_code?, message?}`。outcome=blocked 时，核心已在第三段把任务转为 needs_user 并写好 failure_code。只有协议级错误才 raise：bad_request、not_found、stale_run、validation_failed、conflict、internal。`ERROR_CODES` 不增加。

**delivery-publish（包 F，`cmd_delivery_pr.py`）：** 与 advance-task 一样走三段式，网络操作一律不在事务内。

已登记 PR 时，发布和同步在推送前即核对 PR URL/编号所属仓库与当前 origin 一致，不能先向改变后的仓库推送再发现身份失配。发布和同步入口必须核对当前执行轮次的 Review pass 与评审提交快照，不能靠 tick 排队条件代替核心硬闸；无本轮 pass 或仅有旧周期 pass 时，不推送、不创建 PR、不发评论。每个远端副作用前与最终落库事务，重新按当前时间核验 claim、租约及执行周期；慢读/推送期间租约过期或被接管后停止后续副作用，不使用命令启动时的固定时间放行。网络操作不持有 SQLite 写事务。

1. `git_delivery.push_branch`：推送前核验 `git remote get-url --push --all origin` 的真实目标，只允许单一且与交付 PR 仓库一致的 GitHub 地址；fetch 地址一致不能替代 push 地址检查。对会继续改写目标的不稳定 URL rewrite 链明确阻断。使用核验过的固定目标和 `<head_sha>:refs/heads/<branch>` 非强制推送（源 SHA 为已通过本轮 Review 的提交快照），保留已有认证、钩子和签名设置。 SSH 别名同前置检查按当前仓库和本机配置重新确认；不将推送地址改写为 github.com，也不绕过单一推送目标、仓库一致性及 URL rewrite 守卫。
   - 被拒（rejected/non-fast-forward/fetch first）→ blocked，failure_code=branch_diverged；
   - 其他失败 → `publish_failures += 1`，返回 retry。
2. 查找已有 PR：`gh pr list --repo <owner/name> --head <branch> --state all --json number,url,state,headRefOid,mergedAt,baseRefName,headRefName,isCrossRepository`。
   - 仅复用源仓库为当前 origin（非 fork）、head 分支/目标 base 分支均与交付快照相同且 headRefOid 等于本次 pushed_sha 的 OPEN/MERGED PR；已登记 PR 的编号也必须一致。身份/提交不匹配则 branch_diverged，不写评论或登记合并；缺少身份字段不能作为可信交付。
   - 只有 CLOSED 的 PR → blocked，failure_code=pr_closed。
3. 没有 PR → `gh pr create --repo <owner/name> --base <base_branch> --head <branch> --title "[Kaneo] <标题，≤100 字符>" --body-file <临时文件>`，再用 `gh pr view` 取 number 和 url。
   - 正文包括：任务标题、task_id、修复轮数、提交列表（sha12 + 标题）、verify 命令、「由 KaneoPilot 自动创建」；
   - 正文不含绝对路径、主机名或凭据。
4. Kaneo 评论：先用 `find_marker_comment(task, "[kaneopilot:delivery:<task_id>]")` 查找，不存在再 `post_comment`，内容为 `[kaneopilot:delivery:<task_id>] 已创建 PR：<url>`。
5. 第三段（事务）：写入 pushed_sha、pr_number、pr_url、pr_state、kaneo_comment_at，`publish_failures` 清零，`next_sync_at` 设为 now+5 分钟；记录事件 `delivery_pushed`、`pr_opened`、`kaneo_comment_posted`。

连续 3 次 retry → blocked，failure_code=gh_unavailable。gh 的环境变量为 `GH_PROMPT_DISABLED=1`、`GH_NO_UPDATE_NOTIFIER=1`、`NO_COLOR=1`、`GH_PAGER=`，每次调用超时 60 秒。

**delivery-sync（包 F）：**

1. 先读 Kaneo 任务（只读，无事务）：
   - 远端已 done → 返回 `{outcome:"kaneo_done"}`；
   - 否则执行 `gh pr view <number> --repo <owner/name> --json number,url,state,mergedAt,headRefOid,baseRefName,headRefName,isCrossRepository`。
2. 先核对 PR URL 所属 origin 仓库、编号、非 fork、head/base 分支以及 headRefOid=当前 pushed_sha=head_sha；不一致立即 branch_diverged，不登记合并、不推进 done。字段缺失不可作为可信证据。
3. 按 PR 状态处理：
   - MERGED → 第三段写入 merged_at 和 pr_state，记事件 `pr_merged`，返回 merged；
   - OPEN → 写入 last_sync_at 和 next_sync_at，返回 waiting。next_sync_at 的取法：距 review_registered_at 不到 1 小时为 +5 分钟，否则为 +30 分钟；
   - CLOSED → blocked，failure_code=pr_closed，记事件 `pr_closed`。

**delivery_runner（包 F）：**

1. 领取 delivery 请求。
2. 没有 PR，或 `pushed_sha≠head_sha` → 先调用 delivery-publish。
3. 调用 delivery-sync。
4. 按 sync 结果收尾：
   - merged → 调用 `advance-task {task_id, request_id, run_id, expected_execution_run_id, target:"done", expected_current:"in-review"}`；
   - kaneo_done → 同样调用，但 `expected_current:"done"`（被动）。
5. report：
   - 成功 → succeeded；
   - retry 或 waiting → succeeded，并在 progress 中说明；
   - blocked → failed，核心已转入待处理；
   - advance-task 返回 remote_conflict 或 remote_unavailable → failed。

**advance-task 的交付身份（主控 Step 3）：** 每次只读远端任务返回后、更新远端状态前，重新核验当前时间下的 claim、租约及任务执行周期；读取期间租约过期或被接管时不得继续远端写。最终本地事务仍再次核验，网络调用不持有 SQLite 写锁。

- 带 `request_id` 时用 `require_stage_identity(stages=("review","delivery"))`；
- 受 PR 交付管理的任务，非被动的 `target=done` 要求 `delivery.merged_at` 已记录，否则 conflict；
- 被动对 `(done,done)` 不受此限。

## Claude/Codex 后端与统一 Review 结算

**Claude CLI（包 G，`claude_cli.py`，规划与 Review 共用）：**

```
claude -p --output-format stream-json --verbose --restricted --tools Read,Grep,Glob
  --strict-mcp-config --permission-mode dontAsk --no-session-persistence
  --json-schema '<内联 JSON>' --model <M> --effort <E> --max-budget-usd <X> [--add-dir <输入目录>]
```

- **调用方式：**
  - prompt 从 stdin 传入，cwd 为 plan_cwd 或 Review 快照；
  - 绝不使用 `--bare`；
  - 施工前用 `claude --help` 逐个确认参数存在。缺参数就停下汇报，不要改用其他参数凑。
- **事件解析：**
  - `--tools` 仍仅允许 Read、Grep、Glob；init 事件的 tools 通常为其子集。传入 `--json-schema` 时，允许 CLI 额外暴露宿主内建 `StructuredOutput`（Claude 2.1.251 的真实形状），只用于生成结构化答复；无 schema 时拒绝该工具，Bash/Edit/Write/MCP 等始终拒绝。其他工具出现即终止进程并失败（「Claude 工具集不符合只读要求」）；
  - result 事件读取 subtype、is_error、structured_output、total_cost_usd、session_id。
- **费用：**
  - 每次运行都调用 `record-cost`（source=claude）；
  - `--max-budget-usd` 取 min(5, 本段剩余预算)，不低于 0.5；上限为 null 时取 5。
- **路径：** 二进制由 `constants.claude_bin()` 解析，可被 `KANPILOT_CLAUDE_BIN` 覆盖。
- **受限认证继承：** Claude 2.1.251 的 `--restricted` 会忽略用户设置；普通 CLI 已认证不代表受限调用已认证。仅从用户全局配置目录（`CLAUDE_CONFIG_DIR`，缺省/空白为 `~/.claude`，非空值展开后必须为绝对路径）的 `settings.json.env` 向子进程内存继承认证、服务地址与模型别名白名单；显式进程环境优先。白名单为 `ANTHROPIC_API_KEY`、`ANTHROPIC_AUTH_TOKEN`、`CLAUDE_CODE_OAUTH_TOKEN`、`ANTHROPIC_BASE_URL`、`ANTHROPIC_MODEL`、`ANTHROPIC_DEFAULT_HAIKU_MODEL`、`ANTHROPIC_DEFAULT_SONNET_MODEL`、`ANTHROPIC_DEFAULT_OPUS_MODEL`、`ANTHROPIC_SMALL_FAST_MODEL` 及对应 `*_MODEL_NAME`。不载入仓库配置、hooks、工具、MCP、agent teams 或任意其他 env，不修改全局配置，不把凭据写入 argv、临时文件、DB 或日志。配置不可解析时明确失败，不静默改用另一个服务；未设置认证仍按 CLI 实际错误报告。

**Claude 规划（包 G）：**

- planner_config 新增 `claude {model, effort}` 分组配置。`planner_config.BACKENDS` 直接引用 `constants.PLANNER_BACKENDS`，claude 已由主控在 Step 2 加入，G 不改 constants。
- `model_snapshot(conn, group, backend="claude")` 返回 publish-plan 期望的 `{planner_model, reasoning_effort}` 形状。
- planner_runner 529–541 新增 claude 分支，其输出走既有的 `parse_output` 和 publish 流程。
- Codex 的 `launcher_argv`（codex_planner 297）中，`cwd` 改为 `job.get("plan_cwd") or job.get("repository") or "."`。这个值就是 `-C` 工作根；只读沙箱 `-s read-only` 保持不变。
- Plus 规划在自动化任务上使用 `plan-<sha12>` 快照（第 3 节）。
- planner_prompt.md 和 pi_desktop_planner 的内联 prompt 改动：
  - 要求恰好一个 verify 块；
  - 不再指示「新建 worktree/分支/提交」；
  - 补充 same_task 修复指引：工作树已含前几轮提交，只修 Review 报告列出的问题，verify 要覆盖修复点。

**统一 Review 结算（包 H，`review_settle.py`）：**

结果 schema（所有键必填，additionalProperties=false）：

```json
{"conclusion": "pass|changes_requested|blocked", "report_markdown": "...",
 "fix": null 或 {"title": "...", "description": "...", "priority": "REVIEW_FIX_PRIORITIES 之一"}}
```

- **字段规则：** fix 在 changes_requested 时必须非空，其他结论必须为 null。fix 的可空写法先用 `"type":["object","null"]`；两种 CLI 的实际兼容性在 Step 15-F 核对，不兼容时停下汇报，不要把 fix 改成可选字段。
- **结算流程（`settle`）：**
  1. 把报告写到 `report_path(task_id, run_id)`，即沿用的 `<APP_SUPPORT>/reports/<task_id>/<run_id>.md`，并计算 sha256。
  2. 带 Review 身份（request_id、run_id、expected_execution_run_id）调用 register-review。
  3. 按任务类型分支：
     - 非自动化任务：保持原行为——pass → advance-task done；changes_requested → 先 create-review-fix，再 register；blocked → register 后 report needs_user。
     - 自动化任务：pass 只登记，不推进；changes_requested 用 same_task 方式登记；blocked 照常登记。
  4. 最后 report。
- **防重复：** `_registered_matches` 负责崩溃后重跑时避免重复登记：fix_request、review_history 或 Review 列中任一处已有同一 review_key，即视为已登记。
- **兼容：** pi_desktop_review 原有对外函数在 review_settle 中保留 re-export，旧测试照常通过。

**Claude/Codex Review（包 H，`cli_review.py`）：**

- 入口与 `run_stage` 同签名。
- 复用 claude_cli 和 codex_planner 的 `launcher_argv`/`child_env`/`map_event`；Codex 使用只读沙箱，cwd 为 Review 快照。
- 两者共用新 prompt `review_cli.md`。
- Codex 成本记为 null（source=codex），token 数只写日志。

**交付任务的 Review 材料：**

- `git diff --no-color <base_sha> <head_sha>`，以及 `git log --oneline base..head`；
- 有上一轮 Review 时，加上 `<review_history[-1].head_sha>..<head_sha>` 的增量 diff 和上一轮报告。

diff 超过 400KB 时只附 `--stat`，并注明「请在只读工作树中查看具体文件」。执行工作树 HEAD 与 head_sha 不符或不干净 → report needs_user（review_blocked，note「执行工作树与已提交版本不一致」），绝不审查未提交的内容。

**review_config（包 H）：** 新增 `claude {model, effort}`、`codex {model, reasoning_effort}`，并更新 snapshot 和 capabilities。

**Review 领取守卫（主控）：** claim(review) 领到受自动化管理的任务、但 Review 后端是 pi 时：该请求置为 failed，任务转 needs_user（review_backend_unsupported），然后继续看下一个候选。

**成本记录（包 D 实现 `record-cost`，主控注册命令）：**

- payload：`{task_id, request_id, run_id, stage, cost_usd|null, source}`。
- 以 (request_id, run_id) 去重；cost_usd 为 null 时 `cost_unknown_steps += 1`；记录事件 `cost_recorded`。
- 上限只在决策点检查：intake、自动确认、修复轮开始。pass 之后不再检查上限，也不中途打断正在运行的轮次。

## 任务 extra 字段

`db.set_extra` 只合并顶层键，因此嵌套对象必须在同一事务里整体读出、修改、写回。主控在 core 中提供访问函数：

- `automation_of(task)`、`delivery_of(task)`；
- `update_automation(conn, task, now, **changes)`、`update_delivery(conn, task, now, **changes)`。

任何包都不得自己拼 extra_json。

```json
{
  "automation": {
    "policy_revision": 3, "adopted_at": "...", "fix_round": 0,
    "segment_started_at": "...", "segment_cost_base_usd": 0.0,
    "plan_wanted": false, "plan_attempts": 0, "manual_takeover": false,
    "cost_usd": 0.0, "cost_unknown_steps": 0,
    "cost_log": [{"request_id": "...", "run_id": "...", "stage": "plan", "cost_usd": 0.42, "source": "claude", "at": "..."}],
    "last_skip": {"reason": "budget", "at": "..."}
  },
  "delivery": {
    "mode": "pr", "branch": "kaneo/<id>", "worktree_path": "...",
    "base_branch": "main", "base_sha": "...", "head_sha": "...",
    "commits": [{"execution_run_id": "...", "sha": "...", "at": "...", "files": 3, "fix_round": 0}],
    "pushed_sha": null, "pr_number": null, "pr_url": null, "pr_state": null,
    "publish_failures": 0, "kaneo_comment_at": null, "merged_at": null,
    "last_sync_at": null, "next_sync_at": null
  },
  "fix_request": {
    "fix_round": 1, "title": "...", "description": "...", "priority": "...",
    "review_key": "...", "review_run_id": "...", "execution_run_id": "...",
    "report_path": "...", "report_sha256": "...", "head_sha": "...",
    "created_at": "...", "state": "open"
  },
  "review_history": [{"review_key": "...", "conclusion": "changes_requested", "report_path": "...",
    "report_sha256": "...", "execution_run_id": "...", "head_sha": "...",
    "registered_at": "...", "fix_round": 0, "fix": {"title": "...", "priority": "..."}}],
  "plan_verify": {"plan_sha256": "...", "commands": ["..."], "error": null},
  "verification_override": {"execution_run_id": "...", "actor": "user", "at": "...", "note": "..."}
}
```

字段规则：

- **数量上限：** `cost_log` ≤50 条，`review_history` ≤20 条，超出时丢最旧的。
- **无人值守段：** `segment_*` 记录当前无人值守段的起点。时长按 `now - segment_started_at` 计，成本按 `cost_usd - segment_cost_base_usd` 计。用户执行 resume、replan 或 accept_verification 时开启新段。
- **plan_verify：** 每个任务都写，不限自动化任务。
- **delivery.mode：** 只有 `pr` 或 `manual` 两个值；转为人工处理时改为 `manual`。
- **术语：**「受自动化管理」= 有 automation 对象且 `manual_takeover=false`；「受 PR 交付管理」= `delivery.mode=="pr"`。

## 常驻调度与菜单栏

**AppRuntime：** 单例，App 启动时（AppDelegate `applicationDidFinishLaunching`）创建，持有 `EngineSupervisor` 和 `MCPServiceManager.shared`。

- 启动时依次执行：
  1. 恢复 MCP 服务，沿用 MCPServiceManager 现有的检查与启动入口；
  2. `LocalNetworkGate.probeAtLaunch()`（原先挂在窗口 `.task` 上）；
  3. 启动调度循环。
- 退出时沿用 `stopOwnedProcessForAppExit()`。

**当前调度：** EngineSupervisor 监督常驻 `engine run {mode:legacy,intake:true,spawn:true}`，退出退避重启，并每 20 秒调用一次 `recover-execution-goals` 恢复观察者。周期 tick 由引擎执行，不由 App 定时器执行；每循环（默认 60 秒）在 intake 后、spawn 前对有效 live 项目补给 monitor/review，同循环可领取，详见 protocol §5.27.15。CLI 独立引擎同样适用。

ExecutorScheduler/SchedulerDecision 是历史实现，当前不再拉起或调用周期 tick；其清理不在本次范围。子进程环境与路径沿用现有 CoreLocator/CoreClient 及核心 runner。

**其他约束：**

- 有存活子进程时持有 `ProcessInfo.beginActivity(.idleSystemSleepDisabled)`，全部退出后释放。
- 保留 `runNow`、`LocalNetworkGate`、开发开关 `executorSchedulingEnabled`；为 SettingsView 保留静态的 `stages` 和 `nextFireDate(for:)` 兼容接口，直到包 J 更新设置页。
- 暂停和恢复只调用核心 `set-paused {stage:null, paused}`，不在 Swift 里另存状态。

**菜单栏（MenuBarView）：** MenuBarExtra 包含：

- 状态行：运行中/已暂停、在途任务数、待处理数、上次 intake 时间；
- 「打开 KaneoPilot」「暂停/恢复」「立即检查」「退出」。

关闭主窗口后，激活策略切到 `.accessory`；重新打开窗口时切回 `.regular`。

**登录项（LoginItem）：** 基于 `SMAppService.mainApp` 实现 register/unregister/status。状态为 `.requiresApproval` 时，界面提示用户去「系统设置 → 登录项」批准。

## 待处理原因、处置与事件

| failure_code | 来源 | 自动化任务可用动作 |
|---|---|---|
| verification_unconfirmed | monitor | 材料可信时 run_verification_checks/reverify；legacy/shadow可accept_verification（→ running + monitor）；replan、mark_done、dismiss |
| execution_failed | monitor | resume（沿用确认计划 → ready + 新 dispatch 请求）、replan、mark_done、dismiss |
| fix_rounds_exhausted | register-review | resume（用户授权再修一轮：fix_round+1，fix_request 置为 open，plan_wanted，开新段）、replan、mark_done、dismiss |
| auto_budget_exceeded | register-review | resume（同上，在新预算段内进行）、replan、mark_done、dismiss |
| verification_environment_failure | live monitor 核心验证（protocol §5.18.6 四类环境签名） | failure_stage=monitor 时 resume（重新验证本轮，不限托管 → running + monitor；原 run/plan/worktree，不派发或耗补救轮）、replan、mark_done、dismiss |
| fix_branch_unavailable | 引擎补救 monitor / register-review review | monitor：resume（重新验证本轮）、replan、mark_done、dismiss；review：replan、mark_done、dismiss，禁止 resume |
| automation_stopped | register-review（项目已关闭） | resume（需先重新开启项目，同上）、replan、mark_done、dismiss |
| review_blocked | review | resume（→ in_review + 新 review 请求）、replan、mark_done、dismiss |
| review_backend_unsupported | claim(review) | resume（需 Review 后端已改为 Plus/Claude/Codex，→ in_review + review）、mark_done、dismiss |
| branch_exists、delivery_base_unknown | intake | resume（→ needs_plan + plan_wanted，重新准备分支）、dismiss；候选成立时 revert_engine_fix（恢复原轮验证待处理并退额） |
| delivery_blocked_files、delivery_commit_failed | delivery-commit | resume（→ running + monitor，重新走完成判定与提交）、replan、mark_done、dismiss |
| no_changes | delivery-commit | replan、mark_done、dismiss |
| branch_diverged、gh_unavailable、pr_closed | delivery | resume（→ in_review + delivery，next_sync_at=now）、mark_done、dismiss |
| remote_conflict | advance-task、intake、begin-send | resume（接受远端当前需求和状态，回到被阻塞的步骤；派发中途不可用）、replan、mark_done、dismiss |
| planner_failed（自动重试一次后）、needs_input | plan | replan、dismiss；也可以在规划面板补充材料后手动提交 |
| dispatch_failed | dispatch | 沿用 retry/restore/accept_duplicate/mark_sent；另有 replan、dismiss |

**处置规则：**

- **resume / accept_verification：** 只对上表列出的原因可用，derive/resolve 同时核对 failure_code 和 failure_stage。verification_environment_failure 的 monitor resume 不要求托管，非托管不写 automation 段字段；创建可领取 monitor 请求，不能重新 dispatch。
- **revert_engine_fix：** 候选、before 全快照、两个 expected、事务外远端/binding 校验、事务内快照比较与恢复，以 protocol §5.27.12 为唯一共享契约；拒绝零任务写入，不修改原收据，不操作分支磁盘，不修改评审 automation.fix_round。全历史用于去重，未撤回行用于全部额度和两处 empty_diff 规则。engine_fix_summary 在撤回后仍显示退额。
- **remote_conflict 的 resume：** 走三段式。
  1. 读取远端任务（不在事务内）。
  2. 远端状态必须是下一步所需的值：monitor 需要 in-progress、in-review 或 done；delivery 需要 in-review 或 done；plan 不限。不符 → conflict，提示「请先在 Kaneo 调整到 X 再继续」。
  3. 在事务内把 requirements_sha256 和 kaneo_status 更新为远端当前值，记录事件 `requirements_accepted`。
  4. 回到上一个失败请求对应的步骤：monitor → running + monitor；delivery → in_review + delivery；review → in_review + review；plan → needs_plan + plan_wanted；dispatch → 拒绝，改用既有的发送恢复动作。
- **replan：** 对自动化任务改为 needs_plan + plan_wanted，`plan_attempts=0`，开新段。如果远端需求有变化，用与上面相同的三段式先接受当前需求；用户点「重新规划」即视为已看过新内容。无法同分支修复时二次确认「将从目标分支新建工作树，本轮改动不会带入」。
- **dismiss：** 对自动化任务显示为「转为人工处理」：在原 dismiss 语义（needs_user → ready）之外，置 `manual_takeover=true`、`delivery.mode="manual"`，记录事件 `automation_stopped`。此后 intake、自动确认、交付补建、same_task 登记全部跳过这个任务，它按普通手动任务处理，但继续使用原分支工作树。转为人工处理不停止观察、收尾与评审；live 项目的 monitor/review 由引擎周期 tick 补给。
- **release_lost：** 释放丢失的自动化 plan 请求时，若 `plan_attempts<2` 则置 plan_wanted。
- **tick 自动释放：** 自动化任务的 monitor/delivery 丢失请求，由 tick 按 release_lost 的语义自动释放（actor=scheduler，事件 `lost_request_released`）；live 项目由引擎周期 tick 执行。dispatch、plan、review 的丢失请求仍留给用户处理。
- **plan 失败：** 自动化任务第一次失败时置 plan_wanted，让 intake 自动重试一次；第二次失败转 needs_user，保留 failure_code=planner_failed。

**新事件：** branch_worktree_adopted、engine_fix_reverted、automation_adopted、auto_confirm_skipped、fix_round_started、delivery_committed、delivery_pushed、pr_opened、kaneo_comment_posted、pr_merged、pr_closed、cost_recorded、requirements_accepted、automation_stopped、lost_request_released。

`BOARD_FAILURE_CODES` 不增加，新原因通过 failure_stage 归类。归类如下：

| failure_stage | failure_code |
|---|---|
| monitor | verification_unconfirmed、verification_environment_failure、fix_branch_unavailable（引擎补救） |
| plan | branch_exists、delivery_base_unknown |
| review | fix_rounds_exhausted、auto_budget_exceeded、automation_stopped、review_backend_unsupported、fix_branch_unavailable（评审修复） |
| delivery | delivery_*、no_changes、branch_diverged、gh_unavailable、pr_closed |

## 执行期提问转交（P2–P7，protocol §5.28）

目标契约已冻结，当前为实现中。执行保持interactive，目标契约轮和执行轮的asktool都由worker发现，
问题转needs_user/execution_input_required但轮次仍running。只能回答、全部跳过或停止本轮；
任务不创建第二执行者、不重规划、不消耗修复轮。规划与Review硬拦截语义保持原样。

等待不计入轮次时限或自动化本段时长预算，核心按待答集合的等待区间并集记录；
多个同时问题不重复扣除，段预算只扣与本段重叠部分。成本预算仍按实际费用累计。
默认24小时最长等待，执行配置可设0不限，轮次冻结快照；到期经核心收据stop→必要时abort，
detail=execution_question_expired，保留问题和草稿并进入execution_failed恢复。

runtime输入写仅核心提交使用，经scope归属验证、reserve_downstream及完成收据；不持SQLite写锁调用Plus。
unknown不自动重发、冻结答案，只读pending仍在不能判为not_applied；用户显式重试只允许相同冻结答案。
晚返回不能覆盖新的run/请求/operation状态。停止接受不等于持久终态。

引擎模式不影响提问报告、送达或核心结算。提交、App后台恢复与详情重连共同确保同身份只读observer；
legacy/shadow和engine关闭同样覆盖，恢复启动用原子占位去重。只读worker不取得回答或通用停止写权限；
到期走专用核心操作。scope到期仅以同身份重注册，旧token失效且不得进入job、事件、SQLite或日志。

failure_code=execution_input_required映射reason_code=execution_question（阶梯3），
推荐answer_execution_questions，替代skip_execution_questions/stop_execution。
execution_question_opened/answered/closed事件同步中文标签。详情与session-view透出同一execution_question DTO，
attention-list透出question_preview；DTO、答案映射、五命令和测试验收以protocol §5.28为唯一权威。


## 规划评审目录生命周期（protocol §5.29）

新规划/评审先租仓库池槽位；容量不足排队，禁止回退到真实仓库或新建per-run树。转录、计划/评审正文与冻结材料在释放前持久化。Plus状态未知、尚未结束的turn、活进程都不得因心跳过期清理；恢复使用租约代次隔离旧消费者。执行分支及非规划UUID保持既有持久语义。

历史回收默认为dry-run，候选需无活动/历史路径引用、超过7天且干净；每批最多10项，需用户根据具体清单确认，重审指纹、SQLite一致备份、intent回执、无force删除与prune缺一不可。save_state不释放磁盘，缺历史行不证明无引用。Plus会话和projects表不在回收范围。空闲整池回收同样遵守确认/引用守卫。

Codex Desktop显式交还：先完成所有目录读取，经kp_desktop_submit_plan/review或kp_desktop_fail持久提交并取得终态，再以原request/run/claim_token调用kp_desktop_return_slot(confirm=true)。交还后禁止任何槽位访问、heartbeat或新提交；仅允许不读取槽位的终态/交还幂等结果。submit本身不释放租约；消费者未交还时维持unknown并按容量等待。核心以同代SQLite accepted收据证明协作交还，owner_state=returned，不伪造owner_stopped或原生进程退出。原有未决操作、未知改动、产物先保存及整池GC守卫继续适用；历史非池目录不扩大回收资格。
