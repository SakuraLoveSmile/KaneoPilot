# 切换手册（主 agent 执行记录）

按 docs/automation-contract.md §4。每步记录结果。

## 前置

- [x] `core/`、`macos/`、`tests/` 集成完成，测试绿（核心 47/47、集成 123/123）
- [x] `python3 core/kanpilot.py doctor` 离线全绿（Kaneo ping 离线跳过）
- [x] `scripts/build_app.sh` 产出 `dist/KaneoPilot.app`，已启动并初始化真实 DB

## 1. 停旧流程新领取

- [x] `~/.codex/automations/kaneo-devin/automation.toml` → `status = "DISABLED"`
- [x] `~/.codex/automations/kaneo-devin-2/automation.toml` → `status = "DISABLED"`
- [x] `~/.codex/automations/kaneo-in-review/automation.toml` → `status = "DISABLED"`
- [x] `~/.codex/automations/kaneo-todo-2/automation.toml` → `status = "DISABLED"`
- [x] `kaneo-todo` 无 automation.toml，保持不动
- [x] 未取消任何 Devin 会话；send_state=unknown 任务保持原样

## 2. 迁移

- [x] `migrate` 成功：backup `~/Library/Application Support/KaneoPilot/migration-20260920T125601Z`；imported tasks=10 requests=5 events=11；warnings 无
- [x] `list-tasks` 抽查：2 running、3 in_review、2 ready、2 done、1 needs_plan 符合预期
- [x] running 任务各有 queued monitor 请求；in_review 各有 queued review 请求

## 3. 接新执行端

- [x] 四个 automation.toml 已安装至 `~/.codex/automations/kaneo-{plan,dispatch,monitor,review}/`，`status="ACTIVE"`
- [x] rrule 与冻结表一致：plan `…0,15,30,45`；dispatch `…4,19,34,49`；monitor `…8,23,38,53`；review `…12,27,42,57`
- [x] 真实领取已验证：plan-scan 请求由 kaneo-plan 领取→bridge 枚举→sync-tasks→report succeeded（run `4b66fd15`）；plan 请求领取→远端任务已删→report needs_user（run `f28f352a`）

## 4. 恢复与验证

- [x] `enqueue` 排队验证：dispatch 请求 `48d9750c`（task `yfgrc2fi`，send_state=not_sent）已 queued，等 dispatch 执行端 13:19 起领取
- [ ] 执行端领取+心跳 → `派发中` → Devin Agent 附件发送 → confirm → `执行中`（等自动化首个触发窗口）
- [ ] monitor 请求接续观察 running 任务（队列中 2 条，等 13:08 起触发）
- [ ] review 请求处理 in_review 任务（队列中 2 条，等 13:12 起触发）
- [x] 验证结果记录到本文件末尾

## 验证记录

### 2026-09-20（UTC 时间）

- 迁移：backup `migration-20260920T125601Z`；tasks=10 requests=5 events=11；warnings 无。真实 DB `~/Library/Application Support/KaneoPilot/kanpilot.db`。
- 旧自动化 4 条 DISABLED 已核验（kaneo-devin / kaneo-devin-2 / kaneo-in-review / kaneo-todo-2）。
- 新执行端 4 条已安装 ACTIVE，rrule 与冻结表一致。
- **真实 executor 闭环 1（plan-scan）**：request `445daf3f` / run `4b66fd15`。bridge `list-todos` 枚举 5 项目 2 个 To Do → 规范快照落 `~/Library/Application Support/KaneoPilot/sources/` → `sync-tasks` upserted=2 changed=0 → `report` succeeded。任务标题/项目字段已填充。
- **真实 executor 闭环 2（plan 异常路径）**：request `acf6f03d` / run `f28f352a`，task `ey9s9vq8`（STU-2）。远端核验：该任务已从 Kaneo 删除（/api/task 400，全项目遍历确认不存在）。`report needs_user` → task `needs_user`，failure_reason 已落库，等人工 resolve。
- **dispatch 排队**：request `48d9750c`（task `yfgrc2fi`，send_state=not_sent，计划已发布）。queued，等 kaneo-dispatch 首个窗口（每小时 :04/:19/:34/:49）。
- 待观察：dispatch 真实领取与 Devin 发送、monitor（2 条 queued）、review（2 条 queued）、done 归档。
- 已知数据缺口（不影响切换）：迁移任务的 `source_path` 指向旧扁平 schema 快照、`requirements_sha256` 为空；首个 plan-scan 已把仍存在的 To Do 任务刷新为规范快照。已删远端任务（ey9s9vq8）走 needs_user 人工处理。
