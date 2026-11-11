# KP-BR01 软件创建任务分支使用标准命名

- 状态：待体验
- 用户目标：后续软件创建的分支遵循标准命名，解释截图仍显示 kaneo/<task_id> 的原因。
- 原因：git_delivery、task_workspace 等入口硬编码旧前缀，KP-WT01 明确保留该规则；不是界面缓存。
- 范围：手动规划、自动接收、评审恢复、构建修复的分支创建及身份核验。已有任务不自动改名；不动运行库、远端分支和已有 PR。
- 决定与契约：唯一规则见 docs/protocol.md「软件创建任务分支的命名（KP-BR01）」。日期首次准备时冻结；已有 intent 保留原名称。
- 分支：fix/20261112-kp-br01-standard-task-branches；工作区：主仓库（本次未创建临时 worktree）；目标 main。
- 基线：ea02654；开始时已有未跟踪 .local/ 与 docs/plans/kp-cache01-macos-build-cache-cleanup.md，保留。
- 并行：branch_audit 只读核查调用链；branch_tests 独占新增命名测试和历史 Git 夹具；branch_review 只读独立审查；主 agent 负责源码、规格、集成验证。
- 验收：新任务创建标准名且主工作区不变；旧分支继续使用；准备中断恢复/标题变更/跨日重试保持名称；错误任务/仓库拒绝；执行合同冻结实际登记分支，旧合同保持字节。
- 本次验证（基线 ea02654 + 本分支当前未提交改动）：
  - 相关 8 文件回归：276 passed、23 subtests passed，1 项构建修复只读预览失败；原因是新增 reserve 提前写 setting，已修复。
  - 修复后完整重跑 core/tests/test_build_failure_ai_action.py：22 passed（只读预览、确认、恢复和交付）。
  - 最终 core/tests/test_branch_naming.py：23 passed（台北日期、类型/slug、真实 Git、新旧身份、跨日与标题变更、错仓库、升级收养及脏目录拒绝）。
  - tests/test_auto_pipeline.py + tests/test_pi_round_contract.py：14 passed、4 subtests passed，覆盖新分支进入完整隔离执行/交付路径。
  - 其余已通过文件：test_task_workspace.py、test_git_delivery.py、test_execution_stage_contract.py、test_worktree_layout_migration.py、test_auto_intake.py、test_review_resolution.py。
  - compileall 与 git diff --check 通过。仅临时 Git、隔离 SQLite、假 Kaneo/GitHub/执行器，不调用真实模型、生产写入或编译。
- 测试调整依据：旧 Git 交付夹具显式传旧名，保留历史兼容与拒绝覆盖的检查；新规划/碰撞用例改查标准名；假 GitHub 返回真实任务分支。未删除、跳过或降低门禁。
- 独立审查：发现并修复旧自动接收创建后未登记的升级收养缺口；最终复核未发现新增实质问题。
- 收尾：未创建临时 worktree；原有其他任务 worktree、未跟踪文件和用户数据均保留。下一步按发布授权走云端 Packages 并安装后体验；不将源码验证当作已安装生效。
- 云端构建授权：2026-11-12 用户要求排队构建云端新版本；提交并推送本任务分支，触发 Packages 默认预览构建，不创建正式或测试 Release。
- 云端队列：Packages #216，push 事件，源码 d66669a8edb76903d26964d6544083461244e3ed；2026-11-12 已确认 queued。运行：https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/43062631673 。使用分支 push 自动触发，未重复手动派发。
- #216 云端结果：失败。macos-package、Android、两组 macOS 夹具与 native-platforms 成功；Python 3.11/3.13 的四个用例失败：产物过滤推送断言、交付演练拒绝推送断言仍硬编码旧分支；两个评审修复自动接收用例使用“未登记的标准分支”却在模拟旧版恢复。
- 本次修正：前两处按实际登记分支断言完整目标引用（保留 no-force、裸仓库拒绝与恢复验证）；历史恢复夹具显式创建旧名并断言传入旧名，不放宽生产收养门禁。保留独立新标准分支验证。
- 本次本地复核：core/tests/test_delivery_artifact_filter.py、core/tests/test_review_fix_workspace.py、core/tests/test_branch_naming.py、tests/rehearsal/test_rehearsal_delivery.py 合计 92 passed、6 skipped（既有历史演练跳过项未修改），66.06s；git diff --check 通过。重新提交云端完整门禁及两端打包。
- 重新构建：Packages #217，源码 9bbf4737a2e7690e2968b4a7f3db869946cdbf60；运行 https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/43066910925 。已完成，整体 success。身份、Python 3.11/3.13 共10个核心/集成分片、Android、macOS DMG、两组桌面夹具、native-platforms 全部 success；非默认 session-fulltext-probe 与预览通道 publish 按工作流条件 skipped。
- 本次独立只读复核确认：测试仍覆盖完整分支引用、no-force、拒绝后的状态与恢复、固定 reviewed SHA 和交付 base；没有放宽生产代码。
- 交付限制：尚未合并或安装；当前安装版不会因源码修改即时改变。

- #217 产物：macos-arm64-local-signed-217（DMG）、android-debug-217（APK）已上传且未过期；来源/全部 job 与产物元数据保存在 build/kp-br01/evidence/packages-217.json、packages-217-artifacts.json。
- 收尾：本次仅修正测试夹具与断言，生产代码和原门禁强度未改；未创建临时工作树；未安装、未创建 Release。

## 测试版发布（2026-11-12）

- 用户明确要求发布新的测试版。通道 test，基础版 0.5.7；分支 fix/20261112-kp-br01-standard-task-branches，提交 343805c64e26ac44286d25af82fa8d1d2734b99f，与 #217 源码仅有任务记录差异。
- Packages #220：https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/43069867531 ，目标 v0.5.7-test.220。已完成，全部必需 job 与 publish 成功；工作流创建注释标签和测试 Release，未手动创建测试标签或复用预览包。
- 公开说明：本次测试重点为标准任务分支命名，覆盖手动规划/自动接收/评审修复/构建修复，保留旧任务兼容和重试已登记名称；已有分支不会自动改名。
- 本次仅发布当前任务分支，不自动合并其他并行任务；不安装或操作运行数据。

- 发布结果：v0.5.7-test.220 已于台北时间 2026-11-12 01:56:34 公开为非 draft 预发布；发布页 https://github.com/SakuraLoveSmile/KaneoPilot/releases/tag/v0.5.7-test.220 。
- 发布后只读核验：注释标签直接指向 343805c64e26ac44286d25af82fa8d1d2734b99f；manifest 版本/基础版/channel/build/source 与两端条目一致；四项公开资产齐全，安装包大小、GitHub 资产 digest、manifest 和 SHA256SUMS 一致，下载的两份清单自身哈希也匹配。未下载或安装两端完整包。
- 证据：主仓库 build/kp-br01/evidence/packages-220.json、release-220.json、release-220-tag.json、release-0.5.7-test.220/{manifest.json,SHA256SUMS}。
- 状态仍为待体验：用户可开启“接收测试版”后检查更新并用新建任务体验；发布成功不代表实机安装或用户验收。
- 发布收尾隔离：等待期间主仓库被切回 main，且本地任务分支新增 72c56a0（包含其他文件）；保留该提交和主仓库现场。从已发布源码 343805c 建立临时 detached 记录树，仅提交本记录并推送本任务远端分支，不推送其他文件；临时路径为 /Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261112-docs-kp-br01-release-record，推送核对后清理。后续工作须先核对本地/远端任务分支差异。
