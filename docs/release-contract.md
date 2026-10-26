# 构建发布交付契约（2026-10-19）

本文件是 protocol §5.27.19 的共享契约。主 agent 唯一维护。第一版在项目显式开启后，由确定性核心调度 GitHub Actions，获取实际安装包与 Release，再完成任务；不调用模型，不在 Pilot 本机代跑项目 verify。未发布的本地工作流不能声称已接通远端。

## T1 项目策略与完成门

`automation.release` 为对象，旧配置等同关闭：

```json
{"enabled":false,"channel":"prerelease","workflow_path":".github/workflows/pilot-release.yml","workflow_ref":"main","version_base":"0.1.0","required_assets":[],"required_jobs":[]}
```

- channel 仅 `prerelease|stable`，默认预发布。workflow_path 只接受 `.github/workflows/` 下无越界的 yml/yaml 文件；ref 为明确的短分支或标签名，不接受 `refs/` 前缀或裸 SHA。version_base 为三个非负十进制数。required_assets 是非空文件名列表，不接受路径、URL、通配符；required_jobs 是工作流 job 显示名列表。开启时资产声明不能为空。
- 与 intake、自动合并开关独立，沿用策略 revision 防覆盖。保存配置不启动 Actions；首次开启或切到 stable 时确认项目、发布渠道及 Actions 可能消耗配额。开启授权后续符合条件的新交付。历史 done 不自动补发、不回退状态；仅显式「补发版本」创建批次。关闭不取消已发送的构建，也不抹除其发布门和回执。
- 开启后的未完成任务必须有当前身份对应的 published 发布证据，才能经 `advance-task`、`resolve mark_done`、通用 Kaneo 同步或父任务收尾进入 done。缺证据保持交付中；远端提前 done 只记观察值。旧关闭项目保持原流程。
- 父任务与精确绑定的二级修复共享批次。批次准入核对 Review、Plus 验证、原始问题绑定、PR 合并及包含完整修复的源码证据，不以成员 done 为准入条件。共享发布成功后，既有完成流程先完成子任务，父任务再按 §5.27.18 收尾，避免循环等待。普通直接修复通常是单成员批次。
- 手动任务已有持久分支、当前执行和提交的正式 Review pass，且项目开启发布时，Review 只成功登记，停在待交付，不直接尝试完成，也不把 `release_required` 当成 Review 失败。核心提供 `resolve {action:"continue_delivery",task_id,expected_execution_run_id,expected_review_key}`，仅用户显式继续；保留原执行、计划、提交、验证和报告，不重新调用模型。历史因 `release_required` 误报 `review_blocked` 的同身份 pass 可由此恢复；其他阻塞不得借此解除。
- 继续交付前核对活跃且未暂停、无其他在途请求或修复、当前计划和正式报告字节、已验证源码及干净分支工作树、项目仓库与远端任务身份。以本地事务冻结用户授权的 task/project/repository/run/plan/requirements/head/review 身份、转 PR 交付并排队；仅这份同身份授权允许 legacy/shadow 引擎继续既有确定性交付，不能开启项目自动化或泛化到其他请求。重复点击复用同一请求；身份变化后不复用授权。旧手动 Review 的缺省修复轮次按其既有手动语义核对，不篡改报告或伪造自动化身份。关闭发布的普通手动任务保持既有完成规则。
- 已授权的同身份交付请求失败后，只有最新精确交付请求已结束、没有在途请求且不存在无关阻塞时，才允许显式重新核验并续排；保留原授权与外部操作回执，未知发送仍只对账。没有关联修复的任务在 PR 创建后展示「等待 PR 合并」及当前提交的实际检查/合并原因，不误称等待关联修复。
- PR 合并后、批次冻结前的发布核对若受阻，SQL 投影从当前交付契约绑定的正式同步结果显示「发布受阻」和「上次发布核对」原因，不误称待构建发布。执行、提交、计划或评审身份变化后不复用旧原因；本次完整准备核验成功后恢复可启动状态。此展示不放宽完整源码验证门。

### 合并后源码的补验证

普通单任务的默认发布目标若与正式验证 head 仅有已存在的 `release/preview.json`、`release/latest.json` 普通文件内容差异，可选择原已验证 head 作为不可变构建检查点。核心须从完整 GitHub tree 核对该 head 的完整源码指纹与正式验证一致，除这两个清单的 blob 外，所有纳入源码快照的 path/mode/blob 完全相同；清单新增、删除、执行位或符号链接变化均不适用。仍须确认该检查点已被当前目标分支包含。发布目标及完整源码指纹保持原已验证 head 的真实值，不把合并后的清单变化宣称为验证通过，也不扩大 `.pi/**` 或构建产物的全局排除规则。后续仅清单更新不改变冻结检查点；发送前出现其他源码差异仍受阻。

此检查点选择不适用于已显式冻结的目标、已有独立补验证引用、外部构建修复或补验证准备。上述入口继续核对各自的完整冻结目标；父子共享发布与原报告、评审、历史指纹保持原规则。软件检查点稳定不代替工作流身份稳定：工作流 ref/head 前移时，已有批次仍遵守原发送守卫，不能借清单例外复用旧工作流确认。

当独立任务的已合并目标还含其他提交，原执行的 Plus 验证不能证明该完整目标。保留原执行、正式 Review 与原验证证据；只有用户显式授权后，核心才可发起独立的发布目标验证。首版限定无二级修复的单任务、当前正式评审通过及同仓库已合并 PR，不扩展父子收尾规则。

- 只读准备冻结当前任务/项目/仓库/run/plan/requirements/head/review 身份、目标不可变 SHA、完整源码指纹、原计划逐字 verify 命令及原 Plus 执行配置。目标必须包含已评审提交。开始命令携带该上下文 SHA；身份或目标变化时拒绝旧确认。
- 在任务目录下创建独立 detached 工作树，不改变用户主检出或原执行分支。验证由 Plus Goal 执行，Pilot 只控制与核证，禁止本地代跑 verify。只允许验证及必要依赖解析，不授权修改业务源码、重写 Review、合并或发布。
- 建工作树、创建会话、发送 prompt 和批准 Goal 前先持久化操作回执；Plus 只通过本项目 runtime 域，以独立 invocation 和 execution scope 控制，不向验证会话挂接原任务 grant。同上下文去重；发送未知时只核对原会话，不自动重发、另建会话或重试模型轮次。
- 无 task/run 的独立 execution scope 批准 Goal 时，运行时仅从上述核心持久回执读取可信验证命令，核对 invocation/scope、工作树、配置、当前任务与冻结上下文，以及唯一 session/planning/proposal/tool/version 身份；不接受调用者自行声明的命令授权。普通执行仍按原冻结 dispatch spec 核对。若本地授权检查在运行时预约发送前明确拒绝，且同一 proposal 的运行时操作回执确实不存在，可重新核对原待批契约后继续一次；已有 unknown/accepted 操作仍只对账，不借此重发或开启新会话。
- 批准唯一 Goal 时固化冻结的 required verification commands；批准前与报告结束后核对 HEAD、工作树源码及 index 与冻结目标一致。允许已有 `.pi/**` 和已定义构建产物，不以忽略业务源码变化来完成验证。
- 核心核对真实报告 session/proposal/execution/turn 身份、structured/ready/completed/met 底线，以及每条冻结命令的通过记录和整数退出码 0。验证证据单独登记，不替换原 engine verdict 或 Review。报告缺失、fallback、失败、身份不符或源码漂移均拒绝发布。
- 已完成的补验证报告若有未满足的条件，不修改原 Goal 或报告，也不将 partial 升格。用户已授权继续补验证时，可显式重启独立验证：命令携带原验证 ID、原上下文 SHA 和本次读取的报告 SHA；核心再次核对原会话已停止、原执行轮次完成、报告身份与字节一致、源码未变，拒绝运行中、未知发送及非报告类阻塞。新尝试保留原回执和报告，以原验证 ID 与报告 SHA 绑定新的唯一 invocation、工作树和会话；重复同一重启命令复用该尝试，不重发。验证目标、逐字命令和原 Review 均不变。新 Goal 必须明确宿主 `.pi/**` 附件及既定缓存/构建产物不属于业务源码，报告引用真实工具调用证据；仍要求所有验收条件满足、逐字命令通过和源码冻结。
- 发布准备和发送前复核原评审证据及补验证回执、真实报告与源码身份。仅同身份 verified 结果可证明该冻结目标；目标须包含原评审 head，且当前远端目标分支仍包含它。后续分支正常前进不改写已验证检查点。批次上下文同时绑定这份补验证证据，仍须完整发布回执才能进入 done。
- 新独立验证使用提示契约 v3，明确命令结果引用采用 `kind=tool_result` 和真实 `toolCallId`，不得将 `bash`、`commandShellId` 或文件路径当作调用身份。v1/v2 的提示原文和哈希保持可读可核，不迁移已启动尝试。Plus 在当前执行中返回的引用纠正错误可由执行端处理；Pilot 仍只核对正式 Host 观察与原生整数退出码，不修改终态报告、代跑命令或自动新开付费尝试。

### 普通任务补验证入口（KP-V01）

- 普通单任务的 `release` 投影增加可选 `can_prepare_verification`、`can_sync_verification`、`can_restart_verification` 与 `verification`（沿既有独立验证 view）。旧 DTO 缺省不开放操作；`external_repair` 的原字段保持兼容。投影仅 SQL 读取，不查询 GitHub、不调用模型、不运行验证命令。
- 首次准备入口仅对当前正式评审通过、PR 已合并、无二级修复、发布策略已启用且仓库映射一致、原 Plus 执行已完成且未归档、无暂停/人工接管/其他在途非交付请求的活跃任务开放；须有当前交付契约精确绑定的 `release_target_unverified` 正式阻塞观察，无当前批次或验证引用。是否真正可以启动仍由只读准备和开始命令重新核对完整上下文，不把界面资格当验证通过或费用授权。
- 已有尝试通过当前任务身份、上下文 SHA 与核心持久回执绑定后展示；核对/重启复用原有资格，未知结果不能新开尝试。阶段和真实原因替代旧发布预检错误；可信 verified 仅恢复发布准备入口，仍不替代发布证明或完成门。暂停、归档、身份失效与不受支持的执行端不提供新的操作。
- 核心 `allowed_actions` 增加符合条件的 `prepare_release_verification`，并为普通任务复用 `sync_release_verification`、`restart_release_verification`；详情与底栏使用相同资格。普通首次按钮为“验证合并后的源码…”，现有外部修复仍为“验证修复后的源码…”。确认展示冻结目标、模型、全部验证命令与可能费用；取消不启动。后续发布沿项目原有策略和确认要求，本改动不增加自动发布授权。
- 不新增数据库表、不更改验证执行和外部副作用协议，不重写正式报告或扩大完整源码例外。切换任务、当前执行/评审/提交或验证身份变化后，桌面丢弃旧候选；开始命令的完整上下文守卫继续兜住异步期间变化。

## T2 批次、版本与外部操作

- 复用 runtime_operations 持久化批次与 dispatch intent，不新增请求 stage。所有状态写经固定核心命令；SQL 投影和完成门不进行网络、文件、子进程操作。
- 批次冻结项目/仓库、原始任务、成员 task/run/plan/requirements/head、验证与评审证据、目标 SHA、策略、工作流身份及分支 ref。相同冻结身份复用批次；同项目已有未确认 dispatch 时不能借新上下文绕过。同项目尚有未发布成功的 stable 批次时，不冻结第二个 stable 批次，先处理原批次；预发布不受该额外串行门限制。发送前与落库前复核身份；网络不占 SQLite 写事务。
- target_sha 为包含本批次完整修复的不可变提交。历史已收尾任务复用其保存的最终检查点，不把后来的工作流提交冒充原验证版本。workflow 从配置 ref 运行，每个构建 job checkout target_sha 并核对 HEAD；run.head_sha 代表工作流 ref，单独绑定，不能误当构建 SHA。
- 版本按已发布稳定版的完整列表与同项目本机已冻结 stable 批次确定下一 patch，version_base 为初始基线；列表不完整时阻塞。没有已发布稳定版时可从 version_base 开始。预发布不占用稳定版序号，tag 为 `v<version>-pilot.<build_number>`；stable tag 为 `v<version>`。build_number 为同项目单调递增的正整数，至少当前 UTC 秒数，最大 2147483647。分配后冻结；发布碰撞不覆盖旧 tag/资产。各平台构建传数字 version 和 build_number，不能把任意预发布字符串作为 iOS build-name。
- 一次 dispatch 固定 5 个 string 输入：`target_sha`、`release_batch_id`、`task_manifest_json`、`task_manifest_sha256`、`tag_name`。manifest 规范 JSON（sort_keys、UTF-8、紧凑 separators），总输入不超过 60 KiB。任务 manifest schema=1，含 batch_id、repository、project_id、root_task_id、target_sha、members、channel、version、tag、build_number、required_assets；每个 member 含 task_id/execution_run_id/plan_sha256/requirements_sha256/head_sha。
- REST 固定 `X-GitHub-Api-Version: 2026-03-10`。元数据与 Actions artifact ZIP 下载入口使用 JSON Accept；后者跟随重定向后仍按二进制 ZIP 校验。该 REST 版本已移除 PR 的 `merge_commit_sha`，历史合并提交通过 `gh pr view --json` 的 `mergeCommit.oid` 查询，仍须核对同仓库、同 PR、已合并状态及冻结的 head SHA，不以当前分支或工作流 SHA 替代。Release manifest asset 下载使用 `application/octet-stream`。响应按二进制读取不决定请求媒体类型，既有大小及归档限制保持不变。dispatch 200 返回 workflow_run_id/run_url/html_url；只接受该仓库、工作流、event=workflow_dispatch、冻结 ref/head 与精确 `pilot-release-<batch_id>` display_title 对应的 run。
- Actions 失败 job 日志入口使用 JSON Accept；仅对此固定日志 GET 允许 `--allow-escape-sequences`，在进程内捕获原始 UTF-8 字节，保留原大小限制。展示或冻结日志前去除 ANSI/终端控制字符并脱敏，保留文本与换行；无有效正文、UTF-8 不完整、身份不符或超限仍明确拒绝。该选项不能使写命令或其它入口获得只读重试资格。
- 只读 GitHub 证据查询遇到明确的连接层瞬时错误时，最多尝试 3 次并短暂退避；每次重新读取，不复用旧响应。认证、权限、资源不存在、请求拒绝、JSON 或身份校验错误不重试。此恢复仅适用于确定的只读查询，dispatch、merge 及其他远端写仍只发送一次；发送前查询耗尽且无批次/dispatch 回执时，不得误报已启动构建。
- 批次冻结后、dispatch intent 之前的确定未发送状态私有标记 `not_sent`；失败或重启可重新核对原批次后继续首次发送，仍使用原批次 ID。尚无发送 intent 的批次不得误报为远端结果未知。创建 dispatch intent 与标 unknown 必须在同一短事务原子预约，先持久 prepared/unknown 再发送；丢响应只读分页查找相同 marker、workflow 和 ref/head 的运行。唯一匹配才认领；零个保持 unknown，多个明确受阻；不按时间取最新、不自动重发。活跃 writer 不被抢占；崩溃恢复按现有 PID birth 身份验证，不能泄露生命周期锁。
- 已有精确 dispatch 回执的 `unknown / sent_or_unknown` 批次纳入原有到期、项目范围与数量限制的后台只读核对；沿用原批次和完整运行身份门，不创建或重新发送请求。零匹配时明确显示「GitHub 暂未查到本批次运行；将自动核对，暂不重新发送」，以当前观察替换过期的网络 blocker；找到原运行后展示真实构建状态，构建失败不再残留旧 EOF。确定未发送、已弃用、归档失败来源以及 failed/blocked 不因本规则自动推进，原调度回执保留。
- 确定未发送的批次允许显式弃用：仅 `not_sent`、无 run ID 且从未存在 dispatch 回执的批次可操作。弃用保留审计和已占版本，清除仍指向它的成员引用；下一次准备可冻结新批次。已发送、unknown 或任何已有 dispatch 回执均禁止弃用。预约发送时须在同一事务重读批次和成员引用，已弃用批次不可被旧调用复活或发送。
- 构建失败不自动重试。用户可在 Actions 重跑后点击「同步发布结果」；相同 run 的新 attempt 仍须重核全部证据。unknown 入口只对账，不重发。
- 显式一次恢复例外：用户明确接受原请求延迟生效可能重复触发后，CLI-only `recover-release-dispatch {task_id,batch_id,expected_context_sha256,expected_dispatch_request_sha256,confirm:true}` 可恢复原预发布批次一次。重核完整原上下文、成员引用、策略/暂停、tag 与工作流；完整列表已有唯一精确 marker 时只同步，多个或任何同 marker 身份异常均拒绝发送。零匹配不等于未送达。发送前同一事务写入固定 `dispatch-recovery:<batch_id>` unknown 回执，保留原 dispatch 完整审计、冻结输入及本次 writer 身份；任意已有恢复回执都不得再发送。网络不占事务，不清空旧回执或改成 not_sent，不创建新版本。后台与普通 unknown 操作仍仅对账；恢复丢响应同样不能再次重发。同步提交复核 batch 和调度回执快照，陈旧观察不覆盖新恢复；writer 按持久回执精确归属回收。恢复通过只读观察解决时保留其来源与原未知审计，不声称 GitHub 具备 CAS 或全局恰好一次。
- 构建 failed 可从原任务「AI 处理…」显式启动一次构建修复，按 protocol「构建失败的一次性处理」核对并冻结真实失败日志、run/attempt、旧批次和源码基线。修复使用新分支、可信验证、新评审和新 PR，保留原已合并 PR 与失败发布批次。该授权止于新修复 PR，不自动合并或再次发布；旧批次成为只读历史。再次发布必须单独预检并确认，仍受版本、批次、完整证据与项目发布策略约束。
- 同次构建修复的新 PR 真实合并后，可显式准备并确认新批次。若原失败批次为正式版，仅在该修复的精确归属、新执行/Review/提交和合并证据全部成立时，将此一个旧 failed 批次排除出正式版 pending 冲突；保留旧证据、版本/tag，不排除其他未发布正式批次。连续失败可沿逐条可信、同项目/仓库/根任务且版本递增的修复关系识别前驱链，各轮仍须单独确认。后继正式批次可信 published 后，以其已接受回执、冻结上下文及发布证明持续识别该可信前驱链，后续任务不再被此前驱链阻挡；后续普通任务仅凭关联字段或尚未 published 的后继不足以解除占位。新版本继续按 inventory 递增，不能重发旧批次或用此例外自动发布。批量同步跳过已归档的修复来源批次，继续同步当前批次；单项历史批次仍只读。

### 发布说明修正（2026-10-23）

- Release 正文首行使用 `<!-- kaneopilot-owner:<canonical JSON> -->` 隐藏机器归属，旧裸首行仍可核对；annotated tag message 保持原 canonical marker，不移动 tag。完整列表中有较早稳定版时，选择低于候选版本的最高已发布稳定版，完整读取它与固定 target SHA 的 compare 提交，沿第一父链汇总 PR 标题及直接提交，过滤明确的自动 feed 记录，避免跨预发布或历史 PR 关联缺失造成遗漏；初版无稳定基线时使用固定 tag/target 的 GitHub generate-notes。包含实际变更说明及中文安装提示，不能从 task ID 编造更新内容。恢复保留人工说明；仅原脚本的精确默认正文可自动补齐。应用更新 feed 的 `body` 仅含用户说明，客户端兼容清除历史有效 owner 首行。
- CLI-only `repair-release-notes {task_id,batch_id,expected_context_sha256,release_id,expected_body_sha256,notes,confirm:true}` 只修正文。核心绑定冻结批次、任务、完整 tag/target/run/manifest 归属与发布 ID，核对旧正文哈希；先在 `runtime_operations` 固化旧/新正文和标签/资产快照并置 unknown，再事务外发送一次 body-only PATCH。重复同请求及丢响应均只读对账；未决说明操作不能用新参数绕过。写后必须确认精确新正文、原标签和资产身份不变，才记录 accepted。此命令不重建、不调度、不移动标签、不替换资产、不推进发布完成状态，也不提交更新 feed；远端没有 CAS，本地回执不消除外部并发窗口。

### 外部修复已合并后的显式衔接（2026-10-22）

原失败预发布的构建修复也可能在 Pilot 之外完成。任务详情提供「关联已合并修复…」，由用户填写同仓库修复 PR 编号，先只读核对再确认关联。首版限定无二级修复的独立任务、当前已确认 failed 的预发布批次和预发布策略；不扩展正式版占位例外，不启动新的 AI 修复轮。

- `prepare-external-build-repair {task_id,pr_number}` 返回 `{task_id,context_sha256,candidate}`。candidate 含 `task_id,pr_number,pr_url,head_sha,base_branch,target_sha,target_source_sha256,source_batch_id,source_tag,source_target_sha,source_workflow_run_id,source_workflow_attempt,preservation_kind,context_sha256`。它核对原执行、需求、计划、正式 Review 和原已合并 PR，失败批次与真实 run/attempt，以及同仓库、同目标分支且真实 MERGED 的不同修复 PR；冻结修复合并提交和完整源码指纹。此命令不写状态、不调用模型、不创建批次。
- 若当前仅因交付阶段的 GitHub 环境错误（`gh_unavailable|gh_not_logged_in|gh_missing|gh_timeout`）进入 `needs_user`，外部关联预检可以在未暂停、未手动接管且无其他人工恢复要求时重新核对上述全部证据。只读准备不清除阻塞；候选保存原本地状态和失败字段，确认事务复核未变后才解除这项已由新证据排除的旧阻塞、恢复 `in_review` 并关联。其他失败不沿此入口恢复；幂等重放不得清除后来的新失败，后续模型 runtime 仍遵循正常状态守卫。
- 原交付必须仍保留在修复目标中。正常历史核对原评审 head 为目标祖先；历史重写而无共同祖先时，仅此显式关联路径可用原 PR 的**完整改动路径列表**核对原 head 与新目标逐路径 blob/mode 相同，原删除路径仍须缺失，重命名覆盖新旧两条路径。列表缺页、截断、路径或模式变化均拒绝，不泛化放宽普通发布的祖先守卫。`preservation_kind` 为 `ancestor` 或 `files`，后者在确认中说明历史已变化且原交付文件已逐项核对。
- `accept-external-build-repair {task_id,pr_number,expected_context_sha256,confirm:true}` 再次核对候选并在事务内复核任务身份和旧引用，登记独立的 accepted `runtime_operations` 人工接纳回执及 `external_build_repair_ref`。返回 `{ok:true,repair}`；相同确认幂等，漂移拒绝。原 `release_ref`、`release_verification_ref` 和失败批次归属保存在来源历史后解除当前引用。原 delivery、执行、正式 Review、engine verdict 和旧失败批次均不修改；人工接纳不构成新的正式 Review pass。
- 新发布目标强制走现有独立 Plus 完整验证。冻结关联回执、不可变目标、完整源码指纹、原需求与计划正文/哈希以及全部逐字 verify 命令；Goal 必须覆盖原需求、计划验收与完整目标，沿用报告、真实命令退出码和源码/index 的全部核对。旧 Goal、本地构建及普通 CI 不能冒充新目标证据。准备验证免费且只读；开始验证的确认单独展示模型和可能费用。本次关联本身不调用模型。
- 新验证尝试的工作树遵循统一约定，放在 `$HOME/Documents/Code/Project/Project Worktree/<项目标识>/<验证标识>/`，隔离测试显式覆盖根目录；已持久化的旧尝试继续使用原路径，不迁移，不因环境根变化而改写。运行时授权必须核对核心已接受的工作树创建回执与精确路径。
- 仅新目标补验证真实通过后，允许 `prepare-task-release` 冻结该目标的新预发布候选。新批次、版本/tag、单次发送及发布证据继续沿原规则。后台 `after_merge`、重启或项目自动发布设置不能代替用户的单独发布确认。新批次绑定人工接纳与独立验证证据，不能伪称原 Review 覆盖了新目标。
- 旧批次经来源历史成为只读历史，单项/批量同步不得覆盖新流程，也不能重发旧 tag。新批次存在时继续展示和跟踪新批次，发布完成门不因旧记录归档而解除。

`release` DTO 增加可选 `can_link_external_repair` 和 `external_repair`。后者为 `{pr_number,pr_url,target_sha,source_batch_id,source_tag,context_sha256,can_prepare_verification,can_sync_verification,can_restart_verification?,verification?}`；verification 沿补验证 view（state、target_sha、verification_id、reason 等）。SQL 投影只读取可信持久回执，不进行远端/模型调用；预检仍重新核证。验证准备响应补充 `task_id`，已有字段不变。关联事务复核来源批次的本地状态、attempt 与冻结发布策略；后续验证和发布仍须符合已接纳的预发布策略，策略漂移明确受阻，不将此外部关联例外用于正式版。

验证受阻后的操作按持久事实提供。审批确定尚未发送且满足既有安全恢复条件时保留「核对验证结果」，只推进原尝试。仅已完成报告不满足条件、旧回执已拒绝且无未知下游操作时提供「重新验证…」；`prepare-restart-release-verification {task_id}` 只读重新核证原会话与完成报告，返回 `{task_id,context_sha256,expected_verification_id,expected_context_sha256,expected_report_sha256,verification}`，不新建会话、不调用模型。单独确认费用后，沿 `restart-release-verification` 的精确旧验证/上下文/报告参数启动新尝试，保留旧失败报告。其他失败和未知操作不泛化为自动重试。

自动推进独立完整验证（2026-10-22）：用户在 Pilot 确认一次模型费用并 start/restart 后，冻结权限为 `auto` 的当前尝试由已有 `recover-execution-goals` 后台子进程持续核对：原规划 → 精确批准本轮唯一 Goal → 原执行 → 完成报告核验，无需切到 Plus 再批准或持续点击核对。该命令保留原 `goal_recoveries`，追加 `release_verifications` 数组；每项含 `task_id`、`verification_id` 及同步结果或独立错误。后台只扫描活跃任务当前引用、精确核心启动回执、auto 配置和未终结的尝试；遵守全局、任务、交付及已有恢复入口暂停门、人工接管。不开新尝试，不重发未知 prompt/审批，不自动重启失败验证，不自动构建发布。原 Goal 已由外部批准时，符合上述范围的当前尝试可按严格历史守卫持续只读收取原报告，但历史观察不能授予验证通过，也不能补造审批回执。保留任务详情及底栏的原 ID「核对验证结果」作为人工恢复入口。

Plus 的 history 可以含 pending Goal。存在有效且唯一的原 pending 时走既有精确 runtime 批准；仅无 pending 且存在同会话、原规划轮次的 approved Goal 时进入严格只读历史续接，外来、多项、过期或内容漂移不能猜测。自动与手动核对共享原尝试并发守卫和唯一审批 operation，慢读不能覆盖新进度。执行轮次完成但报告尚未 ready 时继续只读等待，已 ready 的失败或证据不足报告才受阻，不用新付费轮次处理报告传递延迟。所有远端读取与源码核证保持在 SQLite 写事务之外。非 auto 尝试保持其冻结权限，不静默扩大权限。

只读历史核对写回时，当前验证已被其他同步推进则返回最新记录；当前验证未变但最终提交事务发现关键本地快照变化时，丢弃本次临时观察，以 `unknown / release_verification_history_conflict` 保留原尝试，允许下一轮再次只读核对。完整核证发现的源码、身份或配置漂移仍按原守卫拒绝。历史只读来源跨后续错误持久保留；即使再次出现 pending，也不能据此发送批准。

已批准 Goal 的只读续接（2026-10-22）：

- 原尝试停在 planning，或因 `release_verification_proposal` 受阻且尚未记录审批发送时，允许「核对验证结果」读取原会话。仅在核心冻结上下文、原 prompt、完整且无追加用户输入的会话、唯一 Goal 提交与批准历史、Goal 正文及 artifact、规划和执行轮次均精确相符时登记 `approval_observation.source=plus_history`；保留原阻塞原因及报告哈希，不补造 `approval_sent` 或 runtime 审批回执，不发送批准、prompt 或新模型请求。任意未知下游写入、来源歧义、截断、源码或身份漂移均拒绝续接。
- 只读续接的本地提交守卫保留任务身份、冻结证据、执行配置、暂停/人工接管、当前验证及外部关联引用和下游操作回执；交付轮询的时间、进度、自动合并显示信息、阶段契约/结果及 delivery 请求领取与完成不属于验证输入，不能仅因这些刷新拒绝接回。关键本地快照的提交竞争沿上述只读冲突规则处理，不自动重试或发送任何模型写入。旧版已保存的 `blocked / release_verification_history` 仅在 `history_read_only=true`、未记录审批发送且原因逐字为「历史观察写回前原尝试或下游写状态已变化」时沿同一只读入口重新核证；其他历史身份错误不泛化开放，未知下游操作仍拒绝。
- 该观察只确认原执行及报告的归属。没有本项目精确审批回执和冻结命令绑定的历史尝试不能借此成为 verified；原报告保留原样，转录中的命令成功记录仅供审计，不替换结构化报告。冻结命令须有 Host `checkObservations` 的同命令、同 checkId、原生整数退出码 0 及 passed 证明；inconclusive、失败或缺失确定性证明明确显示为验证证据不足，不得用顶层 met 覆盖。`evidenceResolution` 保留诊断；Host 对 Bash callId 的核证结果可为 observation passed 而 resolution unresolved，不能把这种合法组合误判为失败。
- 原执行已完成、报告可读且归属可复核但证据不足时，保存真实报告身份、哈希与证据不足原因，按 `release_verification_report` 阻塞提供显式重启准备。准备和取消保持只读；只有单独确认模型及费用后才启动新尝试。重启再次核对原会话空闲、无待批/待答与未知写入、上下文/报告/源码相同；旧尝试及报告不改写为通过。
- Plus 重启后，空闲会话的 `currentTurnId` 可以缺省或为 null。仅上述已保存完整失败报告的显式重启准备及确认允许恢复核对：完整持久转录须只有原冻结用户输入、唯一原 SubmitGoal、原规划及执行归属，末条属于原执行；原执行须有同身份的 completed 与有效结束时间，原审批回执或历史观察、报告 ID/执行 ID/SHA/正文 SHA/原始响应须完全相符。运行中、非空其他轮次、空字符串、截断、追加输入或身份变化均拒绝。该恢复只确认重启资格，不改变普通报告通过、后台同步或 verified 的守卫，也不证明没有无消息的孤立 turn。
- 新尝试持久化 prompt 版本与哈希，明确每次 Bash `command` 和报告 `checks.command` 均与冻结命令完整逐字一致；不能靠额外 export、PATH 或 cd 包装修饰实际调用而在报告中省略。环境须在宿主准备，缺环境如实阻塞。历史尝试缺版本时保留旧 prompt 原文及原哈希核验，不能因升级提示词丢失续接资格；上下文去重与旧尝试身份不变。
- 核心 `actions` 按持久资格提供 `sync_release_verification` 或 `restart_release_verification`，桌面底栏优先显示对应动作并沿用详情区的同一核心命令与确认门。发布投影显示验证当前阶段与真实阻塞原因，不再统一显示「等待完整验证」；Swift 不自行推导恢复或重启资格。`verification.failure_code` 为可选诊断字段，旧 DTO 保持兼容。

界面保留现有「构建与发布」区域、字体、间距和确认框。未关联时在失败区提供「关联已合并修复…」；关联后显示修复 PR、新目标、原失败版本和验证状态，提供「验证修复后的源码…」或「核对验证结果」。原失败记录可展开查看，不继续推荐修复旧源码。验证通过后复用「准备新批次…」及既有构建发布确认；取消准备/确认不写入、不启动模型或 Actions。切换任务后丢弃候选，不把旧任务结果展示给新任务。

独立补验证中的普通提问沿 protocol「独立补验证中的提问」转发。规划或执行等待回答时，当前问题进入 Pilot 的待处理与任务概览，底栏优先提供答案提交；原执行、评审和发布状态保留。草稿只在该验证核心回执保存，答案只发往精确原 ask。未知送达不自动重发，用户在 Plus 回答也须按原工具结果核对；回答后原验证继续，不重新启动验证或另开 Goal。此问答入口不等于授权新模型尝试、重启、发布或放宽验证证据。

## T3 发布证据与共享 DTO

只有以下全部满足才为 published：

1. 冻结 Actions run completed/success；配置的 required_jobs 全部 success，skipped 不算通过。发布证据核对结束、接受回执之前，再取同 run 确认身份与 run_attempt 未变且仍为 completed/success。
2. Release 非 draft，channel 对应 prerelease 精确，tag 精确，标签最终解引用 commit 等于 target_sha。
3. 本 run 有未过期的 `kaneopilot-provenance` artifact，内含唯一 `kaneopilot-release.json`；Release 同名 manifest 与其字节相同。
4. manifest schema=1，含 batch_id、repository、target_sha、task_manifest_sha256、task_manifest（完整冻结对象）、workflow_run_id、run_attempt、channel、version、tag、build_number、assets。assets 含 name/size/sha256，不含 manifest 自身；全部 required_assets 都在其中。
5. manifest 与冻结输入、run/attempt 一致；每个必需 Release asset 的文件名、正大小及 GitHub `sha256:` digest 与 manifest 一致。缺 digest 明确受阻，不以 artifact zip digest 代替包内文件 hash，不下载巨大安装包作隐式兜底。下载地址必须来自同一 GitHub repo/tag。

先建立含批次归属的 annotated tag 和 draft，上传完整包与 manifest 后再发布。中断恢复仅认领同 batch/run/target/task manifest 身份的 tag 与 Release，逐个核对已有安装包大小和 SHA256 后只补缺失包，禁止替换安装包。相同 run 的新 attempt 允许更新自身 `kaneopilot-release.json` 至当前 attempt，并以 overwrite 上传唯一 provenance artifact；须再次核对所有已有包未变，不能覆盖陌生批次。Pilot 仍只接受当前 attempt 完整成功的证据。

GitHub 上传失败若留下严格 `state=starter`、整数 size=0 的占位，确认全部归属后可清理该空占位并补传：安装包仅限 owned draft，自身 provenance 也允许 owned published；真实包、非空、未知状态或陌生资产均不得清理。原 tag 发布入口同样禁止覆盖已发布包；稳定清单使用远端实际资产元数据。同版本已有清单只在包身份一致时保持不变，不一致明确受阻；原合法非标准稳定标签继续作为预发布，不更新稳定入口。

预发布固定 make_latest=false，不能回写 Music 的稳定 `release/latest.json`。stable 更新入口必须按版本单调前进，提交清单遇到并发 push 时重新读取并比较版本，不用旧清单覆盖新版。iOS unsigned 资产仍明确标 unsigned，不声称普通用户可直接安装。已核验发布回执保留完整指纹；临时 Actions artifact 后续过期不撤销历史完成证据。

TaskSummary/TaskDetail 增可选 `release` DTO：

```text
state: ready|waiting_group|dispatching|unknown|queued|building|publishing|blocked|failed|published
title, reason, required:bool, channel, context_sha256?, batch_id?, root_task_id,
member_task_ids:[str], target_sha?, version?, tag?, build_number?,
workflow_run_id?, workflow_url?, release_url?, checked_at?,
assets:[{name,url,size,sha256}], blockers:[{code,message}], can_start:bool, can_reconcile:bool,
can_abandon?:bool
```

CLI：

- `prepare-release-verification {task_id}` → `{context_sha256,verification}`；只读冻结候选目标、命令和模型说明，不调用模型。
- `start-release-verification {task_id,expected_context_sha256}` → `{verification_id,verification}`；显式授权后仅启动同上下文的独立 Plus 验证。
- `sync-release-verification {task_id,verification_id}` → `{verification_id,verification}`；推进原会话 Goal 审批与核证，不重新发送 prompt。
- `prepare-task-release {task_id}` → `{task_id,context_sha256,release}`；只读准备可审查内容，不发送、不落批次。受阻时 `context_sha256=null`，`release.reason` 给出真实原因；客户端须解码并展示原因，不得将其变成解析错误。仅非空指纹、`can_start=true` 且非 `unknown` 的候选可进入发布确认及启动；空指纹不得发送启动命令。
- `start-task-release {task_id,expected_context_sha256}` → `{outcome,release}`；显式补发或已授权自动交付，冻结后最多发送一次。
- `sync-release-batch {batch_id}` → `{outcome,release}`；只读远端对账及本地回执写入，不启动新批次。
- `sync-release-batches {limit?}` → 有界轮询已有批次，不因历史 done 或新配置创建批次。
- `abandon-unsent-release {batch_id,expected_context_sha256}` → `{outcome:abandoned,release}`；返回原批次 DTO，required 与操作 flags 均 false。随后刷新重新投影，保留原审计，不发送 Actions。
- 内部：`validate_policy(raw)`、`projection(conn,task)`、`completion_gate(conn,task)`、`after_merge(ctx,task,payload=None)`；after_merge 返回 outcome=disabled|waiting|published|blocked，供既有 delivery 轮询与完成流程接入。

UI 沿用项目「自动处理」表单与任务详情「交付」区。配置开关下提供渠道，其他字段折叠；首次开启或切正式版明确确认。任务展示核心发布状态、版本/渠道、Actions/Release 链接与资产下载；仅保留一行看板摘要。按核心 can_start 显示「构建并发布…」，有未发送 batch 时显示「继续构建发布…」；历史 done 显示「补发版本…」。can_abandon 才显示「弃用未发送批次…」，确认时说明原批次和未发送事实；unknown 硬禁启动和弃用，仅「核对发布结果」。其他已启动状态提供「同步发布结果」。发布可核对时底栏同样推荐该动作，不再推荐只检查计划与 Kaneo 的「重新校验」；核心允许 continue_delivery 时优先推荐「继续交付」，说明将复用已有评审并走 PR、合并及已配置的构建发布。构建 failed 是失败；权限、身份或缺包 blocked/unknown 是阻塞。发布中的未完成任务保留评审/交付列，published 后仍由真实 done 驱动完成列。

## 实施归属与验收

- 主 agent：本契约及 protocol/state/ui/automation 规格、策略/CLI/derive、调度与全部 done 门接入、集成与最终原生核验。
- 核心 worker：release_delivery.py、release_github.py、新专项测试、parent_closeout.py 的软件就绪证据与发布门及原收尾回归；独占这些文件。
- UI worker：Swift Models/AppViewModel/ProjectMappings/TaskDetail/Board/TaskCompactList、FakeCore 与相关 Swift 夹具；独占这些文件。
- Music workflow worker：独立 Music 工作树中的工作流与其隔离验证材料；不写主工作树、不 push、不触发 Actions 或发布。
- 前后端依此契约并行，主控负责共享入口集成。独立审查只读核对未知结果、完成门及正式/预发布通道。核验覆盖成功、失败、unknown不重复、缺包/错误tag/SHA/成员/旧run/跳过job、共享父子无环、历史done不回溯和策略关开兼容。先隔离验证、Swift构建和真实原生页面，再准备远端发布确认；本地模拟不冒充真实 Actions/Release。

### 已发送批次的短暂读取失败

已发送的构建批次同步遇到 GitHub 只读传输超时或瞬时错误时，保留同一批次、workflow run、目标提交和派发回执。已有 queued/building/publishing 状态的前两次失败保留最后一次确认的阶段，说明结果尚待自动核对，并沿 next_check_at 的既有 60 秒间隔复查；连续第三次才转 blocked。成功核对清零连续读取失败次数。认证、权限、身份不匹配和真实构建失败立即保留阻塞或失败，不混入网络重试。未知派发继续只读按 marker 对账，任何读取重试都不能重新 dispatch、创建批次或触发模型。
