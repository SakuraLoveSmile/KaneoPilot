# KaneoPilot 执行端契约 v1（冻结）

四个 Codex 定时执行端每 15 分钟检查所属阶段，错开起始分钟；无请求立即结束。业务周期（规划/评审）由核心 settings 单独配置——领取频率不等于反复执行全部任务。

## 1. 执行端与调度

| 执行端 | stage | 定时（RRULE） | 现对应 automation |
|---|---|---|---|
| kaneo-plan | plan | `FREQ=HOURLY;BYMINUTE=0,15,30,45` | kaneo-todo-2 改造 |
| kaneo-dispatch | dispatch | `FREQ=HOURLY;BYMINUTE=4,19,34,49` | kaneo-devin 改造 |
| kaneo-monitor | monitor | `FREQ=HOURLY;BYMINUTE=8,23,38,53` | kaneo-devin-2 改造 |
| kaneo-review | review | `FREQ=HOURLY;BYMINUTE=12,27,42,57` | kaneo-in-review 改造 |

每个执行端运行开始：`kanpilot claim --stage <stage> --executor <id>`；`claimed:false` → 报告原因并结束。`claimed:true` → 按 kind 流程执行；长操作每 < lease_seconds 调 `heartbeat`；结束调 `report`；然后继续 `claim` 直到无请求。

## 2. 各 stage 流程

### plan（executor id `kaneo-plan`）

- `kind=plan-scan`：经 `kaneo_bridge.py list-todos` 枚举远端 → `kanpilot sync-tasks` 上报 → report succeeded。核心据此生成/失效 plan 请求。
- `kind=plan`：读取 context.task 与计划规范（沿用旧 planner 规则的产物形态：中文自包含计划、Plan/<项目>/<任务名>_<时间>.md、frontmatter 全字段、原始快照存 KaneoPlanner/state/sources）；产出后 `publish-plan` → `report succeeded`。失败 `report failed|needs_user`。
- review_fix 计划：只处理核心登记的 fix task（context.payload 含 review_key 等）；沿用旧 review_fix 校验语义。

### dispatch（executor id `kaneo-dispatch`）

- 桌面插件：`computer-use@openai-bundled`，本机入口 `mcp__node_repl__js` + `@oai/sky`（沿用旧契约不变：Agent 模式、SWE-2 Max、Local、仓库选择、附件完整路径、禁用 unified-computer-use/cua_repl/回车发送）。
- 流程（与旧 dispatch.md 步骤一一对应，命令换成 kanpilot）：
  1. `claim --stage dispatch` → context（plan_path/attachment_name/repository/session_marker/instruction/original_todo_slug）。
  2. Computer Use 建 Local 编辑器草稿 + 附件 + 短指令 → 写证据 JSON → `dispatch-prepare`。
  3. `dispatch-begin-send` 成功后点一次可见发送按钮。
  4. 观察同会话已发送消息+附件 → 补 message_visible → `dispatch-confirm`。
  5. 异常 `dispatch-fail --send-state not_sent|unknown --reason <短原因>`。
  6. UI 操作期间每 ≤10 分钟 `heartbeat`。
- 锁屏/权限拒绝/发送不明立即停止；单次恢复规则沿用旧契约（noWindowsAvailable/offscreen 各一次）。
- 桌面 UI 单操作者由核心 dispatch 串行位保证；claim 返回 `stage_busy` 即结束。

### monitor（executor id `kaneo-monitor`）

- `kind=monitor`：Computer Use 读原会话（session_marker/session_id 定位）→
  - 仍执行：`report succeeded` + `progress=<观察摘要>`。
  - 已完成：`advance-task --target in-review`（核心受控远端迁移）→ `report succeeded`。
  - 执行失败/异常：`report needs_user`（或 `failed`）+ 证据。
  - 观察受阻（锁屏/找不到会话）：`heartbeat` + `report failed`，不改状态。
- 也可经 `advance-task --target done` 归档远端已 done 的任务。

### review（executor id `kaneo-review`）

- `kind=review`：评审仓库实现 vs 计划与需求 → 报告写 `KaneoReviewer/reports/<task_id>/<ts>.md` → `register-review` →
  - `pass`：`advance-task --target done` → `report succeeded`。
  - `changes_requested`：`create-review-fix`（dedupe）→ 核心登记 fix task 并排 plan 请求 → `report succeeded`。
  - `blocked`：`register-review blocked` → `report needs_user`。

## 3. 不变量（执行端必须遵守）

- 一切状态读写只经 kanpilot 命令；不直接改 DB、不复活旧文件锁、不写替代管理脚本。
- 无请求立即结束；不轮询刷屏。
- 远端 Kaneo 写只能经核心命令（dispatch-begin-send、advance-task、create-review-fix）；批量远端读取经 bridge。
- 发送证据字段全部来自 UI 观察；不推测、不修饰。
- 关闭/中断不意味着操作结束：已 claimed 请求由租约派生失联，人工 resolve 才释放。
- 暂停只影响新领取；执行端不得因全局暂停中止手中已 running 的 Devin。

## 4. 切换步骤（主 agent 执行）

1. 停旧流程新领取：四个旧 automation `status → DISABLED`（或 toml → `.disabled`）。**不取消**任何 Devin 会话、不重发 send_state=unknown 的任务。
2. `kanpilot doctor` 全绿 → `kanpilot migrate`（备份 → 导入 → 校验计数）。
3. 更新四个 automation.toml：新 prompt（指向 kanpilot 与本契约）、新 rrule、`status=ACTIVE`；旧 kaneo-todo 保持 disabled。
4. 验证新执行端：各跑一次 `kanpilot claim --stage <s> --executor <id> --dry?`（无 dry 参数——直接运行 automation 或以 CLI 模拟），确认无请求即退/有请求正确处理。
5. 恢复自动推进：确认首轮回合无异常后视为接入完成。
6. 旧 skill（kaneo_dispatch.py/kaneo_bridge.py）保留只读备查；自动化不再调用 dispatch 助手（bridge 读取仍可用）。旧 manifest 文件原样保留。

## 5. 验收检查点

- 四个执行端 schedule 错开分钟、无请求即退。
- 桌面 `安排下一步` → `已排队`；执行端领取+心跳 → `已开始/派发中`；窗口关闭重开状态一致。
- 发送不明任务在桌面呈现证据与三个专用按钮；任一决议后才生成对应请求。
- running 任务迁移后由 monitor 请求接续观察，不靠旧锁。
