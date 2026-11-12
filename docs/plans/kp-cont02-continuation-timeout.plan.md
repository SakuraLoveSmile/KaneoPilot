# KP-CONT02 同一 Goal 继续执行的计时与收尾修复

- 状态：实现中（Pilot 修复和 test.232 已发布、待体验）；Music 正常重核/独立评审后进入补验，新执行因 NETWORK_ERROR 结束；原生完整材料额度变更待用户决定，业务尚未发布，不视为已验收。
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
- T7 发布源码 `8d3e290908fdc875f1709896ae36c74844d375b1` 已推送任务分支。云端 Packages 测试发布 run 230：<https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/43183608785>，基础版本沿用 0.5.7；同源码 push 预览229已请求取消，避免重复构建。此刻未发布，不沿用 test.228 的包检查结果。新规划实际 run `6163ff5e-a41c-47af-a8a2-b46783d4a453`，已持久 native_child 检查点；后续仍须正常计划发布/执行确认/原生补验/独立Review。
- 云端230的两组 shard3 核心检查失败：新增真实内核扫描用例在Linux遇到同UID但不可读取的 GitHub runner 进程 `/proc/1028/cwd`，源码按 fail-closed 正确拒绝。用例误把整个宿主当可控夹具，不能因此跳过用例或放宽生产扫描。后续仅隔离Linux测试的进程清单，仍读取真实临时进程cwd/FD，并增加不可检查活PID必须拒绝的回归；macOS真实全量lsof保留。230发布门禁拦截，尚无test.230发布。
- 云端失败修正仅涉及新增测试文件：Linux枚举受控self/临时child进程，仍真实读取内核cwd/FD；mac继续全量lsof。新增受保护活PID拒绝回归，不改生产scanner、不skip或降门禁。主agent重跑 `43 passed in 31.99s`，worker独立43通过，reviewer确认边界合理、无实质问题；Linux实际结果待新云端检查。
- 测试隔离修订源码 `7a6acc0570941e22566d28c02a22840556ed2177` 已推送；新测试发布 Packages run232：<https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/43185538086>。旧230已确知两组核心失败，记录原失败后请求停止剩余无诊断必要的构建；同源码push预览231亦取消，避免占用资源。仅以232的完整门禁、公开Release和包检查为本次发布证据，不能沿用230的部分绿灯。
- 新补验计划正常发布：`bfa771d5df36cd57bf2b539e3c79539be5800555eb3c51e0f1ff2c355af70035`，路径 `/Users/sakurasep/Documents/Code/Plan/projects/e8magole7hbivl8103wevkss/这里的歌词展示有点小了吧_感觉不协调_2026-11-12_16-48-23.md`。现有调度器自动确认并派发新run `62d4390e-8bbe-4599-88b2-eb5c6f1dea59` / session `2d20c98b-7e16-48d7-b0ce-19119b5c6288`；主agent未重复claim/dispatch。该轮仅做开工只读核对，随后原生turn返回 NETWORK_ERROR；核心如实 execution_failed / needs_user，不是原误超时，尚无Goal/构建/设备或业务verify通过。
- 独立计划审查发现两个执行前置：完整新APK放入.pi/evidence（历史APK约60.83MiB）与单文件20MiB、全树100MiB边界冲突，旧157份证据已81.33MiB；不能删除旧证据/只留哈希/抽帧替代完整录像。另自定义.pi/runtime不在交付豁免范围，成功交付必须清理且无进程引用，阻塞时保留但不推进完成。其余原验收和隔离边界完整保留。暂停原样重试；先明确合规材料传递及runtime收尾，网络恢复后走正常核心路径。

## T8 原生完整材料前置（用户已确认，实现中）

- 只读分析与独立审查确认：当前每任务100MiB不是每批100MiB，分卷不能透明绕过；旧证据、完整候选包和完整录像必须保留。不能排除APK、移动旧证据或仅传哈希来声称满足原契约。
- 已向用户提出有界变更：仅原生Review证据单文件128MiB、总量512MiB；通用规划/源码快照材料继续20MiB/100MiB。完整文件仍经无链接读取、大小上限、双次身份/库存核对、全字节SHA、task/run/plan/head绑定、冻结/收尾哈希守卫；独立Review仍须实际读取完整材料，不能由资源校验代替pass。此为重要输入边界调整，未得到答复前不实施、不生产恢复或重派该依赖部分。
- 若获确认，改动限定共用原生证据冻结通道及相应边界测试/规格，不扩大Git豁免；补验成功交付前显式确认临时AVD/认证runtime已清理、无运行引用。新计划和verify需经正常核心路径更新，不能原地改已冻结计划。

## test.232 发布证据

- 源码 `7a6acc0570941e22566d28c02a22840556ed2177`；<https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/43185538086> 全部必需job success，仅显式未启用fulltext probe skipped，包括两组之前失败的shard3及原生平台探针均通过。
- <https://github.com/SakuraLoveSmile/KaneoPilot/releases/tag/v0.5.7-test.232> 已公开，非draft、prerelease，四项资产齐全；2026-11-12 09:03:34Z发布。没有覆写test.228或旧标签，没有数据库schema变更。
- 完整下载APK、arm64 DMG、manifest、SHA256SUMS；所有哈希/大小/source SHA/version/build=232/channel/branch/arm64身份一致，hdiutil verify为VALID。包与cloud-result.json/package-check.json保留于主工作区build/kp-cont02/release-232/。未覆盖安装App或现有手机；体验入口为App开启接收测试版后检查更新。
- 本任务未合并main，且T8仍待用户决定，保留本任务工作树，不满足收尾清理条件。原Music执行已停在真实NETWORK_ERROR，不在未知发送状态下盲重发；未把《感谢陪伴》用户确认当作已完成原生验收。
- 用户在本次回复明确选择“按推荐额度修复原生证据通道”，授权原生Review单文件128MiB/总512MiB；保留generic20/100与完整安全校验。worker独占constants.py、review_snapshot.py及原生材料测试，独立reviewer只读核查共用入口/最终守卫，主agent独占规格/本记录和定点业务恢复。
- 实现限定native显式限额，复核逐文件避免同时构造第二个512MiB集合；generic源码freeze默认不改、不全局排除.pi路径。Pi/Codex提交分支Review走共用native入口；历史manual Review仍必须独立满足其通用源码快照限制，保留兼容边界。
- 更新既有原生超限用例的注入常量，从PLAN_INPUT改为NATIVE_REVIEW_EVIDENCE，以验证用户确认的新独立额度；未删除/跳过原拒绝场景。新增真实128/512精确边界、+1预读拒绝及generic20/100不变回归，保留链路完整性/安全测试。独立审查通过，新限额模块独立14通过；主agent组合检查进行中。
- 经核心prepare-plan-input把用户确认额度及runtime收尾补入同任务，输入 `ee1e9bd1-a02c-4405-bd05-e13b85d8eaf3` / `b58b672f59944783e36f34a4b56f5db5ba7c3540eb4446c713049249c1a8d0cb`，can_start=true、无issue。正常start-plans提交请求 `c89e7779-528d-4aab-abb9-d1589e129960`，脱离工具会话的定点planner --once启动（PID32846）；未原地改冻结计划，未复用错误网络轮次，未抢其他任务。

- T8最终组合态：主agent使用真pytest运行native/limits/snapshot/CLI Review/Desktop queue/stage contract，`132 passed, 76 subtests passed in 476.85s`；worker独立专项42通过，共用入口95通过、8 subtests通过；reviewer无阻断项。未跳过或放宽门禁，未调用业务执行端验证。`git diff --check`通过，准备提交任务分支并走云端测试发布。

- T8发布源码 `9e0428cb91cfcd3a7e60342bfac16f3eef0cab86` 已推送；云端 Packages 测试发布 run234：<https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/43194535718>，基础版本0.5.7。同源码push预览233已核对身份并请求取消，避免重复；234进行中，尚无新Release/安装证据。

- test.234已公开：<https://github.com/SakuraLoveSmile/KaneoPilot/releases/tag/v0.5.7-test.234>，非draft/prerelease，全部18个job中17个success，仅显式fulltext probe skipped。完整四资产已下载至主仓库build/kp-cont02/release-234，SHA/大小/source/version/build/通道/分支/arm64一致；hdiutil verify退出0、checksum VALID。未安装App或操作现有手机。

## T9 补验规划发布恢复（实现中）

- c89e规划模型正常exit0、完整last-message保留，但发布因远端in-progress被拒；旧review_remediation已executing，原remote_status=in-review不再被expected_remote_slugs接受，automation旧replan_remote_status仍to-do。不能直接改远端回ToDo或伪造request/job。
- automation自动发起4ef58e46-db16-46eb-ae1e-fb9f704f87fd / d643728b-0352-408b-b4b0-40b39ae97733，已被领取。独立只读分析确认该input85c23e40-1863-420c-8d86-beb898f2491f丢失supplement且无review_remediation，不等同原已确认输入；保留当前owner，不抢占、不放宽发布门禁。原c89e完整稿已包含128/512和runtime收尾，优先修正规划产物恢复与自动重试输入保全，而非重复消耗模型。

- 独立审查原c89e完整稿无P1，保留全部原验收和三条verify，但第4条verify仅ps/PID不是真实cwd/openFD扫描（P2），须修订后才能执行。因此不新增已完成产物直接登记恢复框架，保留原稿审计；本轮沿现有resolve replan→prepare/start正常路径刷新当前远端授权，自动重试缺陷仅修输入保全与完整稿发布失败止损。
- 4ef旧自动规划正常回needs_user（reported，询问860dp范围），无queued/claimed后经核心resolve replan成功（当前in-progress已重新绑定）。新输入bac954a4-d715-48eb-9a47-8d0354d1afba / 86378301133007a016be8491c28d4c944ac2b5c042050b2eeaaea3da036cb618，can_start=true/issues=[]；保留完整supplement、原正式计划与c89e完整稿作参考，新增真实内核引用扫描要求、不扩大860dp补验为业务源码修改。正常start-plans提交25772a91-bb05-4a59-9b9c-9ab1ee1cdf58，定点detached planner PID93893。原远端门禁不修改，未抢owner或伪造规划成功。

- T9预修订组合180 passed、16 subtests passed（32.96s），覆盖retry inputs/intake/planner inputs automation/automation core/planner errors。独立review另发现新owner完成后active为空时，旧retry的成功/失败路径可覆盖新状态的并发缺口；正在补事务内任务与原失败请求身份门禁，该次通过不作为修订后最终证据。

- T9最终源码组合：重试入口资格/双快照及提交/失败事务守卫，原冻结输入完整复用，native完成ready稿publish_failed止损；其他后端不要求native job。两个独立复现的owner竞争窗口均修复，独立review最终无阻断；专项11 passed/19 subtests。主agent最终组合retry/intake/planner inputs automation/automation core/planner errors/v2 planning `198 passed, 22 subtests passed in 39.79s`，git diff --check通过，均隔离夹具，无真实模型/生产调用。新增内部start_plans参数，不增加CLI命令、不改远端门禁/DB schema。准备提交并发布后续测试版。

- T9发布源码f9b3a08b1450a7910ae28ea128fd0cd8769523ad已推送。Packages测试发布run236：<https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/43201320187>，基础版0.5.7；已核对同源码push235并请求取消，只以236完整门禁与Release核验为新版本证据。

- run236两组shard1失败（3.11/3.13）：test_review_fix_workspace中两个旧夹具把未adopt新Review子任务直接_submit(wanted=True)，生产初次实际走wanted=False；新重试资格门正确拒绝，拟按真实首次接收路径修夹具，保留全部原HEAD/等待/不block断言，补非法wantedTrue拒绝。初次review_fix需在prepare前核原Review登记，保留安全顺序。不放宽并发门禁；236不发布，保存失败日志后停止剩余无诊断必要构建。

## T10 发布前验证命令契约（实现中）

- 新正式补验计划正常发布1290b2060194be5722bf187b5e173808bb80e22b76c7ecba6c09632a95667cc7，18-38-24.md；scheduler自动派发e6747f76-86f8-4477-b6a7-7427dbb29287。独立审查确认内核引用P2已修及原范围完整，但未校验命令长度；执行端在首次会话建立前因单命令>4096拒绝，job failed/exit1/sessionNone、verification_requirements_invalid。没有新模型执行或设备操作。
- 核心publish仅parse verify未按Plus Host字节限额拒绝，导致ready后才失败；拟发布前按纯DB当前执行backend及派发实际冻结backend复用原validator，规划共享prompt明确1–5条、4096/65536与去重/无NUL换行，不放宽原限额。auto_dispatch通用异常收尾将本次记unknown（实际jobnot_sent），当前needs_user/dispatch_failed；须经正规未发送证据恢复，不能改DB/直接原样retry。

- T10与236兼容修订最终组合 `456 passed, 70 subtests passed in 114.99s`；覆盖Plus/Codex前置恢复、发布校验、verify/transport、dispatch/branch、Review首次接收、重试/intake/自动化与v2规划。独立review分别通过，未放宽原断言或门禁。新Plus proof专项21 passed/41 subtests，真实mode=ro prove再次ready且含DB路径绑定；生产尚未恢复。
- T10新增publish/dispatch对实际Plus路由的同validator前置拒绝，模板明确命令边界；新增proof仅精确user+expectedrun的replan、同一已知长度失败，旧job/request/round不改，缺证继续拒绝。新计划须保持最多5条且每条<=4096字节；若需本轮新helper，完整源码/确定SHA冻在计划，短verify先核同一份字节再执行，不执行旧证据脚本或改扩展名绕Git豁免，不改业务源码和依赖。

- T10发布源码e79e782351bf47d64215b666a82abc2ecc5b2b88已推送。Packages测试发布run238：<https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/43205569780>，基础版0.5.7，同源码push237已核对并请求取消。236最终cancelled，两个shard1真实失败证据保留于主仓库build/kp-cont02/cloud-236，不存在test.236发布。
- 经最终已提交核心resolve replan（expected_execution_run_id=e6747f76-86f8-4477-b6a7-7427dbb29287）proof与远端/事务重核通过，任务正常needs_plan，旧dispatch仍failed、旧job仍failed/not_sent/exit1；独立审计记录plus_preflight_replan_confirmed，不改旧记录。完整新输入2d76aff0-9dbb-42cf-b227-f3af85714bac / e598c5361eedf5ccd548642f5ec281bb655a60e41db21e9ff5347e7e8f35cbce，can_start=true/issues=[]；normal start请求f58868ec-c599-41bf-9082-15692137c75f已claimed，run42da1d89-fba8-4af4-a412-880ec0322b14，PID31646，仍gpt-6.1-sol/max。补充完整原计划/新稿/全部确认要求，并明确短verify与完整新helper哈希冻结；尚未验证或操作设备。

- run238全部Python分片已结束：仅shard2/5两种Python因四个旧fixture缺verify失败，其余全部通过；日志/云端身份保存在主仓库build/kp-cont02/cloud-238，停止剩余原生任务，不存在test.238发布。失败为BuildFailure两个后继新ready任务与T6两个新Plus派发用例使用简化无verify计划，新前置门正确拒绝。修订只补这些局部合法verify，生产源码/全局helper/所有原业务断言不改；定向7 passed/2subtests，最终相关完整文件组合进行中。

- run238局部fixture修订最终主agent组合：两个完整文件+发布校验/Plus proof/Review接收/retry/intake，`192 passed, 67 subtests passed in 162.61s`；独立审查通过，diffcheck通过，原全部业务断言未降低，生产源码与e79相同。无需因局部fixture再次重跑全部核心；后续云端仍执行全套必需门禁。

- run240已核对存在：<https://github.com/SakuraLoveSmile/KaneoPilot/actions/runs/43207916047>，source3146ee6b4e3703ab79850a91bc6122ee054d2498，workflow_dispatch测试发布。首次POST响应unexpectedEOF，但随后用单次临时HTTP1只读查询证实请求已创建，未重复POST，未改系统网络/代理。核对同源码push239后请求取消；只以240完整门禁/Release/包核验为后续发布证据。

- T10新正式补验计划334b804887bd5efe905b48c0074c0e2dd610f9e577d3dfe81fdfe966bea61037（19-23-59.md）正常发布，主agent与独立review静态复核两份helper完整5750/7225字节及SHA匹配，短verify只读一次字节核验后执行；五条命令130/24/21/386/387字节，前三原文保持。正常scheduler派发run1ac2ab6f-3fa4-4729-b1f4-5e6044edbd86/sessiond8549537-929c-4de0-bc12-58af948a29be，真正进入monitor/running，没有重复claim。
- 执行端提出三问，实证sudo -n拒绝（缺root内核扫描权限），并提前询问登录与可选额外analyst。经正常submit-execution-answers固定operation kp-cont02-t10-continue-independent-preparation已accepted：不改权限/不把失败当通过，先做独立构建/AVD/安装并到真实登录入口再请用户交互；沿用用户确认歌曲可播事实；不启用额外付费analyst，主执行者串行完整核对。权限单独向用户确认，仅两条固定只读lsof；最小sudoers候选在主仓库build/kp-cont02/readonly-lsof-permission，visudo -cf语法通过，尚未安装系统。业务验收/第4verify仍未通过。

## test.240 发布证据

- source3146ee6b4e3703ab79850a91bc6122ee054d2498，Packages240全部必需job与publish success（18项中17success，仅显式fulltext probe skipped）。公开Release <https://github.com/SakuraLoveSmile/KaneoPilot/releases/tag/v0.5.7-test.240>，非draft/prerelease，2026-11-12 11:37:21Z发布，APK/arm64 DMG/manifest/SHA256SUMS四资产齐。
- 完整资产已下载主仓库build/kp-cont02/release-240，全部SHA/大小/source/version/build240/channel/branch/arm64一致；hdiutil verify退出0、checksum VALID，cloud-result与package-check持久保留。未安装App或现有手机，体验入口开启测试版检查更新；无schema变更。
- Music当前仍monitor/running，尚无候选包与原生验证成功。只读构建日志显示11:35:15Z固定源码release构建因GeneratedPluginRegistrant引用integration_test缺类失败，执行端仍排查；Pilot未本地代跑Music verify或修改业务源码。sudo两条固定扫描权限请求仍待用户答复，最小候选未写系统。任务未合并main，活跃补验与未完权限/登录保持实现中，工作树保留，不满足收尾清理。

- 恢复跟进只读核验：11:41:05Z执行端重建成功（exit0，APK约93.2MB），保留stale与release-filtered生成产物来源记录；候选已安装专用AVD，未动现有手机。当前run仍1ac2ab6f，monitor/needs_user，正常执行问题a5ea275d-c0ca-496d-b566-e54c39a6a066等待用户在应用内交互登录；没有冒充登录或原生验收通过。主控已提供具体模拟器入口，请用户登录后继续；root两条固定只读lsof权限仍未获答复，不改系统配置。

## T11 用户改为真机验收（实现中）

- 用户明确授权仅两条固定root只读lsof；最小配置已通过macOS管理员授权安装到/private/etc/sudoers.d/kaneopilot-lyrics-lsof，root:wheel/0440，visudo完整配置检查通过。主控仅做权限可用性检查，两条精确命令均exit0、stderr0；原始输出/permission-check.json保留主仓库build/kp-cont02/readonly-lsof-permission。这不是代跑Music冻结verify，也不是runtime已清理证明。
- 用户最新要求“请在真机中测试”，覆盖先前AVD选择。只读识别已连接Mi 10（8085d06e），现有com.sakuramusic.app版本0.3.8/versionCode1793783652，设备端APK SHA b1cb7654ddf22b8c125c49b77d1722fbf408dd8680a796a4653baa22258f8ea0，与新候选23c9eddef6ac7405e5c1eb04be47e3636f492197fd02090a1b1d986cb2b68591不同。未安装/卸载/降级/清数据，未读取DB或认证。
- 通过submit-execution-answers operation kp-cont02-user-physical-device把用户设备变更送达当前执行者（accepted）；执行者当前正常running，先保全材料并仅收尾本任务AVD，再经正常replan更新设备绑定，旧计划不原地改写，不把未登录或未采集验收当通过。后继计划须保留全部原验收与128/512、完整来源及verify门，明确真机数据保全和同条件基线对照方式；现有手机不能用卸载/降级换包完成对照。

- 补充只读aapt核验：round5候选versionCode1793783648，低于真机1793783652；禁止直接安装或-d。后继真机计划已准备完整supplement，要求保持业务源码3908a33，以Flutter build-number参数生成严格更高版本并核签名/身份/数据保全；不能在现有真机反复装历史基线包。未做任何真机写入。

- 真机补验完整input a23d8402-98f9-49cc-8c38-cf7ebc20bbe9/188880c8272ccad8c979697171919aec0795c2eb159c38b189fdbf10da429e29已正常prepare（can_start=true/issues=[]）；resolve后现有自动规划抢先提交3ce23aee-c90d-4bb7-9b56-f09f8b62af40/run a281c98f，输入51ac8f75且supplement空。手动start-plans返回planning conflict，未启动第二owner、不覆盖冻结输入；先保留当前owner，待正常终态后核新计划是否包含用户真机要求与数据保全/签名/升级条件。
- 独立只读根因核验：运行中的App/MCP/engine仍test.226，旧bundle缺_evidence_material和已提交成果复用；其5MiB门把本轮3个证据APK误判为交付。当前任务HEAD分类effective=[]，真实提交仅2个Dart业务文件，安全。无需删除证据、修改5MB门或再改Music源码；应先安装并重启已核验test.240。此前约定不自动安装，已向用户明确询问更新/短暂停调度，等待答复；尚未安装/停进程。

- T11稳定签名只读核验：主Music工作区原有android/app/key.properties与keystore.jks存在，keytool仅读公钥export exit0，公开证书SHA b2bb4810ec0074fe6e2341689fde03755f5be7567d779d2cce9d9318d0fc7a8d，与历史稳定包一致；未输出/持久化密码或私钥。此事实已加入完整新supplement，但不能推定当前真机证书，固化执行端安装前仍须核对。
- 当前自动planner3ce23仍claimed且租约有效，native child PID775仍活，owner来自旧226 bundle。正常退出App会停止所属调度/后代，当前不安全安装；需等该owner自然终态并核request/job/child/槽位，再执行用户同意的正常更新。更新确认问题已发出，尚无答复；未停止或替换App。

## test.240 实际安装（用户已授权，进行中）

- 用户确认“现在更新并重启”。通过App设置→高级→软件更新正常检查，test.240已下载并校验；尚未点安装，先等待当前native规划自然终态。通过侧栏暂停自动推进（原先运行中），仅阻止新领取，在途1项继续；更新核验后恢复原运行状态。
- 已用SQLite mode=ro backup API保存一致性数据库备份到主仓库build/kp-cont02/install-240-backup/kanpilot-before-update.db，目录0700/文件0600，69,816,320B，SHA2100782ef437349153b303dfc9ebbf0ed66acc9a292c94c59941ca671eaf9ca1，immutable只读integrity_check=ok；未复制活动DB文件拼接WAL。manifest同目录保留，不输出DB/凭据内容。

- 实际安装完成：旧3ce23自然needs_input终态（真实问题为真机不兼容时路线），708/775/786均已退出，无claimed请求，plan/review槽位free后，点击App“安装并重新启动”并确认。新版App PID16345于20:37:43启动；设置界面显示0.5.7-test.240（240），内置git_delivery/cmd_delivery/review_snapshot/constants逐文件与发布源码一致，新engine/MCP进程均来自新版bundle。正常更新器回执launch_requested，保留旧App于/Applications/.KaneoPilot-backup-hsesbeua/KaneoPilot.app；未手动覆盖运行中bundle。
- 已恢复原自动推进（UI运行中），数据库27任务保留，无schema变更。回执及安装核验installation-check.json保存主仓库build/kp-cont02/install-240-backup；本次安装完成不等于Music业务验收通过。
- 用户真机决定已直接沿submit-plan-answers选择physical_only（不重复询问AVD路线），补充已找到的稳定签名、更高build-number/数据兼容条件、安装新版根因消除及完整helper冻结要求；operation kp-cont02-t11-physical-only-signed-upgrade accepted，新request0a282bfc-5b05-4522-be58-15f687a884a8已由新版调度器正常claimed/run db4a55ab-10b2-4f20-a13a-de3a44d381e9，模型配置未改变。真机尚无安装或原生验收通过；等待新规划及后续固化执行，保留工作树。
