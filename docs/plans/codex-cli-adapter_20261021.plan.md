# Codex CLI 规划、评审与执行适配

状态：待体验。原工作区的 Qoder 未提交改动保留；本次在 `codex/codex-cli-adapter` 独立工作树实施，不修改生产配置，不发布或安装正式应用。

## 用户效果与任务

- T1：沿用已有 Codex CLI 规划及评审入口，核实只读材料、结构化结果、错误回传与配置快照能正常工作。
- T2：任务执行可选择 Codex CLI；在登记的隔离工作树执行，记录真实线程和命令事件，遇到问题可在 Pilot 回答，支持停止及保留现场恢复。Pilot 不代跑任务 verify。
- T3：设置页沿用现有执行设置布局增加 Codex CLI、独立模型和思考等级；切换后端保留其他配置，仅影响新轮次。Desktop 主动领取入口保留。
- T4：通过隔离 CLI 全链路、关键拒绝路径、已有回归、Swift 夹具、候选构建及原生设置界面核验。真实付费模型、生产任务与正式切换单独记录。

## 冻结接入契约

执行配置新增 `exec_config.codex = {model, reasoning_effort}`，执行快照为 `{backend:"codex", model, reasoning_effort, sandbox:"workspace-write", execution_question_max_wait_seconds}`。模型和思考等级用现有 Codex 模型目录校验；不改变规划/评审配置或用户全局 Codex 配置。缺配置或环境不可用明确拒绝，不回落 Plus/pi。执行权限固定为 Codex 的工作树写入沙箱，审批策略 never；不开放无沙箱执行。服务商及认证复用既有 `codex_provider`。

执行复用 `execution_contract` 和 requests/execution_rounds，不新增数据库或第二队列。核心启动前写 runtime_operations 操作回执，worker 仅写同轮运行证据并经核心命令回报，不直写 SQLite。实际 `thread.started` 且核心心跳成功后写首个 session 事件，dispatch-confirm 核对同后端、配置和线程。线程内的 turn 身份为适配器维护的单调代次，清楚标记来源，不伪造 Codex 未提供的原生 turn ID。

`codex exec --json --output-schema` 输出 `{status:"completed|needs_input|failed", summary, checks:[{command,exit_code}], questions:[{title,options}]}`。最终 JSON 是模型声明；通过验收还须冻结命令逐条有 CLI `command_execution` 完成事件、独立整数退出码为 0、同一最终源码快照，重复/缺失/布尔退出码/日志截断/源码漂移均不放行。使用原生事件的命令须原文匹配（只允许可验证的 shell 包装展开），不根据自然语言猜测等价。正常退出、turn.completed 和模型自报均不单独构成成功。

需要提问时 worker 保持本轮活动，问题经已有 execution_questions 持久化并显示；答案先经核心操作回执及同问题身份写入运行目录，worker 记录消费代次后才显式 `codex exec resume <thread>`。回答授权该次继续；读取状态和 round-observe 绝不发送新输入。相同答案操作幂等，未知结果不自动重发。没有可确认活动 worker 的崩溃现场保留为 unknown/需恢复，不能通过观察隐式续跑。显式执行重试使用已有新轮次及保留工作树机制。

停止先持久记录同 run 操作，再核对 worker/CLI 进程出生身份发信号；只有进程终止且终态证据齐备才报告确认停止。等待问题可走现有停止入口；活动执行取消同样停止其受管 CLI 子进程。App 停止观察不得留下仍可修改工作树而被当作完成的孤儿进程。

所有 Codex 新执行在 legacy/shadow/live 均要求结构化报告与源码绑定。报告、命令事件、spec/job 与 plan 的哈希进入现有 verdict 回执和提交后恢复核验。交付提交、Review 入队及远端写沿既有核心确定性路径，Codex 不自行 push/merge/release。

## 实施分工与验收

T1 主 agent 复用并核查现有规划评审；只读 mapper 分别查明执行链与规划/设置入口，交付文件和调用链证据，不修改源码。

T2 分三份独占模块并行实施，均使用 Sol/high worker，不派下级 agent。runtime worker 独占新增 `codex_execution.py`、`codex_worker.py` 和 runtime 专项测试，提供配置校验/能力探测、worker、报告读取及执行输入客户端。inputs worker 独占 `execution_questions.py`、`derive.py`、`cmd_queue.py` 与输入专项测试，接入问题、停止及重试。verdict worker 独占 `engine/commands.py`、`engine/verdict.py`、`auto_monitor.py`、`cmd_read.py`、`verification_recovery.py` 与验收专项测试，接入严格报告及只读观察。共同输入为本计划冻结的配置、job/spec、结构化报告、原生命令事件和 source hash；runtime 客户端为问题与停止提供同轮身份和幂等操作回执。完成交接后仅由主 agent 维护集成改动，涉及原专属文件时先解除其并行写入。

T3 配置/UI worker 独占 `exec_config.py`、`cmd_config.py`、Swift 模型/设置、FakeCore、Swift 夹具及配置专项核心测试，与 T2 并行，依赖上述配置形状；由核心返回已保存配置、默认值与能力，界面不另行保存密钥或执行配置。主 agent 独占 dispatch、harness/constants、交付/远端推进、Review 材料与 docs 的集成修改，以及无提问时的停止入口。

T4 tester 使用 Luna/medium 在隔离夹具中执行指定回归，交付命令、计数及日志，不修改源码；独立 reviewer 使用 Sol/high 只读检查实际变更中的身份、并发、停止、交付和证据边界。主 agent 负责真实 worker 联合运行、候选构建和最终原生界面核验，不以子 agent 自报作为用户验收。

隔离验证至少覆盖 dispatch→真实 CLI 事件形状→结构化报告→verdict→本地交付→Review 入队，以及退出 0 缺报告、缺命令、错误身份、源码变化、重复/失败退出码、提问回答、停止、观察不重发和重复启动。测试不得使用生产 DB/远端/用户仓库作为任务夹具；模拟模型结果与真实模型结果分开报告。

## 2026-10-21 实施与证据

T1 已核对现有 Codex CLI 规划、结构化 Review、模型/思考等级目录及服务商入口，继续复用；未接入 app-server。T2/T3 代码已完成，T4 原生界面已在用户解锁后补验；随后按用户“可以 用 6.1 sol 进行一次验证吧”的授权，完成一次真实执行及确定性验收推进，整体为“待体验”。没有修改生产配置、安装正式应用、提交本次适配分支或推送远端；真实模型只用于下述隔离执行任务。

- 环境：本机 `codex-cli 0.157.0`；核对 `exec` 与 `exec resume` 的 help。官方 non-interactive-mode/app-server 文档用于确认接入方式；help、版本检查不调用模型。
- 干净 `5673ed7` 基线：原关键组合 341 passed、25 subtests passed；运行输出保留在会话，无单独日志文件。
- 候选关键 Core 组合：490 passed、82 subtests passed、7 failed，失败均为新增 `test_codex_verdict.py` 夹具漏登记 worker PID。保留原失败日志 `/tmp/kp-codex-cli-candidate-20261021-core-pytest.log`。按 `round_store.mark_started` 补齐夹具，新增缺登记/worker 不符/observer 不符对旧回执的拒绝验证；该模块最终单独复跑为 9 passed、19 subtests passed，日志 `/tmp/kp-codex-verdict-final-20261021-pytest.log`。生产身份门未放松，未将不同轮次计数合并成一次全绿结果。
- 根级 `tests/test_pi_dispatch.py tests/test_codex_dispatch.py`：12 passed，日志 `/tmp/kp-codex-cli-candidate-20261021-root-pytest.log`。其中新 Codex 四项实际启动受管 worker/CLI 子进程，在临时 Git 工作树改文件和运行冻结命令，覆盖 legacy/shadow/live 的严格验收、本地交付、Review 入队、提前提问、答案只消费一次及观察不重发。模型决策和 CLI JSONL 事件由 stub 生成，此处不是实际 Codex 服务验收。
- Swift DTO/视图模型夹具：223/223；执行答案夹具通过，包含 Codex accepted 后等待消费时冻结重复提交。Release 候选构建通过：`/tmp/kaneopilot-codex-candidate/KaneoPilot.app`，日志 `/tmp/kaneopilot-codex-candidate-build.log`。当前 23 个变更核心模块与候选包逐字节哈希一致；fixture 修正不改变候选业务源码。
- 用户解锁后的原生 UI 核验：Codex CLI 设置可显示模型及支持的思考等级，保存 max 后显示成功；另将未保存草稿改为 xhigh，切换 pi 再切回仍保留 xhigh，隔离持久状态保持 max。普通 running 轮次可打开“停止本轮”确认并取消；accepted/open 提问的输入、保存草稿和跳过均禁用，“重新核对”与停止入口可用。该提问状态为直接播种的独立 FakeCore 夹具，用于渲染和控件状态核验，不代表真实模型已经接收答案。
- 原生核验发现并修复了 Codex 提问仍显示 Plus 来源、accepted 后仍要求用户回答及重试提示写 Plus 的遗漏。仅修正文案与等待接收的状态样式，重新 Release 构建及打包检查通过，日志 `/tmp/kaneopilot-codex-ui-final-build.log`。新预览已实测显示“来源：Codex CLI 执行会话”“等待执行端接收”，答案选项保持禁用。预览重签名后，在临时副本去除本地签名比较，执行文件与新候选完全一致，SHA-256 为 `19535eb5bb189fef86c5a2f3ad6514d9445d94286f62f6e38693dacb0a13c41f`。
- 独立 reviewer 最后只读复核未发现剩余确定性缺陷；其复核未独立运行测试，也不作为原生 UI 或真实模型证据。
- 另行报告的既有 Plus 用例 `test_goal_report_contract.py::TestT6WorkspaceNaming::test_worker_passes_project_name_and_title_exactly` 在候选与干净基线均失败（worker 返回 1，预期 0），不属于本次 Codex 夹具问题；未改 Plus 业务以掩盖该基线结果。
- 真实模型执行：本机 `codex-cli 0.157.0` 在新临时仓库与隔离 DB 中创建唯一文件 `feature.txt`，实际会话上下文确认 `gpt-6.1-sol / high`、`workspace-write`、`approval_policy=never`。任务身份 `8f71a278382d4900a6e69da6`，run `670f0feb-cecd-49d8-b670-b991073faea8`，原生线程 `01a15c75-fe64-7653-8b84-2a637ad9a2d8`。180 秒独立超时守卫下，模型阶段耗时 30.128 秒；只有一个原生线程、一个完成轮次，无应用层重试或 resume。
- 本次两条冻结验证分别检查文件精确内容与 `git diff --check`，均有原生 started/completed 事件及整数退出码 0，报告与同一源码 SHA-256 绑定。Pilot 只读取和核对证据，没有代跑任务 verify。正常等待核心 monitor 调度后，live verdict 为 passed、`evidence_source=codex_report`；核心在临时仓库生成交付提交 `26e943f6c27f14bb997ca0eac31028e86fa7d318`，任务推进到 `in_review`，Review 为 queued，评审输入包含该轮报告、原生命令日志及 job/spec。没有启动真实规划或 Review 模型，Kaneo 写入全部指向本机 loopback 夹具。
- 本次真实验证记录为 `build/codex-cli-evidence/real-codex-result.json`，原生模型上下文、用量及证据文件哈希见 `real-codex-native-proof.json`，完整执行证据与评审输入分别保存在同目录 `real-execution/`、`real-review-input/`。临时环境根为 `/private/tmp/kp-codex-real-20261021.5j2e5leo`；收尾确认受管 worker/CLI 均不再存活。一次启动前检查曾因 macOS 临时目录符号链接未规范化而拒绝计划，修正一次性 runner 的路径后才执行 dispatch-start；该失败在模型调用前，保留为 `real-codex-preflight-path-error.*`，不计作模型重试，也未放松生产路径检查。

关键组合使用 `/Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11 -m pytest -q`；Core 模块为 `test_codex_exec_config.py test_codex_execution.py test_codex_inputs.py test_codex_verdict.py test_cli_review.py test_planner_config.py test_harnesses.py test_execution_stage_contract.py test_auto_dispatch.py test_auto_monitor.py test_execution_questions.py test_execution_input_runtime.py test_execution_retry.py test_verification_recovery.py test_process_owner.py test_goal_check_coverage.py test_engine_verdict.py test_cmd_config.py`（均在 `core/tests/`）。只重跑修正涉及的验收模块，其余上述结果仍来自第一次组合运行。

## 原生核验与剩余验收门

1. 原生界面：本机隔离核验已完成。独立 bundle ID `local.kaneopilot.codex-cli-preview`，以 FakeCore、独立 support/DB/plans/repos/reports/空凭据运行；环境与夹具保存在 `build/codex-cli-evidence/`，没有生成最初预留的 `requests.jsonl`，不将其作为证据。已保存 `codex-stop-confirm.jpg`、`codex-exec-settings-saved.jpg`、`codex-draft-retained.jpg`、修复前 `codex-question-before-copy-fix.jpg` 与修复后 `codex-answer-waiting-native.jpg`。原生预览在核验后已退出，正式应用与生产配置未操作。
2. 真实 Codex CLI 执行：本次已通过上述单轮隔离验收，包括真实模型执行、原生验证事件、严格 verdict、本地交付及 Review 入队。真实规划/Review 模型未在本次调用，相关服务效果不能由入队、材料收集、stub 或历史结果替代；本次授权的一次模型验证已用完，不自动追加调用。
3. 合并与正式切换：未授权、未执行。原工作区 Qoder dirty 改动保留，本次内容只在 `codex/codex-cli-adapter` 工作树；不得直接覆盖原工作区。
