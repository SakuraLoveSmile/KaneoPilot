# Schema v6 迁移顺延记录 — 2026-10-10

## 结论

**`SCHEMA_VERSION` 保持 5，v6 新增列 / GC 不在本轮执行。** 新引擎（`core/kanpilot/engine/`）
的全部状态都落在**既有结构**上，没有引入任何新列、新表或 schema 升级：

| 引擎状态 | 落点 | 说明 |
|---|---|---|
| `engine_mode`（legacy/shadow/live） | `settings.project_mappings.items[pid].engine_mode` | 映射**条目层**键，`set-project-engine-mode` 写入；旧行缺该键即回落进程缺省 |
| `verify_env` | `settings.project_mappings.items[pid].verify_env` | 同上，`set-project-verify-env` 写入 |
| `fallback_model` | `settings.project_mappings.items[pid].fallback_model` | 同上，`set-project-fallback-model` 写入；缺省不配置 = 不换模型 |
| 核心亲判回执 | `tasks.extra_json` → `engine_verdict` | `engine-verdict` 写入；不覆盖交互式 `verification_checks` 回执 |
| 有界补救轮次记账 | `tasks.extra_json` → `engine_fix_rounds` | 同计划 ≤2 轮，按 `plan_sha256` 判定 |
| 一次性模型覆盖 | `tasks.extra_json` → `engine_model_override` | dispatch claim 冻结轮次快照时应用，新轮次开始后由引擎清除 |
| 模型选择审计 | `events`（kind=`model` / `model_hint`） | `model: fallback` / `model: primary` + 配置提示 |
| 引擎租约与心跳 | `settings`（engine health 键） | DB 级单实例租约 |
| 待处理收件箱 | 只读聚合 `needs_user` 行 | 无自有存储，`attention-list` 与看板同源 |

## 顺延理由（旧数据保留优先于升级）

1. **无需求**：本轮引擎能力全部可由 v5 结构承载（上表），v6 的新增列没有对应消费者。
   为升级而升级只会增加迁移面。
2. **可回退**：strangler 三态（`engine_mode: legacy|shadow|live`）要求旧链路随时可回。
   schema 一旦升级，回退到旧版本二进制就要先降库——「旧数据必须保留、可回退」的边界被削弱。
   保持 v5 时，旧版二进制与新版二进制可读同一个 DB。
3. **GC 风险**：v6 原计划包含的清理（历史 rounds / attempts / events 裁剪）会**删除审计证据**。
   verdict 由核心亲判、外部副作用先写 `runtime_operations` 回执，这些都依赖历史行可追溯；
   在沙盒 live 验收（`docs/evidence/sandbox-live-<date>.md`）跑完之前不做任何裁剪。
4. **零迁移成本**：所有新键都写在 `extra_json` / `settings` 的 JSON 值内，旧行缺键即缺省，
   不存在「读旧数据要先升级」的窗口。

## 重新评估的触发条件

满足任一条再重开 v6 评估（届时仍须先写迁移 + 回退验证，再动 `SCHEMA_VERSION`）：

- 上表任一状态被证明需要按列索引/检索（例如收件箱要按 `reason_code` 建索引查询），
  JSON 内扫描成为可测知的瓶颈；
- 沙盒 live 验收通过、旧模块按 strangler 计划下线，历史行的保留义务结束；
- 出现跨版本二进制必须共存之外的新需求（例如多工作区共享同一 DB）。

## 复核方式

- `grep -n "^SCHEMA_VERSION" core/kanpilot/constants.py` → `SCHEMA_VERSION = 5`
- `cd core && python3 -m kanpilot migrate` 在 v5 库上为幂等空操作（无待执行迁移）
- 引擎单测：`cd core && python3 -m pytest tests/test_engine_fallback.py -q`
