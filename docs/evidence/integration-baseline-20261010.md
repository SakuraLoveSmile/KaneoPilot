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
