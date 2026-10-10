# Core Test Baseline — 2026-10-10（Phase 3 退出时更新）

## 1. 运行命令

```bash
cd core && python3 -m pytest tests -q
```

注：仓库根 `pytest.py` 是透传 shim——本机装有真实 pytest 9.1.1（`~/.pyenv/shims/pytest`），`python3 -m pytest` 会把参数交给真 pytest；无真实 pytest 时才回落 `unittest discover`。早前「本机缺 pytest」的判断已作废（见 `spike-verify-env-20261010.md`）。

## 2. 结果总览

| 阶段 | 命令 | 结果 |
|---|---|---|
| Phase 0 基线（commit `9d0ecf0`，`git worktree` 检出 `pre-engine-rewrite`） | `cd core && python3 -m pytest tests -q` | **7 failed**（见 §3） |
| Phase 3 退出（分支 `feat/engine-rewrite`，含全部 Phase 0–3 改动） | `cd core && python3 -m pytest tests -q` | **2150 passed, 0 failed, EXIT=0** |

**净变化：7 个基线失败全部修复，未引入任何新失败。** 测试总数从基线 2137 增至 2150（新增 `test_engine_*` 7 个文件、`test_abandon_task` 扩展 2 例）。

## 3. 基线 7 个失败的分类与修复

以下 7 个失败在 `pre-engine-rewrite`（9d0ecf0）上用 `git worktree` 独立复现，确认**全部为预存在失败，非本目标引入**。逐条分类如下：

### 3.1 夹具失效（fixture drift）→ 已修复（改测试，不动生产语义）

| 测试 | 根因 |
|---|---|
| `test_cli_review.py::test_run_stage_records_cost_before_settlement[False-claude]` | 见下 |
| `test_cli_review.py::test_run_stage_records_cost_before_settlement[False-codex]` | 同上 |
| `test_cli_review.py::test_run_stage_records_cost_before_settlement[True-claude]` | 同上 |
| `test_cli_review.py::test_run_stage_records_cost_before_settlement[True-codex]` | 同上 |
| `test_claude_cli.py::test_invalid_settings_env_settles_review_without_launch` | 同上 |

**根因**：`runner_scene` 夹具把 `conn` 指向一个**空库**（`tmp_path/isolated.db`，未插入任何任务行），但 `pi_desktop_review._collect_core_verification` 会**直连** `db.require_task(conn, task_id)` 读取核心验证回执——这条路径不经夹具 mock 的 `core_call`，于是抛 `not_found: task not found: t1`，`run_stage` 以 `needs_user`/`review_blocked` 收场并返回 1，测试断言 `code == 0` 失败。

**修复**（仅测试）：在 `runner_scene` 里把任务行真实 `INSERT INTO tasks`（含 `plan_path`/`plan_sha256`/`run_id`/`extra_json.execution_round`），使夹具反映「任务确实在库中」的现实。

### 3.2 生产代码缺空值保护 → 已修复（最小防御性改动）

| 测试 | 根因 |
|---|---|
| `test_cli_review.py::test_delivery_materials_full_diff_increment_and_large_diff` | 见下 |

**根因**：该测试显式传 `conn=None`（只读材料收集，不需要库），`_collect_core_verification` 直接 `db.require_task(None, ...)` → `AttributeError: 'NoneType' object has no attribute 'execute'`（db.py:457 `get_task`）。

**修复**（生产代码，1 处）：`pi_desktop_review._collect_core_verification` 开头加 `if conn is None: return {}`。语义正确：无 DB 连接即无核心验证回执可收集，返回空而非崩溃。

### 3.3 测试未结束规划轮次 → 已修复（改测试）

| 测试 | 根因 |
|---|---|
| `test_v2_planning.py::TestPurge::test_purge_session` | 见下 |

**根因**：测试 `claim` 了 plan 请求后**未结束该轮次**就调 `trash-session`；`_session_trash_reason` → `round_store._plan_round_has_activity` 检出仍有 `claimed` 请求，按 §5.16.2 以 `conflict`（"该规划轮次仍有排队或领取中的请求或活动轮次，暂不能移除会话"）阻止。该守卫是后加的行为约束，测试写于守卫之前，属于测试滞后。

**修复**（仅测试）：在 `trash-session` 前先 `report {outcome:"succeeded"}` 显式结束规划轮次。未改守卫——守卫语义正确（有在飞规划请求时不应删会话）。

## 4. 根目录集成测试

`python3 -m pytest tests -q` 的基线与退出值另见 `docs/evidence/integration-baseline-20261010.md`（与本文件同批采集）。

## 5. 结论

- Phase 0–3 对 core 隔离测试的净影响：**7 个预存在失败 → 0 失败，无新失败**。
- 生产代码改动仅 2 处且都是防御性/兼容性：`pi_desktop_review._collect_core_verification` 的 `conn is None` 早返回；`cmd_abandon` 的 `allow_purged` opt-in（Phase 0 新增命令自身）。
- 其余全部为测试侧修正，未改动任何被测生产语义。

## 6. 另：Phase 0 期间的两处真实回归修复（保留记录）

 baseline_scout 在 Phase 0 报称修复了两处，其中一处与基线状态重合、一处仍有效：

 | 文件 | 修复 | 现状 |
 |---|---|---|
 | `core/kanpilot/cmd_remote.py` ~396 | `cmd_advance_task` 的验证门在 `current["run_id"]` 为空时被误触发 | **仍有效**（`git diff` 可见 `if current["run_id"] and ...`），保留 |
 | `core/kanpilot/cmd_session.py` `_session_trash_reason` | 报称 `exited_request_id` 未初始化导致 `UnboundLocalError` | **与基线一致**：`pre-engine-rewrite` 的该文件已在 546 行有 `exited_request_id = None`，`git diff` 为空。即该"修复"实际是把文件恢复到了基线内容，无净变化 |
