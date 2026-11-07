# KP-C04：App 重启后的执行中断与问题收尾

状态：待体验。首轮中断识别修复已发布 v0.5.1；升级后发现的后台日志恢复误判已修复并发布 v0.5.2。适用验证、公开包核验及两轮 worktree 收尾完成；真实业务执行仍需用户在 App 内确认。

## 目标与现场

- 截图任务 `csim4lhdf81m1k7wib615fdv`，执行 run `b6f88006-64dc-4a84-a981-91d6455e9509`。本次只读核查：旧 worker 在台北 17:00:22 因所属 App 实例停止结束；现 App 17:00:24 启动；17:05:47 提交原答案后问题被关闭为 expired / execution_failed。job 为 aborted，worker 已退出，第四轮答案未见消费证明；原问题、草稿及工作树仍保留。
- 基线 `aa436b582c7e7b206c97ab58a0a722120df2374e`，来源 `main`；任务分支 `fix/20261107-kp-c04-app-interruption-recovery`，工作树 `/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261107-fix-kp-c04-app-interruption-recovery`；合入目标 `main`。用户随后明确要求“发布新版本”，已授权提交、推送、必要合入及正式发布，并明确确认正式版本号 **v0.5.1**。
- 主工作区原有未跟踪 `.local/` 与 `docs/plans/kp-cache01-macos-build-cache-cleanup.md` 保留。真实业务 DB、执行目录和当前安装 App 不作为测试夹具，不直接更改；不自动重跑 MediaPlayer 或调用模型。

## 已确认范围与规则

1. 正常关闭 App 时仍停止本机 Codex 执行及受管子进程。用结构化终止原因区分正常 App 退出和身份错误、验证失败等异常；不按任意摘要关键词改类。
2. 已确认结束且进程退出的 App 中断显示 `needs_user / monitor / execution_interrupted`，派生为阻塞、标签“执行已中断”，保留已有成果与原始执行证据。问题使用已有 stopped 状态并注明中断原因；实际等待超时继续 expired，真实失败保留失败门禁。
3. 利用既有轮次观察/核对入口及时收尾旧问题及轮次索引，不新增启动扫描器或自动模型续发。答案只有同操作、同内容的持久消费证明才能显示已接收；闭合问题保留原答案并停止显示无法使用的重提入口。
4. 历史版本已经产生的同类 aborted / expired 记录只在严格核验原 run/spec/job、App 所有权与进程结束证据后的确定性核对中修正当前分类，保留审计及原始文件，不批量迁移、不伪造消费、成功或活动等待者。证据不足保留原分类。
5. 继续任务沿用现有 `resolve resume` 的 run 指纹、冻结计划、原工作树、远端需求和终态检查；界面说明新轮次会再次调用模型。中断不能触发自动补救或自动重发答案。
6. 界面以用户截图的任务概览为基线，只改异常标签、关闭问题的说明/操作和恢复入口，复用现有字号、间距、橙色阻塞样式及布局，不换风格。

共享业务与字段契约由主 agent 在 `docs/protocol.md` 的 Codex 执行/问题段落维护；UI 行为在 `docs/ui-spec.md` §4.17 维护。本记录仅追踪实施、验收和证据。

## 实施与分工

- T1：只读 analyst（Sol / high）核查 worker → 观察 → 问题 → monitor → resume 链路，明确最小规则位置及兼容门禁；不写源码/数据、不派生。
- T2：核心 worker（Sol / high）独占 `core/kanpilot/{process_owner,codex_worker,codex_execution,execution_questions,cmd_dispatch,cmd_session,round_store,auto_monitor,cmd_queue,derive,cmd_remote}.py` 及对应 `core/tests/test_{process_owner,codex_execution,codex_inputs,execution_questions,auto_monitor,session}.py`。在既有 `recheck` 末尾核对当前 Codex 的历史失败/中断分类，材料或远端检查失败不降级；新增审计事件 `execution_interruption_classified`。主 agent 维护规范，不与 worker 同写。
- T3：主 agent 并行修改 macOS 状态映射、答案表单和既有 Swift 夹具，必要时同步 Android 对新原因码的显示；不改变业务 API 权限。继承已有 UI，不新增全局设计。
- T4：主 agent 检查最终组合差异，运行隔离 Python 用户流程与静态核对；独立 reviewer（Sol / high）只读审查停止/消费证明、历史兼容与重试守卫。测试仅使用临时 DB、Git 仓库、假服务及本地协议替身；禁止真实模型、生产访问与本地平台编译。

## 验收场景

- 等待答案期间关闭 App：受管进程退出，已有成果/问题/答案保留；重新观察即显示“执行已中断”，无须用户先提交答案；轮次不再显示运行中。
- 答案消费前中断：不标已回答、不重发；若消费已持久确认则保留已回答事实。正在运行/缺失停止证明/身份冲突不能被伪造为已中断。
- 旧版已 expired / execution_failed 的同类记录可通过核对修正；再次核对幂等，原 job/spec/事件文件不改。正常超时、用户主动停止和真正失败仍符合原规则。
- 新 App 登记保留仍被当前活跃 task/run 引用的旧 stopped 身份，升级后仍能严格核对旧问题；原 run 替换后允许按原规则剪枝。保留身份不计作活动实例或存活进程。
- 中断恢复入口可用，并绑定当前 run；沿用计划与原工作树、保留已有改动，以新轮次执行；没有用户确认时零模型调用、零重发。
- macOS 原异常红色失败卡变为橙色中断提示；闭合问题显示原因、原答案和复制入口，不再显示灰色“使用相同答案重新提交”。开放且送达未知的问题仍可按原后端规则核对。

## 本次实现与验证（2026-11-07）

- T1–T3 源码已完成：结构化关闭原因、既有观察入口主动收尾、历史记录严格更正与所有权证据保全、已结束轮次索引同步、当前 run 的用户显式重试、两端中断标签和 macOS 闭合答案说明。11 个核心模块、3 个核心回归文件及 macOS/Android 对应实现/夹具已签名提交为 `1a6e44bfbef2b801cd470df30f98cd5cf83c9e62`，推送触发 Packages #154。
- T4 独立审查检查实际组合差异，发现并修正旧 expired 已有完整消费证明却不能在已打开卡片纠正为 answered 的 P2；补核心 `answered / termination_reason=app_closed`、原状态审计/时间保留，以及 UI 对带标记终态的纠正、同状态迟到回复不得抹去标记。最终复核无剩余实质发现。UI 审查未编译、未运行原生界面，不冒充界面验收。
- 验证解释器：`/Library/Frameworks/Python.framework/Versions/3.11/bin/python3`，Python 3.11 / 真实 pytest 9.1.1（site-packages）。均为临时 DB/Git/假 Kaneo 与本地 CLI 协议替身，真实业务模型调用和生产访问为 0。默认 `python3` 的 Python 3.14 没有 pytest，未用仓库 shim 回落执行。
- 独立核心 worker 在 P2 修正前候选运行：`<解释器> -I -m pytest core/tests/test_codex_inputs.py core/tests/test_codex_execution.py core/tests/test_auto_monitor.py -q` → **203 passed，32 subtests passed**，17.00 秒；`<解释器> -I -m pytest core/tests/test_process_owner.py core/tests/test_execution_questions.py core/tests/test_session.py -q` → **150 passed，11 subtests passed**，103.97 秒。两次完整命令以当前工作树为 cwd，仅有工具回执，没有日志/JUnit文件。
- 主 agent 组合态运行：`PYTHONDONTWRITEBYTECODE=1 <解释器> -I -m pytest tests/test_codex_dispatch.py -q --junitxml=build/evidence/kp-c04/codex-dispatch.xml` → **7 passed**，40.92 秒。实际托管 worker 与假 Codex CLI 验证派发、问答续接、失败及正常完成，不连接真实模型；这轮在审查 P2 的局部修正前，普通派发路径未受该修正影响。
- P2 最终修正后只重跑受影响输入/恢复模块：`<解释器> -I -m pytest core/tests/test_codex_inputs.py -q --junitxml=build/evidence/kp-c04/codex-inputs-review-fix.xml` → **20 passed，14 subtests passed**，9.56 秒，exit 0；日志为同目录 `codex-inputs-review-fix.log`。主 agent 已读取日志和两份 JUnit，均 0 failures / 0 errors；覆盖新 App 登记→旧问题更正→显式恢复保留改动、已消费/未消费/矛盾消费、并发完成/归档/其他阻塞、幂等及原文件字节保全。
- 新增 Swift/Kotlin 回归使用现有夹具，未降低或跳过原断言；本地未运行 Swift、Xcode 或 Gradle。最后 `git diff --check` 和 14 个变更 Python 文件 AST 解析通过；基线及 28 个变更 tracked 文件的 SHA256 记录于 `build/evidence/kp-c04/source-snapshot.json`，不把旧广域通过数量说成最终候选全量重跑。
- Packages #154 的 macOS 打包、两组桌面回归及 Android 单测/lint/打包通过；Python shard 4 在两解释器均由 `test_event_kind_labels` 发现本次新增事件的核心中文表漏登记。补齐 `derive._EVENT_KIND_CN`，不改门禁或测试预期；本地真实 pytest 重跑现有 5 项标签检查通过，JUnit 为 `build/evidence/kp-c04/event-kind-labels.xml`。最终候选需重新通过云端门禁，#154 的部分成功不作为整体通过。
- 原生核验：下载 #154 的云端独立身份 App，GitHub artifact digest、ZIP 大小/哈希、来源 SHA/build、严格深度签名及 11 个包内核心模块与提交逐字节核验通过。使用 FakeCore、空凭据、独立 HOME/目录、关闭引擎，在 1180×760 dark 下实测橙色中断卡、未接收/已接收两种闭合问题、展开原答案、复制入口、无无效重发按钮、沿用计划恢复的模型调用说明及取消。未验证剪贴板实际内容；未确认恢复、未调用模型，业务操作命令为 0。证据在主仓 `build/kp-c04/{cloud-154,native}/`；两预览已按独占 PID 退出。最终追加仅为核心事件名称表，UI 源码未变，不把 FakeCore 结果当真实业务执行验收。
- 最终候选 `ab53d28f12bada2abd2113652526e07b8b7ed03c` 的 [Packages #155](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/42362495755) **16 个必需 job 全部成功**，可选全文探针和预览的 publish 按条件跳过。检查覆盖 Python 3.11/3.13 核心与集成、两组 Swift 桌面夹具、Android 单测/lint/打包、arm64 DMG/内置运行时及无模型原生平台门禁；实际 CI 结论和报告保存在主仓 `build/kp-c04/cloud-155/`。最终独立 UI ZIP 的来源/build、GitHub digest 和包内 11 个核心模块逐字节核验通过，UI 源码与原生已验候选一致，见 `native/source-binding.json`。
- 最终候选已 fast-forward 合入并推送远端 `main`；签名 annotated `v0.5.1` 直接指向该提交，GPG 验证通过，已推送触发 [正式 Packages #157](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/42363677386)，build 157。同源码自动触发的 main 预览 #156 已取消以避免重复占用构建资源；正式 #157 独立运行全部发布门禁，未降低检查。
- worktree 收尾已完成：来源提交已在远端 main，工作树干净、全部 agent 完成、无活动进程/打开文件引用。144 个忽略文件（包括本地 JUnit、日志及源码指纹）已归档到主仓 `build/kp-c04/worktree-ignored-snapshot.tar.gz`，逐项哈希核验通过；正常移除本任务 worktree 及已合并本地分支，原目录和本地引用均不存在。远端来源分支、其他工作树、主仓原有两项未跟踪文件保留；证据为 `worktree-ignored-snapshot.json`、`worktree-cleanup-preflight.json` 和 `worktree-cleanup.json`。
- 正式发布完成：[Packages #157](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/42363677386) 的 16 个必需检查和 publish 全部成功，可选全文探针按条件跳过。公开 [v0.5.1 Release](https://github.com/SakuraLoveSmile/KaneoPilot/releases/tag/v0.5.1)（ID `437506281`）为非 draft、非 prerelease 的 latest，发布于 `2026-11-07T10:37:53Z`。产品源码 `ab53d28f12bada2abd2113652526e07b8b7ed03c`，build 157；签名 annotated 标签对象 `4fc251c02994eef07d940f9dd1c6be560aa00bbf` 直接指向该提交，GPG 验证通过。完整 CI 结论、检查报告及无模型原生平台证据在主仓 `build/kp-c04/cloud-157/`。
- 公开四项资产已完整下载并通过 GitHub digest、SHA256SUMS、manifest、来源/版本/build/latest 一致性核验。DMG 42,967,797 bytes，SHA256 `64ebe6c47e3c0cdb46443ec2cec944577d52ed5cee7c8b6339cf8d2e43501604`；APK 8,557,680 bytes，SHA256 `1d3945f658f94167d86cdd54d47f19f73266fc8d793e2ffbd0901b15a86b8244`。原包、清单及 `verification.json` 保存在主仓 `build/kp-c04/release-0.5.1/`。
- 安装包只读核验：DMG 完整性、DMG/内部 App 严格签名、`local.kaneopilot`、0.5.1/build157、arm64 正确；包内 11 个受影响核心模块与发布提交逐字节一致，只读挂载已卸载。Android 包名 `com.sakurasep.kaneopilot`、0.5.1/versionCode157、APK 签名通过，证书 `5062db967ce58d65df2c47895649df43464bab9a6eadf135f3a1a81ff4785ada` 延续既有正式签名。结果和命令日志为同目录 `package-verification.json` 及对应日志；未安装覆盖正式 App、未做 Android 实机或真实业务模型验收。
- 公开发布说明仅移除签名标签注释带入的 PGP 签名块，保留功能说明、安装要求和 `kaneopilot-ci:<sha>:157` 标记；读回确认 Release 身份和全部资产未变，前后正文证据保存在公开包目录。未移动标签或覆盖资产。

## 剩余工作与下一步

- 最新正式版为 [v0.5.2](https://github.com/SakuraLoveSmile/KaneoPilot/releases/tag/v0.5.2)，适用检查、发布核验和收尾完成，状态“待体验”；用户明确确认体验后才改为“已验收”。本次没有数据库 schema 变更。
- 体验入口：macOS“设置 → 高级 → 软件更新”，Android“连接 → 软件更新”，或下载公开 v0.5.2。升级后在原任务的“执行已中断”提示中选择“重新执行（沿用计划）”，确认后才创建新轮次并调用模型，保留原计划、原工作树和已有改动。
- 本次未重跑真实 MediaPlayer 任务、未直接改业务 DB、未安装覆盖正式 App、未停止 Gradle daemon 或 MediaPlayer。`2026-11-07T11:47:20Z` 只读投影确认原 run 仍为 `needs_user / monitor / execution_interrupted`，已安装版本 0.5.1/build157；该投影不代表用户之后的操作结果，证据为主仓 `build/kp-c04/post-release/current-projection.json`。
- 两轮任务 worktree 与已合并本地分支均已清理，可恢复证据归档在主仓 `build/kp-c04/`；主工作区、其他任务、共享根目录及远端来源分支保留。

## 升级后的恢复阻塞（2026-11-07）

- 现场只读核验：安装版本为 0.5.1/build157，原任务已正确归类 `needs_user / monitor / execution_interrupted`，旧 round 为 failed；没有重跑原 run。计划实际 SHA 与登记相同，HEAD 与 20 个业务候选文件在两次取样间未变。唯一变化是原 run 私有 Gradle 日志 `.pi/kanpilot/codex/b6f88006-64dc-4a84-a981-91d6455e9509/gradle/daemon/9.3.1/daemon-3023.out.log`，后台 PID 3023 仍追加内容；10 秒内大小 1,026,948→1,028,014，内容 SHA 不同。证据为主仓 `build/kp-c04/post-release/live-snapshot.json`。不停止该进程、不改真实 DB/执行材料、不自动调用模型。
- 原因：恢复准备和事务复核都把全部未忽略文件作为候选；本任务 41,325 个候选中 41,305 个属于 `.pi`，运行日志变化被当作本次确认后的成果变化。现有通用算法还服务 replan/review 等流程，本轮不全局改变其语义。
- 后续基线 `1acbe9ad0b54f9a8a00ea658ede0001e681ccf54`，分支 `fix/20261107-kp-c04-retry-runtime-state`，工作树 `/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261107-fix-kp-c04-retry-runtime-state`，合入目标 `main`。唯一业务契约更新在 protocol §5.27.12，使用显式版本的新重试指纹及窄范围运行文件排除；旧 queued 保持旧算法。
- T5：主 agent 维护协议/本记录、独立复核实际用户路径及云端发布；既有 Sol analyst 只读核对排除边界和全部消费者；Sol worker 独占 `git_delivery.py`、`cmd_queue.py`、`cmd_dispatch.py`、`dispatch_validate.py` 及对应核心测试，贯通新旧算法选择和回归；完成后 Sol reviewer 只读独立审查。共享分支/目录，不另开 agent 工作树；只用临时 DB/Git/假服务，原任务和生产模型不作为夹具。
- 后续验收：日志内容变化、合格运行文件增删可跨准备/事务/领取/派发；源码、计划、分支/HEAD、mode 与索引 blob 变化仍拒绝；tracked/staged 内部路径、非 UUID、其他 `.pi` 和 symlink 不豁免，no-follow 核验中祖先替换拒绝且不读外部；旧 queued/旧 direct 修复恢复保持 v1，未知版本拒绝；现有显式用户确认、新 UUID、成果保全及零自动重发断言继续通过。
- 正式发布授权沿用；用户在具体修复与本地验证可审查后，进一步明确确认新正式版本号 **v0.5.2**，不复用 v0.5.1。必要本地检查后推送云端，不在本地编译平台代码；完成后安全收尾后续工作树。

- 首次后续回归发现 3 个新用例构造/预期问题：Git 不枚举未跟踪 FIFO，故将该新增特殊文件用例改用真实 tracked 候选 FIFO；协议明确指纹沿 Git 枚举边界，不新增全目录特殊节点扫描。另两项 legacy direct 修复恢复用例漏计原流程会回填 `review_repair_key`，保留原行为并精确校正新断言。没有删/跳过用例或降低已有门禁；暂存候选特殊类型仍须拒绝。独立审查另发现特殊 verification_recovery 提前返回路径漏验新版本号，已在共用终态核验入口补齐，并保留其独立源码/索引/合并快照守卫；最终重跑结果见下文。

- 后续最终源码由 4 个核心模块及 4 个核心测试文件组成；新指纹及版本传播、旧记录兼容、索引 blob 证据和 no-follow 目录证明已完成。目录 fd 缓存最多 32 个，取样后复验已观察目录身份；通用 v1 默认语义未变。独立 Sol reviewer 复核源码后无剩余实质问题；特殊恢复测试的缺失 MCP 夹具已按原成功路径补齐，只调整测试环境，不改生产门禁。
- 最终本地验证使用 `/Library/Frameworks/Python.framework/Versions/3.11/bin/python3 -I -m pytest`：`core/tests/test_git_delivery.py -q` → **149 passed**，93.94 秒；`core/tests/test_execution_retry.py core/tests/test_codex_preflight_retry.py core/tests/test_review_repair_retry.py core/tests/test_task_ai_action_verification_recovery.py -q` → **92 passed、24 subtests passed**，114.41 秒。完整日志/JUnit 为后续 worktree `build/evidence/kp-c04/retry-fingerprint-final.*` 与 `retry-lifecycle-verified.*`；JUnit 将子用例计入 116 项，均 0 failures/errors/skips。首轮及中间失败记录保留，未跳过、删除或放宽原检查。
- 主 agent 在同一最终组合源码运行 `tests/test_codex_dispatch.py -q` → **7 passed**，41.98 秒，真实受管 worker + 假 Codex CLI/临时 DB/Git/本地假 Kaneo，日志/JUnit 为同目录 `retry-codex-dispatch.*`。真实模型和生产 DB 写入为 0，平台代码未本地编译。
- 主 agent 在真实原工作树只读连续计算最终 v2 指纹：日志大小 **1,136,746→1,137,812 bytes** 且 SHA 改变，新指纹两次均 `5e9ca6897bb4fc51796fadf0119f27555fcd6fa1c168f9207bcf837fd22ffd41`，计划 SHA 一致，每次约 5.6–5.7 秒。证据为主仓 `build/kp-c04/post-release/retry-state-verification.json`，其中 4 个模块 SHA 已与最终源码逐项比对。没有 resolve、任务启动、真实 DB 写入或模型调用。
- `git diff --check`、受影响 Python AST 与最终源码快照核验通过；后续候选 `d0706e4586f673d6541baf6891f937f43c798db2` 已通过 [Packages #158](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/42367731380) 全部 16 项必需门禁；用户已明确确认 v0.5.2，不复用或覆盖 v0.5.1。

- 候选 #158 的源码身份与全部 job 结论已独立核验，14 组平台/隔离报告目录、无模型原生平台证据已下载到主仓 `build/kp-c04/cloud-158/`；原生 `result.json` 为 passed、source SHA 相同、model_calls=0、production_access=false，107 项 SHA256SUMS 逐项一致。可选全文探针与 preview publish 按条件 skipped，不能说成执行通过。
- 修复已 fast-forward 合入并推送远端 main；签名 annotated `v0.5.2` 直接指向 `d0706e4586f673d6541baf6891f937f43c798db2`，标签对象 `2dd962324be266ed9a057d0a9c34ac7097f29d10`，GPG 验证通过。推送触发[正式 Packages #160](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/42368821705)，build 160；同源码 main 预览 #159 已确认 cancelled，避免重复占用构建资源，正式 run 独立执行全部门禁。
- 后续 worktree 与已合并本地分支已安全清理。153 个忽略文件归档至主仓 `build/kp-c04/retry-worktree-ignored-snapshot.tar.gz`（2,064,204 bytes），逐项哈希核验，归档 SHA256 `08e5afc241795f45dc0fdb14bcb09ac41d6d7956a3ab565acb5eaca1a2fc7733`。删除前确认源码已在远端 main、tracked 干净、全部 ignored 字节仍与归档相同且无打开文件/活动进程引用；正常移除 worktree 和本地分支后复核目录/引用不存在，远端来源分支及主仓两项原有未跟踪文件保留。证据为 `retry-worktree-ignored-snapshot.json`、`retry-worktree-cleanup-preflight.json`、`retry-worktree-cleanup.json`。

- **v0.5.2 正式发布与核验完成**：[Packages #160](https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/42368821705) 全部 16 项必需门禁及 publish 成功，可选全文探针按条件 skipped。公开 [v0.5.2 Release](https://github.com/SakuraLoveSmile/KaneoPilot/releases/tag/v0.5.2)（ID `437541193`）为非 draft、非 prerelease 的 latest，发布于 `2026-11-07T11:47:40Z`（台北 19:47:40）；源码 `d0706e4586f673d6541baf6891f937f43c798db2`、build 160。远端 signed annotated 标签对象/直接源码均与本地验证一致，未移动标签或替换资产。
- 公开四资产完整下载，GitHub digest、SHA256SUMS、manifest、版本/build/source/latest 一致性核验通过。DMG **40,928,463 bytes**，SHA256 `d2b995c7978a73dbcec6191cf5c2bdf1057c32eec15d36ed0294b01bb5b57698`；APK **8,557,684 bytes**，SHA256 `77b72bdb4d232d6a0d124c845d1a2838bff9de20b2d29074d11436f7378e27b0`。清单及完整结果保存在主仓 `build/kp-c04/release-0.5.2/verification.json` 与同目录四资产。
- DMG 完整性、DMG/内部 App 严格签名、bundle ID `local.kaneopilot`、0.5.2/build160、arm64 均通过；包内本轮及首轮共 13 个相关核心模块与发布提交逐字节一致，只读挂载已卸载。Android 包名 `com.sakurasep.kaneopilot`、0.5.2/versionCode160、APK 签名及沿用的固定证书 SHA256 `5062db967ce58d65df2c47895649df43464bab9a6eadf135f3a1a81ff4785ada` 核验通过。Mac 继续 ad-hoc、未公证；没有本地平台编译、安装覆盖或 Android 实机/真实模型验收。证据为同目录 `package-verification.json` 和命令日志。
- 正式 run 的全部 job 与 15 组报告目录已下载到主仓 `build/kp-c04/cloud-160/`；原生平台报告 passed、source 一致、model_calls=0、production_access=false，107 项 SHA256SUMS 一致。公开正文只剥离签名标签注释中的 PGP 签名块，保留安装要求与 `kaneopilot-ci:<sha>:160` 来源标记，读回确认 Release 身份和四资产均未变；前后正文及 `body-cleanup-verification.json` 保存在公开包目录。
