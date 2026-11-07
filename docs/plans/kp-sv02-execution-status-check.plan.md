# KP-SV02：概览核验真实执行状态

状态：实现中（核心与界面代码已完成，180 项本地隔离检查通过；Swift 编译与原生界面核验尚未运行）。2026-11-07 用户提供 MediaPlayer 的 `NETWORK_ERROR` 任务概览截图，要求增加校验，并明确选择“核验真实执行状态”。

目标：在概览直接核对当前执行是否仍在运行、已结束、失败或无法确认，并显示检查依据、时间和下一步建议。保留原报错，校验不重跑、不调用模型、不改变任务或远端状态。

- 基线：`main@f1c1cea0552ce3439322e379bf821ee9474f486a`。
- 分支：`feat/20261107-kp-sv02-execution-status-check`。
- 工作树：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261107-feat-kp-sv02-execution-status-check`；合入目标 main；用户于 2026-11-07 明确允许提交、推送任务分支并完成云端预览与原生界面核验，未授权合并或发布。
- 主工作区已有 `.local/` 和 `docs/plans/kp-cache01-macos-build-cache-cleanup.md` 未跟踪内容，保留不动；其他 worktree 不属于本任务。

## 已确认决定与实施

共享返回契约唯一见 `docs/protocol.md` 的 KP-SV02 条目；视觉唯一见 `docs/ui-spec.md` 的 KP-SV02 条目。既有 `recheck` 会更新远端投影与审计，不复用为严格只读校验。`get-session(status_only)` 的文件/进程读取可复用，但其索引回填与缓存写入必须在本入口禁用。旧 running 文件不证明进程存活，Plus 空闲不证明原轮次完成；终态不表示需求验收通过。

| 步骤 | 归属与依赖 | 交付与验收 |
| --- | --- | --- |
| T1 | analyst（Sol/high）只读核查核心；已交回。主 agent 维护规范、记录及 CLI 接入。 | 已确认 `observer_alive`、Plus 双身份核验、状态读取的写入点和隔离测试入口。 |
| T2 | worker（Sol/high）独占 `core/kanpilot/execution_status.py`、`cmd_session.py`、`session_control.py`（仅实时 turn 字段）和 `core/tests/test_execution_status.py`。契约冻结后可与 T3 并行；不再分派。 | 实现只读投影及真实边界回归，不写业务状态或日志，不触发模型/Kaneo。 |
| T3 | 主 agent 独占 macOS Models、TaskDetailContentView、Components、FakeCore、CLI 及规范。 | 概览入口与只读结果面板，核验期间禁重复点击，切换任务/轮次丢弃旧结果；可重试和查看执行会话。保留整页布局与原恢复权限。 |
| T4 | 主 agent 组合核验与收尾；必要时 reviewer（Sol/high）只读审查。 | 临时 DB/文件及 fake Plus 的 Python 检查；Swift 编译仅云端 Packages；最新云端独立 App 配 FakeCore 核对正常、未知/冲突、重复点击和窄窗口。 |

所有测试使用本任务临时目录和随机 loopback 端口，无真实 Kaneo、模型或业务任务写入。主 agent 核对最终组合态。原生界面需要新云端产物；本地实现与检查已完成，用户已允许提交、推送本分支并运行云端预览构建及原生核验；不发布版本。

## 可检查场景

1. 原失败 job + 同身份 Plus 仍在运行/已手动继续：显示实时状态，原 `NETWORK_ERROR` 保留；没有新请求或 prompt。
2. Plus 已空闲但原 job 仍 running，或 Codex worker 已死/身份未知：显示状态未确认及原因，不当作执行成功。
3. 原 job 和终态记录一致、进程已退出：显示原轮次已结束/失败；仍说明需求验收以验证与评审为准。
4. 来源缺失、半写、Plus 断开、返回身份不匹配或校验期间切轮：未知或明确拒绝，可重新核验，无旧结果串任务。
5. 校验前后 DB 与原 job/spec/事件原字节不变；失败、连续点击、关闭面板均不创建执行请求。

## 本次证据与剩余工作

- T1–T3 代码已完成；T4 本地组合态通过，云端编译与原生交互尚未运行，因此仍为“实现中”。提交前的候选绑定上述基线与 `build/kp-sv02/local-verification.json` 内 10 个实际源码/测试文件 SHA256。
- 主 agent 在当前组合态运行 `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tests python3 -m unittest -q test_execution_status test_session test_session_status test_session_control test_execution_retry`（cwd `core`）：**180 tests / 59.917s / exit 0**。包含新增 26 项与原会话、状态、Plus 控制、沿用计划恢复回归；没有改旧测试预期或降低门禁。
- 新回归经 CLI 入口，以 SQLite `query_only` 拒绝写入并核对 DB dump/文件字节；覆盖旧 running + 死亡/未知进程、当前 Plus 活动与后续轮次、Goal 等待批准、半写/身份错误/换轮、缺实时 turn、核验期间终态证据变化、PID 复用、历史读取的原回填行为及脱敏。
- FakeCore 新命令本地 smoke 通过：响应绑定 task/run，原 NETWORK_ERROR 保留，夹具文件字节不变，命令日志只有 check-execution-status；没有恢复或模型请求。6 个受影响 Python 文件 AST 检查、`git diff --check` 通过。
- reviewer（Sol/high）只读审查发现并已修复三项边界：实时 turn 缺失时不能借用历史 turn；新只读命令须跳过 CLI 写动作守卫；普通 PID 存活不代表本轮进程归属。复核当前组合态未发现剩余实质缺陷。主 agent 亲自运行上述组合态测试，不将子 agent 自报当最终证据。
- **未运行**：云端 Packages / Swift 夹具与 App 编译、900×600 原生面板、真实运行端/MediaPlayer/设备任务；没有新包、安装或发布。Swift 解码夹具已补代码，不能视为编译通过。
- 下一步：按已获得的授权提交与推送任务分支，使用 Packages；核对精确 SHA 的完整 job 结果，下载独立身份 App，使用 FakeCore 核验入口、加载、已确认/未知/失败、长文字、关闭/切轮和重复点击，再记录结论。未改变用户真实任务或正在运行的软件。
- 本任务尚未合入，worktree 保留供评审与构建；主工作区和其他 worktree 不变。合入且适用验证通过后再按保全规则收尾本任务目录。
