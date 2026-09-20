# 切换手册（主 agent 执行记录）

按 docs/automation-contract.md §4。每步记录结果。

## 前置

- [x] `core/`、`macos/`、`tests/` 集成完成，测试绿（核心 47/47、集成 123/123）
- [x] `python3 core/kanpilot.py doctor` 离线全绿（Kaneo ping 离线跳过）
- [x] `scripts/build_app.sh` 产出 `dist/KaneoPilot.app`，已启动并初始化真实 DB

## 1. 停旧流程新领取

- [ ] **需 App 侧应用**：Codex 自动化真实存储是 `~/.codex/sqlite/codex-dev.db`（`automations` 表），`automation.toml` 文件是导出物，改文件不影响调度器。旧 4 条在库中仍 ACTIVE（kaneo-devin-2 next_run 23:01）。应用方式见 §3 注。
- [x] `~/.codex/automations/kaneo-*/automation.toml` 文件已置 `status = "DISABLED"`（导出物已改，待库侧生效）
- [x] `kaneo-todo` 无 automation.toml，保持不动
- [x] 未取消任何 Devin 会话；send_state=unknown 任务保持原样

## 2. 迁移

- [x] `migrate` 成功：backup `~/Library/Application Support/KaneoPilot/migration-20260920T125601Z`；imported tasks=10 requests=5 events=11；warnings 无
- [x] `list-tasks` 抽查：2 running、3 in_review、2 ready、2 done、1 needs_plan 符合预期
- [x] running 任务各有 queued monitor 请求；in_review 各有 queued review 请求

## 3. 接新执行端

- [x] 四个 automation.toml 已安装至 `~/.codex/automations/kaneo-{plan,dispatch,monitor,review}/`（文件就位，格式与 app 导出物一致）
- [x] rrule 与冻结表一致：plan `…0,15,30,45`；dispatch `…4,19,34,49`；monitor `…8,23,38,53`；review `…12,27,42,57`
- [ ] **需 App 侧注册**：新 4 条不在 codex-dev.db，调度器不会触发。应用载荷已备好：`automation/rendered/apply-updates.json`（8 条：4 旧 DISABLED + 4 新 ACTIVE，camelCase 与 headless-v1 迁移同格式）。应用通道：Codex App 内会话的 `automation_update` 工具（mode=update，逐条传入），或在 App 自动化界面手动停用旧 4 条并按文件内容新建 4 条。**不直接写 codex-dev.db。**
- [x] 核心命令链路已由人工执行端验证：plan-scan 领取→bridge 枚举→sync-tasks→report succeeded（run `4b66fd15`）；plan 领取→远端任务已删→report needs_user（run `f28f352a`）

## 4. 恢复与验证

- [x] `enqueue` 排队验证：dispatch 请求 `48d9750c`（task `yfgrc2fi`，send_state=not_sent）已 queued，等 dispatch 执行端 13:19 起领取
- [ ] 执行端领取+心跳 → `派发中` → Devin Agent 附件发送 → confirm → `执行中`（等自动化首个触发窗口）
- [ ] monitor 请求接续观察 running 任务（队列中 2 条，等 13:08 起触发）
- [ ] review 请求处理 in_review 任务（队列中 2 条，等 13:12 起触发）
- [x] 验证结果记录到本文件末尾

## 验证记录

### 2026-09-20（UTC 时间）

- 迁移：backup `migration-20260920T125601Z`；tasks=10 requests=5 events=11；warnings 无。真实 DB `~/Library/Application Support/KaneoPilot/kanpilot.db`。
- **重要更正**：自动化真实存储在 `codex-dev.db`，文件为导出物。旧 4 条库中仍 ACTIVE（devin-2 next_run 23:01 本地）；新 4 条已装文件但未注册。已生成 `automation/rendered/apply-updates.json` 供 App 内 `automation_update` 应用。
- **真实 executor 闭环 1（plan-scan）**：request `445daf3f` / run `4b66fd15`。bridge `list-todos` 枚举 5 项目 2 个 To Do → 规范快照落 `~/Library/Application Support/KaneoPilot/sources/` → `sync-tasks` upserted=2 changed=0 → `report` succeeded。任务标题/项目字段已填充。
- **真实 executor 闭环 2（plan 异常路径）**：request `acf6f03d` / run `f28f352a`，task `ey9s9vq8`（STU-2）。远端核验：该任务已从 Kaneo 删除（/api/task 400，全项目遍历确认不存在）。`report needs_user` → task `needs_user`，failure_reason 已落库，等人工 resolve。
- **dispatch 排队**：request `48d9750c`（task `yfgrc2fi`，send_state=not_sent，计划已发布）。queued 保持有效，kaneo-dispatch 注册后首个窗口（:04/:19/:34/:49）即会领取——可作为新执行端真实派发验证。
- 待观察（依赖执行端注册）：dispatch 真实领取与 Devin 发送、monitor（2 条 queued）、review（2 条 queued）、done 归档。
- 已知数据缺口（不影响切换）：迁移任务的 `source_path` 指向旧扁平 schema 快照、`requirements_sha256` 为空；首个 plan-scan 已把仍存在的 To Do 任务刷新为规范快照。已删远端任务（ey9s9vq8）走 needs_user 人工处理。
