# KP-C02：Codex 执行权限与失败收尾修复

状态：实现中（正式 v0.4.6 / build 118 已发布，本机已安装该版本；2026-11-06 真实新轮次暴露平台构建前置条件和诊断信息遗漏。本次彻查已完成，尚未实施后续修复，详见文末）。用户于 2026-11-04 要求修复会话 `01a1a6cc-2c36-7bd1-a606-c65cf2ce2f33` 暴露的问题，随后要求“构建新版本”，并明确选择“正式 v0.4.5：合入 main，发布正式更新”；后续修复版本已明确确认“发布正式 v0.4.6”。

- 目标：冻结 verify 中直接调用的 Flutter 能写入自身 SDK 缓存及本轮工具配置、使用本地测试所需回环通信，同时限制工作区外写入与公网；Codex 收到正确的 JSON 报告要求；失败轮次进入需要处理状态，保留原始原因和未提交成果。
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
- 修复阶段 `/Applications/KaneoPilot.app` 未更新，生产旧任务仍需修复版本的监控重新核对；原 MediaPlayer 改动与冻结记录保留。当时未运行原任务 verify、未在本机编译 KaneoPilot、未安装、未调用真实付费模型或发布。恢复执行应使用既有显式新轮次重试；旧轮次不会因新代码自动获得写权限。后续正式发布见文末记录。
- 后续体验：安装修复版本后核对原失败任务的实际状态收尾；如需继续原任务，显式启动新执行轮次。正式发布、下载包核验与本任务资源收尾见文末记录；原任务工作树和现用 App 保留。

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
- #115 attempt 2：只重跑三个未完成的 macOS job，其余 12 个成功结果沿用。初次只读快照仍显示排队；2026-11-05 恢复时确认三项最终均 cancelled、没有执行步骤、runner_id 为 0，GitHub 再次回执 `The job was not acquired by Runner of type hosted even after multiple attempts` 及 arm64 容量提示。完整终态和注释为 `cloud-main-attempt-2-final.json`，不能将旧排队快照当作当前状态。
- 截至 2026-11-04 的检查点，latest 为 `v0.4.4`，尚未创建/推送 `v0.4.5` 标签或 Release；正式发布源码保持 `f65be9451cb75eb547f8d8c1251565b6976713f3`，证据文档更新不冒充已构建版本。本机未安装、未重试原任务或调用模型。
- 当时保留工作树 `/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261104-fix-kp-c02-codex-execution`：修复虽已合入，但适用云端检查尚未完成，仍需继续正式发布。7 个非缓存忽略文件已逐一核对 SHA-256 并备份到独立归档的 `worktree-evidence/`，清单为 `worktree-evidence-manifest.json`；其余 147 个忽略文件是可再生成的 Python/pytest 缓存。未清理其他任务或主工作区 `.local/`。
- 当时冻结的后续顺序：读取 #115 当前状态，成功后核对同一 SHA、15 个必需 job 与正常跳过项；保存完整回执；创建直接指向发布源码的 annotated 签名标签；等待正式 Packages 的 16 个必需 job，核验公开 APK/DMG/manifest/SHA256SUMS 和 latest 后收尾。安装和真实模型执行仍未授权。

## 2026-11-05 云端恢复

- GitHub Status 重新查询为 Actions operational、无未解决事故；Actions/Pages 事故于台北 01:01、后续 Actions 事故于 02:16 标记 resolved，官方根因报告尚待发布。这是恢复重跑的新依据，不把官方泛化事故当作本任务容量故障的已确认根因。原始状态与事故回执为 `github-status-recovery-20261105.json`。
- 对原 #115 请求一次 `rerun-failed-jobs`，已确认 attempt 3 受理；源码仍为 `f65be9451cb75eb547f8d8c1251565b6976713f3`。三个 macOS job 均已分配执行机并开始实际桌面检查和 DMG 打包，其余 12 项成功沿用。保留同一 run 和全部门禁；未换平台、未本地编译。
- main #115 attempt 3 已整体 success：同一源码的 15 个必需 job 成功，仅 `publish` 与可选全文探针正常 skipped；完整扁平回执为 `cloud-main-final.json`。
- 再次 fetch 并核对远端 main 包含发布源码、latest 仍为 v0.4.4、远端与本地均未占用 v0.4.5 后，创建并核验签名 annotated 标签。标签对象 `1eb51dbc9f229d709c1c84eae454ee5f7cc5a4bf`，直接指向 `f65be9451cb75eb547f8d8c1251565b6976713f3`，本机 `git verify-tag` 为 Good signature，已推送。
- 正式标签触发 Packages **#116 / 41963965432**：`https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/41963965432`，源码与标签一致，整体 **success**；16 个必需 job 成功，仅可选全文探针 skipped。标签与完整构建回执分别为 `tag-publication.json`、`cloud-release-final.json`。

## 正式交付与剩余体验

- `v0.4.5` 于 **2026-11-05 11:58:29（台北）**公开发布，非 draft、非 prerelease，latest 已指向它：`https://github.com/SakuraLoveSmile/KaneoPilot/releases/tag/v0.4.5`。正式版本 `0.4.5` / build `116`，源码固定为 `f65be9451cb75eb547f8d8c1251565b6976713f3`；后续任务记录提交不改变该源码身份。
- 四项公开资产完整：`KaneoPilot-0.4.5-android.apk`、`KaneoPilot-0.4.5-macos-arm64.dmg`、`manifest.json`、`SHA256SUMS`。下载后的字节数、GitHub digest、SHA256SUMS 与 manifest 逐一一致；APK SHA-256 `03eb488c498bdb9712e3a19a160534a5d26f9638078d185137fba096c760ff6d`，DMG SHA-256 `4764bf54c0c6f0982140ca30a747a2a49eaf96a72edd857133ae81afdac98914`。原始 Release、latest 和独立核验为 `release.json`、`latest.json`、`release-asset-verification.json`。
- 下载包核验通过：Android 包名 `com.sakurasep.kaneopilot`、版本/构建号正确，签名证书继续为 `5062db967ce58d65df2c47895649df43464bab9a6eadf135f3a1a81ff4785ada`。DMG 完整性及 DMG/App 严格签名通过；10 个 Mach-O 全部仅 arm64、最低系统要求均不高于 14.0、无依赖越界；内置 **153 个 Python 源文件**与发布提交逐字节一致。详见 `release-package-verification.json`、`package-verification-log.json`。只读镜像已卸载，未启动或安装 App，未读取生产 DB。
- 既有工作流将签名标签的 PGP armor 带入公开正文；仅删除正文中的这一签名文本并读回核对，保留原版本说明、CI marker 和安装要求，四项资产的 ID、digest、大小保持一致；未移动标签或替换资产。回执为 `release-body-cleanup.json`。
- 本次修复、合入、云端检查和正式发布均已完成。Android 真机、真实模型执行、安装后的原失败任务恢复仍待体验；只有用户明确确认后才标已验收。现用 App、原业务任务和原成果保留。
- 资源收尾已完成：7 个非缓存忽略文件已独立归档并再次核对哈希；确认工作树没有活动文件引用、来源提交已在 main、最终记录已完整迁回 main 后，正常移除本任务临时工作树并删除已合并本地分支。远端任务分支及其余 6 个工作树保留，主工作区无关 `.local/` 与缓存清理计划均未暂存。实际清理回执为 `cleanup-receipt.json`，记录迁移补丁与核对为 `closeout-plan.patch`、`record-transfer.json`。

## 2026-11-06 Flutter 验证环境补齐

- 用户明确要求修复会话 `01a1ac35-5a49-79e1-a919-77f3e872aaa9` 暴露的问题。只读证据对应同一业务任务、新 run `3b1f97fc-622b-4cb8-bffb-a392391b7bd4`；安装版确认为 `0.4.5` / `116`，相关核心源码与本仓基线一致。SDK 缓存已在冻结可写范围内；四项 Flutter 验证均因 `~/.config/flutter/tool_state` 不可写退出 1；使用任务临时 XDG 配置后的局部 Flutter 测试又被 server socket 限制阻断，adb 监听同样受限。会话正常报告 failed，任务已进入 needs_user，原改动保留。
- 本次基线为本地 main `3896b8e07411cf11d7cbfedfecfcfcf3926a0199`。工作树 `/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261106-fix-kp-c02-flutter-sandbox`，分支 `fix/20261106-kp-c02-flutter-sandbox`，目标本地 main。远端 fetch 当前因 TLS 连接失败受阻，不把本地 origin/main 当成本次远端核验。
- 保留原业务工作树、冻结计划、历史权限、生产 DB 与已发布 v0.4.5。继续修复不代表安装、续跑付费模型、移动既有标签或发布一个未确认的新版本。KaneoPilot 原生编译仍仅走云端。

T5（主 agent，权限能力由只读 analyst / gpt-6.1-sol high 并行核查）：根据当前 CLI 的实际能力冻结 Flutter 工具环境；配置放入本轮工作区独占目录，SDK 仅缓存可写；允许本机测试所需回环通信并保持公网连接限制，不开放整个用户目录。新环境随 spec 和阶段契约绑定，启动及 resume 重新核对；旧 spec 不自动扩权。先验证配置、监听与外部拒绝，再接入启动链。主 agent 唯一维护业务代码和 docs，analyst 不改文件、不启动模型或沙箱探针。

T6（worker / gpt-6.1-sol high，已完成）：独占新的环境回归测试文件；覆盖任务专属配置、非 Flutter 与历史兼容、权限/环境字段篡改、目录链接逃逸、启动前失败不调用模型及恢复语义。共享接口由主 agent 写在本节，测试与实现并行；不修改已有断言来掩盖不满足的门禁。

T7（主 agent）：在最终组合态运行适用的 Python 隔离检查和受管 stub dispatch，再通过不调用模型的真实 Codex sandbox 检查配置写入、IPv4/IPv6 回环通信及外部写入/公网拒绝。使用独立最小 Flutter 夹具核验真实工具启动与测试路径，不在 Pilot 本地代跑原任务 verify、不操作原任务数据、不编译 KaneoPilot Swift/Android。独立 reviewer 复核权限和历史兼容；实测与未验证项分别记录，适用检查完成后再标待体验。

### 本次共享接口与权限决定

- 当前 CLI 0.164.0 的初始真实探针表明：`network.enabled=false` 会拒绝本地监听；单独改 true 会放开公网，不能采用。在未继承域名规则的 profile 下，显式启用 `features.network_proxy=true`、`network.enabled=true`、`mode="limited"`、`allow_local_binding=true` 和空 `domains`，可同时通过回环通信、直接公网 EPERM 和代理请求 403。后续冲突对照发现空表会递归合并，不能清除已有规则，因此最终采用下述每轮独立命名。底层可能允许通配 bind，承诺范围是限制非回环流量，不声称 bind(0.0.0.0) 必定失败。初始三组实测为 `build/kp-c02-environment-evidence/*probe.json`。
- 新增 `core/kanpilot/codex_environment.py`，主 agent 唯一写入。`freeze(spec)` 仅对冻结命令识别的 Flutter SDK 缓存非空的新执行写入 `tool_environment`；`validate(spec)` 重新推导并核对完整字段；`launcher_config(spec)` 返回命名权限、受限代理和显式 shell 环境配置的 `-c` 参数；`child_env(spec, base_env)` 返回只覆盖冻结工具环境变量的副本；`preflight(spec, env)` 准备本轮私有目录并用 `codex sandbox` 检查配置/临时写入、回环收发和禁外网，失败不得启动模型。
- `tool_environment` 固定 `v: 1`：`environment` 只含 `XDG_CONFIG_HOME`、`TMPDIR`、`FLUTTER_SUPPRESS_ANALYTICS`；前两者位于 `<cwd>/.pi/kanpilot/codex/<run_id>/config`、`tmp`，后者为字符串 `"true"`。`network` 固定上述受限代理字段、`allow_upstream_proxy=false`、`domains={}`。spec/阶段契约绑定整个对象；启动前拒绝字段、路径、符号链接或 SDK 缓存的变化。旧 spec 没有对象时保留原 sandbox 和环境，Plus 不受影响。
- `preflight` 返回仅含安全结果的回执：`v: 1`、`checks`（各检查名到布尔结果）、`status: "passed"`；失败抛出 `KanpilotError("codex_environment_unavailable", ...)`。worker 将成功回执写入本轮证据并在每次初次启动/resume 前重做，失败终态保留原因，不发送模型输入。
- 新 profile 的文件系统保留全盘只读、工作区和冻结 SDK 缓存可写，并显式保护工作区 `.git`、`.codex`、`.agents`。命名 profile 与旧 `sandbox_mode` 互斥，所以新分支不再同时传 `-s workspace-write` 或 `sandbox_mode`；不切换到无沙箱。目录准备使用不跟随符号链接的逐级创建，主机用户配置不写入。
- `permission_profile(spec)` 由新 run UUID 与 cwd 派生 `kanpilot_flutter_<sha256前24位>`，避免复用已有静态 profile 的域名/文件系统表；launcher 与 sandbox 统一调用，resume 同名。真实对照中原固定 profile 会继承 `example.com=allow`，独立 profile 则返回 CONNECT 403；不把空表形状当作已清空规则的证据。预检使用真实公网域名 `https://example.com/`，只接受 HTTPS CONNECT 阶段的代理 403，不接受目标站点 HTTP 403、保留 IP 被拦或普通网络失败。`--ignore-user-config` 并非 sandbox 的可用参数，未使用该无效方案，也未修改宿主 Codex 配置。
- T6 由 worker 独占 `core/tests/test_codex_environment.py`；只读新模块和既有 adapter/worker/阶段契约，其他所有文件由主 agent 写入。测试按上述接口并行准备，可在模块尚未实现时保留失败；不改变共享接口、不修改旧测试，不启动真实模型或生产服务。双方临时 Git/HOME/SDK/DB 均在各自临时目录，真实沙箱探针只由主 agent 执行。

### 本轮结果与验证（2026-11-06）

- T5/T6/T7 完成：Flutter 专属环境随阶段契约冻结；每次首次启动/resume 都先核对并做无模型环境预检。配置/临时目录通过描述符创建、不跟随符号链接；SDK 仍仅缓存可写，历史 spec 不自动获得新权限。CLI 全局配置未修改，Plus 路径保持原逻辑。预检失败无模型调用，`auto_dispatch` 从已退出、同 task/run、not_sent、failed 的持久终态保留具体原因，进入需要处理；送达状态仍保守保留 unknown，不自动重发。
- 主 agent 最终核心组合使用 `/Users/sakurasep/.pyenv/versions/3.10.10/bin/python -m pytest`，范围为 `core/tests/test_codex_environment.py`、`test_codex_execution.py`、`test_execution_stage_contract.py`、`test_codex_exec_config.py`、`test_codex_inputs.py`、`test_codex_verdict.py`、`test_auto_monitor.py`、`test_auto_dispatch.py`，退出 0，**282 passed、110 subtests passed**，32.36 秒。最终受管组合 `tests/test_codex_dispatch.py tests/test_pi_dispatch.py -q`，退出 0，**15 passed**，64.60 秒。HOME/SDK/Git/DB/CLI 均为隔离夹具；集成覆盖新环境完整传递、预检失败无 session/零模型、具体原因进入 needs_user、重复启动去重。CLI 与模型决策是 stub，不冒充真实模型证据。
- 最终实现/测试文件指纹为 `83dc08704fe5e5c41c84f5dac732eabd2c0795c5e8513c542ac84ef40edbb7ce`，逐文件哈希与运行范围见独立归档 `verification-summary.json`；规格/计划文档不计入该代码指纹。测试加强了新 profile 全量边界断言，没有删除、跳过或降低旧权限门禁。
- 原生 Codex CLI **0.164.0** 的最终代码配置实测：配置/临时文件可写；IPv4/IPv6 回环收发成功；直接公网连接 EPERM；真实公网域名在 HTTPS CONNECT 阶段被代理 403 拒绝。项目元数据 `.git/.codex/.agents`、SDK 源码及其他私人目录写入 EPERM。静态 profile 继承域名放行与新轮次 profile 拒绝的对照见 `profile-isolation.json`。同 Mac 经主非回环 IP 访问监听端口未能收发，回环正对照成功；此证据不代替实体局域网设备入站验证。通配 bind 实际允许，不宣称被拒绝。
- 独立最小 Flutter 工程使用真实 **Flutter 3.47.7 / Dart 3.13.5**：最终代码生成 profile 后，`flutter --version`、夹具离线依赖准备、`flutter analyze --no-pub`、`flutter test --no-pub` 全部退出 0，测试实际 **1 passed**，`tool_state` 仅生成在本轮 `.pi/.../config`。离线依赖准备使用工作区独占 PUB_CACHE 元数据和只读现存包源；最终 analyze/test 使用宿主默认环境加冻结的三个环境变量。没有在 Pilot 代跑原业务 verify，也没有编译 KaneoPilot Swift/Android。
- 早期探针的失败与局限保留：直接对默认 pub cache 做离线 pub get 因 `active_roots` 不可写退出 66，正式修复范围仍为原冻结的 `--no-pub` 命令，不据此开放整个 pub cache。最初保留 IP 的代理拒绝不足以证明域名策略；补充真实域名对照后发现递归合并，已改为独立 profile。空表不清除旧规则；全局 `*=deny` 为 CLI 无效语法，未采用。`domain-conflict-control.json` 的项目配置对照未证明配置加载，因此不作为清空规则证据。最终通过证据以 `final-native-verification.json`、`profile-isolation.json` 为准。
- 独立 reviewer（gpt-6.1-sol/high）只读审查指出无 session 原因丢失和站点 403 误判，两项均已修复并纳入最终回归；收口复核未发现未解决的实质性问题。只读核验原生产 spec/job 的阶段契约通过、环境字段仍缺省；原任务未重试、未写生产 DB、未调用模型。安装版仍 **0.4.5 / 116**，未安装、构建或发布后续版本，macOS/APK 业务构建缓存、真实模型和实体设备仍未验证。
- 本轮交付目标仅为本地 main；不推送、不创建标签、不覆盖 v0.4.5 或选择未确认的新版本号。证据与本地集成/资源收尾回执保存在 `/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/.archives/20261106-kp-c02-flutter-sandbox/`。该目录保全全部非缓存探针材料；本次临时工作树与已合并本地分支按工作约定收尾，其他工作树、主仓无关文件与原业务工作树保留。

## 2026-11-06 后续云端版本构建

- 用户随后明确要求“构建新版本”，授权将已合入本地 main 的修复送入既有 Packages 云端流程。重新 fetch 核对远端 main 为 `3896b8e07411cf11d7cbfedfecfcfcf3926a0199`，本地仅领先修复提交 `9f9e07444149f872633b9951090f48c1fd103569`；实现/测试文件仍与前述指纹逐一一致，无新增源码变化。已将该修复提交推送 main，保留无关 `.local/` 和缓存清理计划。
- 构建前 latest 实时确认为 `v0.4.5`。正式版 `v0.4.6` 的发布说明及独立核验脚本已准备在 `/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/.archives/20261106-kp-c02-release-0.4.6/`。`docs/build-release.md` 要求“版本号由维护者明确决定，不自动猜测下一版”；用户随后明确回复“确认，发布正式 v0.4.6”，本轮版本与公开正式 Release 已获授权；不覆盖既有标签或资产。
- main 推送触发 Packages **#117 / 42080508694**：`https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/42080508694`。源码为 `9f9e07444149f872633b9951090f48c1fd103569`，attempt 1 整体 **success**；15 个必需 job 成功，仅 `publish` 与可选全文探针正常 skipped。三个 macOS job 均实际分配 runner 并完成，没有重现此前容量排队。GitHub CLI 状态查询期间出现 EOF；改用 REST 只读核对取得完整终态，没有据此重跑构建或降低门禁。完整回执为 `cloud-main-final.json`。
- 本次 main 检查包按工作流采用内部版本 `0.0.0` / build `117`，仅用于正式发布前源码门禁，不作为用户所要求的新 0.4.x 版本交付。已独立下载并核验两个包：DMG SHA-256 `c40b3b171d04fefa1a45e38037dca0d786d24ab032080375a304257acdcc1a97`，APK SHA-256 `dcc14817e021be077246df85d85df5b166ed902128e3f53e84f91ab727144ff4`。两者的源码/版本/构建号、metadata 大小与哈希一致；DMG 完整性及 DMG/App 严格签名、arm64-only、8 个关键内置核心模块逐字节匹配通过；APK ZIP 完整性、包名、版本/构建号和签名与 metadata 相符。Debug APK 不是正式签名包。回执为 `main-macos-verification.json`、`main-android-verification.json`，只读镜像已卸载。
- APK 初次本地核验被现有损坏的 jenv Java shim 阻断；改为显式调用现有 JDK 17 和 apksigner.jar 后通过，没有修改用户 Java 配置或安装依赖。这是本地核验入口问题，不是云端构建失败，原始情况见 `main-android-local-check-attempt-1.json`。
- main 门禁与独立包核验完成后创建记录提交 `abb7d76dae16698a695c30051cd327db162c73d7` 并推送；它仅更新任务文档，产品源码仍固定为已检查的 `9f9e07444149f872633b9951090f48c1fd103569`。本轮没有新增临时工作树，原修复工作树和已合并本地分支已安全清理；主工作区、其他任务和恢复归档保留。

## 正式 v0.4.6 发布

- 用户明确确认正式 `v0.4.6` 后，重新核对远端 main 包含已通过的发布源码、版本与标签未占用，创建直接指向 `9f9e07444149f872633b9951090f48c1fd103569` 的签名 annotated 标签。标签对象 `06d9b1fdfea38df52f9f8f49467b51eacfc9a3cd`，本机 `git verify-tag v0.4.6` 为 Good signature；已推送并读回相同远端 ref，未移动或复用标签。标签正文与 Git 默认去注释处理后的发布说明一致；正式公开正文使用完整 Markdown 排版，并保留工作流安装说明与来源标记。
- 正式 Packages **#118 / 42086214147**：`https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/42086214147`，attempt 1 整体 **success**，源码与标签一致；16 个必需 job 全部成功，仅可选 `macos-session-fulltext-probe` 正常 skipped。三个 macOS job 均实际执行，无容量排队失败；未重跑、换平台或降低门禁。完整回执为 `cloud-release-final.json`。
- 正式 Release 于 **2026-11-06 02:49:01（台北）**公开，非 draft、非 prerelease，latest 已指向 `v0.4.6`：`https://github.com/SakuraLoveSmile/KaneoPilot/releases/tag/v0.4.6`。两端版本均为 **0.4.6 / build 118**，源码固定为 `9f9e07444149f872633b9951090f48c1fd103569`；后续任务记录提交不改变已发布包的源码身份。
- 四项公开资产已独立下载，字节数、GitHub digest、SHA256SUMS 与 manifest 逐一一致：APK SHA-256 `51e432aee0825c6a4bf22ca5e49a2479627c3ecd177fa4c89fc1f164ad1a30cf`，DMG SHA-256 `87a0bf47c922d87c0b59d0055edd65172144c971d69fb0058516c0f839cc06fa`，`manifest.json` SHA-256 `3caad3403ef094f095d334880c90bb0d892e4bcd5b7355f758ea1665e3e60c95`，`SHA256SUMS` 自身 SHA-256 `3ccfa2efa456e41f45d1912c1d0839bd41de8fe6eef9b959710c734801970ecf`。回执为 `release.json`、`latest.json`、`release-asset-verification.json`。
- 独立 tester 使用现成只读脚本核验，主 agent 检查输出与发布组合身份：DMG 完整性、DMG/App 严格签名通过，10 个 Mach-O 均符合 arm64 与 macOS 14 边界；全部 **154 个**内置 Python 文件与固定源码逐字节一致。APK ZIP 完整性、包名、版本和构建号通过，正式证书 SHA-256 仍为 `5062db967ce58d65df2c47895649df43464bab9a6eadf135f3a1a81ff4785ada`。本机仅检查已下载的包，没有编译、签名、安装或启动 App；只读镜像已卸载。回执为 `release-package-verification.json`、`package-verification-log.json`、`android-zip-verification.json`。
- 公开说明已移除 Git 标签签名 armor 并恢复 Markdown 标题，保留原版本说明、来源 marker 与工作流安装要求；读回确认 Release 身份、四项资产 ID/大小/digest、latest、标签对象和目标源码均不变。只改发布正文，未覆盖标签或资产；回执为 `release-body-cleanup.json`。
- 本轮发布不包含安装、重试原业务任务、真实模型调用或生产 DB 写入；原业务工作树与已安装 `0.4.5 / 116` 保留。
- 后续体验：安装正式包后核对环境预检和失败原因；如需继续原任务，显式发起新轮次。原业务 verify、真实模型、macOS/APK 业务构建及实体设备仍未重跑，本记录不将云端或最小 Flutter 工程检查等同于这些验收。临时修复工作树和已合并本地分支已在前述收尾中移除，本次发布未新增工作树；其他任务、主仓无关文件及独立归档保留。

## 2026-11-06 真实任务再次失败：只读彻查

- 用户要求“任务又执行失败了，需要你彻查原因”。本轮以只读核心命令和已落盘原生证据调查，不修改业务源码、冻结计划、生产任务状态或原始证据，不发起模型、构建、verify 或重试；仅补充本权威记录与独立诊断证据。主 agent 核对版本、逐条原生命令退出码、证据指纹与失败信息传递；两名 analyst（gpt-6.1-sol/high）分别只读核查平台构建环境/验收覆盖和 macOS 生成物/工作树准备链，无文件写入者冲突。
- 当前安装版实测 **0.4.6 / 118**；五个关键核心模块与主仓 `20696271f16bf399b2010f63bce8a64e6299206c` 一致。失败任务仍为 `csim4lhdf81m1k7wib615fdv`（MediaPlayer，“希望能够正常观看剧集”），本轮 run **`43875508-417f-4483-9df0-699ed0e8b031`**，会话 **`01a1aeef-4a14-71e1-91c6-3a8e8dd7ed24`**，实际执行时间台北 **11:37:59–11:47:11**。只读状态为 `needs_user / execution_failed`，没有重现旧监控一直 running 的问题；`last_progress` 仍是历史“本轮仍在运行”，不作为当前状态依据。
- 原始证据根目录为 `/Users/sakurasep/Library/Application Support/KaneoPilot/runs/csim4lhdf81m1k7wib615fdv/43875508-417f-4483-9df0-699ed0e8b031/`。`execution_contract.validate_spec(spec, job=job)` 只读校验通过；spec、原生事件和最终 JSON 的哈希逐一匹配 job。CLI 正常退出 **0** 并明确返回 `status: failed`，受管 worker 因该报告退出 **3**；不是 CLI 崩溃、服务商断线或没有生成最终报告。

| 最终冻结命令 | 原生退出码与结果 | 原生 JSONL 行 |
| --- | --- | --- |
| `./tool/check_layering.sh` | 0，layering ok | 137 |
| `~/devsdk/flutter/bin/flutter analyze --no-pub` | 0，No issues found | 139 |
| `~/devsdk/flutter/bin/flutter test --no-pub` | 0，363 tests passed | 141 |
| `LANG=en_US.UTF-8 ~/devsdk/flutter/bin/flutter build macos --debug --no-pub` | 1，SPM 本地生成包缺失，且 Xcode 宿主写路径被拒 | 143 |
| `~/devsdk/flutter/bin/flutter build apk --debug --no-pub` | 1，Gradle wrapper 锁文件写入 EPERM | 145 |

- **确定根因一：构建所需权限/目录没有进入冻结环境。** 当前 `codex_environment.py` 只定向本轮 XDG_CONFIG_HOME/TMPDIR/analytics，额外可写根只有 Flutter `bin/cache`。Android 仍使用宿主 `~/.gradle/wrapper/dists/gradle-9.3.1-all/9ot9r568e8zfvvd4mn8rbu1j0/gradle-9.3.1-all.zip.lck`，在 `ExclusiveFileAccessManager` 以写方式打开时即 `Operation not permitted`。只读确认该分发包、解包目录和锁文件均已存在；Flutter 随后的“downloading artifacts”是泛化提示，不能据此归因为网络下载失败。macOS 的 SPM/项目预取阶段仍向 `~/Library/Developer/Xcode/DerivedData/Runner-…/Logs/*` 写入并报权限 513；错误结果包又写宿主 `/var/folders/…/T` 并被拒。Python 临时文件可写不证明 Xcode 的各子阶段已实际采用冻结 TMPDIR，具体临时目录路由仍需隔离验证。
- **确定根因二：`--no-pub` 的平台生成物前置条件未满足。** 致命错误是 `macos/Flutter/ephemeral/Packages/FlutterGeneratedPluginSwiftPackage` 不存在，不是 `resource_bundle_accessor.swift` 缺失。该目录位于可写工作区，追加宿主权限不能生成它。Flutter 3.47.7 的 `runner/flutter_command.dart:2020–2025,2056–2059` 在 `--no-pub` 时跳过 pub 与平台工具再生成；正常链为 `refreshPluginsList → injectPlugins → DarwinDependencyManagement.setUp → generatePluginsSwiftPackage`。当前仅 Dart package_config/package_graph 存在，`.flutter-plugins-dependencies`、macOS ephemeral 与 Pods 均缺失。本轮没有执行 pub get/upgrade/clean；KaneoPilot 的 `task_workspace.py:217–225`、`git_delivery.py:338–361` 只创建/校验 Git 工作树，没有保证 Flutter 平台依赖准备完成。已有 package_config 的最初来源不能从本轮证据确定。
- **冷启动遗漏：** 原生行 66 的首次定向 Flutter test 被 `169.254.169.254` 网络策略拒绝（工具退出 -1）；关闭 analytics 没有阻止 Flutter 的 Azure bot 检测。SDK `base/bot_detector.dart:40–77,91–107` 在未设置 bot/CI 标志且本轮 tool_state 没缓存时探测该地址，版本检查也会调用它。执行端在行 91 临时使用 `CI=true` 后进入实际测试，并留下本轮 `tool_state.is-bot=true`；后续原文 analyze/test 才顺利启动。这是新轮次冷状态的确定覆盖缺口，不能把后续暖状态通过等同于首次启动可靠。中间还出现过一项业务测试失败，随后定向及两次全量 363 运行通过；它不是最终两端构建失败的直接原因。
- **诊断信息丢失：** `codex-final-1.json` 已包含两端具体原因，但 `codex_worker.py:292–294` 将 `status: failed` 统一转换成“Codex 报告执行失败”，只把通用字符串写入 job/terminal；`auto_monitor.py:215–217` 又拼接相同 terminal/detail，最终界面显示重复的“Codex 报告执行失败；Codex 报告执行失败”。这不改变真实失败结论，但隐藏了可操作原因，属于独立缺陷。
- **为何 v0.4.6 未发现：** 既有真实 Flutter 夹具只包含 lib/test，执行 --version、离线 pub get、analyze/test，没有 macOS/Android 平台工程；受管集成使用 CLI/Flutter stub。它们验证了沙箱基础边界、契约传递与失败收尾，未覆盖空配置直接首次启动，也未覆盖 Xcode/Gradle 真实完整构建。Packages #118 检查的是 KaneoPilot 自身，而 MediaPlayer 的冻结计划要求两端构建；发布门禁成功不能补足该验收缺口。前次记录虽列出未验证范围，实际修复对完整原任务的支持仍不充分。
- **已排除及保留未知：** 本次没有安装版本或证据轮次错配；MediaPlayer 的 macos/pubspec/lock/metadata 跟踪文件没有本轮差异，插件声明的隐私资源存在，当前日志还没有进入 Swift 编译。media_kit 的 SPM 不支持提示当前只是警告；CoreSimulator/XPC/FSEvents 异常未证明为最终致命原因。不能保证修复上述前置条件后就一定完成所有平台编译或真实播放验收。
- **后续修复范围（未实施）：** 保留旧缺省/v1 spec 行为，为新轮次版本化冻结按命令所需的工具能力；定向并核验 Gradle、Xcode 各阶段必要写目录及缓存，不开放整个 HOME/公网；独立处理 Flutter 冷 bot 检测和平台依赖准备；区分基础安全预检与完整命令就绪；持久保留失败报告摘要及原生命令原因，消除重复通用提示。验收应覆盖冷配置直接启动、含插件的 macOS/Android 真实受限构建、缺缓存/缺生成物的模型前准确失败、resume、旧 spec/Plus 兼容，以及现有只读目录和禁公网反例。Pilot 仍不代跑业务 verify，实际业务验证由执行端运行。
- 诊断归档为 `/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/.archives/20261106-kp-c02-build-failure-43875508/`，包含五项最终原生命令输出、冷启动/临时 CI 证据、来源文件指纹与 `diagnostic-evidence.json`，哈希清单为 `SHA256SUMS`。原业务 lib/test 指纹仍为报告中的 `b309ce0fc5eb9836d7fb33ae8f8fd49f88a8bf32025d84be4b455f539eaaf05c`；本次未修改或清理其成果，未创建临时工作树，未提交、推送或发布后续版本。

## 2026-11-06 平台构建环境修复与下一正式版（实现中）

- 用户已授权修复并发布新正式版本，随后明确选择正式 **v0.4.7**；当前 latest 为 v0.4.6。继续完成修复、验证、集成与签名标签正式发布；不安装 App、不重试原业务轮次、不调用付费模型。
- 任务沿用 KP-C02。基线 main/origin/main 为 `20696271f16bf399b2010f63bce8a64e6299206c`；工作树 `/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261106-fix-kp-c02-platform-build-environment`，分支 `fix/20261106-kp-c02-platform-build-environment`，合入目标 main。诊断文档变更已核对备份并转入此树；主仓无关 `.local/` 和缓存清理计划、其他任务工作树及 MediaPlayer 成果保留。
- T8 环境契约：新轮次 v2 按直接冻结 Flutter 命令选择能力，使用 `BOT=true` 避免冷启动 metadata 探测；历史缺省/v1 不变。native 构建先在同一受限沙箱离线执行 `flutter pub get --offline --enforce-lockfile` 准备生成物，不改变锁定依赖、不代跑 verify。Pub/Gradle 私有写目录和只读缓存来源冻结；仅复制必要 wrapper 分发，禁止复制宿主凭据、Gradle properties/init.d。macOS 明确冻结 DerivedData 与系统用户临时目录必要宿主写能力，不将共享系统临时目录称为私有 tmp；其他宿主路径保持只读与禁公网。
- T9 失败说明：只有完整核对身份、原生终态/hash、源码快照后的 failed report 才保留摘要及非零 checks；脱敏和限长、不生成成功 envelope、不自动重试。monitor 去重同时保持原失败门禁。
- T10 实际验收：云端固定 Flutter/Codex 版本、含原生插件的专用 macOS/Android 工程，使用实际命名沙箱与冷配置，覆盖离线准备后 analyze/test/两端原文构建、权限反例、缺缓存、首次和 resume；隔离检查覆盖旧 spec/Plus 与阶段契约。编译只在 GitHub-hosted 云端，非原业务任务。
- 文件归属：主 agent 独占环境/能力识别/规格/组合验证与发布；Sol worker `platform_cache_setup` 独占新缓存准备模块及对应测试；Sol worker `platform_cloud_fixture` 独占云端平台夹具脚本/独立 workflow/其静态测试；Sol worker `preserve_failure_details` 独占 worker/monitor 及对应测试，已回报定向 29+149 passed、15 subtests（待主 agent 组合核验）。只读 analyst 核实 Xcode 各调用及临时目录机制；完成实现后独立 reviewer 审实际差异。共享接口是 v2 tool_environment，所有 agent 同一任务树、无生产 DB/端口/业务进程。
- T11 发布收尾：通过完整修复和云端真实平台验收，合 main 并检查精确源码，再创建维护者确认版本的签名 annotated 标签；等待正式 Packages，独立核对公开四资产/源码/签名/latest，保存证据并安全移除本任务工作树与已合并本地分支。构建、发布、安装、原任务重试和用户验收分别记录。

- 本轮实现已完成候选：v2、锁定离线依赖种子与平台准备、必要宿主写根、真实子进程组清理、失败诊断保留与去重；全部仍为“实现中”，真实云端平台门禁未通过前不称完成。复合非原生 Flutter 命令维持基础能力兼容。
- 本次本地隔离验证：Python 3.10.10 的 environment/execution/monitor/cache-setup/dispatch 组合 **229 passed + 79 subtests**；setup 真实后代/超时/取消/输出/清理失败等 **15 passed**；脚本静态门禁及夹具定向 **32 tests OK**，完整脚本集此前 **226 tests OK**（后续新增 3 个夹具反例，仅由上述定向结果覆盖）；git diff --check 通过。这些为 Python/模拟执行链和真实 Python 子进程检查，没有本地 Flutter/Xcode/Gradle 编译。
- 原失败 spec 的只读重校验：`2b4c51faadf139238edd1c78f82e5367f648cb16821294be2ed65583bddb8530` 未变；当前实现对其 v1 工具环境及 stage contract 校验均通过，没有扩权、模型调用或业务 verify。
- 独立 reviewer 找到并已修正：复合命令派发兼容、settings FIFO 阻塞、Flutter 自定义 XDG 实际 settings 路径、准备后代进程生命周期。云端夹具另移除 BotDetector 全部 16 个 CI 信号，验证仅冻结 BOT=true，并以新 config/noBOT 真实 metadata 拒绝作为反例，防止 Actions 自带 CI 状态掩盖冷启动缺陷。

- 候选 `c061104` 的 Packages #119 / 42166020491 被后续分支 push 按既有并发规则取消，不是代码门禁失败。独立审查后补齐“准备进程组清理未确认”的持久错误路径与 v0.4.7 说明，提交 `6aa3457`；定向 environment/setup 共 45 passed + 55 subtests，workflow 25 tests OK。原生夹具改为与其他检查并行，publish 仍等待所有检查，未降低门禁。
- #120 / 42166798545 的 native job 127725446372 实际获 runner，12 秒内在 SDK 准备失败；原生日志明确请求 `commandlinetools-mac-15859902_latest.zip` 返回404，未进入 Flutter 安装或平台构建。这是新增夹具选错下载编号，非业务任务验证失败或 macOS runner 容量。按同一 runner 回执中的已安装 command-line tools 16.0 改为直接核对并使用该工具，SDK36/build-tools36/NDK28.2 仍固定。原始回执/日志在任务独立归档 cloud-120-jobs.json / cloud-120-native.log；新源码继续完整云端门禁。
