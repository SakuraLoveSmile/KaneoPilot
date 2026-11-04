# KP-C02：Codex 执行权限与失败收尾修复

状态：实现中（代码与隔离验证完成，正在云端构建修复包，现用安装版未更新）。用户于 2026-11-04 要求修复会话 `01a1a6cc-2c36-7bd1-a606-c65cf2ce2f33` 暴露的问题，随后要求“构建新版本”。

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
- 当前 `/Applications/KaneoPilot.app` 未更新，生产旧任务仍需修复版本的监控重新核对；原 MediaPlayer 改动与冻结记录保留。未运行原任务 verify，未编译 KaneoPilot、未安装、未调用真实付费模型、未发布。恢复执行应使用既有显式新轮次重试；旧轮次不会因新代码自动获得写权限。
- 后续交付：按云端 Packages 流程构建并安装修复版本，再核对原失败任务的实际状态收尾；如需继续原任务，显式启动新执行轮次。尚未合入，因此保留本任务工作树和证据；合入目标并完成适用验证后按项目约定清理。

## 2026-11-04 云端构建

- 用户要求构建新版本，提交、推送当前修复分支以触发 Packages。使用默认 preview 通道，产物为 Android Debug APK 与 macOS arm64 DMG，预览版本按工作流为 `0.0.0`，build 为 Actions run number。正式版本号、Release、合入和安装不在本次构建操作中。
- 构建前重新 fetch `origin/main`，基线仍为 `50aa7e26372a65976f434d1873546823bdae91c7`，与本地 HEAD 无分歧。代码/测试快照仍为 `1aac2a0f928967db625f9a32313dbbc48c34404f565aeb9a99bd5d26f3386e7e`，没有新增源码修改，因此沿用同一候选的隔离验证证据。
- 下一步：核对索引范围后提交并推送；等待该源码 SHA 的 Packages 全部必需 job 完成，下载并校验产物、内置核心与构建身份。构建结果在完成后追加。
