# 切换手册（主 agent 执行记录）

按 docs/automation-contract.md §4。每步记录结果。

## 前置

- [ ] `core/`、`macos/`、`tests/` 集成完成，测试绿
- [ ] `python3 core/kanpilot.py doctor --json '{"online": true}'` 全绿
- [ ] `scripts/build_app.sh` 产出 `dist/KaneoPilot.app` 可启动

## 1. 停旧流程新领取

- [ ] `~/.codex/automations/kaneo-devin/automation.toml` → `status = "DISABLED"`
- [ ] `~/.codex/automations/kaneo-devin-2/automation.toml` → `status = "DISABLED"`
- [ ] `~/.codex/automations/kaneo-in-review/automation.toml` → `status = "DISABLED"`
- [ ] `~/.codex/automations/kaneo-todo-2/automation.toml` → `status = "DISABLED"`
- [ ] `kaneo-todo` 保持 `.disabled` 不动
- [ ] 不取消任何 Devin 会话；send_state=unknown 任务保持原样

## 2. 迁移

- [ ] `python3 core/kanpilot.py migrate` → 记录 backup_path、imported 计数、warnings
- [ ] `python3 core/kanpilot.py list-tasks` 抽查：running 任务在、needs_user 保留、ready 不带活动请求
- [ ] `python3 core/kanpilot.py list-requests --json '{"status":"active"}'`：running 任务各有 monitor 请求

## 3. 接新执行端

- [ ] 四个 automation.toml 换新 prompt（automation/prompts/*.md 注入 {PREAMBLE}）+ 新 rrule + `status="ACTIVE"`
- [ ] rrule：plan `FREQ=HOURLY;BYMINUTE=0,15,30,45`；dispatch `…4,19,34,49`；monitor `…8,23,38,53`；review `…12,27,42,57`
- [ ] 各执行端干跑一轮（或等首个 tick）：`claimed:false` 立即退/正常领取

## 4. 恢复与验证

- [ ] 桌面 `安排下一步` → `已排队` → 执行端领取+心跳 → `派发中` → confirm → `执行中`
- [ ] monitor 请求接续观察 running 任务
- [ ] 记录验证结果到本文件末尾

## 验证记录
