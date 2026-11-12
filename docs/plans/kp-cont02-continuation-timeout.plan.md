# KP-CONT02 同一 Goal 继续执行的计时与收尾修复

- 状态：实现中（Pilot 修复和 test.228 已发布、待体验）；Music 已通过正常重核，新的独立评审因真实原生验收缺口阻塞，现进入补验处理，尚未发布，不视为业务已验收。
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
- T4（2026-11-12 用户反馈后）：预算 worker 独占 `cmd_request_continuation.py`、`verification_recovery.py` 与新增预算恢复测试，成功人工继续开启新自动预算段；为有当前 run/plan 可信原 empty-diff 回执的预算覆盖提供既有 reverify 恢复，完整重核才能排 monitor，不重发执行。
- T5：成果 worker 独占 `engine/verdict.py`、`cmd_delivery.py`、`git_delivery.py` 与已提交成果回归，核对任务提交链、原 task/run trailer、base/head/分支/源码及当前报告后复用已有 SHA，登记当前 run 复用关系，不创建空提交。仅未跟踪普通 `.pi/evidence/**` 作为运行材料保留、不提交；链接、特殊文件、嵌套仓库及 tracked 变动仍拒绝。
- T6：评审材料 worker 独占 `pi_desktop_review.py`、`review_snapshot.py` 与新增材料测试，在共用收集入口冻结原生证据与哈希，保持相对引用，沿用单文件 20MiB／总量 100MiB 上限；证据是待核验输入，不是 pass。主 agent 维护规格、集成和真实定点 monitor/Review/交付观察；独立 reviewer 只读检查最终组合。
- T7（客户端中断后）：worker 独占 `planner_runner.py`、`worktree_recovery.py` 及新增原生恢复测试，补充发送前 child 身份检查点及仅 Codex plan 的显式中断恢复。主 agent 独占规格/本记录，独立 reviewer 只读审查；测试使用隔离 DB/假进程，真实现场先只读预检与完整备份，通过后仅核心命令恢复。不得以父进程消失单独证明停止，不假写模型成功，不操作其他任务或设备。
- 数据与运行资源：测试只使用临时 DB/目录与假控制面，不访问生产 DB 或付费执行端；编译、签名与打包只在 GitHub 托管 Actions。

## 验收

1. 原轮开始超过四小时但刚继续的轮次，不再立即超时；当前继续段真正超过预算仍失败；无继续标记保持原计时。
2. 有可信同 task/run/session 继续操作的新 turn 可越过旧审批检查点进入只读观察并正常记录终态、报告；身份漂移或未授权 turn 仍拒绝，不代批新 Goal。
3. 重复继续操作不重复发 prompt；再次继续保持有效计时和观察绑定，不让截短历史导致合法轮次丢失。
4. 最终源码组合态的适用隔离测试通过；云端检查与两个平台构建成功；测试预发布包含匹配源码和构建号的四项资产。
5. 原先已提交的两处歌词源码可在可信提交与当前报告/源码绑定通过后进入新独立 Review；真正零成果、未登记/外来提交、源码漂移继续阻塞。
6. 无新代码的补验复用原任务提交，不创建空提交、不丢原生证据，不把 Pi 的 `met` 等同于评审通过。
7. 继续后的自动预算与当前人工继续段一致；重复观察、重放、未知/失败发送不延长预算。原预算覆盖的误阻塞经完整 reverify 恢复，不要求再实施一轮。
8. 独立评审材料包含 157 个现有原生证据文件（约 81.3MiB，实际数量/内容以冻结清单为准），全过程校验内容哈希；评审通过后沿原 PR/自动合并/发布设置推进。

## 第二轮核查（test.226 后）

- 现场已安装 test.226。14:13 核心把原 `empty_diff` 质量补救因旧 `automation.segment_started_at` 预算过期升级为 `auto_budget_exceeded`；继续按钮只重置了轮次预算，未开新的自动预算段。
- `delivery.head_sha=3908a33`、`base_sha=d3382cbf`，二者有两处真正歌词源码差异，原提交 task/run trailer 与登记一致；当前工作树只剩 `.pi` 证据未跟踪。原 verdict 仅看 `git status`，把补验无新源码误判为整项任务无成果；原 delivery-commit 又按当前 run 找不到提交，仍将尝试创建新提交并报 no_changes。
- 最新 Goal 报告通过不是独立 Review pass。已有 Review history 仅旧计划的 blocked；本轮必须重新独立评审，不伪造通过或跳到发布成功。
- Review 共用材料入口未冻结 `.pi/evidence`，固定提交的评审工作树不会带入这些未跟踪截图录像。真实材料 157 文件、85,280,099 字节、最大 3,970,062 字节，现有限额可完整容纳，不降低门禁。
- 本轮尚未操作生产恢复；待组合源码验证后经 `resolve reverify` 与定点 `claim(request_id)` 仅处理本任务 monitor，避免取走其他任务，不抢占已领取请求。
- 第二轮独立审查未发现阻塞问题，覆盖预算事务/幂等、回执与源码重核、完整提交链、复用账本并发保护和原生材料冻结。提交复用以 `reused_from_execution_run_id` 明示原轮，追加 `delivery_commit_reused` 事件，原提交 trailer 不变。
- 当前生产只读预检：已登记成果 helper 精确返回原 `3908a33` 与两处歌词文件；当前 Git binding、回执证据和恢复资格均匹配；原生材料安全冻结/复核返回 157 文件、85,280,099 字节，无执行脚本或模型操作。
- 第二轮主 agent 组合验证：预算/继续/恢复及界面资格相关检查 `53 passed, 17 subtests passed`（74.95s）；成果、verdict、Git 交付、运行材料、原生 Review、快照、CLI、任务分支和结算/阶段契约的最终组合 `386 passed, 23 subtests passed`（371.68s），均为真 pytest 与隔离夹具。独立审查通过，`git diff --check` 通过。
- 第二轮源码 `f0e1ffc64b1fc28e5e74c87193cebdeb674812e5`。经核心 `resolve reverify` 及定点 monitor `cc0ac89e-da3f-40c2-b96f-83c0075aae77`，完整 Goal/冻结 verify/源码核验通过，复用原 `3908a33`，显式追加当前 run 的提交复用登记；真实任务进入 `review / in_review`，清除了旧预算误阻塞，没有新派发执行。
- 新独立 Codex Review 请求 `5437a28b-ea82-402d-8fec-13b73748677d`，invocation `fd1589d4-2e2d-42b9-80fe-e524518d857f`；冻结并校验 157 份原生证据，正常登记 `blocked`，真实任务变为 `review / needs_user / review_blocked`。报告 `~/Library/Application Support/KaneoPilot/reports/wjhpqxfqxr3084vxtc19e1uo/6356ba98-1c94-45d9-82f9-86780ce61f58.md`：未发现确定代码缺陷，但候选 APK 无法追溯至指定提交、可播放空歌词样本缺失、双栏播放/暂停与860宽seek尚未验证；另记录原执行换包清数据事故的恢复完整性未证明。未跳过评审、未创建 PR/合并/业务发布；现有证据不足以宣称用户任务已可发布。
- 新测试版云端 Packages run 228：<https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/43172845344>，来源 `f0e1ffc`，基础版 `0.5.7`；全部必需 job success，仅显式未开启的全文探针 skipped。相同源码的 push 预览 227 已主动取消，仅以完整测试发布 228 为证据。
- 已公开测试预发布 <https://github.com/SakuraLoveSmile/KaneoPilot/releases/tag/v0.5.7-test.228>，非 draft、prerelease，四项资产齐备。完整下载 APK、arm64 DMG、manifest、SHA256SUMS；所有 SHA256、文件大小、source SHA/version/build=228、test 通道/分支/arm64 架构均匹配，`hdiutil verify` 通过。证据与交付包保留在主工作区 `build/kp-cont02/release-228/`（含 cloud-result.json、package-check.json），不作为缓存删除。未替换已安装 App，未安装 Android 包；开启测试通道后可检查更新。
- 阻塞后只读调查确认：旧会话可追溯 profile 构建→对齐签名→安装，现存 profile 包与 a2 的 arm64 `libapp.so` 字节相同；但构建时锁文件变更、完整源码快照缺失，仍不能证明完整固定 HEAD 的包来源。执行树无可复用音频夹具，未读取用户媒体库/凭据。现有手机后来已升级更高版本，未再次换包、降级或改数据；事故前数据完整恢复未证明。已询问可正常播放且无歌词的现有测试输入与隔离环境；后续由固化执行端从固定源码构建取证，不在 Pilot 本地代跑 verify，不擅自新增数据源或改用户 DB。
- 用户指定无歌词样本《感谢陪伴》。它正是历史取证使用的 Hanser／一抹憨色第10首、00:49 曲目；应修正“没有样本”的表述。旧候选包 UI 停在末尾且点击不推进的事实需重新核验，不能据此认定当前歌曲资源不存在或服务端缺失；`dumpsys` 单独位置冻结不构播放失败证明。已进一步询问当前版本是否正常播放，未重复要求选择别的曲目。双栏取证使用《他守恒》，其交互缺口仍独立存在。已有 AVD 属 StudyTool，未复用其数据或启动；新隔离环境和现有账号仍待确定。
- 用户随后确认《感谢陪伴》在当前版本正常播放。按已授权修复目标，经核心 `prepare-review-repair` 保留原成果、原计划和新阻塞报告，准备同任务补验规划；`prepare-plan-input` 补充上述用户事实、固定 `3908a33`、零业务源码/依赖改动、新建专用空白模拟器、不操作现有手机、不提取凭据/复制DB、不新增服务/改音乐库及缺认证停在登录入口等边界。材料无 issue、can_start=true。仅为本任务提交 Codex 规划 `a4d027a1-24f7-4ed0-a700-3a0e79ef9b80`，定点 `planner_runner --request-id ... --once` 运行，保留发布后确认执行门；未运行其他任务规划。

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

## T7 原生规划中断恢复

- 客户端中断后原定点 planner PID 52835 已不存在；无 last-message/plan-draft。已由核心如实 report failed，task 返回 needs_plan；原 leased/running plan 槽位仍占用，旧恢复入口只支持 Plus，不能手工清锁或伪造 owner_stopped。
- 完整现场备份位于主工作区 `build/kp-cont02/planner-recovery/e034fea0-4999-43c3-9630-656891ee9dca/`，槽位归档 85,463,716 bytes、SHA256 `1dc114546d748b2f4df758e7163028a84207a90be5395e06dbde6e4f6d2bd0c2`；包含槽位树/Git 索引与原 job/events/codex/schema，现有备份检查匹配，未改原现场。
- worker 只读实际预检返回 ready / native_planner_interrupted / planner_result=unconfirmed；完整日志无未完成工具，完整进程扫描无相关进程。此为恢复资格预检，不是已执行恢复、规划完成或业务验收；等待隔离回归与独立审查。
- T7 初步组合检查（新增扫描修订前）：既有 worktree recovery/pool `122 passed in 115.26s`；planner_errors/codex_exec_config/planner_partial_preflight/task_workspace `54 passed, 21 subtests passed in 70.67s`。独立预审要求补齐脱离进程组的后台进程占用核验及原生日志会话绑定；未因此降低恢复条件，尚未执行生产恢复。
- 独立审查与主 agent 均逐条读取了原日志的 31 项 command_execution：仅文件/源码/环境查询，无后台服务/派生进程/外部异步动作；未发现现场存在 daemon 的证据。恢复实现仍需把这份 agent 审计绑定日志与命令账本哈希，并拒绝任意已完成工具自动满足恢复条件。内核 cwd/打开文件扫描补强占用检查，但不称绝对后代证明；当前旧 ready 预检不沿用，待最终守卫后重跑。
- T7 最终独立审查无剩余实质阻塞。主 agent 组合检查 `217 passed, 21 subtests passed in 202.42s`；最后 signed-UID 兼容修订后新增 native 文件完整重跑 `42 passed in 33.01s`。规划 provider/link-images/preflight/input/method 相关检查 `74 passed, 19 subtests passed in 35.33s`；`git diff --check` 通过。均为隔离夹具，未调用真实模型或代跑 Music verify。
- 主 agent 最终真实核心 dry-run 返回 ready，日志/31条命令审计/备份/本机 PID 与内核占用检查通过。经 `worktree-slot-recover dry_run:false confirm:true`，operation `kp-cont02-native-recovery-e034fea0` 已 accepted，旧槽位变 free；原 job 如实 failed、exit_code=null、planner_result=unconfirmed，未产生成功计划。结果保存于原备份目录的 recovery-result.json；审计附件 SHA `71f7ba46d9333b34a8696f0720b7be4f46dd2903ac2885d09f8bc3981cc28280`。
- 正常 `start-plans` 复用同一已确认输入，重新提交本 task 请求 `96a65dbe-3191-4070-b002-4b99063f7405`；仅定点 planner_runner --once，以脱离工具会话的独立进程启动（PID 39419），保留原配置与计划执行确认门。启动信息在主工作区 build/kp-cont02/progression/restart-plan-launch.json，尚待模型完成与计划验收，未派发业务补验。
