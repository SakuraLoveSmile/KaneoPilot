# Spike: Fallback Execution Model ID — 2026-10-10

## 采集方式（真实探测，非推测）

对真实 Plus（pi-desktop-plus 0.15.7，控制面 `http://127.0.0.1:37123/mcp`）发起**只读** MCP 探测：`initialize` → `notifications/initialized` → `tools/call pi_desktop_invoke {operation:"providers/list"}` → 对每个 provider 再调 `providers/listModels {source:"cache"}`。未调用任何写操作，未输出 token，未修改任何配置。

- 采集日期：2026-10-10
- serverInfo：`{"name": "pi-desktop", "version": "0.15.7"}`
- 探测脚本（临时，未入库）：`$PI_SCRATCH_DIR/probe_plus_models.py`
- 完整目录快照：`plus-model-catalog-20261010.json`（本目录）

## 目录概况

4 个 provider，共 153 个模型（`source=cache`，均为本地缓存目录，符合 `pi_desktop_backend.py` 对 `cache`/`fallback` 来源的白名单要求）：

| provider id | 启用 | 默认模型 | 模型数 |
|---|---|---|---|
| `707a4c67-4ee0-484d-b635-730372501eaf` | 是 | `deepseek/deepseek-v4.1-flash` | 87 |
| `29801b84-7fc1-4d46-aa0e-7762b934b403` | 是 | `gemini-3.7-flash-high` | 14 |
| `c43106c3-791e-498a-bafd-58b7e790d528` | 是 | `step-3.7-flash` | 9 |
| `d5c3f00e-6a39-4b1d-8c68-651a6bf1aed9` | 是 | `mimo-v2.6-flash-free` | 43 |

## 结论：fallback 执行模型 id

**主执行模型**（既定决策，不重开）= `deepseek/deepseek-v4.1-flash`，`model_source=inherit`，provider `707a4c67-4ee0-484d-b635-730372501eaf`（该 provider 的默认模型即此，生产 `settings.executor_runs` 亦为此值）。

**fallback 执行模型 id = `google/gemini-3.7-flash`**（同一 provider `707a4c67-…` 内切换）。

选定理由：

1. **同 provider 切换**：fallback 复用主模型的 `connection_file` 与鉴权，不需要第二套 provider 配置，也不需要新增凭据——符合「不输出/不持久化 Kaneo token」「不修改 Plus」边界。
2. **跨厂商**：主模型属 deepseek 家族，fallback 属 google 家族，厂商级故障不相关（空 diff / 连续失败时换家族才有意义）。
3. **同档位**：`gemini-3.7-flash` 与 `deepseek-v4.1-flash` 同为 flash 档，成本与延迟量级一致，不会因 fallback 而击穿 `max_task_cost_usd=10`。
4. **目录内确认存在**：`google/gemini-3.7-flash` 在 provider `707a4c67-…` 的 87 个 cache 模型中，`pi_desktop_backend.py` 的 `resolve_pi_desktop_model` 可在 cache 来源中解析到，不会落入「模型不存在」分支。

备选（同 provider 内可替换，按优先级）：`stepfun/Step-3.7-Flash` → `z-ai/glm-5.3-flash` → `Qwen/Qwen3.8-Flash` → `deepseek/deepseek-v4.1-flash-fast`（同家族，仅在主模型限流时用）。

## 形态与接线

- 引擎空 diff / 连续质量失败时的换模型：写 `model_source="custom"` + `provider_id="707a4c67-4ee0-484d-b635-730372501eaf"` + `model_id="google/gemini-3.7-flash"`（`exec_config.py` 的 `resolve_execution_config` 对 `custom` 要求三者齐全）。
- 事件记录：`model: fallback`（`model_source=custom`）+ 原主模型快照，便于审计「是否静默换模型」——规格要求「探针失败即 `validation_failed`、不静默换模型」。
- fallback 模型 id 不进常量硬编码：由 `exec_config` / 项目映射可配，缺省取本文件结论值。

## 失效与刷新

- 目录为 `source=cache`，可能随 Plus 升级/账号变更过期。`kanpilot doctor --live` 的 plus 探针应能复跑本探测并核对 fallback id 仍存在；不存在时给出明确修复项（换备选或刷新目录），**不得**静默换模型。
- 复跑命令：MCP `pi_desktop_invoke {operation:"providers/list"}` + `{operation:"providers/listModels", args:[{providerId, source:"cache"}]}`。
