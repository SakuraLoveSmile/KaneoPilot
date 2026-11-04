# KP-C02：Codex 执行权限与失败收尾修复

状态：实现中（修复已合入 main；正式 v0.4.5 等待云端 macOS runner，尚未发布或安装）。用户于 2026-11-04 要求修复会话 `01a1a6cc-2c36-7bd1-a606-c65cf2ce2f33` 暴露的问题，随后要求“构建新版本”，并明确选择“正式 v0.4.5：合入 main，发布正式更新”。

- 目标：冻结 verify 中直接调用的 Flutter 能写入自身 SDK 缓存；Codex 收到正确的 JSON 报告要求；失败轮次进入需要处理状态，保留原始原因和未提交成果。
- 范围：KaneoPilot 核心和隔离回归测试。不改 MediaPlayer 源码、冻结计划或旧执行证据，不代跑其 verify，不发起付费模型执行。
- 基线：`main` / `50aa7e26372a65976f434d1873546823bdae91c7`。分支 `fix/20261104-kp-c02-codex-execution`；工作树 `/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261104-fix-kp-c02-codex-execution`；集成目标 `main`。修复阶段先完成代码与隔离验证；本次构建使用该任务分支提交，后续构建证据见下节。
- 生产证据只读：任务 `csim4lhdf81m1k7wib615fdv`，run `872fd957-3f0d-4b31-9ea5-8277d92fef0f`。Flutter 四条命令均因 `engine.stamp.tmp.*` / `engine.realm` 无写权限退出 1；CLI 正常退出 0，最终 JSON 为 failed，worker 退出 3；轮次 failed 而任务总状态仍 running，监控反复 observation_blocked。原任务工作树保留。

## 冻结实现与验收

T1（主 agent）：仅从已冻结 verify 的直接 Flutter 调用静态定位 SDK；给规范化后仍在同 SDK 下的现存 `bin/cache` 增加 `--add-dir`。路径随执行 spec 和阶段契约冻结，启动再核对；旧 spec 缺字段继续不增加权限。保持 workspace-write、approval_policy=never、网络限制及现有模型/服务商配置，不开放用户主目录、SDK 源码、其他项目或任意命令推导的写路径。覆盖带空格 SDK、LANG 前缀、重复命令、缓存符号链接逃逸、非 Flutter 命令、冻结路径篡改和旧 spec 兼容。

T2（主 agent）：Codex 专用指令要求最终 `{status,summary,checks:[{command,exit_code}],questions}` JSON；不再要求 SubmitGoalReport。Plus 保留现有 Goal 报告契约。以真实 dispatch 生成的 spec/子进程输入核对后端分流，继续要求每条冻结命令真实独立退出码和当前源码快照。

T3（worker，gpt-6.1-sol / high）：独占 `core/kanpilot/auto_monitor.py`、`core/tests/test_auto_monitor.py`。Codex 已确认结束的 failed/aborted 观察结果继续读取终态并走既有 needs_user/execution_failed，保留失败原因；deferred、运行中、完成和 Plus 路径保持原语义。不启动或续发 CLI。

T1/T2 与 T3 在同一工作树并行，主 agent 唯一写其余文件和 docs；均使用临时 Git、DB、HOME/SDK 和本地夹具，不共享生产数据库或服务。集成后独立 reviewer（Sol/high）只读复核权限、历史兼容与状态收尾，主 agent 检查最终组合态。

T4（主 agent）：运行 Codex 配置/worker/契约/问答/verdict/监控及真实受管 stub dispatch 的隔离检查。通过无需模型的本机 Codex sandbox 探针验证新增缓存可写、相邻 SDK 源码不可写；不编译 KaneoPilot Swift/Android。测试与原生应用、原任务实际执行、正式发布分别记录。

## 执行记录

- 2026-11-04：核对主仓 main，仅有无关 `.local/` 未跟踪；创建本任务独立工作树。现用 `/Applications/KaneoPilot.app` 的相关核心源码与基线一致。
- 官方依据：OpenAI Docs `https://learn.chatgpt.com/docs/developer-commands.md`，确认额外可写目录应使用 `--add-dir`；本机 `codex exec --help` 同样提供该参数，未调用模型。
- T1/T2/T3 已实现。未增加全局 Codex 设置或执行配置字段；新增权限只在新执行 spec 内冻结，覆盖缓存重定向（包括链接到 SDK 自身根目录）拒绝、路径篡改和历史缺字段兼容。Plus 原指令与基线逐字节一致；Codex 的实际 dispatch 输入只要求最终 JSON。
- 主 agent 最终核心组合：`/Users/sakurasep/.pyenv/versions/3.10.10/bin/python -m pytest core/tests/test_codex_execution.py core/tests/test_execution_stage_contract.py core/tests/test_codex_exec_config.py core/tests/test_codex_inputs.py core/tests/test_codex_verdict.py core/tests/test_auto_monitor.py -q`，退出 0，**215 passed、55 subtests passed**。耗时 32.96 秒。隔离 HOME、SDK、Git、DB 与 CLI stub，无生产服务调用。
- 主 agent 最终受管进程组合：`/Users/sakurasep/.pyenv/versions/3.10.10/bin/python -m pytest tests/test_codex_dispatch.py tests/test_pi_dispatch.py -q`，退出 0，**14 passed**，耗时 76.54 秒。包含新非空 SDK 缓存路径的 dispatch/spec/阶段契约/CLI argv 传递、实际本地夹具 verify 退出码、失败 worker → monitor → needs_user/execution_failed、原失败原因、成果保留及只启动一次。模型决策与 CLI 协议为 stub；不是实际模型或 Flutter 构建验收。
- T3 回归在旧实现上先复现 **2 failed**，修复后监控文件 **144 passed**，随后已纳入上述主 agent 核心组合。没有删除、跳过测试或降低门禁。
- 本机 macOS Codex sandbox 对照退出 0：未加缓存路径时 cache / SDK 源码 / 其他目录均 denied；加入冻结 cache 路径后只有 cache allowed，其余仍 denied。使用 `~/Library/Caches/kp-c02-sandbox-*` 内的临时 SDK 与工作区，结束已清理，未写真实 SDK。当前 CLI 的 `sandbox` 要求命名 permission profile，因此探针用等效文件系统根验证 seatbelt；真实 exec 的非空 `--add-dir` 由上述受管 stub 集成验证 argv，未声称真实模型端到端通过。原始结果：`build/kp-c02-evidence/sandbox-probe.json`。
- 探针准备时先发现新版 CLI 不再使用 `sandbox macos` 子命令，随后旧 sandbox 配置调用提示需要 `--permission-profile`；按当前 help 与官方 permissions 文档改用独立临时命名策略后通过。这些准备失败没有修改业务代码或用户配置，不记为验收通过。
- 对原生产 spec/job **只读**运行新代码的 `execution_contract.validate_spec` 通过，旧 spec 没有新增权限；从原冻结命令计算的新轮次候选精确为 `/Users/sakurasep/devsdk/flutter/bin/cache`。没有回写旧 spec、报告、任务状态或重试模型。
- 独立 reviewer（gpt-6.1-sol/high）只读审查实际业务差异，未发现实质性问题；其结论不替代上述实测。`git diff --check` 通过。最终代码/测试差异摘要及哈希另存 `build/kp-c02-evidence/verification-summary.json`。
- 当前 `/Applications/KaneoPilot.app` 未更新，生产旧任务仍需修复版本的监控重新核对；原 MediaPlayer 改动与冻结记录保留。未运行原任务 verify，未在本机编译 KaneoPilot、未安装、未调用真实付费模型、未发布。恢复执行应使用既有显式新轮次重试；旧轮次不会因新代码自动获得写权限。
- 后续体验：安装修复版本后核对原失败任务的实际状态收尾；如需继续原任务，显式启动新执行轮次。修复已合入 main，正式发布仍等待适用云端检查，因此暂保留本任务工作树；检查与发布完成后按项目约定清理。

## 2026-11-04 云端构建

- 用户要求构建新版本，提交、推送当前修复分支以触发 Packages。使用默认 preview 通道，产物为 Android Debug APK 与 macOS arm64 DMG，预览版本按工作流为 `0.0.0`，build 为 Actions run number。正式版本号、Release、合入和安装不在本次构建操作中。
- 构建前重新 fetch `origin/main`，基线仍为 `50aa7e26372a65976f434d1873546823bdae91c7`，与本地 HEAD 无分歧。代码/测试快照仍为 `1aac2a0f928967db625f9a32313dbbc48c34404f565aeb9a99bd5d26f3386e7e`，没有新增源码修改，因此沿用同一候选的隔离验证证据。
- 已核对索引并创建签名提交 `a2022389c66c04e1d5bae552d353c02ea9302235`（本机签名状态 G），推送至同名任务分支。Packages push run **41852259905 / build 114**：`https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/41852259905`，源码 SHA 与该提交一致。
- Packages 于 2026-11-04 23:09:18（台北时间）整体完成，结论 **success**；identity、android、macos-package、两组 macos-fixtures 及 Python 3.11/3.13 的全部检查分片共 **15 个必需 job 成功**。按 preview 规则跳过 `publish` 和未显式启用的 `macos-session-fulltext-probe`，共 2 个正常跳过项。完整 run、job 和 artifact 记录为归档中的 `cloud-run.json`，watch 命令退出 0，日志为 `packages-114-watch.log`。
- 产物归档：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/.archives/20261104-kp-c02-packages-114`。主包：`macos/KaneoPilot-ci-114-macos-arm64-local-signed.dmg`、`android/KaneoPilot-ci-114-android-debug.apk`。两者均为预览 `0.0.0` / build `114`。
- Mac 本地核验退出 0：DMG SHA-256 `47eda26059acb6f90c5a6ca1f9eb9fd22572f8b26178c475bacef3b6149c01a2`，镜像完整性、DMG/App 严格签名、arm64-only、版本/构建号通过；6 个关键内置核心文件与构建提交逐字节一致，只读挂载已卸载。
- Android 本地核验退出 0：APK SHA-256 `8f5945946301e647d6a2778c1ec853bbbc7b3c55f770fff3eaaf01f937c32f57`，ZIP 完整性、aapt 应用标识/版本/构建号及 apksigner 签名通过，证书指纹与云端 metadata 相符。预览 Debug APK 的证书和正式版不同。
- 可复查脚本与原始核验结果保存在上述归档的 `verify-macos.py` / `macos-verification.json`、`verify-android.py` / `android-verification.json`，完整包哈希见 `SHA256SUMS`，修复阶段隔离证据另存 `source-verification/`。本机仅核验下载包，未编译或安装应用；原任务未重试。
- 构建源固定为 `a2022389c66c04e1d5bae552d353c02ea9302235`；后续仅补充本记录的文档提交不改变该包的源码身份，不作为已构建的新版本。当时分支尚未合入 `main`，包与证据已独立归档；后续正式集成见下节。

## 0.4.x 版本纠正

- 用户指出交付版本应沿用 `0.4.x`。上面的 `0.0.0` 是误选默认 preview 通道的产物，不作为本次版本交付完成依据。
- 重新查询 GitHub：当前 latest 为 `v0.4.4`，`origin/main` 仍为 `50aa7e26372a65976f434d1873546823bdae91c7`；本次补丁候选基础版本为 `0.4.5`。任务分支已推送提交 `d48a7f085705abbf2335c47755910222299adb40`，与已核验构建源码仅有任务记录差异。
- 已准备用户可见修复说明 `build/kp-c02-evidence/release-notes-0.4.5.md`，包含 Flutter 缓存权限、失败收尾和 Codex 报告提示；本次没有数据库 schema 变更，旧失败轮次不会自动重试。
- 现有流程的 `0.4.x` 包均伴随 Release：正式 `v0.4.5` 需先合入 `main`；测试版 `0.4.5-test.<build>` 可使用任务分支但会公开 GitHub 预发布。用户已明确选择“正式 v0.4.5：合入 main，发布正式更新”，本轮按该选择继续，不再使用 preview 或测试通道。

## 正式 v0.4.5 发布

- 正式说明为 `docs/releases/v0.4.5.md`，包含三项修复、无 schema 变更、旧轮次需显式重试和已有异常终止限制。用户已授权合入 main 与公开正式 Release；没有安装或真实模型执行授权。
- 发布前再次核对主工作区 main 仅有无关未跟踪 `.local/`，远端 main 仍为原基线，latest 仍为 v0.4.4。业务源码、测试与已核验的 `a2022389` 相同；新增内容仅为版本纠正记录和正式更新说明。
- 后续顺序：提交发布文档、快进合入并推送 main；等待该完整提交的 Packages 必需检查成功；创建直接指向它的 annotated 签名 `v0.4.5` 标签并推送；等待正式 Packages、核验公开四项资产和 latest，保全证据后清理本任务 worktree 与已合并本地分支。远端任务分支保留。
- 正式构建和核验材料独立保存在 `/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/.archives/20261104-kp-c02-release-0.4.5/`，不纳入公开 Git。
- 发布文档签名提交为 `f65be9451cb75eb547f8d8c1251565b6976713f3`（本机签名状态 G），已从原 main 快进合入并推送；主仓无关 `.local/` 保留。该完整提交的 main Packages 为 **#115 / 41856959659**：`https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/41856959659`。正式标签将固定同一源码提交，先等待本次 main 检查。
- #115 attempt 1：12 个非 macOS job 成功；3 个 macOS job 没有启动任何步骤，排队 15 分钟后 cancelled，整体 failure。GitHub 注释明确 `The job was not acquired by Runner of type hosted even after multiple attempts`，并提示 macOS arm64 capacity constraints；不是源码或检查失败。完整 run/job/注释已保存为 `cloud-main-attempt-1.json`。基于该基础设施故障请求 `rerun-failed-jobs`，保持相同 SHA 和全部门禁；首次 CLI 读 run 的 EOF 未发起重跑，确认 attempt 仍为 1 后改用 REST POST，已成功受理。
- #115 attempt 2：只重跑三个未完成的 macOS job，其余 12 个成功结果沿用。三个 job 自 `2026-11-04T15:40:59Z` 排队，超过 20 分钟后仍没有 runner、步骤或终态；最后只读快照为 `cloud-main-attempt-2-checkpoint.json`。云端队列保留，不再次提交相同重跑请求，不更换构建平台或降低门禁。
- 当前 latest 仍为 `v0.4.4`。尚未创建/推送 `v0.4.5` 标签或 Release；正式发布源码保持 `f65be9451cb75eb547f8d8c1251565b6976713f3`，本节证据文档更新不冒充已构建版本。本机未安装、未重试原任务或调用模型。
- 保留工作树 `/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261104-fix-kp-c02-codex-execution`：修复虽已合入，但适用云端检查尚未完成，仍需继续正式发布。7 个非缓存忽略文件已逐一核对 SHA-256 并备份到独立归档的 `worktree-evidence/`，清单为 `worktree-evidence-manifest.json`；其余 147 个忽略文件是可再生成的 Python/pytest 缓存。未清理其他任务或主工作区 `.local/`。
- 下一步沿本轮已有授权继续：先读取 #115 当前状态。若成功，核对同一 SHA、15 个必需 job 成功及正常跳过项，保存完整最终回执；再用 `docs/releases/v0.4.5.md` 创建直接指向该提交的 annotated 签名标签并推送，等待正式 Packages 的 16 个必需 job、核验公开 APK/DMG/manifest/SHA256SUMS 和 latest 后收尾。若仍排队则保留队列；若失败先读取该次 GitHub 回执，依据新证据决定恢复方式。安装和真实模型执行仍未授权。
