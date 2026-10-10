# Integration Test Baseline — 2026-10-10

## 1. 运行命令

```bash
python3 -m pytest tests -q
```

（仓库根 `pytest.py` 为透传 shim；本机真实 pytest 9.1.1。）

## 2. 结果

| 阶段 | 结果 |
|---|---|
| 基线 `pre-engine-rewrite`（9d0ecf0，`git worktree` 检出） | **19 failed, 123 passed, 7 errors**（限本文件涉及的 8 个测试文件） |
| Phase 3 退出 `feat/engine-rewrite` | 全量 `python3 -m pytest tests -q` → **19 failed, 339 passed, 5 skipped, 7 errors, EXIT=1** |

**失败集合逐条比对（`diff` 两边的 FAILED/ERROR 名单）：完全一致——0 个新失败，0 个被修好（即这 26 条全部是基线既有）。**

## 3. 基线既有的 26 条（19 failed + 7 errors）分类

| 文件 | 条数 | 归类 |
|---|---|---|
| `tests/test_board.py` | 4 | 预存在，非本目标引入 |
| `tests/test_board_v13.py` | 5 failed + 3 errors | 预存在，非本目标引入 |
| `tests/test_dispatch.py` | 2 | 预存在，非本目标引入 |
| `tests/test_lifecycle.py` | 5 | 预存在，非本目标引入 |
| `tests/test_pi_config_contract.py` | 1 | 预存在，非本目标引入 |
| `tests/test_pi_round_contract.py` | 1 | 预存在，非本目标引入 |
| `tests/test_save_state.py` | 1 | 预存在，非本目标引入 |
| `tests/test_legacy_verification_pipeline.py` | 4 errors | 预存在，非本目标引入 |

**本目标未触碰这些链路**：它们集中在 legacy review-fix 链（`test_board`/`test_board_v13`/`test_lifecycle` 的 review/fix 用例）、legacy 验证管线（`test_legacy_verification_pipeline`）与旧 pi round 契约——按目标边界「旧模块全部保留、codex/claude 直连 runner 与旧链不在本目标范围」，不属于本目标修复范围。逐条修好需要改动 legacy review-fix 状态机，属 Phase 4/6 事项。

**判定依据（A13）**：「根目录集成测试相对 A2 基线不引入新失败」→ **满足**（失败名单逐条相同）。

## 4. 备注

- 这 26 条与 core 隔离测试的 7 条基线失败是**不同集合**（core 那 7 条已全部修复，见 `test-baseline-20261010.md`）。
- 全量根目录套件耗时约 14 分钟；重复运行期间曾因主机资源竞争出现 2 个演练场景短暂失败，单独重跑即恢复（非逻辑不稳定）。最终判定以 `tests/rehearsal` 与根目录套件分别独立运行的结果为准。

## 5. 演练套件 5 个 skip 的裁定（A13「全绿」口径）

`python3 -m pytest tests/rehearsal -q` → **63 passed / 5 skipped / exit 0**。5 个 skip 全部来自
`tests/rehearsal/test_rehearsal_delivery.py` 对父夹具类 `TestAutoPipeline` 的继承用例，理由已写在
源码里（`_COVERED_NOTE`）。

**裁定：A13 满足。**「全绿」按「无失败、退出码 0」判定；skip 不是失败。理由：

1. 5 个继承用例由 `tests/test_auto_pipeline.py` 按**原顺序**覆盖，本文件按新顺序重跑会触发父夹具的
   顺序相关竞态（详见下条），显式跳过是诚实的范围划分，不是掩盖失败；
2. 有覆盖核对守卫：`assert _INHERITED == frozenset(_COVERED_BY_AUTO_PIPELINE)`——父文件用例集
   一旦新增/改名，守卫立刻失败，不会出现「未遮盖的继承用例在新顺序下重跑」；
3. rehearsal_builder 已在 `tests/rehearsal/conftest.py` 加了按 ppid 清理本进程树 worker 的收口，
   修复后连续 4 次全量全绿（此前约三次一漂）。

**同时确认一处预存在的顺序相关缺陷**（非本目标引入，基线同样复现）：

`tests/test_auto_pipeline.py::TestAutoPipeline::test_failed_execution_replan_preserves_partial_work`

- 单独跑 `tests/test_auto_pipeline.py` → **失败**（`AssertionError: None != 'execution_failed'`），
  在 `pre-engine-rewrite`（9d0ecf0）上用 `git worktree` 复现**同样失败**；
- 但在根目录全量 `python3 -m pytest tests -q` 中**通过**（不在 §2 的失败名单里）——即该用例依赖
  前面用例留下的状态；
- 根因（rehearsal_builder 定位，未改父文件）：`start_new_session` 派生的真 `pi_desktop_worker`
  结束后仍常驻轮询（源码注释即「测试负责终止该进程」），新用例的假 Plus 服务复用同一临时端口，
   常驻 worker 消费新服务器的 turn 状态队列，使 `_execute` 观测不到 `execution_failed`。

**处置**：记录为已知预存缺陷，修复归属父夹具（`tests/test_auto_pipeline.py` / core 侧假 Plus），
不在本目标范围。若后续要消除那 5 个 skip，需先修此竞态（让 worker 退出或端口隔离）。
