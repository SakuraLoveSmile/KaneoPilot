# Abandon Stuck Tasks — 2026-10-10

## 1. 决策依据：优先复用 `resolve`，但封闭枚举不支持 `abandoned`

- 检查 `core/kanpilot/constants.py` 中 `VALID_RESOLVE_ACTIONS`：
  ```python
  VALID_RESOLVE_ACTIONS = (
      "retry", "restore", "accept_duplicate", "mark_sent",
      "replan", "release_lost", "mark_done", "dismiss", "resume", "accept_verification", "recover_review", "reverify",
  )
  ```
- `core/kanpilot/cmd_queue.py:cmd_resolve` 在入口处执行：
  ```python
  if action not in constants.VALID_RESOLVE_ACTIONS:
      raise util.KanpilotError("bad_request", f"invalid action {action}")
  ```
- 结论：`resolve` 不支持自定义 `resolution=abandoned`，因此按 Lead 指示新增最小 `abandon-task` 核心命令（`core/kanpilot/cmd_abandon.py`）。

## 2. 6 个 stuck 任务基线状态（通过 `get-task` 查询）

| task_id | status | stage | save_state | 说明 |
|---------|--------|-------|------------|------|
| `gdpoa1zo0vbozksnjkui45br` | needs_plan | plan | active | 卡在需规划，等待工作区登记 |
| `krw2in4s1l2257ih6dn90f9u` | needs_plan | plan | active | 同上 |
| `ozb7d8vw1sbp653zs7u0jn2f` | in_review | review | active | 卡在 review |
| `ey9s9vq8q82gkb1mzj1lxchf` | needs_user | plan | purged | 终态，不可修改 |
| `uycfqe3hvh454ml6mdq80vic` | ready | dispatch | purged | 终态，不可修改 |
| `yfgrc2fikdzbl8c4dd65tp7q` | ready | dispatch | purged | 终态，不可修改 |

## 3. 执行 abandon 命令与结果

### 3.1 `gdpoa1zo0vbozksnjkui45br`

**命令：**
```bash
python3 core/kanpilot.py abandon-task --json '{
  "task_id": "gdpoa1zo0vbozksnjkui45br",
  "note": "stuck in needs_plan with missing workspace registration; abandoning per phase0 cleanup"
}'
```

**返回摘要：**
```json
{
  "ok": true,
  "deduplicated": false,
  "task": {
    "task_id": "gdpoa1zo0vbozksnjkui45br",
    "save_state": "archived",
    "status": "needs_plan",
    "extra": {
      "resolution": {
        "action": "abandoned",
        "actor": "user",
        "at": "2026-10-10T09:33:51.391845+00:00",
        "note": "stuck in needs_plan with missing workspace registration; abandoning per phase0 cleanup"
      }
    }
  }
}
```

### 3.2 `krw2in4s1l2257ih6dn90f9u`

**命令：**
```bash
python3 core/kanpilot.py abandon-task --json '{
  "task_id": "krw2in4s1l2257ih6dn90f9u",
  "note": "stuck in needs_plan with missing workspace registration; abandoning per phase0 cleanup"
}'
```

**返回摘要：**
```json
{
  "ok": true,
  "deduplicated": false,
  "task": {
    "task_id": "krw2in4s1l2257ih6dn90f9u",
    "save_state": "archived",
    "status": "needs_plan",
    "extra": {
      "resolution": {
        "action": "abandoned",
        "actor": "user",
        "at": "2026-10-10T09:33:53.905348+00:00",
        "note": "stuck in needs_plan with missing workspace registration; abandoning per phase0 cleanup"
      }
    }
  }
}
```

### 3.3 `ozb7d8vw1sbp653zs7u0jn2f`

**命令：**
```bash
python3 core/kanpilot.py abandon-task --json '{
  "task_id": "ozb7d8vw1sbp653zs7u0jn2f",
  "note": "stuck in in_review with no progress; abandoning per phase0 cleanup"
}'
```

**返回摘要：**
```json
{
  "ok": true,
  "deduplicated": false,
  "task": {
    "task_id": "ozb7d8vw1sbp653zs7u0jn2f",
    "save_state": "archived",
    "status": "in_review",
    "extra": {
      "resolution": {
        "action": "abandoned",
        "actor": "user",
        "at": "2026-10-10T09:33:54.XXX000+00:00",
        "note": "stuck in in_review with no progress; abandoning per phase0 cleanup"
      }
    }
  }
}
```

## 4. 最终状态验证

```text
gdpoa1zo0vbozksnjkui45br: needs_plan plan archived abandoned
krw2in4s1l2257ih6dn90f9u:  needs_plan plan archived abandoned
ozb7d8vw1sbp653zs7u0jn2f:  in_review review archived abandoned
ey9s9vq8q82gkb1mzj1lxchf: needs_user plan purged (不可修改)
uycfqe3hvh454ml6mdq80vic: ready dispatch purged (不可修改)
yfgrc2fikdzbl8c4dd65tp7q: ready dispatch purged (不可修改)
```

- 3 个 active stuck 任务：`status/stage` 未改，仅 `save_state` 切为 `archived`，`extra.resolution.action=abandoned` 可 DB 查询。
- 3 个 purged 任务：保持终态，`abandon-task` 对 purged 任务返回 `conflict`，不修改任何数据。

## 5. 复用/新增决策记录

- **优先复用失败**：`resolve` 的 `VALID_RESOLVE_ACTIONS` 为封闭枚举，不含 `abandoned`，入口即校验失败。
- **新增命令**：`abandon-task` 为最小实现，仅修改 `save_state`（默认 `archived`）和 `extra_json`（写入 `resolution={action, note, at, actor}`），不触碰状态机字段。
- **幂等保护**：重复执行返回 `deduplicated: true`，不影响现有数据。

## 6. 补充：3 个 purged 遗留任务的 abandon 登记（Lead 追加）

 原 `abandon-task` 对 purged 任务一律返回 `conflict`，导致 A3「6 个任务已按 abandon 处置（DB 可查）」只能覆盖 3 个 active 任务。已在 `core/kanpilot/cmd_abandon.py` 增加显式 opt-in 参数 `allow_purged`：

 - 语义：purged 任务状态已终，**不改变 `status`/`stage`/`save_state`**，只写入 `extra.resolution={action:"abandoned", purged:true, ...}` 与 `task_abandoned` 事件，使放弃决定在 DB 可查。
 - 默认仍拒绝（`allow_purged` 缺省 false），保持原有冲突保护；`core/tests/test_abandon_task.py` 增加 2 个用例覆盖新分支与原冲突分支（共 6 用例全通过）。
 - CLI 同步注册 `allow_purged`（`core/kanpilot/cli.py`）。

 执行记录（2026-10-10T10:02:51Z）：

 | task_id | status | save_state | resolution.action | resolution.purged |
 |---|---|---|---|---|
 | `yfgrc2fikdzbl8c4dd65tp7q` | ready | purged | abandoned | true |
 | `uycfqe3hvh454ml6mdq80vic` | ready | purged | abandoned | true |
 | `ey9s9vq8q82gkb1mzj1lxchf` | needs_user | purged | abandoned | true |

 **6/6 全部 DB 可查**；`events` 中 6 条 `task_abandoned`（actor=user）。已 done 的 8 个 9 月遗留行（`iuhj2yhyyoq8gwzsxeeeo3hd`、`qfknffl0ecvoy93f2eqntpw1`、`iuj7drzyzdc139wkpli4grk5`、`g5qd79sdtb2f7wk27jmm4zj2`、`ynvxqmnzn4kikiafhze4v7xz`、`fen4jh136mrv2ey6g0bjfcu0`、`ejng3ptpydtd04jsb7m55ia3`、`uedvmrsucbgy8v2e1di4b789`）`resolution` 均为空、保持只读历史未动。Kaneo 侧由用户人工收尾，本次未调用任何 Kaneo 写接口。
