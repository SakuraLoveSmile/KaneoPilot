# KP-C03：执行缺材料或决定时显示阻塞

状态：实现中（代码及本地隔离验证完成；用户已授权构建，继续云端编译、原生夹具及候选界面核验）。

- 用户目标：缺少隔离设备、授权测试会话或人工验收时，任务等待补充材料或决定，不计为执行失败，也不提前宣称完成。
- 起点：2026-11-07，主仓 `main` / `ecb313a7278806da2a91d3bb3a44d2e90c7f3c20`。主仓无关 `.local/` 与 `docs/plans/kp-cache01-macos-build-cache-cleanup.md` 未跟踪，保留原样。
- 工作树：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261107-fix-kp-c03-execution-input-classification`；分支 `fix/20261107-kp-c03-execution-input-classification`；预期合入 `main`。用户随后要求“构建新版本”，承接本会话提交/推送修复分支并运行 Packages 的具体请求；按项目默认生成分支预览产物，正式版本号、合入、发布、安装及付费重跑沿后续明确授权。
- 只读核实截图任务 `csim4lhdf81m1k7wib615fdv` / run `52044e49-bee9-4d69-a746-e184061dc89f`：当前 `needs_user / monitor / execution_failed`，摘要明确五项检查通过，缺隔离设备及授权 Emby 会话，真实播放验收未完成。原轮次已经结束，不能伪造仍在等待回答的执行问题。

## 确定行为与验收

契约唯一依据：`docs/protocol.md §5.31` 的 Codex 报告与本次 `await_input` 更正规则。

T1（主 agent）：补全首次执行及回答后续接指令。材料、设备、权限、授权会话、人工操作/验收和用户选择不足时返回 `needs_input` 与可回答问题，复用现有问答、阻塞分类和计数。只有检查通过而真实验收未完成不能标完成；真实失败和验证门禁保留，不用摘要关键词自动改类。核查实际传给 CLI 的两次输入及隔离 worker 行为。

T2（worker，gpt-6.1-sol / high，原子任务不派生）：增加显式、只改当前分类的 `resolve await_input`。输入绑定当前 run、原 job SHA256 和具体缺口说明；只接受已结束、无活跃请求的 Codex failed 报告，且冻结检查在报告及原生证据中全部通过。保持 `needs_user`，新原因码 `execution_followup_required` 派生为 `manual_block / blocked`，标签“待补充材料或决定”。不改原始 job/报告/事件流、轮次索引、工作树、计划或任何验收证据，不创建请求、执行或成功证据。审计保留原失败信息与证据指纹；同参数幂等，陈旧身份、活动进程、非零检查、缺失或矛盾证据均拒绝。补齐后显式重试沿用既有精确终态、远端需求、计划及工作树门禁，仍由用户授权新轮次。

T1/T2 可并行。worker 唯一维护 `execution_followup.py`、`test_execution_followup.py`、`cmd_queue.py`、`cli.py`、`derive.py`；主 agent 唯一维护 constants、worker 指令、其他测试与 docs。只读 analyst 已核实最小链路及旧问题无法通过既有命令安全更正。全部测试使用临时 DB/HOME/Git 与 CLI stub，不连接业务服务或模型。

T3（主 agent）：检查组合态的 blocked 标签、计数、具体缺口、无成功/新模型副作用与原始证据保全；独立 reviewer 只读检查实际差异。界面复用旧布局与样式，仅通过核心异常分类更换标签、图标颜色和阻塞/失败计数，并让现有重试确认识别新原因码。基线用现用 App 观察；候选 Swift 仅云端编译并运行既有原生夹具，取得候选包后再核验受影响界面，不在本机编译。核实适用检查且兼容版本可用后，可经上述核心动作更正用户指出的本地记录；不重跑 MediaPlayer 验证或模型，不操作远端业务数据。后续集成/发布沿用户授权。

## 执行与剩余工作

- 2026-11-07：已核对主仓/工作树/HEAD/未跟踪文件；创建上述独立任务树。源码核查显示现有 `needs_input → execution_input_required → severity=blocked` 与 macOS 阻塞计数已正确，漏项来自 Codex 指令仅提“需要用户决定”，已结束 `failed` 没有无模型的更正入口。
- T1 首次隔离实测：Python 3.10.10 的 `core/tests/test_codex_execution.py` **32 passed + 15 subtests**，实际 stub CLI stdin 覆盖首次和同线程回答后的材料/设备/授权会话/真实验收分类要求。真实 failed 即使全部报告 checks 为 0 也不会自动改类。未调用模型或真实业务验证。
- 旧 run 的只读核验发现：最终五条原生命令（seq143–152）确为独立整数退出码 0，开始/结束源码与原报告最终快照一致；此前曾失败后修复重跑，最后又执行同源码的 git 状态、diff 检查及产物 hash 核对。既有成功 verdict 会对后续工具事件清空验证窗口；本次更正不把原轮次判成功。更正动作核对末次原生命令的实际退出与原报告快照，成功 verdict 的全窗口门禁不变。
- T2 已完成。显式更正操作的隔离回归覆盖末次原生命令、失败后重跑、后续只读收尾、报告身份/指纹、原始文件与轮次保全、幂等、事务内活动请求竞态、真实进程仍活动、自动化不得更正或继续等边界。旧业务 run 的只读 `_proof` 通过，核对 5 个证据文件；没有对生产数据库执行该更正。
- 独立 reviewer（gpt-6.1-sol / high）发现并修正三项：新原因码须接入 Swift 现有重试资格/确认路由，防止遗漏当前 run；新原因码的 `resume` 须仅由用户明确发起，自动化在材料准备或远端读取前拒绝；缺材料导致 verify 未运行时 `needs_input` 允许只列已运行项，不能编造退出码。定点复核后未发现新的实质问题。主 agent 完成必要 Swift 小改与既有夹具断言，不改变布局或成功门禁。
- 最终组合本地实测（Python 3.10.10；基线 HEAD + 本任务未提交差异）：`python -m pytest core/tests/test_execution_followup.py core/tests/test_codex_execution.py core/tests/test_execution_retry.py core/tests/test_codex_preflight_retry.py core/tests/test_codex_inputs.py core/tests/test_codex_verdict.py core/tests/test_auto_monitor.py core/tests/test_board.py core/tests/test_engine_board_attention.py -q`，退出 0，**282 passed + 68 subtests**，73.20 秒。受管进程组合 `python -m pytest tests/test_codex_dispatch.py -q`，退出 0，**7 passed**，43.82 秒。均为隔离 Git/DB/HOME/CLI/Kaneo 夹具，无真实模型、业务 verify 或生产服务访问。最终 `git diff --check` 通过。
- 主 agent 经原生 UI 只读核实现用 `/Applications/KaneoPilot.app` 为 **v0.4.8 / build 138**：截图任务仍显示失败 1 / 阻塞 0，原因与原报告一致。该安装版本的核心和 Swift 均不识别新原因码的恢复语义，因此**当前原记录保持原样，待兼容版本可用后再更正**；不将候选代码中的恢复通过误当成已安装版本可继续。原任务成果与全部旧证据保留。
- 候选源码/测试/协议指纹保存于 `build/kp-c03-evidence/source-snapshot.json`；该目录归本任务，保留用于后续云端对照。尚无提交/推送/合并/发布/安装，工作树保留继续本任务。
- 下一步：取得提交并推送任务分支、运行 Packages 预览验证的授权；云端完成 Swift 编译及既有原生夹具，再用精确候选包核验“阻塞、待补充材料或决定、沿用计划重新执行”路径。正式发布/安装沿后续明确授权，兼容版本可用后再经核心命令更正原记录。必要验证尚未完成，维持“实现中”。

## 2026-11-07 云端构建

- 用户要求“构建新版本”，授权提交并推送当前修复分支，执行 Packages 默认 preview。分支构建产物版本为 `0.0.0`，build 取 Actions run number；正式 latest 当前为 `v0.4.8`。本次先完成可检查的两端构建及原生验收，不猜测稳定版本号。
- 恢复时已核对路径、分支、HEAD、索引、未提交差异与全部 worktree；主仓无关文件仍保留。17 个源码/测试/协议文件与已测候选指纹 `9eabd1f83e8e076eff287eeff947a821e06d412ed550161ce8758915b065dc0f` 全部一致，因此沿用该精确差异的本地 282 tests + 68 subtests 和 7 项受管进程回归证据。
- 下一步：记录提交与云端 run 身份，等待全部必需 job，通过后下载并核验两端产物，使用同 run 的独立签名 App 进行隔离原生界面检查。
