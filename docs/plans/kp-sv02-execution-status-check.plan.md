# KP-SV02：概览核验真实执行状态

状态：待体验（已合入 main，180 项隔离检查、原生核验和正式 Packages #163 通过；2026-11-08 已公开发布 v0.5.3 并确认为 latest）。2026-11-07 用户提供 MediaPlayer 的 `NETWORK_ERROR` 任务概览截图，要求增加校验，并明确选择“核验真实执行状态”。

目标：在概览直接核对当前执行是否仍在运行、已结束、失败或无法确认，并显示检查依据、时间和下一步建议。保留原报错，校验不重跑、不调用模型、不改变任务或远端状态。

- 基线：`main@f1c1cea0552ce3439322e379bf821ee9474f486a`。
- 分支：`feat/20261107-kp-sv02-execution-status-check`；已合入 main，本地分支已清理，远端来源分支保留。
- 工作树：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261107-feat-kp-sv02-execution-status-check`；已于 2026-11-08 安全移除。用户于 2026-11-07 先授权提交、推送与云端预览，后明确“发布吧”，授权合入与正式发布；发布前核对 latest 为 v0.5.2，按补丁版本 v0.5.3 交付。
- 主工作区已有 `.local/` 和 `docs/plans/kp-cache01-macos-build-cache-cleanup.md` 未跟踪内容，保留不动；其他 worktree 不属于本任务。

## 已确认决定与实施

共享返回契约唯一见 `docs/protocol.md` 的 KP-SV02 条目；视觉唯一见 `docs/ui-spec.md` 的 KP-SV02 条目。既有 `recheck` 会更新远端投影与审计，不复用为严格只读校验。`get-session(status_only)` 的文件/进程读取可复用，但其索引回填与缓存写入必须在本入口禁用。旧 running 文件不证明进程存活，Plus 空闲不证明原轮次完成；终态不表示需求验收通过。

| 步骤 | 归属与依赖 | 交付与验收 |
| --- | --- | --- |
| T1 | analyst（Sol/high）只读核查核心；已交回。主 agent 维护规范、记录及 CLI 接入。 | 已确认 `observer_alive`、Plus 双身份核验、状态读取的写入点和隔离测试入口。 |
| T2 | worker（Sol/high）独占 `core/kanpilot/execution_status.py`、`cmd_session.py`、`session_control.py`（仅实时 turn 字段）和 `core/tests/test_execution_status.py`。契约冻结后可与 T3 并行；不再分派。 | 实现只读投影及真实边界回归，不写业务状态或日志，不触发模型/Kaneo。 |
| T3 | 主 agent 独占 macOS Models、TaskDetailContentView、Components、FakeCore、CLI 及规范。 | 概览入口与只读结果面板，核验期间禁重复点击，切换任务/轮次丢弃旧结果；可重试和查看执行会话。保留整页布局与原恢复权限。 |
| T4 | 主 agent 组合核验与收尾；必要时 reviewer（Sol/high）只读审查。 | 临时 DB/文件及 fake Plus 的 Python 检查；Swift 编译仅云端 Packages；最新云端独立 App 配 FakeCore 核对正常、未知/冲突、重复点击和窄窗口。 |

所有测试使用本任务临时目录和随机 loopback 端口，无真实 Kaneo、模型或业务任务写入。主 agent 已核对最终组合态，并使用新云端独立 App 完成原生界面核验；正式发布绑定同一产品源码。

## 可检查场景

1. 原失败 job + 同身份 Plus 仍在运行/已手动继续：显示实时状态，原 `NETWORK_ERROR` 保留；没有新请求或 prompt。
2. Plus 已空闲但原 job 仍 running，或 Codex worker 已死/身份未知：显示状态未确认及原因，不当作执行成功。
3. 原 job 和终态记录一致、进程已退出：显示原轮次已结束/失败；仍说明需求验收以验证与评审为准。
4. 来源缺失、半写、Plus 断开、返回身份不匹配或校验期间切轮：未知或明确拒绝，可重新核验，无旧结果串任务。
5. 校验前后 DB 与原 job/spec/事件原字节不变；失败、连续点击、关闭面板均不创建执行请求。

## 本次证据与剩余工作

- T1–T4 已完成：本地组合态、分支与主线云端构建、原生交互及正式发布检查通过；用户尚未确认真实业务体验，因此为“待体验”。提交前的候选绑定上述基线与 `build/recovery/kp-sv02-v0.5.3/evidence/local-verification.json` 内 10 个实际源码/测试文件 SHA256；正式发布源的这 10 个文件已再次核对一致。
- 主 agent 在当前组合态运行 `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tests python3 -m unittest -q test_execution_status test_session test_session_status test_session_control test_execution_retry`（cwd `core`）：**180 tests / 59.917s / exit 0**。包含新增 26 项与原会话、状态、Plus 控制、沿用计划恢复回归；没有改旧测试预期或降低门禁。
- 新回归经 CLI 入口，以 SQLite `query_only` 拒绝写入并核对 DB dump/文件字节；覆盖旧 running + 死亡/未知进程、当前 Plus 活动与后续轮次、Goal 等待批准、半写/身份错误/换轮、缺实时 turn、核验期间终态证据变化、PID 复用、历史读取的原回填行为及脱敏。
- FakeCore 新命令本地 smoke 通过：响应绑定 task/run，原 NETWORK_ERROR 保留，夹具文件字节不变，命令日志只有 check-execution-status；没有恢复或模型请求。6 个受影响 Python 文件 AST 检查、`git diff --check` 通过。
- reviewer（Sol/high）只读审查发现并已修复三项边界：实时 turn 缺失时不能借用历史 turn；新只读命令须跳过 CLI 写动作守卫；普通 PID 存活不代表本轮进程归属。复核当前组合态未发现剩余实质缺陷。主 agent 亲自运行上述组合态测试，不将子 agent 自报当最终证据。
- 已提交并推送 `cb17e5a6d8410a1b88dc00041758cee3bf4c2e7e`。Packages [#161](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/42381158937) 绑定该 SHA：16 个必需 job 全部 success（Python 3.11/3.13 五分片、Swift 两组夹具、macOS/Android 打包及平台检查），2 个预期 skipped（非本次启用的全文探针、publish）。证据在 `build/recovery/kp-sv02-v0.5.3/evidence/cloud-161/run-final.json`、`jobs-final.json` 与 `artifacts.json`。
- 下载阻塞已解除：#161 官方 `macos-update-ui-161` 完整制品 30,914,872 bytes / SHA256 `ba87880d69b1f0c58ec7e883900f79c4e26ef6f631b1ff26632d232e25c5632b`；内层 31,776,905 bytes / SHA256 `6df541eb22b2f2d924f7dc7f67eabb08bf891a51dd542e442ecdb3b34a9d35cd`。元数据 source_sha=cb17e5a、build=161、独立 bundle=`local.kaneopilot.software-update-check`；云端 App `codesign --verify --deep --strict` exit 0。未本地编译或重签名。测试的 10 个源码/测试文件摘要与合入 main 的 578b68a 完全相同，差异仅为任务记录。
- 原生场景和命令审计共 15 项通过：900×600 dark 的概览入口、未知/运行中/结束/失败、读取错误、错轮响应拒绝、长证据滚动、会话跳转、连续点击；1180×760 light 的无阻塞入口与面板、加载禁重复核验、关闭后重开和切轮后的在途响应失效。原 NETWORK_ERROR 保留，结束状态保留验证/评审提示。延迟夹具冻结原响应 20 秒；分别于 5.142 秒关闭、12.275 秒刷新切轮，返回后仍显示新结果且没有旧响应哨兵。AX、截图及其摘要见归档 `native/`、`cloud-161/verification.json`。
- 原生核验使用 FakeCore 和独立 support/DB/计划/报告/凭据空文件；命令日志只有读取入口，没有 enqueue、resolve、恢复、执行或模型请求。菜单退出后仍存活的本任务独立测试 App 已核对可执行文件与 PID 后用 SIGTERM 收尾；正式 App、Plus 和真实 MediaPlayer/设备任务未操作。该隔离核验不代表真实业务需求验收或正式安装。
- 已合入并推送 `main@578b68ab929cc8e259405edbdff3b8d04846e73a`；[Packages #162](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/42386989891) 绑定该提交，16 个必需 job success、2 个预期 skipped。源码与本地检查及原生 App 再次核对一致，无 schema 变化。
- 已创建并推送直指该 main 提交的 annotated `v0.5.3`；完整说明在该标签注释及归档 `release-notes-v0.5.3.md`。[正式 Packages #163](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/42389313834) 完成：17 个 job success，1 个预期 skipped（可选全文探针）；publish success。发布源固定为 `578b68ab929cc8e259405edbdff3b8d04846e73a`、build=163，后续任务记录提交不改变发布源码。
- [v0.5.3 正式 Release](https://github.com/SakuraLoveSmile/KaneoPilot/releases/tag/v0.5.3) 于 2026-11-08 00:21:24（台北）公开；API 核对非 draft、非 prerelease，latest 同为 release 437689193，正文构建标记绑定上述源码与 build。四项资产 APK、arm64 DMG、manifest.json、SHA256SUMS 均 uploaded，名称、大小与完整 SHA256 已记录在 `release-163/verification.json`。
- 云端发布 job 在公开前重新生成并核对 manifest 的版本、源码、build、架构及签名指纹，读回四项上传资产并逐一比对字节 SHA256；全部成功后才公开。本机补充下载因网络/TLS 中断及 API 二进制重定向解析失败未完成，已停止重试；未声称本地独立复验 #163 包字节、签名或 manifest。正式发布完整性证据来自已通过的云端读回门禁，公开资产元数据与 latest 已在本机核对。
- 证据已迁至主工作区 `build/recovery/kp-sv02-v0.5.3/evidence/`，截图/AX 摘要搬迁后逐一核对通过；本任务 110 个 `.pyc` 也保存在同级 `bytecode-cache/`。本任务测试进程已退出，移除前核对 worktree 无暂存、未暂存、未跟踪或忽略成果，无打开文件或活动进程引用，来源提交已在 main。已正常移除该 worktree 及已合入本地分支，复核目录和分支不存在；`release-163/cleanup.json` 记录结果。远端来源分支、主工作区原有 `.local/`、缓存清理计划及其他 worktree 保留。
- 体验入口：更新至 v0.5.3，任务概览 →“核验执行状态”。下一步由用户体验真实业务；未正式安装、未重跑 MediaPlayer、未调用真实模型或操作设备，执行状态核验不等于业务需求已验收。
