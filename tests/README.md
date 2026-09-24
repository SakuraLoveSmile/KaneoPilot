# KaneoPilot 集成测试套件

对 `core/kanpilot.py` 的**契约级**集成测试：全部经 `subprocess` 调 CLI（JSON 进出、断言退出码与字段级行为），不 import 核心代码、不直接写业务状态。唯一例外是 `conftest.db_exec` 系列**时间旅行后门**（把租约/完成时间 UPDATE 到过去），只用于表达 CLI 无法表达的时间流逝；schema 依据 state-model §1 冻结表结构。

## 运行

```bash
cd <repo 根>
python3 -m unittest discover -s tests -v          # 全部（数分钟，含真实多进程竞争）
python3 -m unittest discover -s tests -v -k dispatch   # 按名称过滤（Python 3.11+）
```

整合阶段一键执行：

```bash
cd <repo 根> && python3 -m unittest discover -s tests -v
```

- 需要 Python 3.9+；**本机无 pytest**——套件为 pytest 风格（fixture 注入 +
  parametrize），经 `tests/pytest.py`（最小 pytest API 桩）与
  `tests/unittest_compat.py`（`load_tests` 收集引擎：函数级 fixture /
  autouse / parametrize 展开 / yield 收口）在 `unittest discover` 下全量
  执行；每个 `test_*.py` 末尾的 `load_tests` 钩子是接入口。若环境装有真实
  pytest，`import pytest` 命中真模块、桩不生效，`python3 -m pytest tests -q`
  亦可跑同套件。
- 测试绝不访问真实 Kaneo、真实 home 与真实 pi：全部在 `tmp_path` 临时目录，
  `HOME` 被替换为伪目录，远端由 `fake_kaneo` 内存 stub 提供，执行后端走
  `KANPILOT_PI_*` 隔离夹具（伪 pi-bin 桩 `--version`/`auth check`，
  `KANPILOT_PI_FAKE` 四模式 worker）。

## 环境变量约定

| 变量 | 用途 | 来源 |
|---|---|---|
| `KANPILOT_CORE_BIN` | 核心脚本路径 | 测试约定，默认 `<repo>/core/kanpilot.py` |
| `KANPILOT_DB` | DB 路径 | 契约 §1 已规定 |
| `KANPILOT_PLAN_ROOT` | Plan 根 | **测试约定**（契约未规定覆盖手段） |
| `KANPILOT_REPOS_ROOT` | 项目仓库根 | **测试约定** |
| `KANPILOT_APP_SUPPORT` | 应用支持根（DB 默认位置、备份、报告缓存、来源快照） | **测试约定** |
| `KANPILOT_LEGACY_ROOT` | 旧状态根（默认 `~/Library/Application Support/Codex`） | **测试约定**；`migrate` 也可经 `--json` 的 `legacy_root` 参数覆盖 |
| `KANEO_CREDENTIALS_PATH` | credentials.json 路径 | **测试约定** |

> 集成核对点：若核心未实现以上 env 覆盖，测试会因找不到 DB/Plan 根而失败——
> 届时按本表为核心补 env 读取，或改 `tmp_env` 夹具。

## fake kaneo 端口策略

`fake_kaneo` 用 `http.server.ThreadingHTTPServer` 绑定 `127.0.0.1:0`（系统分配
空闲端口），线程内驻留，测试结束 shutdown。凭据写入
`$KANEO_CREDENTIALS_PATH`：`{"baseUrl": "http://127.0.0.1:<port>",
"accessToken": "test", "token": "test", "workspaceId": ...}`。
每个用例独立实例（任务表隔离）。内置五项目（契约 §6 ID）与四列
（to-do/in-progress/in-review/done），支持 `add_task/set_status/
set_description/fail_next/fail_route` 编程与 `requests_to` 请求日志。

已实现端点：`GET /api/project`、`GET /api/column/<pid>`、
`GET /api/task/<id>`、`GET /api/task/tasks/<pid>`、`GET /api/comment/<id>`、
`GET /api/task-relation/<id>`、`GET /api/activity/<id>`、
`PUT /api/task/status/<id>`、`POST /api/task/<pid>`。
响应形状（数组/单对象、字段名 projectId/status/columnId）为**测试约定**，
集成时以核心 bridge 实际解析口径校准 `FakeKaneo`。

## 指纹与工件约定

- `plan_sha256`/`report_sha256`/`repair_plan_sha256` = sha256(文件原始字节)。
- `source_sha256` = sha256(快照文件字节)；快照以规范化 JSON 落盘
  （sort_keys+紧凑分隔符），「规范化重算」与「原始字节」同值。
- `requirements_sha256` = sha256(canonical_json({title,description}))，
  集中于 `conftest.requirements_sha256_for`——核心口径不同时只改此一处。
- 评审报告根约定为 `<KANPILOT_APP_SUPPORT>/KaneoReviewer/reports/<task_id>/`。
- 计划 frontmatter 为 YAML `---` 块（契约 §6 字段全集）。
- legacy manifest schema（`manifest.json` + `tasks{task_id: {...}}`）为测试约定，
  集中于 `conftest.legacy_state` 夹具。

## DB 后门使用规则

只读/写冻结 schema 的以下字段做时间操控：`requests.lease_expires_at`、
`requests.finished_at`、`tasks.send_state/sent`（构造 send 冲突）、
`settings`。**不用于绕过业务流程造状态**——一切业务状态经 CLI 命令流转。

v1.3 新增三个后门（均在 `conftest.py`，各带文档说明为何 CLI 无法表达）：

- `set_save_state(db, task_id, save_state, auto_advance_paused=None)`：只用于
  隔离验证 §5.15.3 调度入口排除（tick/claim/enqueue/sync-tasks）——archive-task
  的前置条件拒绝 planning/dispatching/running/in_review 等正处于 tick/claim
  可调度范围的状态，纯 CLI 无法构造「非 active 且仍会被 tick/claim 选中」的任务。
- `set_task_status(db, task_id, status)`：只用于构造「CLI 无法表达的一次状态
  回退」——评审未通过修复完成后 Kaneo 卡片拖回 in-progress、任务回 running
  再进新一轮 in-review（§5.9）。现状 sync-tasks 只处理 running→in_review/done
  与 requirements 变更，in_review→running 没有 CLI 路径（实现缺口，已交回
  主 agent）；该后门与 `core/tests/test_board.py` 同一构保持一致。
- 只读检查（不改写）：`session_reads` 表（state-model §1）在会话用例中直接 SELECT。

## 会话数据源接缝（v1.6，测试约定）

v1.6 起唯一后端为 pi：会话读取数据源是运行目录下的 `events.jsonl` +
`job.json`（`session_runs_root` 缺省 `~/Library/Application Support/KaneoPilot/runs`，
测试经 env 指向隔离目录）。夹具直接在工作区临时目录写 run 目录
（job.json + events.jsonl，首事件恒为 session）；worker 可用
`KANPILOT_PI_FAKE` 四模式（running/finished/failed/crash）替代真实 pi 进程。

| 变量 | 用途 |
|---|---|
| `KANPILOT_PI_HOME` | 隔离的 pi 家目录（夹具含 agent/models.json） |
| `KANPILOT_PI_BIN` | 指向伪 pi 可执行桩（`--version`/`auth check` 桩响应） |
| `KANPILOT_AGENTS_DIR` | 隔离的 agent.md 预设目录（夹具含 default.md） |
| `KANPILOT_PI_FAKE` | pi_worker 测试模式：running/finished/failed/crash |
| `KANPILOT_PI_START_WAIT` | dispatch-start 等待 session 事件的秒数（测试调小） |

旧后端（dsh/headless/ATIF）轮次：DB 行保留但读取返回 `unavailable`
「旧后端会话已弃用」——`test_legacy_backend_unavailable` 覆盖该回归。
无运行目录时命令返回结构化 `unavailable`/`pending`（exit 0），不抛错；
只有 task 不存在才是 `not_found`。

## 真实并发（v1.3）

`test_concurrency.py` 的竞争用例全部用**真实多进程**：每个竞争者是独立
`python3 core/kanpilot.py` 进程，共用一个临时 DB，用启动闸门文件对齐起跑时刻
（`_spawn_race`）。不用 mock、不用线程，因此测的是 `BEGIN IMMEDIATE` 事务本身。
为此 `FakeKaneo` 监听 backlog 已放大到 128（默认 5 会在十余并发下把连接打回
`URLError`，那是测试设施限制而非被测行为）。

## 文件清单与覆盖

| 文件 | 契约覆盖 |
|---|---|
| `test_queue.py` | §1 调用/信封/DB 解析、§4.1 幂等、§4.2 claim、§4.3 租约失联、§4.4 守卫、§4.7 report、§4.8 tick、§4.9 暂停、§4.10 resolve、§5.1 doctor、§5.3、§5.5 |
| `test_dispatch.py` | §4.5 校验链全分支、§4.6 证据、§4.10 dispatch resolve、§5.6 publish 校验、§5.10 子步骤、state-model §5 串行位 |
| `test_lifecycle.py` | 全链 happy path、§5.4 sync、§5.7 register-review、§5.8 create-review-fix dedupe、§5.9 advance-task、§5.11 recheck |
| `test_migration.py` | §5.12 migrate、state-model §6 映射 |
| `test_desktop_contract.py` | §5.2 字段形状（含 v1.3 新增字段）、§2 display_status、§5.3 推断、§4.10 allowed_actions |
| `test_board.py` | §2 看板列（直映射、失联非失败、伴随字段）、§5.2 board_column 过滤与 counts.board、§5.9 Review 前置守卫、§5.14 v1.2 裁决、§5.15.4 v1.3 异常归列、state-model §2.1/§2.2 |
| `test_board_v13.py` | §5.15.4 六列异常隔离：互斥与计数、exception.kind/label/severity 判定表全码、评审修复链（fix 独立流程 → 原任务回待评审）、历史失败不污染、观测异常自动清除 |
| `test_save_state.py` | §5.15.1 save_state 语义、§5.15.2 archive/trash/restore（禁用条件、排队取消、幂等、恢复核验）、§5.15.3 调度入口排除、§5.15.6 allowed_actions |
| `test_schema_v2.py` | state-model §1.1 schema v1→v2 升级（列/备份/默认值/幂等）、§4.11 migration_needed |
| `test_session_contract.py` | §5.15.5 会话命令契约（全文/长输出/修订/隔离/轮次/游标/断线/重启补齐/connection 态）、state-model §1 session_reads |
| `test_concurrency.py` | §4.1/§4.2 真实多进程 claim/enqueue 竞争、§5.15.2 排队取消与领取竞争 |
| `test_crash_recovery.py` | §4.3 失联不自动释放、state-model §5 串行位持久性、kill -9 模拟 |
| `ui-acceptance.md` | ui-spec §2–§8 人工验收清单（含 v1.3 六列/会话/归档回收站与三尺寸浅深色） |
| `test_pi_dispatch.py` | §5.17.3 可用性门确定性阻断、§5.16.1 轮次索引、§5.18.3 pi 证据全链（prepare→begin-send→start→confirm）与幂等/兼容分支 |
| `test_pi_config_contract.py` | §5.17.1/§5.17.2 执行配置三命令形状、checks 四项、凭据不泄露、非法 set 信封 |
| `test_pi_round_contract.py` | §5.18 pi 轮次闭环（伪 pi 进程 JSONL→events.jsonl）、轮次隔离/迟到事件、round_detail |
| `pytest.py` | 最小 pytest API 桩（仅 unittest discover 下生效） |
| `unittest_compat.py` | unittest `load_tests` 收集引擎（pytest 语义子集） |
| `session_refresh_harness.py` | §5.15.5/§5.18.2 会话刷新验收脚本（独立运行，非 discover 用例） |
