# KaneoPilot 集成测试套件

对 `core/kanpilot.py` 的**契约级**集成测试：全部经 `subprocess` 调 CLI（JSON 进出、断言退出码与字段级行为），不 import 核心代码、不直接写业务状态。唯一例外是 `conftest.db_exec` 系列**时间旅行后门**（把租约/完成时间 UPDATE 到过去），只用于表达 CLI 无法表达的时间流逝；schema 依据 state-model §1 冻结表结构。

## 运行

```bash
cd <repo 根>
python3 -m pytest tests -q            # 全部
python3 -m pytest tests -q -k dispatch
python3 -m pytest tests --collect-only -q   # 只收集（语法自检）
```

- 需要 Python 3.9+ 与 pytest（本机已验 pytest 9.x / Python 3.10）。
- `python3 -m unittest` 可执行但收集不到用例（本套件为 pytest 风格）；无 pytest 时需补 unittest 兼容层。
- 测试绝不访问真实 Kaneo 与真实 home：全部在 `tmp_path` 临时目录，`HOME` 被替换为伪目录，远端由 `fake_kaneo` 内存 stub 提供。

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

## 文件清单与覆盖

| 文件 | 契约覆盖 |
|---|---|
| `test_queue.py` | §1 调用/信封/DB 解析、§4.1 幂等、§4.2 claim、§4.3 租约失联、§4.4 守卫、§4.7 report、§4.8 tick、§4.9 暂停、§4.10 resolve、§5.1 doctor、§5.3、§5.5 |
| `test_dispatch.py` | §4.5 校验链全分支、§4.6 证据、§4.10 dispatch resolve、§5.6 publish 校验、§5.10 子步骤、state-model §5 串行位 |
| `test_lifecycle.py` | 全链 happy path、§5.4 sync、§5.7 register-review、§5.8 create-review-fix dedupe、§5.9 advance-task、§5.11 recheck |
| `test_migration.py` | §5.12 migrate、state-model §6 映射 |
| `test_desktop_contract.py` | §5.2 字段形状、§2 display_status、§5.3 推断、§4.10 allowed_actions |
| `test_crash_recovery.py` | §4.3 失联不自动释放、state-model §5 串行位持久性、kill -9 模拟 |
| `ui-acceptance.md` | ui-spec §2–§8 人工验收清单 |
