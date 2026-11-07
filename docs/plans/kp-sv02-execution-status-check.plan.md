# KP-SV02：概览核验真实执行状态

状态：实现中（核心、180 项本地隔离检查和 Packages #161 已通过；官方预览包已完整核对，原生界面核验中，当前等待 Mac 解锁）。2026-11-07 用户提供 MediaPlayer 的 `NETWORK_ERROR` 任务概览截图，要求增加校验，并明确选择“核验真实执行状态”。

目标：在概览直接核对当前执行是否仍在运行、已结束、失败或无法确认，并显示检查依据、时间和下一步建议。保留原报错，校验不重跑、不调用模型、不改变任务或远端状态。

- 基线：`main@f1c1cea0552ce3439322e379bf821ee9474f486a`。
- 分支：`feat/20261107-kp-sv02-execution-status-check`。
- 工作树：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261107-feat-kp-sv02-execution-status-check`；合入目标 main；用户于 2026-11-07 先授权提交、推送与云端预览，后明确“发布吧”，现已授权合入与正式发布；核对当前 latest 为 v0.5.2 后，按补丁版本 v0.5.3 推进。
- 主工作区已有 `.local/` 和 `docs/plans/kp-cache01-macos-build-cache-cleanup.md` 未跟踪内容，保留不动；其他 worktree 不属于本任务。

## 已确认决定与实施

共享返回契约唯一见 `docs/protocol.md` 的 KP-SV02 条目；视觉唯一见 `docs/ui-spec.md` 的 KP-SV02 条目。既有 `recheck` 会更新远端投影与审计，不复用为严格只读校验。`get-session(status_only)` 的文件/进程读取可复用，但其索引回填与缓存写入必须在本入口禁用。旧 running 文件不证明进程存活，Plus 空闲不证明原轮次完成；终态不表示需求验收通过。

| 步骤 | 归属与依赖 | 交付与验收 |
| --- | --- | --- |
| T1 | analyst（Sol/high）只读核查核心；已交回。主 agent 维护规范、记录及 CLI 接入。 | 已确认 `observer_alive`、Plus 双身份核验、状态读取的写入点和隔离测试入口。 |
| T2 | worker（Sol/high）独占 `core/kanpilot/execution_status.py`、`cmd_session.py`、`session_control.py`（仅实时 turn 字段）和 `core/tests/test_execution_status.py`。契约冻结后可与 T3 并行；不再分派。 | 实现只读投影及真实边界回归，不写业务状态或日志，不触发模型/Kaneo。 |
| T3 | 主 agent 独占 macOS Models、TaskDetailContentView、Components、FakeCore、CLI 及规范。 | 概览入口与只读结果面板，核验期间禁重复点击，切换任务/轮次丢弃旧结果；可重试和查看执行会话。保留整页布局与原恢复权限。 |
| T4 | 主 agent 组合核验与收尾；必要时 reviewer（Sol/high）只读审查。 | 临时 DB/文件及 fake Plus 的 Python 检查；Swift 编译仅云端 Packages；最新云端独立 App 配 FakeCore 核对正常、未知/冲突、重复点击和窄窗口。 |

所有测试使用本任务临时目录和随机 loopback 端口，无真实 Kaneo、模型或业务任务写入。主 agent 核对最终组合态。原生界面需要新云端产物；本地实现与检查已完成，用户已允许提交、推送本分支并运行云端预览构建及原生核验，后追加正式发布授权；发布前补齐原生核验。

## 可检查场景

1. 原失败 job + 同身份 Plus 仍在运行/已手动继续：显示实时状态，原 `NETWORK_ERROR` 保留；没有新请求或 prompt。
2. Plus 已空闲但原 job 仍 running，或 Codex worker 已死/身份未知：显示状态未确认及原因，不当作执行成功。
3. 原 job 和终态记录一致、进程已退出：显示原轮次已结束/失败；仍说明需求验收以验证与评审为准。
4. 来源缺失、半写、Plus 断开、返回身份不匹配或校验期间切轮：未知或明确拒绝，可重新核验，无旧结果串任务。
5. 校验前后 DB 与原 job/spec/事件原字节不变；失败、连续点击、关闭面板均不创建执行请求。

## 本次证据与剩余工作

- T1–T3 代码已完成；T4 本地组合态及云端构建通过，原生交互尚未完成，因此仍为“实现中”。提交前的候选绑定上述基线与 `build/kp-sv02/local-verification.json` 内 10 个实际源码/测试文件 SHA256。
- 主 agent 在当前组合态运行 `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tests python3 -m unittest -q test_execution_status test_session test_session_status test_session_control test_execution_retry`（cwd `core`）：**180 tests / 59.917s / exit 0**。包含新增 26 项与原会话、状态、Plus 控制、沿用计划恢复回归；没有改旧测试预期或降低门禁。
- 新回归经 CLI 入口，以 SQLite `query_only` 拒绝写入并核对 DB dump/文件字节；覆盖旧 running + 死亡/未知进程、当前 Plus 活动与后续轮次、Goal 等待批准、半写/身份错误/换轮、缺实时 turn、核验期间终态证据变化、PID 复用、历史读取的原回填行为及脱敏。
- FakeCore 新命令本地 smoke 通过：响应绑定 task/run，原 NETWORK_ERROR 保留，夹具文件字节不变，命令日志只有 check-execution-status；没有恢复或模型请求。6 个受影响 Python 文件 AST 检查、`git diff --check` 通过。
- reviewer（Sol/high）只读审查发现并已修复三项边界：实时 turn 缺失时不能借用历史 turn；新只读命令须跳过 CLI 写动作守卫；普通 PID 存活不代表本轮进程归属。复核当前组合态未发现剩余实质缺陷。主 agent 亲自运行上述组合态测试，不将子 agent 自报当最终证据。
- 已提交并推送 `cb17e5a6d8410a1b88dc00041758cee3bf4c2e7e`。Packages [#161](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/42381158937) 绑定该 SHA：16 个必需 job 全部 success（Python 3.11/3.13 五分片、Swift 两组夹具、macOS/Android 打包及平台检查），2 个预期 skipped（非本次启用的全文探针、publish）。证据在 `build/kp-sv02/cloud-161/run-final.json`、`jobs-final.json` 与 `artifacts.json`。
- 下载阻塞已解除：重新取得 #161 官方 `macos-update-ui-161` 完整制品（30,914,872 bytes；SHA256 `ba87880d69b1f0c58ec7e883900f79c4e26ef6f631b1ff26632d232e25c5632b`），内层 31,776,905 bytes / SHA256 `6df541eb22b2f2d924f7dc7f67eabb08bf891a51dd542e442ecdb3b34a9d35cd`；元数据 source_sha=cb17e5a、build=161、bundle=`local.kaneopilot.software-update-check`。云端 App 的 `codesign --verify --deep --strict` exit 0；未本地编译或重签名。
- 原生核验使用该独立身份 App、FakeCore、独立 support/DB/凭据空文件等目录，并禁用执行调度。900×600 dark 已观察概览入口、未知/运行中/结束/失败结果，原 NETWORK_ERROR 保留，滚动可查看下一步，结束状态保留验证与评审提示；截图与 AX 在 `build/kp-sv02/native/screenshots/`。剩余读取错误、加载/重复点击、关闭/切轮、会话跳转与 1180×760 light。途中界面工具报告 Mac 锁定，已请求用户解锁；不把已观察的局部结果当成全项通过。
- 发布准备：远端 main 仍为 f1c1cea，v0.5.2 源码为 d0706e4，二者之间仅有旧任务的发布证据文档。候选 v0.5.3 只增加本任务功能；公开说明草稿在 `build/kp-sv02/release-notes-draft.md`，无 schema 变化。当前未合并、创建标签或发布。
- 下一步：解锁后完成上述剩余原生场景及命令日志核对；通过后更新为“待体验”，合入 main 并核对该提交云端检查，再按正式 annotated 标签流程发布 v0.5.3、核验四项资产及 latest，归档本任务证据并安全清理本任务 worktree。未操作真实运行端/MediaPlayer/设备任务。
- 本任务尚未合入，worktree 保留供评审与构建；主工作区和其他 worktree 不变。合入且适用验证通过后再按保全规则收尾本任务目录。
