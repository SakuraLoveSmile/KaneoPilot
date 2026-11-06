# KP-C03：执行缺材料或决定时显示阻塞

状态：待体验（修复、本地隔离检查、Packages #140 全部必需检查、两端包核验及 Build 140 隔离原生界面实测完成；尚未合并、正式发布、安装或更正原业务记录）。

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
- 候选源码/测试/协议指纹保存于 `build/kp-c03-evidence/source-snapshot.json`；该目录归本任务，保留用于后续云端对照。修复已签名提交并推送，尚无合并/发布/安装，工作树保留继续本任务。
- 当前下一步：用户体验 Build 140 预览包；正式版本号、合并/发布/安装沿后续明确授权。原业务记录继续保留，待兼容版本成为实际使用版本后，再经核心 `resolve await_input` 更正。工作树尚未合入 `main`，保留源码、隔离夹具与证据继续本任务，不清理其他 worktree。

## 2026-11-07 云端构建

- 用户要求“构建新版本”，授权提交并推送当前修复分支，执行 Packages 默认 preview。分支构建产物版本为 `0.0.0`，build 取 Actions run number；正式 latest 当前为 `v0.4.8`。本次先完成可检查的两端构建及原生验收，不猜测稳定版本号。
- 恢复时已核对路径、分支、HEAD、索引、未提交差异与全部 worktree；主仓无关文件仍保留。17 个源码/测试/协议文件与已测候选指纹 `9eabd1f83e8e076eff287eeff947a821e06d412ed550161ce8758915b065dc0f` 全部一致，因此沿用该精确差异的本地 282 tests + 68 subtests 和 7 项受管进程回归证据。
- 签名提交：`d147b66467c20245337adf59c0ea43adc1f19059`（Git 签名核验 `G`），已推送任务分支。push 自动触发 [Packages #139](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/42263073806)，run ID `42263073806`，源码绑定同一提交，preview 版本 `0.0.0` / build `139`。未重复启动构建。
- #139 两端打包与两组 Swift 夹具成功，但 `checks (4, 3.11)` / `checks (4, 3.13)` 的同一核心检查 `EventKindLabelParityTests.test_swift_map_matches_core_exactly` 失败：本次新增核心审计事件 `execution_classification_corrected`，遗漏 Swift 记录页的同名中文映射。已补齐“执行分类已更正”；不改测试预期或门禁。#139 不能作为完整通过交付。
- #139 云端独立签名 App（`local.kaneopilot.software-update-check` / `0.0.0` / `139`）的 ZIP 源码身份、大小、SHA256、App 签名及 6 个相关 bundled core 文件与候选一致。以真实核心隔离夹具经 `resolve await_input` 得到的投影，配合既有 FakeCore 和独立根目录实测：看板 **阻塞 1 / 失败 0**；任务列表、需要你、详情均显示“待补充材料或决定”和具体设备/授权会话缺口；“重新执行（沿用计划）”确认说明保留原成果、工作树和执行记录，并明确会再次调用模型。已取消确认；命令日志无 `resolve/enqueue/session-request-start/start-task-ai-action`，只停止本任务预览进程 PID 28070。未改现用 App/业务 DB、未调用模型。
- #139 DMG 与 APK 已下载到 `build/kp-c03-evidence/packages-139/`，元数据源码/build/版本、大小、SHA256 均匹配；DMG 签名指纹匹配。它们仅作失败构建诊断证据，最终交付等待修正后的全绿候选。
- 补齐事件映射后本地运行 `python -m pytest core/tests/test_event_kind_labels.py -q`（Python 3.10.10），**5 passed**；`git diff --check` 通过。重推后继续完整 Packages，最终证据绑定新提交与新 run，#139 的界面观察仅保留为此前候选实测。
- 修正签名提交 `c09da5a67ac7a24267f456b6cd9bcc0a4d6716d1`（`G`）已推送；自动触发 [Packages #140](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/42265110995)，run ID `42265110995`，preview `0.0.0` / build `140`。#139 已由工作流的后续 push 规则取消剩余 native job，保留原失败证据。独立 reviewer 静态复核确认本补丁事件名称与中文均同核心一致，未修改测试/预期/工作流门禁。
- #140 attempt 1：全部 10 组核心检查步骤通过（Python 3.11 / 3.13），Android job 成功；macOS 编译、源码哈希核对及签名完成后，`hdiutil create` 报 `Resource busy`，导致 macos-package job 失败。日志已保存于 `build/kp-c03-evidence/packages-140/reports/package-attempt1/`。这是当前 runner 的 DMG 创建错误，源码不变；GitHub 拒绝在整轮尚运行时重跑单个 job，待本轮其余检查完成后仅重跑该 job 及其依赖后续，不重复已通过的检查或降低门禁。
- #140 的 10 组 Python job、2 组 Swift 夹具、Android 和 native-platforms 最终均成功（15 项必需 job）；取得 native 冷构建/权限证据后，已按同一 run / 同一源码重跑唯一失败的 macos-package（attempt 2，新 job `128062650803`），未重复已通过的检查。
- 为避免 upload-artifact v4 的同名诊断报告冲突，先核验本任务 attempt 1 临时报告 artifact `13527166908` 确属 run #140 / c09da5a，下载 ZIP 与服务端大小一致且 ZIP 校验通过，保留工作树副本并另备份至主仓 `build/recovery/kp-c03-build-140/package-reports-attempt1.zip`（SHA256 `d2b364daab37a280f0c95435aeb72c8d562a34b41d4b145989194f00ca76f461`、6157 bytes，附原 artifact 元数据与 SHA256SUMS），再清理该临时远端诊断 artifact，让重跑上传新报告。未删除其他 artifact、安装包、Release 或标签，旧诊断内容可完整恢复。
- Android #140 主包 `com.sakurasep.kaneopilot / 0.0.0 / 140` 下载后以 aapt 和 apksigner 只核验包；SHA256 `2696023031b6f7d2fe04b51b85a23e41ed4486a9e99bd52d14600ce285baab37`，证书 SHA256 `10837ec7648b454d6b693db9a4fe474508da5fcdc00f4a441c306dc6d347eee6`，均与源码绑定的元数据一致。未安装到 Android 设备。
- 最终云端结果：#140 / attempt 2 / 源码 `c09da5a67ac7a24267f456b6cd9bcc0a4d6716d1`，顶层 `completed / success`。已核对完整 18 个最新 job：identity、10 组 Python 3.11/3.13 核心+集成检查、2 组 Swift 夹具、Android、macos-package、native-platforms 共 **16 项必需 job 全成功**；publish 与非必需 fulltext probe 为预期 skipped。原始 run/jobs JSON 保存在 `build/kp-c03-evidence/packages-140/{run-final,jobs-final}.json`。native `result.json` 的源码一致、status=passed、model_calls=0、production_access=false，前后源码快照相同。
- 最终产物：`KaneoPilot-ci-140-macos-arm64-local-signed.dmg`（arm64 / local.kaneopilot / 0.0.0 / 140），SHA256 `79afcda4380073b5e367e9f8b04dc1eb4438d8fb9dd57f052bee1377cff51419`；DMG CodeDirectory SHA256 `70981b8d199d205bc1c4594f354a402303a902e217d6c0bce232d179f7bc04d4`。下载 ZIP 校验、元数据身份/大小/哈希、DMG 与 App 签名、只读挂载后的 App 身份/arm64 架构，以及 6 个相关 bundled core 文件与当前源码全部一致，已正常卸载卷。独立 UI ZIP 同样核对源码/哈希/大小/签名/版本与 bundled core；本机未编译或重签。
- 最终包下载曾遇 GitHub 连接 EOF；用同一 artifact 的 HTTPS Range 续传，完成后 ZIP 校验、内包哈希及签名全部通过，未交付残缺文件。安装包放在 `/Users/sakurasep/Downloads/KaneoPilot-build-140/`，附 `SHA256SUMS` 和 `build-info.json`；交付目录运行 `shasum -a 256 -c SHA256SUMS`，两包均 OK。APK 为 Debug 签名，不作为正式签名覆盖升级证据。
- Build 140 最终原生实测：以同一真实核心隔离投影和只读 FakeCore adapter 启动云端独立 App；看板 **阻塞 1 / 失败 0**，列表/详情为“待补充材料或决定”与具体设备/会话缺口，“重新执行（沿用计划）”确认文案说明保留计划/原工作树/已有改动/原执行记录且会新调用模型。已取消确认，命令日志无 resolve/入队/模型启动；仅停止本任务 App PID 33484。观察与命令日志保存在 `build/kp-c03-evidence/ui-fixture/native-observation-140.json`、`commands-140.jsonl`。生产 App、原业务 DB 和全部旧证据保持原样。
- 本次构建范围已完成，状态为“待体验”。没有创建 PR、合并、正式 Release、安装或付费重跑；未把隔离界面验证当成原 MediaPlayer 的真实播放验收。任务工作树尚未合并，继续保留；安装包已独立交付到下载目录，不依赖后续保留该工作树。
