# 核心验证环境误判与分支准备回退 — 剩余工作收尾计划

本文件是执行计划 `docs/plans/核心验证环境误判与分支准备回退_20261015.plan.md`（需求基线 D1–D9、P0–P7、编号测试 1–27）与已冻结实施契约的**剩余工作清单**，用于交接或继续执行。不替换、不删减原计划要求。

---

## 0. 当前状态基线（已核验事实）

- 仓库：`/Users/developer/Documents/Code/Project/Personal Project/KaneoPilot`，远端 `https://github.com/SakuraLoveSmile/KaneoPilot.git`。
- 本地 main HEAD：`6749f97`；`origin/main`：`166ae24`；本地 **ahead 70 / behind 0**，尚未推送。
- 原两个前置工作树（`~/.codex/worktrees/execution-ask-relay/KaneoPilot`、`~/.codex/worktrees/running-round-monitor-topup/KaneoPilot`）已被外部移除，仅各留一个 0 字节 `.codex-worktree-name` 标记；`codex/*` 本地与远端分支引用已全部清除。
- 两前置分支的**已提交内容**已并入 main 历史（`0a739c7`、`11f0da2`、`13713b9`、`8affa8a`、`96ccfdd`、`ccb1a43`、`6749f97`），`0a739c7` 为 HEAD 祖先，可达。
- 两前置分支的**未提交脏工作未并入主线**，由三条 archive 引用与备份双份保全：
  - `refs/archive/worktree-pool/202616-relay = 4e5e1e1`
  - `refs/archive/worktree-pool/20261016-topup = 7cd7483`
  - `refs/archive/worktree-pool/20261016-main-stash = 8e71fe2`（等同 `stash@{0}`，未弹出）
- 备份目录 `.pi/premerge-backups/verify-env-20261015/` 完整（manifest + main/relay/topup 计划与脏工作快照，逐文件 SHA256）。
- `codex/verify-env-branch-recovery` 实施工作树**未创建**。
- 主检出当前 5 项未提交改动：
  - 本轮夹具修复：`tests/test_legacy_verification_pipeline.py`、`tests/test_pi_config_contract.py`
  - 外部/用户改动（不得由本轮擅自覆盖）：`core/tests/test_cli_review.py`、`docs/plans/执行期提问转交调度台_20261014.plan.md`、`docs/plans/运行轮次监控补给与评审收尾_20261015.plan.md`
  - 两份计划文件：盘上为集成前旧草稿，HEAD 已跟踪 relay/topup 新版；`docs/plans/核心验证环境误判与分支准备回退_20261015.plan.md` 未被改动且与备份一致。

### 环境与工具约束（必须遵守）

- 测试解释器：`/Library/Frameworks/Python.framework/Versions/3.11/bin/python3`（3.11.4，pytest 9.1.1）。默认 `python3` 是 Homebrew 3.14 且无真实 pytest。
- 仓库根 `pytest.py` 是无真实 pytest 时的兜底桩，会劫持 `python3 -m pytest`；一律用绝对解释器路径。
- 结算判活依赖 `/bin/ps`：沙箱下 100% `Operation not permitted`，导致 `util._ps_alive` 保守返回存活、`observer_alive` 永不为 False。相关测试**必须在允许 `/bin/ps` 的权限下运行**，不得把沙箱失败当作代码失败，也不得以沙箱为由宣称通过。
- 全局 `~/.gitconfig` 含 `commit.gpgsign=true` + `no-use-agent=true`，非沙箱下 `git commit` 会无限挂起；任何提交动作需覆盖 HOME 或关闭签名（不改全局配置）。
- Xcode 工程只有 `KaneoPilot` 一个 target/scheme，**无 XCTest target**；Swift 测试真实入口是 `macos/tests/run-*-fixtures.sh`，不得凭空运行 `xcodebuild test`。
- 项目无 Web 前端；UI 验收形式是隔离 FakeCore 原生交互走查，不是浏览器访问。

### 已完成并有实测证据的部分

| 内容 | 证据 |
|---|---|
| 四视角方案核查与实施契约冻结 | 规划阶段任务完成 |
| 合并前现场保全与发布范围冻结 | 备份 manifest，22 个文件/补丁 SHA256 核验 |
| `core/tests` 两个 Plus 硬策略 stub 同步 | 精确四文件 87 passed + 18 subtests，EXIT=0 |
| main 完整 core 套件 | 2609 passed + 573 subtests，1543.77s，EXIT=0 |
| 结算等待条件、显式终态门与 6 条确定性回归 | 三树 `TestSettlementWait` 各 4 passed；main pair 19 passed + 4 subtests |
| 评审领取身份契约（board/board_v13/lifecycle/dispatch） | 五文件批次 132 passed，401.40s，EXIT=0 |
| review_fix 发布前的固定分支评审前提 | dispatch marker 单点 1 passed |
| 远端冲突负测的真实本地提交前提 | lifecycle / save_state 两单点各 1 passed |
| 派发证据守卫两处落后断言 | 任务24 两用例单点通过 |
| legacy 手动流水线阶段化断言与真实评审前提 | legacy 目标单点 1 passed；legacy+pi_config 批次 15 passed |
| 完整轮次分支工作树契约 | pi_round 单点 1 passed、文件 2 passed |
| pipeline 沙箱免责定性 | `/bin/ps` 可用环境下 11 passed + 4 subtests，EXIT=0 |
| main 完整根 tests | 389 passed / 2 failed / 5 skipped，991.94s；两项失败即已修的 schema 版本与配置键集合 |

### 已实现但**尚未验证**的部分

- `tests/test_legacy_verification_pipeline.py`（schema 版本改引 `constants.SCHEMA_VERSION`）
- `tests/test_pi_config_contract.py`（`CONFIG_KEYS` 补 `execution_question_max_wait_seconds`，并新增其往返与校验用例）

---

## 1. 待决策项（执行前必须逐条确定）

| 编号 | 决策 | 选项 | 影响 |
|---|---|---|---|
| Q1 | 70 个本地提交是否现在快进推送 `origin/main` | 现在推 / 主修复完成后推 / 本轮不推 | 决定发布链顺序；behind=0，拓扑可行 |
| Q2 | archive 引用与 `stash@{0}` 中 relay/topup 脏工作（含 topup 的 `core/kanpilot/*`、engine、协议文档、新增 `test_engine_business_tick.py`） | 正式并入主线 / 保持封存 / 明确废弃 | 若并入，核心文件内容变化，必须重跑受影响验证范围 |
| Q3 | 两份计划文件与 `core/tests/test_cli_review.py` | 恢复 HEAD 新版 / 保留盘上旧草稿与用户改动 / 完全不动 | 直接提交会把两份计划从新版回退为旧草稿；新版仍在 HEAD 与备份中 |
| Q4 | 主修复实施位置 | 新建 `codex/verify-env-branch-recovery` 隔离工作树（自 `6749f97`，推荐） / 直接在 main 工作目录 | 决定主修复与用户未提交内容是否混提 |

未定案前不得执行推送、分支清理、archive/stash 处置或备份删除。

---

## 2. 阶段 A：前置门禁收尾

目标：把已修好的夹具欠账推到完整通过，并按授权完成发布与清理。

1. 在 `/bin/ps` 可用权限下复验：
   - `tests/test_legacy_verification_pipeline.py`、`tests/test_pi_config_contract.py`（含新增校验用例）
   - main 完整根 tests：`-m pytest tests -q`
   - main 独立演练：`-m pytest tests/rehearsal -q`（**从未取得通过证据，必做**）
2. relay 桌面改动的构建与原生夹具（当前 HEAD 已含该改动）：
   - `scripts/build_app.sh`
   - `macos/tests/run-*-fixtures.sh` 全部实际存在的夹具入口
3. 隔离 FakeCore 原生交互验收（原任务13）：用独立 FakeCore 配置启动测试实例，实际走查待处理页与任务详情页核心流程，保存交互结果与截图证据；不得连接生产数据库。仅编译通过或进程存在不能替代交互验收。
4. 三维独立 CodeReview（**此前从未派出**）：同时派出恰好三个独立审查者，分别只覆盖完整性、正确性、影响范围，审阅同一授权前置净改动；合并去重后阻断项进入定向修复 → 复验 → 复审闭环。不得用一个代理同时实现、验证与审查。
5. 按 Q1–Q3 决策执行发布与清理：
   - 推送前重新确认远端仍是本地主线祖先；普通推送，不强推、不 squash、不跳过 hooks；若远端出现新提交或保护策略拒绝则停止并报告。
   - 原计划的两个前置分支 PR 已被本地快进集成取代，无需再建 PR；如仍要走 PR，必须先恢复分支引用并重新验证。
   - 清理顺序：确认来源提交已包含于目标分支 → 迁移或保全未提交文件 → 删除残留 0 字节工作树标记目录 → 按决策处置 archive 引用、`stash@{0}` 与备份目录。删除前必须核验可达性，禁止强制丢弃。
6. 若 Q4 选择隔离工作树：从包含全部前置内容的最新 main 创建 `codex/verify-env-branch-recovery` 与工作树；不得以未提交脏状态作为修复基线。

阶段 A 出口条件：main 完整 core + 完整根 tests + 独立 rehearsal 全绿；构建与 Swift 夹具通过；FakeCore 交互走查有截图证据；三维审查阻断项闭环；发布/清理按决策完成并留下核验记录。

---

## 3. 阶段 B：P0 规格与接口先行

由单一文档执行代理落盘，主协调者定稿；核心与 macOS 实现代理不得并发改动规格。落点按符号与章节重新定位，**不依赖合并前的旧行号**。

需修改：
- `docs/protocol.md`：§4.10、§5.18.6、§5.27.4、§5.27.11、§5.27.12
- `docs/automation-contract.md`：接收、失败处理、同任务修复轮、待处理原因、处置规则、事件、`failure_stage` 分类
- `docs/state-model.md`：失败阶段与恢复状态规则
- `docs/ui-spec.md`：待处理动作、失败文案、项目页校验与补救轮展示
- 本次既有计划文件：记录补充契约与真实执行状态，不把待体验写成已验收

必须补齐的原计划遗漏项：
- 核心守卫与引擎的 `empty_diff` 同计划规则**都只计未撤回补救行**
- 自动补救轮与评审修复轮是两个独立计数器；撤回**不得**改 `automation.fix_round`
- 新增稳定的 `engine_fix_summary` 详情字段，解决撤回后候选消失而无法展示退额的问题
- 失败 stub 与 shadow 路径同步新增环境字段
- 保存目标分支的 git 核验明确位于写事务之外

---

## 4. 阶段 C：核心 P1–P4（单一负责人串行实现）

串行顺序：分类与共享判定 → 消费者 → 撤回。避免 `cmd_queue`、`engine/loop`、`derive` 的并发冲突。

### C1 环境分类与重新验证（P1，对应 D1/D2/D3）

- 文件：`core/kanpilot/engine/verdict.py`、`core/kanpilot/engine/commands.py`
- 新增统一环境原因判定，按优先级处理已脱敏输出尾部：`command_not_found`（启动失败、退出码 127 或既有签名）→ `jdk_version`（Java JVM 版本要求、Unsupported class file major version）→ `android_sdk_missing`（SDK location not found）→ `no_connected_device`（No connected devices）
- 退出成功不因输出含签名而失败；超时与整组时限耗尽必须令 `environment_reason` 为空，不能被残留 JDK 输出吸收；只有启动失败或明确非零退出才进入分类
- 每条检查增加 `environment_reason`，保留 `command_not_found` 兼容字段；整组保留 `environment_failure` 并新增去重保序的 `environment_reasons`；一条命中即整组归环境类，不开自动质量补救
- evaluate、shadow、失败 stub 使用同一字段形状与口径
- 历史收据缺新字段时，通过同一分类入口按原 `command_not_found`/`exit_code`/`timed_out`/`output_tail` 重判，**只用于读取与恢复决策，不回写原收据**
- **实测签名夹具**：在独立临时克隆采集 SDK 缺失与无设备场景，JDK 用已脱敏实测输出核对；禁止在任何执行轮工作树运行 Gradle；临时环境使用独立 HOME、Gradle 缓存与 adb 服务隔离，不断开真实设备、不修改全局服务；采集必须有明确命令、退出码与脱敏输出证据并限制进程与时间；无法可靠隔离或复现时记录阻塞，**不得把手写字符串测试声称为实测通过**
- 文件：`core/kanpilot/verification_checks.py`、`core/kanpilot/derive.py`、`core/kanpilot/cmd_queue.py`
  - 以 `_verification_environment(conn, task, events)` 替换仅构造 PATH 的逻辑，复用 `verdict.project_verify_env` 的项目映射读取器；保留原 PATH 来源再叠加 `verify_env`，显式 `verify_env` 的 PATH 优先
  - 回执只新增排序键名列表 `verify_env_keys` 与完整 `verify_env_sha256`（既有规范化 JSON + SHA256 范式），**不得记录变量值、不得截断哈希**；保持无 profile、无 rc 的 Bash 调用；补充输出脱敏测试确保注入的敏感值不泄漏到事件或日志
  - `failure_code=verification_environment_failure` 且 `failure_stage=monitor` 时，derive 与 resolve 都允许非托管任务 resume；恢复至 running/monitor 并创建既有 monitor 请求，**不重新 dispatch**，非托管任务不写 automation 段字段；不放宽 `verification_recovery.candidate`

### C2 同分支修复守卫（P2，对应 D4）

- 文件：`core/kanpilot/core.py`、`engine/loop.py`、`cmd_queue.py`、`cmd_exec.py`、`constants.py`、`engine/ladder.py`、`derive.py`
- 新增 `same_branch_fix_ready(task, binding=None)`：只读任务、delivery 与已有 binding，返回是否就绪及 `detached`、`no_branch_worktree`、`worktree_mismatch` 原因；**不得在该函数或写事务内执行 git**
- 引擎质量补救与核心权威守卫共用该判定；评审 `changes_requested` 也复用；无法就绪时转 `fix_branch_unavailable`，**先于额度判定**，不调用创建修复轮逻辑、不记新补救行、不消耗计数
- 登记默认 `failure_stage=monitor`，评审路径显式 `stage=review`；ladder 归人工处理，attention 保持既有 `needs_user_other` 回退，不新增 reason_code
- 允许动作：monitor → resume/replan/mark_done/dismiss；review → replan/mark_done/dismiss，**禁止 resume**
- derive 与 resolve 两端均检查阶段，不能只按 `failure_code` 放行；共用说明为「本轮改动在独立工作树，无法同分支修复」

### C3 分支收养、交付模式与目标校验（P3，对应 D5/D8/D9）

- 文件：`core/kanpilot/git_delivery.py`、`auto_intake.py` 及现有 start-plans 处理入口
- 在既有 fetch 与 resolve_base 成功之后、原 `branch_exists` 拒绝之前判断收养，全部成立才允许 fast-forward 到 base：仅本地存在且 origin 无同名分支；预期工作树路径已登记到该分支且目录存在；相对 base 无自有提交；包含全部未跟踪文件的工作树状态为空
- 返回原 delivery 形状与临时 `adopted` 标志；`adopted` 只在命令载荷中传递，**不落入 delivery**；核心持久化交付信息的同一事务内只记录一次 `branch_worktree_adopted`
- 不满足时按五类具体拒绝原因报告 `branch_exists`；不得 force、重置、删除分支、复用未知路径或覆盖工作树
- 交付模式：仅本次新建或收养成功且确实带 branch 的 delivery 才设 `mode=pr`；准备失败、不带分支及空参数路径均保留原 delivery，**不把 manual 改写成 pr**
- 文件：`core/kanpilot/automation_policy.py`、`project_config.py`、`git_delivery.py`
  - 保存显式 `delivery_base` 时在**事务外**读取 origin 本地引用；需要刷新时执行有界 `fetch --prune origin`，统一使用 `git_probe_timeout()`，不新增独立 15 秒参数
  - 三态：确认存在 → 保存；刷新成功后确认不存在 → 拒绝并给出 origin 不存在该分支与可用真实默认分支；fetch 或探测失败 → 允许保存并返回 `delivery_base_check=unverified`，**不得把网络故障误报为分支不存在**
  - `get-project-mappings` 增加 `origin_default_branch` 与 `delivery_base_exists`；只读本地引用、不 fetch，不确定则为 null；`resolve_base` 转译同类错误；复用局部 git 读取避免无界逐项目刷新；不新增阻塞式 `automation_preflight`

### C4 撤回自动补救（P4，对应 D6/D7）

- 新增 `core/kanpilot/engine_fix_revert.py`；修改 `cmd_queue.py`、`engine/loop.py`、`constants.py`、`cli.py`、`derive.py`
- **补救前快照**：`_record_engine_fix` 增加 `before`，在 `cmd_resolve` 写事务内、任何状态转移之前采集再传入：
  - task：`status`、`stage`、`failure_code`、`failure_stage`、`failure_reason`、`blocked_at`、`blocked_summary`、`sent`、`send_state`、`sent_at`、`lock_retained`
  - delivery：完整对象，**禁止字段裁剪**
  - automation：`plan_wanted`、`plan_attempts`、`replan_requested_at`、`replan_remote_status`、`replan_worktree_sha256`、`last_skip`
- **候选条件**（只用内存与数据库读取，同时满足）：项目有效模式 live 且任务托管；`needs_user`、`failure_stage=plan`、失败为 `branch_exists` 或 `delivery_base_unknown`；最近未撤回补救行为 `verify_failed` 且 run 与 plan 与当前任务一致；补救后没有新规划生成、规划请求或 `planner_inputs`，没有 queued/claimed 活动请求；原 `engine_verdict` 的执行轮与计划仍匹配且含可复核 binding
- resolve 新增 `action=revert_engine_fix`，必须显式提供 `expected_execution_run_id` 与 `expected_plan_sha256`；缺失为 `bad_request`，过期或不匹配为 `conflict`
- **三段式执行**：
  1. 事务外取 resolution 快照，按既有 resume-monitor 口径只读核对远端项目及状态；构造恢复后 delivery 视图并带预算重算 binding；远端不符、检查失败或工作树变化均拒绝且不写任务
  2. 事务内重新读取新鲜任务，比较快照、候选与两个 expected 字段；**不得在事务内重新 spawn、访问远端或扫描工作树**
  3. 显式恢复 `needs_user` 与 `stage=monitor`、`failure_stage=monitor`；用统一分类器把旧检查重判为 `verification_environment_failure` 或 `verification_unconfirmed` 并恢复对应说明；`sent=1`、`send_state=sent`；`sent_at` 与 `lock_retained` 取 `before`
- 存量无 `before` 行的保守回退：`sent_at` 取对应执行轮 `dispatch_sent` 事件，`lock_retained=0`；`binding.branch` 为 HEAD 时 delivery 整体恢复 manual；没有足够恢复证据时拒绝，**不能猜测工作树归属**
- delivery 必须经 `db.set_extra` 整体替换（不得 merge 残余 PR 字段）；automation 恢复 `plan_attempts`、关闭 `plan_wanted`、清理原计划指定的重新规划与跳过字段；使用新鲜行并一次合并写入全部 extra 变更，避免陈旧对象覆盖
- 补救行只加 `reverted_at`、`reverted_by`，不删除；原 `engine_verdict` 不改；被舍弃的分支与路径记入 `orphaned_branch` 事件信息，**不操作磁盘**；记录 `engine_fix_reverted` 与 `resolved`，前者包含执行轮、计划摘要、恢复失败码、环境原因与孤留分支信息
- **双计数与稳定详情**：全历史（含已撤回行）用于 `execution_run_id` 去重，防止同轮再次自动补救；未撤回行用于额度、`fix_round` 阶梯、引擎与核心两处 `empty_diff` 同计划规则、引擎快速规划门；**不得写 `automation.fix_round`**（该字段继续表示评审修复轮）
- 冻结桌面 JSON 契约：
  - `engine_fix_summary`：托管任务持续提供 `used`、`max` 两个整数（used 为未撤回行数，max 为引擎硬上限与项目配置的较小值）；非托管为 null
  - `engine_fix_revert`：只在候选成立时提供 `execution_run_id`、`plan_sha256`、`expected_failure_code`，否则 null
  - `allowed_actions`：只有候选成立才包含 `revert_engine_fix`
  - 撤回后 summary 继续存在并显示退额，不能依赖已消失的候选对象，也不能借用评审修复轮作证

---

## 5. 阶段 D：P5 原生桌面与 FakeCore（独立负责人，只改 macos）

前置条件：核心命令 schema 与详情契约已就绪；不允许新 App 向未升级核心发送未识别字段；本轮不替换正式 App。保持冻结字号、间距与窗口度量，不引入新组件库或布局重构。

文件：`macos/KaneoPilot/Core/Models.swift`、`AppViewModel.swift`、`Views/Components.swift`、`Views/PendingView.swift`、`Views/TaskDetailContentView.swift`、`Views/TaskDetailTabs.swift`、`Views/ProjectMappingsView.swift`、`macos/FakeCore/kanpilot-fake.py`

实现要点：
- 四组独立文案：验证环境失败、无法同分支修复、任务分支已存在、PR 目标分支不存在
- 待处理页与任务详情均支持撤回自动补救及二次确认；确认说明为「恢复本轮验证待处理、退还一次自动补救、不重新执行、不改本轮工作树」
- `AppViewModel.resolve` 增加 `expectedPlanSHA256`，准确发送 `expected_plan_sha256`；两个 expected 值必须来自当前候选，不能从不相关缓存拼接
- Models 使用可选解码兼容旧核心；显示独立的自动补救轮 `used/max`，现有评审修复轮保持不变
- 项目页使用真实默认分支占位，展示明确「不存在」错误与核心保存失败消息；`unverified` 使用非阻塞就地提示，**不禁用保存按钮**
- 新事件显示标签与 FakeCore 数据、动作、预期字段与错误响应全部同步

---

## 6. 阶段 E：P6 验证与审查

### E1 自动测试范围

修改既有：`core/tests/test_engine_verdict.py`、`test_verification_checks.py`、`test_git_delivery.py`、`test_auto_intake.py`、`test_automation_policy.py`
新增：`core/tests/test_engine_fix_branch_guard.py`、`core/tests/test_engine_fix_revert.py`
按实际现有入口补充派生详情、CLI schema、桌面契约与 Swift 测试；演练扩展 `tests/rehearsal/test_rehearsal_engine.py`。原计划编号 1–27 全部保留。

必须明确的回归断言：
- 四类签名、成功输出不误判、超时含 JDK 签名仍非环境类、整组环境优先、shadow 与历史收据兼容
- `verify_env` 注入、PATH 优先、完整摘要、敏感值不进入回执事件；非托管 resume 创建可领取 monitor 请求且不 dispatch
- `detached`、缺失或不符工作树均不消耗额度；review 不放出 resume
- 空分支安全收养及所有拒绝分支，事件恰一次，失败后 manual 保持 manual
- 目标分支存在、不存在、fetch 失败三态，以及读取映射不触发 fetch
- `before` 全字段恢复、存量事故形态、两个 expected 字段、远端不符、binding 变化、并发快照变化均正确拒绝且零任务写入
- 活跃行计数退额、全历史去重；引擎与核心 `empty_diff` 判定一致；`automation.fix_round` 不变
- 撤回后 candidate 消失但 `engine_fix_summary` 仍显示正确值
- 事务期间无 subprocess 或网络调用，测试进程可回收

回归原计划指定的五组：`verification_recovery`、`engine_manual_verification_gate`、`verification_check_monitor`、`archive`、`desktop_intake_probe`。

### E2 测试入口与证据要求

- 核心套：从 `core` 目录运行 `-m pytest tests -q`；集成套：从根目录运行同参数；演练套：从根目录运行 `-m pytest tests/rehearsal -q`
- 所有测试使用临时数据库、fake Kaneo 与隔离工作目录；运行中的生产 App 与引擎不停止、不借用；长命令采用受控后台执行并收集真实退出状态
- 先由验证代理取得单元与定向通过证据，再进入需要临时进程的集成、演练与原生 UI 验证
- 验证通过后同时派出恰好三个独立 CodeReview，分别只覆盖完整性、正确性、影响范围，统一审阅本次净改动；阻断问题进入定向修复 → 复验 → 复审闭环；核心与桌面共享契约必须交叉核对

---

## 7. 阶段 F：P7 本地构建与待体验交付

- 运行 `scripts/build_app.sh`，构建隔离分支的 `dist/KaneoPilot.app`；核对 bundle 内 Python 核心与当前源码一致（忽略字节码目录，但不忽略业务文件）
- 通过独立 FakeCore 配置启动测试实例，实际走查：待处理页、详情页、撤回取消与确认、过期候选拒绝、撤回后的补救轮显示、项目目标分支成功/拒绝/unverified 提示；保存交互结果与截图证据；不得连接生产数据库；仅编译通过或进程存在不能替代交互验收
- 完成后停止本次临时进程，清理临时克隆与测试产物；保留必须可恢复的原始计划备份、必要验收证据与本地构建；不得误停既有生产服务
- 最终报告：实际改动、通过与未通过的验证、前置 PR/推送与分支清理结果、工作树位置、剩余风险；状态标为**待体验**

---

## 8. 授权边界（执行中不得扩展）

已授权：
- 验证与范围检查后，将本地 main 尚未发布的提交普通快进推送到 `origin/main`
- 两个前置分支通过审查与测试后，提交必要改动、推送、通过 PR 合并，并安全清理已合并来源分支与临时工作树（注：该前提已被外部本地快进集成改变，按 Q1/Q2 决策执行）
- 本次修复的规格、代码、测试、隔离夹具及本地构建

未授权，不得执行：
- 本次修复分支的提交、推送、PR 合并
- 替换、安装或重启正式 App，启动正式任务，对事故任务执行 resolve、tick 或重新规划
- 写生产数据库、修改 StarTool 执行轮工作树、删除其遗留任务分支、操作真实设备或全局 adb 状态

恒定约束：保持 Python 3.9+ 兼容、标准库及既有 vendored 依赖方式；SQLite 仍是唯一状态写入源；所有正式状态变更只能通过核心命令；不扩大 `verification_recovery` 候选范围；不新增 attention reason_code；不增加阻塞式 preflight；不迁移本轮改动到新分支；不处理执行提问卡片等旁支问题。

---

## 9. 拒绝的替代方案（不得采用）

- 不直接依赖 topup 未提交改动，也不把待发布主线提交悄悄混入前置 PR
- 不强推、不 squash、不覆盖旧文件而无备份；合并后清理不是可选项
- 不增加独立 15 秒 fetch 参数，不截断 SHA256，不裁剪完整 delivery 快照
- 不把外部 I/O 移入写事务，不在详情 candidate 中扫描工作树
- 不把重新运行本轮验证替换为重新 dispatch，不迁移 detached 工作树改动，不改评审修复轮计数
- 不扩展 Gradle daemon 管理、验证总超时策略、无关文档或其他功能
- 不用 bundle 一致、应用已运行或手写签名测试代替用户验收与实测证据

---

## 10. 剩余风险与停止条件

- 前置测试或审查失败、远端主线变化、保护策略或权限阻塞：停止受影响发布步骤，保留现有工作并给出恢复动作
- SDK / 无设备签名无法隔离采集：不得宣称该项完成，提交具体环境阻塞
- 实施期间若用户生成新计划、修改本轮工作树或推进事故任务，撤回候选可能失效；这是必须拒绝的安全边界，不绕过
- topup 脏工作若定案并入，核心文件内容变化需重跑受影响验证范围
- 主检出仍有用户未提交改动（含两份计划旧草稿与 `core/tests/test_cli_review.py`），不隔离实施存在混提与误覆盖风险
- 正式 App 安装与事故任务实际恢复需单独授权，本次完成到待体验即停止

---

## 11. 完成度自检清单

阶段 A（前置门禁）
- [ ] Q1–Q4 决策已记录
- [ ] 两处已实现未验证的夹具修复通过定向验证
- [ ] main 完整根 tests 通过（`/bin/ps` 可用环境）
- [ ] main 独立 `tests/rehearsal` 通过
- [ ] `scripts/build_app.sh` 与 `macos/tests/run-*-fixtures.sh` 通过
- [ ] 隔离 FakeCore 原生交互走查完成并有截图证据
- [ ] 三个独立 CodeReview（完整性/正确性/影响）完成且阻断项闭环
- [ ] 按决策完成推送与清理，残留 0 字节工作树标记目录已处理
- [ ] `codex/verify-env-branch-recovery` 工作树按 Q4 决策就位

阶段 B（P0 规格）
- [ ] `protocol.md` 五个章节更新
- [ ] `automation-contract.md` 七类内容更新
- [ ] `state-model.md` 失败阶段与恢复状态规则更新
- [ ] `ui-spec.md` 待处理动作/失败文案/项目页校验/补救轮展示更新
- [ ] 本次计划文件记录补充契约与真实状态
- [ ] 五项原计划遗漏补齐（empty_diff 只计未撤回行、双计数器、`engine_fix_summary`、stub/shadow 环境字段、目标分支核验在事务外）

阶段 C（P1–P4 核心）
- [ ] 四类环境原因判定与整组归并、超时排除、成功不误判
- [ ] evaluate/shadow/失败 stub 字段同形；历史收据读取期重判不回写
- [ ] SDK 缺失与无设备签名隔离实测采集（或记录环境阻塞）
- [ ] `_verification_environment` 与 `verify_env` 叠加、PATH 优先、`verify_env_keys` + 完整 SHA256、脱敏测试
- [ ] 非托管 `verification_environment_failure` + `monitor` 的 resume 恢复且不 dispatch
- [ ] `same_branch_fix_ready` 纯内存判定与三类不就绪原因；引擎/核心/评审共用；`fix_branch_unavailable` 先于额度且不消耗计数
- [ ] monitor/review 阶段动作差异与 derive+resolve 双端检查
- [ ] 空分支安全收养五条件、五类拒绝原因、`branch_worktree_adopted` 恰一次
- [ ] 失败不改交付模式（manual 保持 manual）
- [ ] `delivery_base` 三态校验与 `get-project-mappings` 新字段（只读本地引用、不 fetch）
- [ ] `before` 快照全字段（task/delivery 完整/automation）
- [ ] 候选条件与 `revert_engine_fix` 两个 expected 字段校验
- [ ] 三段式执行、事务内无 subprocess/网络/工作树扫描
- [ ] 存量无 `before` 的保守回退与证据不足拒绝
- [ ] delivery 整体替换、automation 恢复与新鲜行合并写入
- [ ] 补救行标记不删除、`orphaned_branch` 只记录、`engine_fix_reverted` + `resolved` 事件
- [ ] 双计数（全历史去重 / 未撤回行额度与 empty_diff）、`automation.fix_round` 不变
- [ ] `engine_fix_summary`、`engine_fix_revert`、`allowed_actions` 契约冻结且撤回后仍显示退额

阶段 D（P5 桌面）
- [ ] 四组独立文案
- [ ] 待处理页与详情页撤回 + 二次确认
- [ ] `expectedPlanSHA256` 正确发送且两个 expected 同源
- [ ] Models 可选解码兼容、独立自动补救轮显示
- [ ] 项目页真实默认分支占位、不存在错误、unverified 非阻塞提示
- [ ] FakeCore 数据/动作/预期字段/错误响应与新事件标签同步

阶段 E（P6 验证与审查）
- [ ] 既有五个 core 测试文件修改到位
- [ ] 新增 `test_engine_fix_branch_guard.py`、`test_engine_fix_revert.py`
- [ ] 派生详情、CLI schema、桌面契约、Swift 测试与演练扩展补齐
- [ ] 原编号测试 1–27 全部保留并通过
- [ ] 十类明确回归断言全部覆盖
- [ ] 五组指定回归通过
- [ ] 三个独立 CodeReview 完成且核心/桌面共享契约交叉核对

阶段 F（P7 交付）
- [ ] `dist/KaneoPilot.app` 构建成功且 bundle 内 Python 核心与源码一致
- [ ] 隔离 FakeCore 六项交互走查完成并有截图证据
- [ ] 临时进程、临时克隆与测试产物已清理，备份与验收证据保留
- [ ] 最终报告写明实际改动、验证结果、发布与清理结果、工作树位置、剩余风险
- [ ] 状态标为待体验；明确未执行生产事故恢复、正式安装与实机验收
