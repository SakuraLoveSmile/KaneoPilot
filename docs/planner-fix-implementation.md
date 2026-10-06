# 规划修复实施说明（本次冻结契约）

对应施工交接稿（T1–T4）。本文档由主 agent 维护，是所有执行者唯一的接口依据；
分歧先改本文档与 `docs/protocol.md`，再改代码。各执行者的独占写入范围见
交接稿分工表，**不得越权改共享文件**。

## 冻结接口（其他执行者可依赖，不再变动）

### 1. `kanpilot.codex_provider`（子 agent A 实现，其余只读引用）

```python
def resolve_provider(codex_home: Path) -> ProviderResolution
```

`ProviderResolution`（dataclass，字段冻结）：

| 字段 | 类型 | 含义 |
|---|---|---|
| `provider_id` | `str \| None` | `config.toml` 的 `model_provider`；`None` = 未配置自定义服务商 |
| `name` | `str \| None` | 服务商显示名（`model_providers.<id>.name`） |
| `base_url` | `str \| None` | `model_providers.<id>.base_url` |
| `wire_api` | `str \| None` | 固定协议标识（如 `responses`） |
| `requires_openai_auth` | `bool` | 服务商声明（默认 False） |
| `env_key` | `str \| None` | 实际传给 codex 的凭据变量名（配置内 `env_key` 原样；内联 `experimental_bearer_token` 转为 `KANPILOT_CODEX_PROVIDER_KEY`） |
| `env` | `dict[str,str]` | 须注入子进程的环境变量（内联 token 的落点；**值只在内存**） |
| `config_args` | `list[str]` | `-c` 覆写片段（`key=value`，value 为 TOML 字面量） |
| `auth_ready` | `bool` | 认证输入就绪（env_key 值已就位 / requires_openai_auth 有 auth.json / 无认证要求） |
| `auth_detail` | `str` | 认证就绪说明（中文，不含值） |
| `model_catalog_json` | `str \| None` | 顶层 `model_catalog_json` 路径（存在时原样沿用） |
| `summary` | `dict` | 脱敏摘要：`{provider_id, name, base_url, wire_api, requires_openai_auth, env_key, auth_ready, auth_detail, model_catalog_json}`——可写运行记录/命令输出，**无凭据值** |

- 配置损坏（TOML 解析失败/结构非法）、`model_provider` 指向不存在的服务商、
  认证方式缺失（env_key 无可用环境值且非 requires_openai_auth）→ 抛
  `KanpilotError("validation_failed", "<中文原因>")`；**禁止静默回落官方服务商**。
- `provider_id` 为空（无 `model_provider` 或无 `model_providers` 自定义段）→
  `config_args=[]`、`env={}`，默认认证路径（auth.json 存在性由既有
  `check_available` 负责）。
- `config_args` 至少含：`model_provider`、`<id>.name`、`<id>.base_url`、
  `<id>.wire_api`、`<id>.requires_openai_auth`、`<id>.env_key`（有凭据时）、
  `model_catalog_json`（存在时）。**绝不**以 argv 传递 bearer token。
- TOML 解析：Python ≥3.11 用 `tomllib`；3.9/3.10 用 vendored `tomli`
  （`core/_vendor/tomli`，已由主 agent 落位，经 util 同法 `sys.path` 注入或
  `try import tomllib except ImportError: from _vendor…` 选定其一并注释）。
- `model_catalog_json` 指向的 JSON 由本模块提供只读解析辅助：
  `def model_catalog(path) -> list[dict]`，条目 `{id(slug), name(display_name),
  reasoning_efforts([effort…])}`（`supported_reasoning_levels[].effort`）；
  解析失败返回 `[]`（不抛错，由 capabilities 标「未验证」）。

### 2. `codex_planner.py`（子 agent C 独占）对 A 的消费方式

- `launcher_argv(job, schema_path, out_path, images, provider=None)`：
  `provider` 为 `ProviderResolution \| None`（None=缺省路径）。追加
  `provider.config_args`（每个 `-c <arg>`），`experimental_bearer_token`
  类字段**不**进入 argv。
- `child_env(provider=None)`：在现有基础上并入 `provider.env`。
- `check_available()`：保留现有两项 + 附解析结果：
  `(ok, "codex=…；auth=…；provider=custom/Sub2API(认证就绪)")`；
  解析失败 → `(False, "codex 配置错误：<原因>")`。**「配置就绪」不得表述为
  「真实服务验证成功」。**
- `provider` 为 None 时（解析未做/默认路径）argv 与现状一致（只加 model/effort）。

### 3. `kanpilot.planner_config`（主 agent 实现，B/UI 只读引用）

```python
PLANNER_GROUPS = ("standard", "review_fix")
DEFAULTS = {"standard": dict(constants.STANDARD_PLANNER),
            "review_fix": dict(constants.REVIEW_FIX_PLANNER)}

def load(conn) -> dict | None          # settings.planner_config 原始值（无则 None）
def save(conn, groups: dict, now) -> dict   # 原子写回（含 updated_at）
def snapshot(conn, group: str) -> dict      # 生效值 {planner_model, reasoning_effort}
def validate_group(config, catalog) -> str | None  # 中文错误/None（catalog=[] 未验证放行）
def validate_model_id(value) -> str | None         # 去空白/拒空/拒控制字符
```

### 4. CLI 命令（主 agent 注册在 cli.py COMMANDS）

- `get-planner-config {}` →
  `{"config": {...}|null, "defaults": {"standard":{...},"review_fix":{...}},
   "updated_at": str|null}`。
- `set-planner-config {"standard"?: {planner_model, reasoning_effort},
  "review_fix"?: {...}}` → `{"config": {...}, "updated_at": str}`；
  校验失败 `bad_request` 且旧值不变。
- `planner-capabilities {}` →
  `{"codex": {"path","version"}, "provider": summary|null,
   "auth": {"ready": bool, "detail": str},
   "models": [{"id","name","reasoning_efforts"}], "models_source": str|null,
   "models_verified": bool}`；不发模型请求、不返回凭据。
- `doctor` 的 `codex_planner` 检查 detail 追加服务商摘要（C 的
  `check_available` 已提供文案）。

### 5. `planfile.check_plan_meta` 签名（B 改、调用方传参）

```python
def check_plan_meta(meta: dict, plan_kind: str,
                    expected_model: dict | None = None) -> None
```

- `expected_model={"planner_model":…,"reasoning_effort":…}` 提供时按它比对；
  **不提供时沿用现有常量规则**（旧记录缺登记的兼容路径）。

### 6. `util.redact(text, secrets)`（主 agent 提供）

```python
def redact(text: str, secrets: list[str] | None = None) -> str
```

- 置空/空串原样返回；每个 secret（长度≥8 才替换，避免误伤）以 `«redacted»`
  替换；另把 `Authorization: Bearer <值>`、`api_key=…`/`token=…` 模式值脱敏。
- C 负责把 `provider.env` 的值集合 + 解析出的 token 传给 redact，在
  events.jsonl / codex.jsonl 落盘前处理（HTTP 状态码、模型名、错误正文保留）。

### 7. 运行记录追加字段（C 写 job.json，B/D 只读）

`job["provider"] = provider.summary`（有自定义服务商时）；否则
`{"provider_id": None, "detail": "codex 默认认证路径"}`。

## 各执行者待办（与交接稿一致）

- A：`codex_provider.py` + `core/tests/test_planner_provider.py`（fake
  config.toml 全分支；脱敏断言：token 不出现在 config_args/summary）。
- B：`planner_inputs.py`（start-plans 用 `planner_config.snapshot`）、
  `planfile.py`（expected_model 参数）、`cmd_exec.py`（publish-plan 用请求
  payload.model_config 做 expected_model）、`dispatch_validate.py`（用任务
  登记 planner_model/reasoning_effort；缺登记回落旧规则）+
  `core/tests/test_planner_config.py`。
- C：`codex_planner.py` / `planner_runner.py` / `dsh_events.py` +
  `core/tests/test_planner_errors.py`；最后接入 A 的 provider（接 argv/env/
  job 记录）；错误分类 `model_unavailable|auth_failed|request_rejected|
  timeout|process_exit|crash`；`_EVENT_TO_MESSAGE` 补 `system`。
- D：macos 侧（见交接稿 T4），消费命令 4 的 JSON。
- 主 agent：docs、cli.py、`planner_config.py`、`codex_config` 命令、
  util.redact、tomli vendored、helpers.py 的 `make_codex_fixture`、验收。
