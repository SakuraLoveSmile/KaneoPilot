# KP-CONT02 同一 Goal 继续执行的计时与收尾修复

- 状态：待体验；用户要求修复误报超时并更新测试版，已完成本地/现场验证与云端测试预发布。
- 分支：`fix/20261112-kp-cont02-continuation-timeout`
- worktree：`/Users/sakurasep/Documents/Code/Project/Project Worktree/personal-kaneopilot/20261112-fix-kp-cont02-continuation-timeout`
- 基线：`2352d432aa68cebec162bd3e15620cc66c5d3529`（已安装及最新测试版 `0.5.7-test.224`）；从 main `ea02654` 快进至该版本，保留已发布的目标受阻处理能力。
- 交付目标：本任务分支云端 Packages 测试预发布，沿用基础版本 `0.5.7`，不发布正式版；未授权合并 main。

## 证据与范围

2026-11-12（台北）核对任务 `wjhpqxfqxr3084vxtc19e1uo`、执行 run `446ab281-def2-4b21-9157-2140e1bfd855`、Pi session `7cb513d3-8324-4931-bcf6-ca645d0556b2`：原始开始时间 00:18，13:10 已确认继续到新 turn；13:11 monitor 却按原始开始时间判四小时超时。worker 因新 turn 与旧 Goal 审批检查点不符，13:20 停止接续。直接只读查询 Pi 显示新 turn 于 13:20:09 `completed`，报告 `ready / structured / met`，三个冻结检查最终 exit 0。宿主报告不是 Pilot 已通过独立核验、评审或交付的证明。

只修复同目标继续的计时与已授权轮次观察，不重发模型请求、不降低报告/源码/评审门禁、不直接改生产 SQLite 或手工伪造 job 终态；现场恢复仅经既有核心命令，无 schema 变更。

## 实施与分工

- T1：worker 独占 `core/kanpilot/` 与 `core/tests/` 的相关实现及回归测试：统一当前执行段计时；精确核验已登记继续操作与 turn 绑定后接续观察；保留审批门禁、未知发送不重试及幂等规则。
- T2：主 agent 独占本记录与 `docs/protocol.md`，核对实际变更，运行组合态隔离验证；独立 reviewer 只读审查实际差异。
- T3：主 agent 提交并推送本任务分支，以 `channel=test` 云端 Packages 构建；等待所有必需 job 与发布完成，核对 manifest/安装包身份，提供 App 内测试通道入口。
- 数据与运行资源：测试只使用临时 DB/目录与假控制面，不访问生产 DB 或付费执行端；编译、签名与打包只在 GitHub 托管 Actions。

## 验收

1. 原轮开始超过四小时但刚继续的轮次，不再立即超时；当前继续段真正超过预算仍失败；无继续标记保持原计时。
2. 有可信同 task/run/session 继续操作的新 turn 可越过旧审批检查点进入只读观察并正常记录终态、报告；身份漂移或未授权 turn 仍拒绝，不代批新 Goal。
3. 重复继续操作不重复发 prompt；再次继续保持有效计时和观察绑定，不让截短历史导致合法轮次丢失。
4. 最终源码组合态的适用隔离测试通过；云端检查与两个平台构建成功；测试预发布包含匹配源码和构建号的四项资产。

## 验证与后续

- 实现：会话/monitor 与 worker 都从当前继续段起算，且仅扣除当前段等待；`started_at` 与原 `goal_approval.execution_turn_id` 保留审计。新增 `goal_approval.observation_turn_id` 保存通过核心回执核验的观察检查点。
- 独立审查发现并修复：Goal 回执早于本地接管落盘时的恢复短路、覆盖原审批身份导致再次受阻无法处理、旧等待抵扣新预算，以及新报告已换 turn 后的恢复匹配。未降低原报告/源码/评审门禁。
- 本地使用已有独立解释器 `/tmp/kp-gb01-pytest/bin/python`（Python 3.14 / 真 pytest 9.2.0），不采用仓库内 pytest shim；CI 仍固定 Python 3.11/3.13 与 pytest 8.4.2。
- 初轮组合：132 passed / 48 subtests，1 个新增回归失败；原因是新夹具未填写 task.run_id，与生产已登记身份不同，严格校验正确拒绝。补齐夹具身份，未放宽源码验证。针对性 31 项随后通过；最终组合检查进行中。
- 现场只读核验：新回执校验函数对故障任务返回 True，生产 job 仍 running；此步未执行恢复、未写业务状态。
- 2026-11-12 14:11 台北：经本任务组合源码的既有核心 `round-observe {executor:user}` 正常接管故障 run，返回 `settled / reopened:true`。现场 job 已 `finished / exit_code:0`，原审批身份保留，新观察检查点为继续 turn，`resume_detail` 清空；没有发送模型提示、没有代跑业务 verify、没有直接改 DB/job。
- 常驻调度器随后正常 monitor，任务从误报 `execution_failed / 超时` 改为 `verification_unconfirmed / 执行完成后没有可计入的改动（.pi/** 已排除），判定空转`。这是独立的既有有效改动门禁，未放宽；本次不宣称业务任务已验证/评审通过。`completion_policy=command_verification` 下 worker 不缓存 Goal 报告，由确定性 monitor 按既有规则读取核验。
- 最终组合检查：`cd core && /tmp/kp-gb01-pytest/bin/python -I -m pytest tests/test_continuation_observation.py tests/test_request_continuation.py tests/test_process_owner.py tests/test_goal_blocker.py tests/test_execution_status.py tests/test_execution_continuation.py tests/test_pi_desktop_backend.py tests/test_goal_continuation.py tests/test_session_status.py tests/test_execution_input_worker.py tests/test_execution_questions.py -q`，`267 passed, 82 subtests passed in 299.68s`，退出 0；独立 reviewer 最终无阻塞问题；`git diff --check` 通过。
- 发布源码：`4e80cb527d1167d8e35b69666260cc2218eff75e`；本地组合验证及真实任务接管均使用该提交包含的相同业务源码。
- 云端 Packages：<https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/43166991045>，run number 226，全部必需 job `success`；显式未开启的 `macos-session-fulltext-probe` 为 `skipped`。分支 push 自动构建 225 是相同源码的重复预览，已主动取消，实际发布仅用 226 的完整门禁结果。
- 2026-11-12 测试预发布：<https://github.com/SakuraLoveSmile/KaneoPilot/releases/tag/v0.5.7-test.226>，非 draft、prerelease，基础版本 `0.5.7`，来源分支/提交与上文一致；四项资产齐备。
- 下载核验：Android APK、arm64 DMG 与 manifest 的 SHA256 均匹配 `SHA256SUMS`；manifest 的文件大小、每项 source SHA/version/build=226、test 通道及 arm64 架构一致；`hdiutil verify` 退出 0、DMG checksum VALID。完整包和 `cloud-result.json`、`package-check.json` 保存在主工作区 `build/kp-cont02/release-226/`，属于交付证据，不作为缓存清理。
- 体验入口：已安装正式/测试版开启「接收测试版」后检查更新到 `0.5.7-test.226`；本次未替换 `/Applications/KaneoPilot.app` 或安装 Android 包。没有 schema 变更。
- 真实安装升级与该业务任务的独立评审尚未验证，不等同于本地隔离测试通过。
- 本任务尚未合并 main；保留分支/worktree 供后续集成，未满足合并后的清理条件。
