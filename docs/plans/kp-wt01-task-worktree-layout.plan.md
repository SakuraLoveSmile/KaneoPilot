# KP-WT01 任务级工作树布局与位置设置

- 任务标识：`kp-wt01`
- 状态：实现中（S1 规格已改完，待复核后进入 S2 核心）
- 分支：`chore/20261110-kp-wt01-task-worktree-layout`
- worktree：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261110-chore-kp-wt01-task-worktree-layout/`
- 合入目标：`main`
- 记录日期：2026-11-10（台北）

## 一、目标与范围

把「规划/评审共用池」改成「每任务固定阶段目录」，让一个任务在磁盘上就是一个可读的文件夹；同时把工作树位置、保留天数、并发上限做进设置页。

范围：

1. 目录规范：`<worktrees_root>/<task_id>/{meta.json,run/,plan/,review/,artifacts/}`。
2. 核心：阶段树的分配、校验、隔离、缺失重建、并发排队、终态归档。
3. 一次性迁移：`<task>/branch` → `<task>/run`，旧 `pool/**` 归档、旧池记录作废。
4. 配置：工作树根位置（走 relocate）、保留天数、规划/评审并发上限；设置页新增「工作树」分区。
5. 规格与验收清单同步更新。

不在范围：安卓端、Kaneo 服务端、任务分支命名规则（仍是 `kaneo/<task_id>`）、评审/规划的业务逻辑。

## 二、已确认决定

| 编号 | 决定 | 依据 |
| --- | --- | --- |
| D1 | 每任务固定 `plan/` 与 `review/`，同任务同阶段串行，不按轮次再分子目录 | 2026-11-10 用户选择 |
| D2 | 去掉 `source`；代码树就是 `run/`、`plan/`、`review/`，不设主分支镜像 | 同上 |
| D3 | 目录名用 `task_id`，可读标题写 `meta.json` | 同上 |
| D4 | 终态 7 天后归档（tar）再删，保留 `meta.json` 与 `artifacts/`；天数可调，取 1–90，默认 7 | 同上（补充：允许用户调整时间） |
| D5 | 布局、缺失重建、设置页一次性交付 | 同上 |
| D6 | 新增全局「规划/评审并发上限」，默认 4，超出排队不失败 | 同上 |
| D7 | 旧布局迁移：`branch` → `run`，旧池记录作废、旧池目录归档 | 同上 |
| D8 | 设置页改位置：检测引擎/租约时阻止并提示先停，预览+确认后迁移，旧根只读保留 | 同上 |

## 三、现状与问题

- 执行树：`<root>/<task_id>/branch`，检出 `kaneo/<task_id>`（`core/kanpilot/task_workspace.py:187`）。
- 规划/评审树：`<root>/pool/<repo_key>/slot-N`，每仓库上限 4（`worktree_pool.py:186`、`protocol.md:1790`），按租约复用，失败隔离。
- 计划正文在 `~/Documents/Code/Plan`，评审报告在 app support，job/日志在 `runs/<task>/<run>/`；工作树里只临时放 `.pi/plan/` 与 `.kaneopilot-plan-material/`，释放时清除。
- 工作树根目前只有 `worktrees_root` 设置，没有界面；换位置只能走 `worktree-relocate`。

2026-11-09 的事故说明现状的可解释性不足：Music 的 `pool/50b0e30ee66887c9/` 整个目录在核心之外被删，库里的 3 条 free 记录仍指向它；两次规划请求在 `_validate` 的「是否真实目录」这一条上短路失败（`worktree_pool.py:74`、`:83`），总共 135 毫秒就结束，任务转入「需要你」。而当前没有任何命令能忘掉这类记录：`worktree-gc` 只审计磁盘上存在的目录，`worktree-slot-release` 必须重新通过同一校验。

## 四、目标目录规范

```text
<worktrees_root>/
  .archives/                      # 归档包，复用现有 worktree-cleanup-archive
  <task_id>/
    meta.json                     # 核心写，App 只读
    run/                          # 执行工作树：kaneo/<task_id>
    plan/                         # 规划工作树：detached 冻结 sha
    review/                       # 评审工作树：detached 快照 sha
    artifacts/                    # 只读镜像，不是证据源
      plan/<run_id>/plan.md
      review/<run_id>/report.md
```

规则：

- `task_id` 必须匹配 `[A-Za-z0-9_-]{1,64}`；不含中文、空格、斜杠；不做大小写折叠，重名以 `task_id` 为唯一键。
- `plan/`、`review/` 一律 detached，HEAD = 本轮冻结 sha；不允许检出分支。`run/` 检出任务分支。
- `artifacts/` 只是给人看的副本，删除不影响审计；事实源仍是 `runs/`、`plan_root`、`review_reports_root`。
- `meta.json` 字段：`task_id`、`title`、`project`、`repository`、`branch`、`created_at`、`last_used_at`、`task_status`、`stages.{run,plan,review}.{state,run_id,sha,updated_at}`、`retention_days`。只由核心写。

### 阶段状态机

| 记录状态 | 界面词 | 含义 | 可被新请求领取 |
| --- | --- | --- | --- |
| `free` | 空闲 | 目录存在、身份/HEAD/干净度校验通过 | 是 |
| `leased` | 占用 | 持有租约（holder_id=run_id、lease_token、sha、pid、owner_state） | 否（等待） |
| `quarantined` | 隔离 | 校验失败，保留现场待人工处理 | 否 |
| （缺失） | — | 目录不存在，不是记录状态 | `plan`/`review` 自动重建；`run` 不重建 |

重建规则：

- `plan`/`review` 缺失：在没有活动租约、没有未决 `runtime_operations` 的前提下按本轮冻结输入重建（`git worktree add --detach <sha>`，需要时重新 populate 快照材料）。重建成功写 `worktree_stage_rebuilt` 事件；不允许重建覆盖任何未归档产物。
- `run` 缺失：可能有未推送提交，一律不重建，转 `needs_user` 并给出路径与最后已知 sha。

### 并发与保留

- `worktree_concurrency_limit`（默认 4，1–32）：统计全库 `busy` 的 `plan`+`review` 树；达到上限新请求转 waiting（`worktree_pool_waiting` 语义保留），不失败。
- `worktree_retention_days`（默认 7，1–90）：任务终态后计时；到期由回收流程 tar 到 `.archives/` 再删 `plan/`、`review/`，`run/` 另按合并状态判定（未合并不删）。`meta.json` 与 `artifacts/` 保留。
- 旧 `worktree_pool_size` 保留读取兼容（默认值 4），新逻辑不再按仓库计数。

## 五、核心改动

| 位置 | 改动 |
| --- | --- |
| `worktree_pool.py` | `_pool_path` 改为 `<root>/<task_id>/<stage>` 派生；`_validate` 增加阶段语义（`run` 允许分支，`plan`/`review` 必须 detached）；`quarantine`/`release`/`unquarantine` 保持守卫语义 |
| `task_workspace.py` | `acquire_pool` 增加 `task_id`、`stage`；`release_pool` 容错旧 `pool_lease`（路径不在新布局时只读跳过并记事件） |
| `cmd_read.py` | `worktree_pool` 检查改为汇总阶段树状态；detail 格式保持 `free/leased/quarantined/unconfirmed/waiting` 或被新格式替换（同时更新 Swift 译文） |
| `cmd_worktree.py` | gc/审计按新布局枚举；`worktree-cleanup-*` 支持阶段目录；新增一次性 `worktree-layout-migrate` |
| `cmd_config.py` | `set-config` 接受 `worktree_concurrency_limit`、`worktree_retention_days`；新增只读 `worktree-config`（根、并发、保留、每任务占用） |
| `cmd_worktree_relocate.py` | 复用为设置页迁移后端；前置检查补「无活动阶段树租约」 |
| `db.py` | `worktree_slots` 升 schema 7，增 `task_id`、`stage`；唯一索引改为 `(task_id, stage)`；旧池行迁移时删除 |
| `macos/SettingsView.swift` | 新增「工作树」分区：根路径、更改…、保留天数步进、并发上限步进、占用统计；迁移弹窗含预览与停引擎提示 |
| `macos/AppViewModel.swift` / `CoreClient.swift` | 新增 `worktreeConfig` / `worktreeRelocate` 调用与忙碌/错误处理 |

命令面新增：`worktree-config`、`worktree-layout-migrate`；`set-config` 扩字段；`worktree-relocate` 沿用。

## 六、迁移（一次性）

命令：`worktree-layout-migrate {backup_dir, operation_id, dry_run, confirm, expected_audit_sha256}`。

前置（任一不满足即拒绝）：

1. 引擎已停止，无活动租约、无 `queued`/`claimed` 请求、无 `prepared`/`unknown` 操作。
2. 磁盘空间足够容纳归档包。
3. 审计哈希在预览与确认之间未变。

步骤：

1. 审计现有 `<task>/branch`、`pool/**`、`worktree_slots` 行与引用计数。
2. `git worktree move <task>/branch <task>/run`，同步更新仍指向旧路径的 active delivery 记录（只改当前有效记录，不改历史请求 payload）。
3. `pool/**` 整体打包到 `.archives/pool-legacy-<date>.tar.gz`，删除池目录。
4. 删除 `worktree_slots` 全部旧池行（含当前 3 条隔离），写回执与事件。
5. 重建 `worktree-slots.json` 投影。

失败处理：任一步失败停止且不回滚已完成的 `git worktree move`（Git 侧已登记），写出 `unknown` 回执，人工按 `operation_id` 核对。

## 七、验收场景

| 编号 | 场景 | 期望 |
| --- | --- | --- |
| A1 | 新任务首次规划 | 生成 `<task>/plan`（detached），结束回 `ready`，`.pi/plan/` 与材料已清 |
| A2 | 手动删除 `<task>/review` 后触发评审 | 自动重建并跑完；不再出现永久告警 |
| A3 | 手动删除 `<task>/run` | 不重建，任务转 `needs_user`，提示路径与最后 sha |
| A4 | 同任务连续两次规划 | 第二个请求 waiting，之后自动继续，不失败 |
| A5 | 并发上限 1、两任务同时规划 | 一个执行，另一个 queued，`attention-list` 显示等待 |
| A6 | 把 `plan/` 切到分支后领取 | 校验失败、隔离，doctor 报出，释放接口拒绝 |
| A7 | 执行迁移 | `run/` 就位、`pool/` 已归档、旧池行消失、首页不再有工作树告警、旧执行记录仍可读 |
| A8 | 引擎运行时在设置页改位置 | 按钮阻止并提示先停 |
| A9 | 保留天数设为 1 | 终态任务归档并删除阶段树，`meta.json` 与 `artifacts/` 保留 |
| A10 | 设置页尺寸与主题 | 1180×760、900×600、深色浅色可读可操作，Tab 可达 |

## 八、风险与未决

1. **迁移改写 delivery 路径**：动的是有效记录，必须备份 + 回执 + 拒绝部分成功；建议先在副本库演练。
2. **磁盘增长**：每任务最多 3 棵树，现有 20 个任务 4.1 GB，预计 2–3 倍；回收流程需与布局同批上线。
3. **同任务串行**：重规划期间无法评审，等待状态的界面提示目前只有 attention/doctor，需要在任务页补一行。
4. **claim 内做 Git 操作**：评审在 `cmd_claim` 里取树（`cmd_exec.py:183` 起），失败即 `review_blocked`；新布局必须保证失败可重试且不伪造终态。
5. **旧 `pool_lease` 引用**：历史 job 里的路径不重写，释放路径需容错跳过。
6. **快照基线目录** `worktree-slot-baselines/<slot_id>/<lease_token>`：保留 `slot_id` 语义，避免历史快照失联。
7. **Swift 只能云端编译**：本地只跑 Python 隔离测试与静态核对，编译器结论以 Packages 为准。

## 九、实施阶段

| 阶段 | 内容 | 验证 |
| --- | --- | --- |
| S1 | 规格先行：`protocol.md` §5.18.1/§5.27/§5.29、`ui-spec.md` §4、`tests/ui-acceptance.md`、`README.md` | 旧词 grep；无实现改动 |
| S2 | 核心：路径派生、状态机、重建、并发、回收、迁移命令 | 隔离 DB 测试 + 夹具，不碰真实库 |
| S3 | 设置命令 + Swift 设置页 | 云端 `Packages` 编译与夹具；本机不编译 |
| S4 | 真实迁移一次 + 实机走查 | 需要用户授权后执行；A7、A8 实机验收 |

## 十、执行记录

（实现开始后逐条追加：时间、代码版本、命令、结果、未验证项。）

| 时间 | 动作 | 结果/证据 | 状态 |
| --- | --- | --- | --- |
| 2026-11-10 02:0x | 停止引擎 | `kill -TERM 55255` 后进程退出，`engine status` 无 active 实例；App 偏好 `executorSchedulingEnabled=false`（否则每 5 秒重拉）；恢复时在设置页打开「启用本机引擎」 | 完成 |
| 2026-11-10 02:1x | S1 规格 | `docs/protocol.md`：§5.18.1 执行树 `run`、§5.27.6 doctor 阶段树统计、§5.29 标题与 §5.29.1/5.29.2 路径规则、§5.29.4 投影、§5.29.5 迁移、新增 §5.29.6；`docs/state-model.md` 槽位池改任务阶段树与 schema 7；`docs/ui-spec.md` §4.9 高级页三分区加「工作树」；`tests/ui-acceptance.md` W1–W10；`macos/README.md` 走查与文件说明 | 实现中（未编译、未实现） |
